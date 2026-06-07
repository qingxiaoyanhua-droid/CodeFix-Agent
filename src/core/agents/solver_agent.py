#!/usr/bin/env python3
"""
Solver Agent: 代码修复生成器
=============================
职责：
  - 小模型 CoT 推理生成（4 步：识别 → 根因 → 策略 → 边界检查）
  - AST 函数级精确定位与替换
  - 编译失败后大模型错误分析反馈
  - 复用 L2/L3 记忆注入
  - Symbol-Aware Pre-Search（MAC 论文最佳实践）

从 cot_react_agent.py 的 CoTReActAgent 中提取生成逻辑，
保持与原版完全兼容的 prompt 格式。
"""

from __future__ import annotations

import re
import ast as py_ast
import hashlib
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Tuple

from base_agent import BaseAgent, AgentProfile, AgentCapability, SharedContext

# ==================== CoT Prompt 模板（从 cot_react_agent.py 提取） ====================

COT_SYSTEM_PROMPT = """You are an expert code debugger. Think step-by-step before fixing.

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

COT_RETRY_SYSTEM_PROMPT = """You are an expert code debugger. Based on reviewer feedback, refine your previous fix.

If the reviewer says CORRECT: output the same code unchanged.
If OPTIMIZATION_ONLY: improve code quality without breaking correctness.
If BUG_DETECTED: fix the specific error identified in your previous attempt.

Follow the same format: <think> with 4 steps + <fixed_code>. """

ERROR_ANALYSIS_PROMPT = """You are a senior code reviewer analyzing a code fix attempt.

**Original Buggy Code:**
```python
{buggy_code}
```

**Junior's Chain-of-Thought Reasoning:**
{cot_reasoning}

**Junior's Attempted Fix:**
```python
{attempted_fix}
```

**Actual Execution Result:**
```
{error_message}
```

Analyze the fix attempt and respond in JSON with these fields:

1. STATUS: Classify the current state:
   - "BUG_DETECTED": Code has a functional bug (wrong output, crash, test failure)
   - "OPTIMIZATION_ONLY": Code runs without errors but could be improved (inefficiency, style, edge cases)
   - "CORRECT": Code is correct and complete

2. first_error_step: If BUG_DETECTED, which reasoning step (1-4) contains the FIRST error? (1-4 or 0 if none)

3. error_in_reasoning: What specifically went wrong?

4. correct_reasoning: What should the correct reasoning be?

5. hint: Actionable hint for the fix (do NOT give the full solution).

6. error_type: One of "logic_error|incomplete_fix|wrong_operator|missing_edge_case|inefficiency|other"

7. optimization_note: If OPTIMIZATION_ONLY, describe what could be improved.

