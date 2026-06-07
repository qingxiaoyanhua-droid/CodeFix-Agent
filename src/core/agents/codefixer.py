#!/usr/bin/env python3
"""
CodeFixer: 小大模型协同修复代码的原子操作
=========================================
封装完整循环：小模型生成 → 编译测试 → 失败 → 大模型给 hint → 小模型重试
                                                      ↓
                                           3 次不行 → 大模型亲自下场修复

集成到 MultiAgentOrchestrator 作为可调用的 Agent。
"""

from __future__ import annotations

import time
import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Callable, Literal
from enum import Enum

from agents.base_agent import BaseAgent, AgentProfile, AgentCapability, SharedContext

logger = logging.getLogger(__name__)


# ==================== 结果定义 ====================

class FixStatus(Enum):
    """修复结果状态"""
    SUCCESS = "success"         # 通过编译 + 测试
    COMPILE_ERROR = "compile_error"   # 编译失败
    TEST_FAILED = "test_failed"        # 测试失败
    LARGE_MODEL_DIRECT_FIX = "large_model_direct_fix"  # 大模型直接修复
    MAX_ROUNDS_EXCEEDED = "max_rounds_exceeded"  # 超过最大轮数
    NO_CODE_GENERATED = "no_code_generated"      # 没有生成代码
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class FixResult:
    """
    CodeFixer 返回的结构化结果。

    供 MultiAgentOrchestrator 路由决策用。
    """
    success: bool
    status: FixStatus
    final_code: str
    quality_score: float = 0.0

    # 迭代统计
    total_rounds: int = 0
    small_model_calls: int = 0
    large_model_calls: int = 0
    sandbox_runs: int = 0
    total_time_ms: float = 0.0

    # 详细轨迹（供分析/训练数据收集）
    trajectory: List[Dict] = field(default_factory=list)

    # 错误信息
    error_message: str = ""

    # 大模型是否亲自下场
    large_model_took_over: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "status": self.status.value,
            "final_code": self.final_code,
            "quality_score": self.quality_score,
            "total_rounds": self.total_rounds,
            "small_model_calls": self.small_model_calls,
            "large_model_calls": self.large_model_calls,
            "sandbox_runs": self.sandbox_runs,
            "total_time_ms": self.total_time_ms,
            "trajectory": self.trajectory,
            "error_message": self.error_message,
            "large_model_took_over": self.large_model_took_over,
        }


# ==================== CodeFixer ====================

class CodeFixer:
    """
    小大模型协同代码修复原子操作。

    设计原则：
      1. 原子性：作为一个整体不可分割，orchestrator 一次调用完成
      2. 内部循环：封装小模型重试逻辑，不暴露给 orchestrator
      3. 结果导向：返回结构化 FixResult，含轨迹供训练数据收集

    循环逻辑：
      Round 0:   小模型生成 → 编译测试 → 通过? → SUCCESS
                            ↓ 失败
      Round 1-3: 大模型给 hint → 小模型重试 → 编译测试 → 通过? → SUCCESS
                            ↓ 3次都失败
      Fallback:  大模型直接修复 → 编译测试 → 通过? → SUCCESS / FAIL

    用法（独立）:
        fixer = CodeFixer(
            small_model_fn=small_model_call,
            large_model_fn=large_model_call,
            sandbox=sandbox,
        )
        result = fixer.fix(buggy_code="...", bug_description="...")

    用法（集成到 orchestrator）:
        fixer = CodeFixer(...)
        result = fixer.run(context)  # 遵循 BaseAgent.run() 接口
        # orchestrator 通过 result 决定后续
    """

    def __init__(
        self,
        small_model_fn: Optional[Callable] = None,
        large_model_fn: Optional[Callable] = None,
        sandbox: Optional[ShellSandbox] = None,
        test_runner: Optional[TestRunner] = None,
        max_small_model_rounds: int = 3,
        sandbox_timeout: float = 10.0,
        language: str = "python",
        system_prompt: str = "",
        retry_system_prompt: str = "",
        large_model_direct_prompt: str = "",
        code_extract_pattern: str = r"```python\s*(.*?)```",
    ):
        """
        Args:
            small_model_fn: 小模型调用函数，签名为 (user_prompt, system_prompt) -> str
            large_model_fn: 大模型调用函数，签名为 (user_prompt, system_prompt) -> str
            sandbox: 沙箱实例，默认新建 ShellSandbox
            test_runner: 测试运行器，默认新建 TestRunner
            max_small_model_rounds: 小模型最大重试次数（默认3次）
            sandbox_timeout: 沙箱超时（秒）
            language: 编程语言
            system_prompt: 小模型系统提示词
            retry_system_prompt: 重试时的系统提示词
            large_model_direct_prompt: 大模型直接修复时的提示词模板
            code_extract_pattern: 从模型输出中提取代码的正则
        """
        self.small_model_fn = small_model_fn
        self.large_model_fn = large_model_fn
        self.sandbox = sandbox or ShellSandbox(timeout=sandbox_timeout)
        self.test_runner = test_runner or TestRunner(self.sandbox)
        self.max_small_model_rounds = max_small_model_rounds
        self.language = language

        self.system_prompt = system_prompt or self._default_small_model_system_prompt()
        self.retry_system_prompt = retry_system_prompt or self._default_retry_system_prompt()
        self.large_model_direct_prompt = large_model_direct_prompt or self._default_large_model_direct_prompt()
        self.code_extract_pattern = code_extract_pattern

    # ==================== 公共接口 ====================

    def fix(
        self,
        buggy_code: str,
        bug_description: str = "",
        error_message: str = "",
        test_cases: Optional[List[Dict]] = None,
        task_id: str = "",
    ) -> FixResult:
        """
        完整的协同修复流程。

        这是主入口，封装完整循环，返回 FixResult。
        """
        import re as re_module

        t0 = time.time()
        trajectory: List[Dict] = []
        current_code = ""
        current_cot = ""
        current_hint = ""
        small_calls = 0
        large_calls = 0
        sandbox_runs = 0
        compile_error = ""
        test_error = ""

        # Round 0: 小模型首轮生成
        small_calls += 1
        raw_output = self._call_small_model(
            buggy_code, bug_description, current_hint, round_num=0
        )
        trajectory.append({
            "round": 0,
            "action": "small_model_generate",
            "raw_output": raw_output[:200],
        })

        if raw_output:
            current_cot, current_code = self._parse_cot_and_code(raw_output)
        else:
            return FixResult(
                success=False,
                status=FixStatus.NO_CODE_GENERATED,
                final_code="",
                total_rounds=0,
                small_model_calls=small_calls,
                large_model_calls=large_calls,
                sandbox_runs=sandbox_runs,
                total_time_ms=(time.time() - t0) * 1000,
                trajectory=trajectory,
                error_message="小模型未返回任何输出",
            )

        # 编译 + 测试
        sandbox_runs += 1
        compile_ok, compile_error = self._compile_check(current_code)
        test_ok = False
        test_error = ""
        if compile_ok:
            test_ok, test_error = self._run_tests(current_code, test_cases or [])
            sandbox_runs += 1

        trajectory.append({
            "round": 0,
            "action": "sandbox_verify",
            "compile_ok": compile_ok,
            "compile_error": compile_error[:100] if compile_error else "",
            "test_ok": test_ok,
            "test_error": test_error[:100] if test_error else "",
        })

        if compile_ok and test_ok:
            elapsed_ms = (time.time() - t0) * 1000
            return FixResult(
                success=True,
                status=FixStatus.SUCCESS,
                final_code=current_code,
                quality_score=1.0,
                total_rounds=0,
                small_model_calls=small_calls,
                large_model_calls=large_calls,
                sandbox_runs=sandbox_runs,
                total_time_ms=elapsed_ms,
                trajectory=trajectory,
            )

        # Round 1 - N: 小模型重试 + 大模型 hint
        for round_num in range(1, self.max_small_model_rounds + 1):
            logger.info(f"[CodeFixer] Round {round_num}: 大模型分析 → 小模型重试")

            # 大模型给 hint
            large_calls += 1
            hint_output = self._call_large_model_for_hint(
                buggy_code=buggy_code,
                buggy_code_raw=buggy_code,
                attempted_code=current_code,
                cot_reasoning=current_cot,
                compile_error=compile_error,
                test_error=test_error,
            )
            current_hint = self._extract_hint(hint_output)
            trajectory.append({
                "round": round_num,
                "action": "large_model_hint",
                "hint": current_hint[:150],
                "raw_output": hint_output[:200],
            })

            # 小模型基于 hint 重试
            small_calls += 1
            raw_output = self._call_small_model(
                buggy_code, bug_description, current_hint, round_num=round_num
            )

            if raw_output:
                prev_cot, prev_code = current_cot, current_code
                cot, code = self._parse_cot_and_code(raw_output)
                current_cot = cot
                current_code = code
                trajectory.append({
                    "round": round_num,
                    "action": "small_model_retry",
                    "raw_output": raw_output[:200],
                    "has_changes": code != prev_code,
                })

            # 编译 + 测试
            sandbox_runs += 1
            compile_ok, compile_error = self._compile_check(current_code)
            test_ok = False
            test_error = ""
            if compile_ok:
                test_ok, test_error = self._run_tests(current_code, test_cases or [])
                sandbox_runs += 1

            trajectory.append({
                "round": round_num,
                "action": "sandbox_verify",
                "compile_ok": compile_ok,
                "compile_error": compile_error[:100] if compile_error else "",
                "test_ok": test_ok,
                "test_error": test_error[:100] if test_error else "",
            })

            if compile_ok and test_ok:
                elapsed_ms = (time.time() - t0) * 1000
                quality = self._estimate_quality(trajectory, round_num)
                return FixResult(
                    success=True,
                    status=FixStatus.SUCCESS,
                    final_code=current_code,
                    quality_score=quality,
                    total_rounds=round_num,
                    small_model_calls=small_calls,
                    large_model_calls=large_calls,
                    sandbox_runs=sandbox_runs,
                    total_time_ms=elapsed_ms,
                    trajectory=trajectory,
                )

        # Fallback: 大模型亲自下场
        logger.info("[CodeFixer] 小模型重试耗尽，大模型亲自下场")
        large_calls += 1
        large_direct_output = self._call_large_model_direct_fix(
            buggy_code, bug_description, compile_error, test_error
        )
        trajectory.append({
            "round": self.max_small_model_rounds + 1,
            "action": "large_model_direct_fix",
            "raw_output": large_direct_output[:200],
        })

        if large_direct_output:
            _, large_direct_code = self._parse_cot_and_code(large_direct_output)

            if large_direct_code:
                sandbox_runs += 1
                compile_ok, compile_error = self._compile_check(large_direct_code)
                test_ok = False
                test_error = ""
                if compile_ok:
                    test_ok, test_error = self._run_tests(
                        large_direct_code, test_cases or []
                    )
                    sandbox_runs += 1

                trajectory.append({
                    "round": self.max_small_model_rounds + 1,
                    "action": "sandbox_verify",
                    "compile_ok": compile_ok,
                    "compile_error": compile_error[:100] if compile_error else "",
                    "test_ok": test_ok,
                    "test_error": test_error[:100] if test_error else "",
                })

                elapsed_ms = (time.time() - t0) * 1000
                if compile_ok and test_ok:
                    return FixResult(
                        success=True,
                        status=FixStatus.LARGE_MODEL_DIRECT_FIX,
                        final_code=large_direct_code,
                        quality_score=0.9,
                        total_rounds=self.max_small_model_rounds + 1,
                        small_model_calls=small_calls,
                        large_model_calls=large_calls,
                        sandbox_runs=sandbox_runs,
                        total_time_ms=elapsed_ms,
                        trajectory=trajectory,
                        large_model_took_over=True,
                    )

                # 大模型也失败
                return FixResult(
                    success=False,
                    status=FixStatus.TEST_FAILED if compile_ok else FixStatus.COMPILE_ERROR,
                    final_code=large_direct_code,
                    quality_score=0.0,
                    total_rounds=self.max_small_model_rounds + 1,
                    small_model_calls=small_calls,
                    large_model_calls=large_calls,
                    sandbox_runs=sandbox_runs,
                    total_time_ms=elapsed_ms,
                    trajectory=trajectory,
                    error_message=test_error or compile_error,
                    large_model_took_over=True,
                )

        elapsed_ms = (time.time() - t0) * 1000
        return FixResult(
            success=False,
            status=FixStatus.MAX_ROUNDS_EXCEEDED,
            final_code=current_code,
            quality_score=0.0,
            total_rounds=self.max_small_model_rounds,
            small_model_calls=small_calls,
            large_model_calls=large_calls,
            sandbox_runs=sandbox_runs,
            total_time_ms=elapsed_ms,
            trajectory=trajectory,
            error_message=test_error or compile_error or "大模型未返回有效代码",
        )

    # ==================== BaseAgent 集成 ====================

    def run(self, context: SharedContext) -> Dict[str, Any]:
        """
        遵循 BaseAgent.run() 接口，供 MultiAgentOrchestrator 调用。

        SharedContext 中需要包含：
          - buggy_code: 待修复代码
          - bug_description: bug 描述
          - error_message: 错误信息（可选）
          - test_cases: 测试用例（可选）
          - task_id: 任务 ID（可选）

        返回：
            {
                "status": "ok" | "error",
                "fix_result": FixResult,
                ...
            }
        """
        context.status = "codefixing"

        result = self.fix(
            buggy_code=context.buggy_code,
            bug_description=context.bug_description,
            error_message=context.error_message,
            test_cases=context.test_cases,
            task_id=context.task_id,
        )

        # 写入 SharedContext（供 orchestrator 其他 agent 使用）
        context.solver_output = {
            "fixed_code": result.final_code,
            "quality_score": result.quality_score,
            "fix_status": result.status.value,
        }

        context.record_step(
            agent="CodeFixer",
            action=f"fix_{result.status.value}",
            result=result.final_code[:100] if result.final_code else "no_code",
        )

        return {
            "status": "ok" if result.success else "error",
            "fix_result": result,
            "success": result.success,
            "final_code": result.final_code,
            "quality_score": result.quality_score,
            "status": result.status.value,
            "total_rounds": result.total_rounds,
            "small_model_calls": result.small_model_calls,
            "large_model_calls": result.large_model_calls,
            "sandbox_runs": result.sandbox_runs,
            "total_time_ms": result.total_time_ms,
            "large_model_took_over": result.large_model_took_over,
            "error": result.error_message,
        }

    # ==================== 模型调用 ====================

    def _call_small_model(
        self,
        buggy_code: str,
        bug_description: str,
        hint: str,
        round_num: int,
    ) -> str:
        """调用小模型生成修复代码"""
        if self.small_model_fn is None:
            logger.warning("[CodeFixer] 小模型未配置，跳过")
            return ""

        prompt = self._build_small_model_prompt(
            buggy_code, bug_description, hint, round_num
        )

        try:
            system = self.retry_system_prompt if round_num > 0 else self.system_prompt
            output = self.small_model_fn(prompt, system)
            return output
        except Exception as e:
            logger.error(f"[CodeFixer] 小模型调用失败: {e}")
            return ""

    def _call_large_model_for_hint(
        self,
        buggy_code: str,
        buggy_code_raw: str,
        attempted_code: str,
        cot_reasoning: str,
        compile_error: str,
        test_error: str,
    ) -> str:
        """调用大模型分析错误并给出 hint"""
        if self.large_model_fn is None:
            return ""

        error_msg = compile_error or test_error or "未知错误"
        prompt = f"""You are analyzing a code fix attempt.

**Original Buggy Code:**
```python
{buggy_code}
```

**Attempted Fix:**
```python
{attempted_code}
```

**Chain-of-Thought Reasoning:**
{cot_reasoning}

**Execution Error:**
```
{error_msg}
```

Based on the error, provide a concise hint (2-3 sentences max) to guide the fix.
Do NOT give the full solution. Only a hint about what went wrong.
Return in JSON: {{"hint": "your hint here", "status": "BUG_DETECTED|OPTIMIZATION_ONLY|CORRECT"}}
"""
        try:
            output = self.large_model_fn(prompt, "You are a senior code reviewer. Respond in JSON only.")
            return output
        except Exception as e:
            logger.error(f"[CodeFixer] 大模型 hint 调用失败: {e}")
            return ""

    def _call_large_model_direct_fix(
        self,
        buggy_code: str,
        bug_description: str,
        compile_error: str,
        test_error: str,
    ) -> str:
        """大模型亲自修复代码"""
        if self.large_model_fn is None:
            return ""

        prompt = self.large_model_direct_prompt.format(
            buggy_code=buggy_code,
            bug_description=bug_description,
            compile_error=compile_error or "N/A",
            test_error=test_error or "N/A",
        )
        try:
            return self.large_model_fn(prompt, "You are an expert code fixer.")
        except Exception as e:
            logger.error(f"[CodeFixer] 大模型直接修复调用失败: {e}")
            return ""

    # ==================== Prompt 构建 ====================

    def _build_small_model_prompt(
        self,
        buggy_code: str,
        bug_description: str,
        hint: str,
        round_num: int,
    ) -> str:
        """构建小模型 prompt"""
        prompt = f"Bug: {bug_description}\n\nBuggy Code:\n```python\n{buggy_code}\n```\n"

        if hint:
            prompt += f"\n--- SENIOR REVIEWER HINT ---\n{hint}\n"

        if round_num > 0:
            prompt += "\nRefine your fix based on the hint. Follow the required format.\n"
        else:
            prompt += "\nFix the bug. Follow the required format.\n"

        return prompt

    # ==================== 输出解析 ====================

    def _parse_cot_and_code(self, raw_output: str) -> tuple[str, str]:
        """从模型输出中解析 CoT 推理和修复代码"""
        import re as re_module

        cot_steps = []
        code_pattern = re_module.compile(self.code_extract_pattern, re_module.DOTALL)
        code_match = code_pattern.search(raw_output)
        code = code_match.group(1).strip() if code_match else ""

        # 提取 CoT 部分（去掉代码块）
        cot_text = code_pattern.sub("", raw_output).strip()

        return cot_text, code

    def _extract_hint(self, large_model_output: str) -> str:
        """从大模型输出中提取 hint"""
        import re as re_module

        # 尝试从 JSON 中提取
        json_match = re_module.search(r'"hint"\s*:\s*"([^"]+)"', large_model_output)
        if json_match:
            return json_match.group(1)

        # 降级：直接返回前200字符
        return large_model_output.strip()[:200]

    # ==================== 沙箱验证 ====================

    def _compile_check(self, code: str) -> tuple[bool, str]:
        """编译检查"""
        if not code:
            return False, "代码为空"

        result = self.sandbox.run(code, self.language)
        if result.error in ("DangerousPattern", "TestFileModificationBlocked"):
            return False, f"Security blocked: {result.stderr}"
        if result.error:
            return False, result.stderr
        if result.exit_code != 0:
            return False, result.stderr
        return True, ""

    def _run_tests(
        self, code: str, test_cases: List[Dict]
    ) -> tuple[bool, str]:
        """运行测试用例"""
        if not test_cases:
            return True, ""

        try:
            passed, error = self.test_runner.run_tests(code, self.language, test_cases)
            return passed, error or ""
        except Exception as e:
            return False, str(e)

    # ==================== 质量评估 ====================

    def _estimate_quality(self, trajectory: List[Dict], final_round: int) -> float:
        """估算质量分数"""
        score = 0.85  # 基础分
        if final_round == 0:
            score = 1.0
        elif final_round == 1:
            score = 0.95
        # 多轮尝试降低分数
        score -= final_round * 0.03
        return max(0.5, min(score, 1.0))

    # ==================== 默认 Prompt ====================

    @staticmethod
    def _default_small_model_system_prompt() -> str:
        return """You are an expert code debugger. Think step-by-step before fixing.

**Format your response STRICTLY as follows:**

[Step 1: Bug Identification]
Identify the bug type and its exact location in the code.

[Step 2: Root Cause Analysis]
Explain WHY this code is incorrect. Trace the execution with a concrete input.

[Step 3: Fix Strategy]
Describe the minimal change needed. Why is this fix correct and complete?

[Step 4: Edge Case Check]
List 2-3 edge cases. Verify the fix handles them.

<fixed_code>
```python
def fixed_function():
    # corrected implementation
```
</fixed_code>"""

    @staticmethod
    def _default_retry_system_prompt() -> str:
        return """You are an expert code debugger. Based on reviewer feedback, refine your previous fix.

If the reviewer says CORRECT: output the same code unchanged.
If OPTIMIZATION_ONLY: improve code quality without breaking correctness.
If BUG_DETECTED: fix the specific error identified in your previous attempt.

Follow the same format: [Step 1-4] + <fixed_code>."""

    @staticmethod
    def _default_large_model_direct_prompt() -> str:
        return """You are an expert code fixer. Fix the bug in the provided code.

Bug Description: {bug_description}

Buggy Code:
```python
{buggy_code}
```

Previous compile error:
```
{compile_error}
```

Previous test error:
```
{test_error}
```

Fix the bug. Return the corrected code in a python code block.

<fixed_code>
```python
# Your fixed code here
```
</fixed_code>"""