Respond in JSON:
```json
{{
  "status": "BUG_DETECTED|OPTIMIZATION_ONLY|CORRECT",
  "first_error_step": 1-4 or 0,
  "error_in_reasoning": "...",
  "correct_reasoning": "...",
  "hint": "...",
  "error_type": "...",
  "optimization_note": "..."
}}
```"""


# ==================== CoT Parser（从 cot_react_agent.py 提取） ====================

class CoTParser:
    """从模型输出中解析 CoT 推理和修复代码"""

    COT_PATTERN = re.compile(
        r'\[Step\s*(\d+):\s*([^\]]+)\]\s*(.*?)(?=\[Step|\<fixed_code\>|$)',
        re.DOTALL
    )
    CODE_PATTERN = re.compile(
        r'\<fixed_code\>\s*```(?:python)?\s*(.*?)```',
        re.DOTALL
    )

    def parse(self, raw_output: str) -> Tuple[List[Dict], str]:
        steps = []
        fixed_code = ""

        for m in self.COT_PATTERN.finditer(raw_output):
            step_num = int(m.group(1))
            step_title = m.group(2).strip()
            step_content = m.group(3).strip()
            steps.append({
                "step_id": step_num,
                "title": step_title,
                "content": step_content,
            })

        code_match = self.CODE_PATTERN.search(raw_output)
        if code_match:
            fixed_code = code_match.group(1).strip()

        return steps, fixed_code


# ==================== Solver Agent ====================

class SolverAgent(BaseAgent):
    """
    代码修复生成 Agent

    职责：
      1. 小模型 CoT 生成（首轮 + 重试轮）
      2. 大模型错误分析（编译失败后）
      3. AST 函数级精确定位与替换（解决行号偏移）
      4. Symbol-Aware Pre-Search（MAC 论文最佳实践）

    与现有代码的关系：
      - 复用 cot_react_agent.py 的 prompt 模板
      - 复用 ASTCodeProcessor（字节偏移替换）
      - 复用 CoTParser（输出解析）
    """

    def __init__(
        self,
        agent_id: str = "solver_agent",
        small_model_fn=None,
        large_model_fn=None,
        language: str = "python",
        ast_processor=None,
    ):
        profile = AgentProfile(
            name=agent_id,
            capabilities={AgentCapability.SOLVE},
            description="小模型CoT推理 + 大模型错误分析 + AST精确替换",
            timeout_seconds=30.0,
        )
        super().__init__(agent_id, profile)

        self.small_model_fn = small_model_fn  # fn(user_prompt, system_prompt) -> str
        self.large_model_fn = large_model_fn  # fn(user_prompt, system_prompt) -> str
        self.language = language
        self.ast_processor = ast_processor
        self.parser = CoTParser()

        # 用于 Symbol-Aware Pre-Search（MAC 论文最佳实践）
        self._symbol_cache: Dict[str, List[str]] = {}

    def run(self, context: SharedContext) -> Dict[str, Any]:
        """
        执行代码修复生成。

        根据 current_round 决定：
          - round == 0: 首轮生成（注入记忆 + RAG 案例）
          - round > 0: 重试生成（注入审查反馈）
        """
        context.status = "solving"

        # 选择生成策略
        if context.current_round == 0:
            return self._generate_first_attempt(context)
        else:
            return self._generate_retry(context)

    # ==================== 首轮生成 ====================

    def _generate_first_attempt(self, context: SharedContext) -> Dict[str, Any]:
        """首轮生成：小模型 CoT + 记忆注入 + RAG 案例"""
        user_prompt = self._build_first_prompt(context)

        # 调用小模型
        if self.small_model_fn is None:
            return {
                "status": "error",
                "error": "No small model function configured",
            }

        try:
            raw_output = self.small_model_fn(
                user_prompt,
                COT_SYSTEM_PROMPT,
            )
        except Exception as e:
            return {
                "status": "error",
                "error": f"Small model call failed: {e}",
            }

        # 解析输出
        cot_steps, fixed_code = self.parser.parse(raw_output)

        # AST 函数级替换（如果提供了具体函数名）
        if context.solver_output and context.solver_output.get("target_function"):
            fixed_code = self._ast_replace_function(
                context.buggy_code,
                context.solver_output["target_function"],
                fixed_code,
            )

        result = {
            "status": "ok",
            "cot_steps": cot_steps,
            "fixed_code": fixed_code,
            "raw_output": raw_output,
            "round": context.current_round,
            "model_used": "small",
        }

        context.solver_output = result
        context.record_step(self.name, "first_attempt", fixed_code[:100])
        return result

    def _build_first_prompt(self, context: SharedContext) -> str:
        """构建首轮 prompt，注入记忆 + RAG 案例"""
        bug_desc = context.bug_description
        buggy_code = context.buggy_code

        user_prompt = f"Bug: {bug_desc}\n\nBuggy Code:\n```python\n{buggy_code}\n```\n"

        memory_out = context.memory_output or {}
        retrieval_result = memory_out.get("retrieval_result", {})

        # L2: 失败教训注入（what NOT to do）
        l2_memories = retrieval_result.get("l2_memories", [])
        if l2_memories:
            user_prompt += "\n--- LESSONS FROM PAST SIMILAR BUGS ---\n"
            for i, m in enumerate(l2_memories[:2], 1):
                user_prompt += (
                    f"\nLesson {i}:\n"
                    f"- Bug type: {m.get('bug_type', 'unknown')}\n"
                    f"- What failed: {m.get('failure_reason', '')[:100]}\n"
                )
            user_prompt += "\nApply these lessons to avoid repeating the same mistakes.\n"

        # L3: 成功 SOP 注入（how TO do it right）
        l3_memories = retrieval_result.get("l3_memories", [])
        if l3_memories:
            user_prompt += "\n--- RELEVANT FIX SOPs FROM PAST SUCCESSES ---\n"
            for i, m in enumerate(l3_memories[:2], 1):
                user_prompt += (
                    f"\nSkill {i}:\n"
                    f"- Bug type: {m.get('bug_type', 'unknown')}\n"
                    f"- Fix snippet: {m.get('fixed_code_snippet', m.get('final_fix', ''))[:150]}\n"
                )
            user_prompt += "\nFollow these SOPs where applicable.\n"

        # E-pool: 成功轨迹注入
        e_pool = retrieval_result.get("e_pool_memories", [])
        if e_pool:
            user_prompt += "\n--- SIMILAR SUCCESSFUL FIXES ---\n"
            for i, m in enumerate(e_pool[:2], 1):
                user_prompt += (
                    f"\nExample {i} ({m.get('task_type', '')}):\n"
                    f"- Bug signature: {m.get('bug_signature', '')[:100]}\n"
                    f"- Fix: {m.get('final_fix', '')[:150]}\n"
                )

        # RAG: 相似案例注入
        rag_results = retrieval_result.get("rag_results", [])
        if rag_results:
            user_prompt += "\n--- SIMILAR CASES FROM KNOWLEDGE BASE ---\n"
            for i, r in enumerate(rag_results[:2], 1):
                user_prompt += (
                    f"\nCase {i} (score={r.get('score', 0):.2f}):\n"
                    f"- {r.get('content', '')[:200]}\n"
                )

        # MAC 论文最佳实践：Symbol Pre-Search
        symbols = self._extract_symbols(context.bug_description)
        if symbols:
            self._symbol_cache[context.task_id] = symbols
            user_prompt += f"\n--- RELEVANT SYMBOLS (Pre-Searched) ---\n"
            user_prompt += f"Focus on: {', '.join(symbols[:5])}\n"

        user_prompt += "\nFix the bug. Follow the required format."
        return user_prompt

    # ==================== 重试生成 ====================

    def _generate_retry(self, context: SharedContext) -> Dict[str, Any]:
        """重试生成：注入审查反馈 + L2/L3"""
        reviewer_out = context.reviewer_output or {}

        if reviewer_out.get("outcome") == "success":
            # Reviewer 已确认正确，直接返回
            return {
                "status": "ok",
                "cot_steps": [],
                "fixed_code": (context.solver_output or {}).get("fixed_code", ""),
                "round": context.current_round,
                "model_used": "none",
                "note": "Reviewer confirmed correct, no changes needed",
            }

        # 构建重试 prompt
        prev_cot = ""
        prev_code = ""
        if context.solver_output:
            steps = context.solver_output.get("cot_steps", [])
            for step in steps:
                prev_cot += f"[Step {step.get('step_id', '?')}: {step.get('title', '')}]\n{step.get('content', '')}\n"
            prev_code = context.solver_output.get("fixed_code", "")

        prev_error = reviewer_out.get("compile_error") or reviewer_out.get("test_error", "")
        status = reviewer_out.get("outcome", "BUG_DETECTED")

        user_prompt = self._build_retry_prompt(
            context, prev_cot, prev_code, prev_error, status
        )

        # 调用小模型
        if self.small_model_fn is None:
            return {
                "status": "error",
                "error": "No small model function configured",
            }

        try:
            raw_output = self.small_model_fn(
                user_prompt,
                COT_RETRY_SYSTEM_PROMPT,
            )
        except Exception as e:
            return {
                "status": "error",
                "error": f"Small model retry failed: {e}",
            }

        cot_steps, fixed_code = self.parser.parse(raw_output)

        result = {
            "status": "ok",
            "cot_steps": cot_steps,
            "fixed_code": fixed_code,
            "raw_output": raw_output,
            "round": context.current_round,
            "model_used": "small",
            "reviewer_status": status,
        }

        context.solver_output = result
        context.record_step(self.name, f"retry_{context.current_round}", fixed_code[:100])
        return result

    def _build_retry_prompt(
        self,
        context: SharedContext,
        prev_cot: str,
        prev_code: str,
        prev_error: str,
        status: str,
    ) -> str:
        """构建重试 prompt"""
        user_prompt = (
            f"Bug: {context.bug_description}\n\n"
            f"Buggy Code:\n```python\n{context.buggy_code}\n```\n\n"
            f"--- YOUR PREVIOUS ATTEMPT ---\n\n"
            f"Your previous reasoning:\n{prev_cot}\n\n"
            f"Your previous fix:\n```python\n{prev_code}\n```\n\n"
            f"Execution result:\n```\n{prev_error}\n```\n\n"
            f"--- SENIOR REVIEWER FEEDBACK ---\n\n"
            f"STATUS: {status}\n"
        )

        if status == "CORRECT":
            user_prompt += "The reviewer confirms your fix is CORRECT. No changes needed.\n"
        elif status == "OPTIMIZATION_ONLY":
            user_prompt += "The reviewer says your code runs but could be improved. Improve quality while maintaining correctness.\n"
        else:
            bug_reports = (context.reviewer_output or {}).get("bug_reports", [])
            if bug_reports:
                critical = [b for b in bug_reports if b.get("severity") in ("critical", "high")]
                if critical:
                    user_prompt += f"Critical/High issues found:\n"
                    for b in critical[:3]:
                        user_prompt += f"  - [{b.get('dimension')}] {b.get('description')}: {b.get('suggestion')}\n"
            user_prompt += "Fix the bug based on the reviewer feedback.\n"

        # 重新注入 L2/L3（加强记忆）
        memory_out = context.memory_output or {}
        retrieval_result = memory_out.get("retrieval_result", {})
        l2 = retrieval_result.get("l2_memories", [])
        if l2:
            user_prompt += "\n--- PAST SIMILAR BUG LESSONS ---\n"
            for i, m in enumerate(l2[:2], 1):
                user_prompt += f"Lesson {i}: {m.get('failure_reason', '')[:100]}\n"

        user_prompt += "\nFix the bug. Follow the required format."
        return user_prompt

    # ==================== 大模型错误分析 ====================

    def analyze_error_with_large_model(
        self,
        context: SharedContext,
    ) -> Dict[str, Any]:
        """
        编译失败后，调用大模型进行错误分析。

        参考 MAC 论文：强模型（GPT-4o / DeepSeek）负责分析，
        弱模型（Qwen2.5-Coder-1.5B）负责生成。
        """
        if self.large_model_fn is None:
            return {"status": "no_large_model"}

        buggy_code = context.buggy_code
        prev_cot = ""
        prev_code = ""

        if context.solver_output:
            steps = context.solver_output.get("cot_steps", [])
            for step in steps:
                prev_cot += f"[Step {step.get('step_id', '?')}: {step.get('title', '')}]\n{step.get('content', '')}\n"
            prev_code = context.solver_output.get("fixed_code", "")

        error_msg = (
            (context.reviewer_output or {}).get("compile_error") or
            (context.reviewer_output or {}).get("test_error") or
            "Unknown error"
        )

        prompt = ERROR_ANALYSIS_PROMPT.format(
            buggy_code=buggy_code,
            cot_reasoning=prev_cot,
            attempted_fix=prev_code,
            error_message=error_msg,
        )

        try:
            raw = self.large_model_fn(
                prompt,
                "You are a senior code reviewer. Respond in JSON only.",
            )

            # 解析 JSON
            json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
            text = json_match.group(1) if json_match else raw.strip()
            data = {}
            try:
                import json
                data = json.loads(text)
            except Exception:
                pass

            feedback = {
                "status": data.get("status", "BUG_DETECTED"),
                "first_error_step": data.get("first_error_step", 0),
                "error_in_reasoning": data.get("error_in_reasoning", ""),
                "hint": data.get("hint", ""),
                "error_type": data.get("error_type", "other"),
                "optimization_note": data.get("optimization_note", ""),
            }

            context.record_step(self.name, "large_model_analysis", feedback.get("hint", "")[:100])
            return {"status": "ok", "feedback": feedback}

        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ==================== AST 精确替换 ====================

    def _ast_replace_function(
        self,
        original_code: str,
        function_name: str,
        new_function_code: str,
    ) -> str:
        """使用 AST 字节偏移精确替换函数"""
        if self.ast_processor is None:
            return new_function_code

        try:
            return self.ast_processor.replace_function(original_code, function_name, new_function_code)
        except Exception:
            return new_function_code

    # ==================== Symbol-Aware Pre-Search（MAC 论文） ====================

    def _extract_symbols(self, text: str) -> List[str]:
        """从 bug 描述中提取关键符号（类名、函数名等）"""
        backticks = re.findall(r'`([A-Za-z_][A-Za-z0-9_]*)`', text)
        camel = re.findall(r'\b([A-Z][a-zA-Z0-9]{3,})\b', text)
        dotted = re.findall(
            r'\b([a-z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*){1,3})\b',
            text
        )

        # 优先级：backtick > CamelCase > dotted
        seen = set()
        result = []
        for syms, priority in [(backticks, 3), (camel, 2), (dotted, 1)]:
            for s in syms:
                if s not in seen and len(s) >= 2:
                    seen.add(s)
                    result.append((priority, s))

        result.sort(key=lambda x: -x[0])
        return [s for _, s in result[:5]]

    def get_symbols(self, task_id: str) -> List[str]:
        """获取之前 Pre-Search 的符号列表"""
        return self._symbol_cache.get(task_id, [])