# ==================== BaseAgent 封装 ====================

def create_codefixer_agent(
    agent_id: str = "codefixer_agent",
    small_model_fn: Optional[Callable] = None,
    large_model_fn: Optional[Callable] = None,
    **kwargs,
) -> BaseAgent:
    """
    工厂函数：将 CodeFixer 封装为符合 BaseAgent 接口的 Agent。

    供 MultiAgentOrchestrator 直接注册使用：
        codefixer = create_codefixer_agent(
            agent_id="codefixer",
            small_model_fn=my_small_fn,
            large_model_fn=my_large_fn,
        )
        orchestrator = MultiAgentOrchestrator(codefixer_agent=codefixer, ...)
    """
    fixer = CodeFixer(
        small_model_fn=small_model_fn,
        large_model_fn=large_model_fn,
        **kwargs,
    )

    class CodeFixerAgentWrapper(BaseAgent):
        def __init__(self, fixer_instance: CodeFixer):
            profile = AgentProfile(
                name=agent_id,
                capabilities={AgentCapability.SOLVE, AgentCapability.VERIFY},
                description="小大模型协同代码修复",
                timeout_seconds=120.0,
            )
            super().__init__(agent_id, profile)
            self.fixer = fixer_instance

        def run(self, context: SharedContext) -> Dict[str, Any]:
            return self.fixer.run(context)

    return CodeFixerAgentWrapper(fixer)
