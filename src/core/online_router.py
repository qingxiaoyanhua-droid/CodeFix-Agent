"""
Online Router: Dynamic Pool Selection for Dual-Pool Memory
参考: DECENTMEM Section 4.2

核心设计:
- 每次任务到来时，基于任务特征和当前池权重，决定从 E-pool 还是 X-pool 检索
- 权重由 LLM-as-a-Judge 的 stage-level 反馈实时调整
- 支持置信度阈值：E-pool 有高相关记忆时强制用 E，否则按权重概率

对比你原有的 RAG 检索:
  旧: BM25 + BGE 混合检索 → 固定 top_k → 丢给模型
  新: 路由器先决定用哪个池 → 检索 → judge 评估 → 权重更新
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any
import time


# ==================== 路由器状态 ====================

@dataclass
class RouterState:
    """路由器状态"""
    w_e: float = 1.0  # E-pool 权重
    w_x: float = 1.0  # X-pool 权重
    alpha: float = 0.5  # E-pool 选择概率 = w_e / (w_e + w_x)

    # 历史记录（用于分析）
    decision_history: List[Dict] = field(default_factory=list)
    total_tasks: int = 0
    e_pool_hits: int = 0  # E-pool 被选中的次数
    x_pool_hits: int = 0  # X-pool 被选中的次数

    # 收敛跟踪
    alpha_history: List[float] = field(default_factory=list)  # alpha 随时间变化

    def update_alpha(self):
        self.alpha = self.w_e / (self.w_e + self.w_x)
        self.alpha_history.append(self.alpha)

    def record_decision(self, pool: str, task_id: str, relevance: float,
                       judge_delta: float, judge_score: float):
        """记录一次路由决策"""
        self.decision_history.append({
            "task_id": task_id,
            "selected_pool": pool,
            "relevance": relevance,
            "judge_delta": judge_delta,
            "judge_score": judge_score,
            "timestamp": time.time(),
            "w_e": self.w_e,
            "w_x": self.w_x,
            "alpha": self.alpha,
        })
        self.total_tasks += 1
        if pool == "E":
            self.e_pool_hits += 1
        else:
            self.x_pool_hits += 1


# ==================== 在线路由器 ====================

class OnlineRouter:
    """
    在线路由器

    决策流程:
    1. 接收任务特征 (task_embedding, task_type, bug_signature)
    2. 检查 E-pool 是否有高相关记忆 (relevance >= threshold)
       - 是 → 强制选择 E-pool（exploitation）
       - 否 → 按权重概率选择
    3. 执行检索或生成
    4. 收集 judge 反馈，更新权重

    对应论文 Eq.(2)-(7):
      - 路由器基于权重 w_E, w_X 决定 α = w_E/(w_E + w_X)
      - E-pool 成功 → w_E ↑；E-pool 失败 → w_E ↓
      - X-pool 成功 → 防止 E-pool 过度主导；X-pool 失败 → w_E ↑

    参数:
      - alpha, beta: 权重更新步长（论文默认 α=0.5, β=0.5）
      - force_e_threshold: 强制用 E-pool 的相关性阈值
      - min_alpha, max_alpha: alpha 的下界/上界（防止极端情况）
    """

    def __init__(
        self,
        alpha: float = 0.5,  # 权重更新步长
        beta: float = 0.5,  # 权重衰减系数
        force_e_threshold: float = 0.6,  # 强制 E-pool 的相关性阈值
        min_alpha: float = 0.3,  # alpha 下界
        max_alpha: float = 0.9,  # alpha 上界
        adaptive_threshold: bool = True,  # 是否根据任务类型自适应阈值
    ):
        self.state = RouterState()
        self.alpha_param = alpha
        self.beta_param = beta
        self.force_e_threshold = force_e_threshold
        self.min_alpha = min_alpha
        self.max_alpha = max_alpha
        self.adaptive_threshold = adaptive_threshold

    # ----- 核心决策接口 -----

    def decide(
        self,
        task: Dict,
        e_pool_relevance: float = 0.0,
        task_type: str = None,
    ) -> str:
        """
        决定选择哪个池

        Args:
            task: 任务字典
            e_pool_relevance: E-pool 最高相关记忆的 relevance 分数
            task_type: bug 类型

        Returns:
            "E" 或 "X"
        """
        # 自适应阈值：简单任务更容易复用，复杂任务更需要探索
        threshold = self.force_e_threshold
        if self.adaptive_threshold and task_type:
            complex_types = {"logic_error", "off_by_one", "initialization",
                           "boundary", "null_pointer"}
            if task_type in complex_types:
                threshold = 0.7  # 复杂 bug 要求更高相关性才用 E-pool
            else:
                threshold = 0.5  # 简单 bug 更容易复用

        # 决策逻辑
        if e_pool_relevance >= threshold:
            pool = "E"
        else:
            # 按当前 alpha 概率选择
            pool = "E" if np.random.random() < self.state.alpha else "X"

        # 记录
        self.state.record_decision(
            pool=pool,
            task_id=task.get("task_id", "unknown"),
            relevance=e_pool_relevance,
            judge_delta=0.0,  # 尚未评估
            judge_score=0.0,
        )

        return pool

    def decide_deterministic(
        self,
        task: Dict,
        e_pool_relevance: float = 0.0,
        task_type: str = None,
    ) -> Tuple[str, float]:
        """
        确定性决策接口（用于推理/调试）
        总是选择期望收益更高的池
        """
        threshold = self.force_e_threshold
        if self.adaptive_threshold and task_type:
            complex_types = {"logic_error", "off_by_one", "initialization",
                           "boundary", "null_pointer"}
            if task_type in complex_types:
                threshold = 0.7
            else:
                threshold = 0.5

        if e_pool_relevance >= threshold:
            pool = "E"
        else:
            # 概率选择
            pool = "E" if np.random.random() < self.state.alpha else "X"

        return pool, self.state.alpha

    # ----- 权重更新 -----

    def update(self, used_pool: str, judge_delta: float):
        """
        根据 judge 评估结果更新池权重

        对应论文 Eq.(6)-(7):

        E-pool 被使用:
          Δ = 1 (成功): w_E ← w_E + α
          Δ = 0 (失败): w_E ← max(1.0, β * w_E)

        X-pool 被使用:
          Δ = 1 (成功): w_E ← max(1.0, β * w_E)  ← 抑制 E-pool 过度主导
          Δ = 0 (失败): w_E ← w_E + α  ← 恢复对 E-pool 的信任

        Args:
            used_pool: 使用的池 ("E" 或 "X")
            judge_delta: judge 评估的 improvement（1.0 表示评分提升，0.0 表示下降）
        """
        alpha, beta = self.alpha_param, self.beta_param

        if used_pool == "E":
            if judge_delta > 0:
                # E-pool 这次成功了 → 增加 E-pool 权重
                self.state.w_e = self.state.w_e + alpha
            else:
                # E-pool 这次失败了 → 衰减 E-pool 权重
                self.state.w_e = max(1.0, beta * self.state.w_e)
        else:  # X-pool used
            if judge_delta > 0:
                # X-pool 成功了 → 抑制 E-pool 过度主导
                self.state.w_e = max(1.0, beta * self.state.w_e)
            else:
                # X-pool 失败了 → 恢复对 E-pool 的信任
                self.state.w_e = self.state.w_e + alpha

        # 限制 alpha 范围
        self.state.update_alpha()
        self.state.alpha = np.clip(
            self.state.alpha, self.min_alpha, self.max_alpha
        )

    def update_from_eval(
        self,
        used_pool: str,
        judge_score: float,
        previous_judge_score: float = None,
    ):
        """
        从 judge 评估结果更新权重（更常用的接口）

        Args:
            used_pool: 使用的池
            judge_score: 当前 judge 评分
            previous_judge_score: 上一阶段的 judge 评分（用于计算 Δ）
        """
        if previous_judge_score is not None:
            delta = 1.0 if judge_score > previous_judge_score else 0.0
        else:
            # 无历史分数时：score >= 7 视为成功
            delta = 1.0 if judge_score >= 7.0 else 0.0

        self.update(used_pool, delta)

        # 更新历史记录
        if self.state.decision_history:
            self.state.decision_history[-1]["judge_delta"] = delta
            self.state.decision_history[-1]["judge_score"] = judge_score

    # ----- 分析与调试 -----

    def get_state(self) -> Dict:
        """返回路由器当前状态"""
        return {
            "w_E": self.state.w_e,
            "w_X": self.state.w_x,
            "alpha": self.state.alpha,
            "total_tasks": self.state.total_tasks,
            "E_pool_hits": self.state.e_pool_hits,
            "X_pool_hits": self.state.x_pool_hits,
            "E_pool_ratio": (
                self.state.e_pool_hits / self.state.total_tasks
                if self.state.total_tasks > 0 else 0
            ),
            "alpha_converged": (
                len(self.state.alpha_history) > 20
                and np.std(self.state.alpha_history[-20:]) < 0.05
            ),
        }

    def print_state(self):
        """打印路由器状态"""
        s = self.get_state()
        print("=" * 50)
        print(f"[Router State]")
        print(f"  w_E = {s['w_E']:.2f}, w_X = {s['w_X']:.2f}")
        print(f"  α = {s['alpha']:.3f}  (E-pool selection probability)")
        print(f"  Total tasks: {s['total_tasks']}")
        print(f"  E-pool hits: {s['E_pool_hits']} ({s['E_pool_ratio']:.1%})")
        print(f"  X-pool hits: {s['X_pool_hits']}")
        print(f"  Converged: {s['alpha_converged']}")
        print("=" * 50)

    def convergence_report(self) -> Dict:
        """生成收敛报告"""
        history = self.state.alpha_history
        if len(history) < 10:
            return {"status": "insufficient_data", "message": "需要更多任务才能判断收敛性"}

        recent = history[-20:] if len(history) >= 20 else history
        mean_alpha = np.mean(recent)
        std_alpha = np.std(recent)

        # 检查趋势
        first_half = recent[:len(recent)//2]
        second_half = recent[len(recent)//2:]
        trend = np.mean(second_half) - np.mean(first_half)

        return {
            "status": "converged" if std_alpha < 0.05 else "oscillating",
            "mean_alpha": mean_alpha,
            "std_alpha": std_alpha,
            "trend": trend,
            "total_decisions": len(history),
            "interpretation": (
                f"α 稳定在 {mean_alpha:.3f} 附近 (std={std_alpha:.3f})。"
                f"{'路由器已收敛' if std_alpha < 0.05 else '路由器仍在探索最优 α'}"
            ),
        }

    # ----- 实验接口 -----

    def simulate(self, n_tasks: int, ground_truth_e_better_prob: float = 0.7):
        """
        模拟路由器在人工设定场景下的表现
        用于理解权重更新机制

        Args:
            n_tasks: 模拟任务数
            ground_truth_e_better_prob: E-pool 真正更好的概率
        """
        print(f"\n[Router Simulation] Running {n_tasks} simulated tasks...")
        print(f"Ground truth: E-pool better {ground_truth_e_better_prob:.0%} of the time")

        initial_alpha = self.state.alpha

        for i in range(n_tasks):
            # 模拟 E-pool 相关性
            e_relevance = np.random.beta(2, 2)  # Beta 分布模拟相关性

            # 决定用哪个池
            if e_relevance >= self.force_e_threshold:
                pool = "E"
            else:
                pool = "E" if np.random.random() < self.state.alpha else "X"

            # 模拟 judge 结果
            if pool == "E":
                judge_success = np.random.random() < ground_truth_e_better_prob
            else:
                judge_success = np.random.random() < (1 - ground_truth_e_better_prob * 0.5)

            judge_delta = 1.0 if judge_success else 0.0

            # 更新权重
            self.update(pool, judge_delta)

            if (i + 1) % 20 == 0:
                print(f"  Task {i+1}: α = {self.state.alpha:.3f}, "
                      f"w_E = {self.state.w_e:.2f}, w_X = {self.state.w_x:.2f}")

        final_alpha = self.state.alpha
        print(f"\n[Simulation Result]")
        print(f"  Initial α: {initial_alpha:.3f}")
        print(f"  Final α: {final_alpha:.3f}")
        print(f"  Change: {'+' if final_alpha > initial_alpha else ''}{(final_alpha - initial_alpha):.3f}")

        # 分析
        if ground_truth_e_better_prob > 0.5:
            if final_alpha > initial_alpha:
                print("  ✓ Router correctly learned to prefer E-pool")
            else:
                print("  ✗ Router failed to learn E-pool preference")
        else:
            if final_alpha < initial_alpha:
                print("  ✓ Router correctly learned to prefer X-pool")
            else:
                print("  ✗ Router failed to learn X-pool preference")


# ==================== 集成辅助函数 ====================

def create_decentmem_pipeline(
    dual_memory,
    llm_judge,
    router: OnlineRouter,
    small_model,
    large_model,
    tokenizer,
    embedding_model,
):
    """
    创建 DECENTMEM 风格的代码修复流水线

    完整流程:
    1. 任务到来 → 路由器决定池
    2. 从选中池检索相关记忆
    3. 小模型 + 记忆 → CoT 推理
    4. 执行验证
    5. LLM-as-a-Judge 评估各阶段
    6. 路由器根据 judge 反馈更新权重
    7. 高质量 X-pool 内容合并到 E-pool
    """
    return DECENTMEMCodeFixPipeline(
        dual_memory=dual_memory,
        llm_judge=llm_judge,
        router=router,
        small_model=small_model,
        large_model=large_model,
        tokenizer=tokenizer,
        embedding_model=embedding_model,
    )


class DECENTMEMCodeFixPipeline:
    """
    DECENTMEM 风格的代码修复流水线
    集成: 双池记忆 + LLM-as-a-Judge + 在线路由器
    """

    def __init__(
        self,
        dual_memory,
        llm_judge,
        router: OnlineRouter,
        small_model,
        large_model,
        tokenizer,
        embedding_model,
    ):
        self.dual_memory = dual_memory
        self.llm_judge = llm_judge
        self.router = router
        self.small_model = small_model
        self.large_model = large_model
        self.tokenizer = tokenizer
        self.embedding_model = embedding_model

        # 配置
        self.max_retry = 3
        self.judge_confidence_threshold = 7.0

    def fix(self, buggy_code: str, test_cases: List[Dict],
            task_type: str = None) -> Dict:
        """
        完整修复流程
        """
        task = {
            "buggy_code": buggy_code,
            "task_type": task_type or "unknown",
            "bug_signature": self._extract_signature(buggy_code),
        }

        # Step 1: 路由器决定池
        e_relevance = self._check_e_pool_relevance(task)
        selected_pool = self.router.decide(task, e_relevance, task_type)

        # Step 2: 从选中池检索记忆
        memories = self.dual_memory.retrieve(task, stage="L1", top_k=2, pool_type=selected_pool)
        memory_context = self._format_memory_for_prompt(memories)

        # Step 3: 小模型推理
        previous_judge_score = None

        for attempt in range(self.max_retry):
            # CoT 生成
            prompt = self._build_cot_prompt(buggy_code, memory_context, task_type)
            cot_result = self._generate_cot(prompt)

            # 执行验证
            exec_result = self._verify_fix(buggy_code, cot_result, test_cases)

            # Step 4: LLM-as-a-Judge 评估
            eval_result = self.llm_judge.evaluate_task(
                buggy_code=buggy_code,
                cot_result=cot_result,
                task_id=hash(buggy_code) % 1000000,
                execution_result=exec_result,
            )

            # Step 5: 路由器权重更新
            self.router.update_from_eval(
                used_pool=selected_pool,
                judge_score=eval_result.overall_score,
                previous_judge_score=previous_judge_score,
            )
            previous_judge_score = eval_result.overall_score

            # 成功则记录到记忆
            if eval_result.success:
                self._record_successful_fix(task, cot_result, eval_result, selected_pool)
                break

            # 失败则加入 X-pool（探索）
            if attempt == self.max_retry - 1:
                self._record_failed_attempt(task, cot_result, eval_result)

        # Step 6: X-pool 合并（每轮任务完成后）
        consolidated = self.dual_memory.consolidate_x_to_e()

        return {
            "cot_result": cot_result,
            "evaluation": eval_result,
            "selected_pool": selected_pool,
            "router_state": self.router.get_state(),
            "consolidated_to_e": consolidated,
        }

    # ----- 内部辅助方法 -----

    def _extract_signature(self, code: str) -> str:
        """提取 bug 特征"""
        import ast
        try:
            tree = ast.parse(code)
            sigs = []
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    sigs.append(f"{node.name}/{len(node.args.args)}")
            return ", ".join(sorted(sigs))
        except:
            return code[:100]

    def _check_e_pool_relevance(self, task: Dict) -> float:
        """检查 E-pool 中最高相关记忆的 relevance"""
        memories = self.dual_memory.retrieve(
            task, stage="L1", top_k=1, pool_type="E"
        )
        if not memories:
            return 0.0
        return memories[0].relevance_score(task)

    def _format_memory_for_prompt(self, memories: List) -> str:
        """将记忆碎片格式化为 prompt 上下文"""
        if not memories:
            return "无相关记忆"

        lines = []
        for m in memories:
            lines.append(
                f"[经验] Bug类型: {m.task_type}\n"
                f"推理: {m.cot_steps}\n"
                f"修复结果: {'成功' if m.fix_status == 'success' else '失败'}\n"
            )
        return "\n".join(lines)

    def _build_cot_prompt(self, buggy_code: str, memory_context: str,
                          task_type: str) -> str:
        """构建 CoT prompt"""
        return f"""你是一个专业的代码修复助手。

[历史经验参考]
{memory_context}

[Buggy Code]
```python
{buggy_code}
```

请按照以下四步推理:
1. [Step 1: Bug Identification] 识别 bug 类型和位置
2. [Step 2: Root Cause Analysis] 分析根因
3. [Step 3: Fix Strategy] 制定修复策略
4. [Step 4: Verification] 设计验证方案

用 <fixed_code>...</fixed_code> 包裹最终修复代码。"""

    def _generate_cot(self, prompt: str) -> str:
        """调用小模型生成 CoT"""
        # 复用你现有的 CoT 生成逻辑
        from cot_react_agent import CoTReActAgent
        agent = CoTReActAgent(
            small_model=self.small_model,
            large_model=self.large_model,
            tokenizer=self.tokenizer,
            embedding_model=self.embedding_model,
        )
        # 这里需要适配你的 agent 接口
        return agent.cot_generate(prompt)

    def _verify_fix(self, buggy_code: str, cot_result: str,
                    test_cases: List[Dict]) -> Dict:
        """验证修复结果"""
        # 复用你现有的 shell_sandbox 逻辑
        from shell_sandbox import ShellSandbox
        sandbox = ShellSandbox()

        fixed_code = self._extract_fixed_code(cot_result)
        return sandbox.run_tests(fixed_code, test_cases)

    def _extract_fixed_code(self, cot_result: str) -> str:
        """从 CoT 结果中提取修复代码"""
        import re
        match = re.search(r'<fixed_code>(.*?)</fixed_code>', cot_result, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _record_successful_fix(self, task: Dict, cot_result: str,
                               eval_result, pool: str):
        """将成功的修复记录到双池"""
        from dual_pool_memory import MemoryPiece
        import hashlib

        steps = self.llm_judge._parse_cot_steps(cot_result)
        judge_scores = {f"L{i+1}": e.score for i, e in enumerate(eval_result.stage_evals)}

        piece = MemoryPiece(
            piece_id=hashlib.md5(task["buggy_code"].encode()).hexdigest()[:12],
            task_type=task["task_type"],
            bug_signature=task["bug_signature"],
            full_buggy_code=task["buggy_code"],
            cot_steps=[{"step_id": k, "content": v} for k, v in steps.items()],
            final_fix=self._extract_fixed_code(cot_result),
            fix_status="success",
            judge_scores=judge_scores,
            execution_pass_rate=1.0,
            source="decentmem_pipeline",
            agent_pool=f"{pool}-pool",
            stage_pool="L1",
        )

        self.dual_memory.add_to_e_pool(piece)

    def _record_failed_attempt(self, task: Dict, cot_result: str,
                               eval_result):
        """将失败的尝试记录到 X-pool"""
        from dual_pool_memory import MemoryPiece
        import hashlib

        steps = self.llm_judge._parse_cot_steps(cot_result)
        judge_scores = {f"L{i+1}": e.score for i, e in enumerate(eval_result.stage_evals)}

        piece = MemoryPiece(
            piece_id=hashlib.md5((task["buggy_code"] + "failed").encode()).hexdigest()[:12],
            task_type=task["task_type"],
            bug_signature=task["bug_signature"],
            full_buggy_code=task["buggy_code"],
            cot_steps=[{"step_id": k, "content": v} for k, v in steps.items()],
            final_fix=self._extract_fixed_code(cot_result),
            fix_status="failed",
            judge_scores=judge_scores,
            execution_pass_rate=eval_result.overall_score / 10.0,
            source="decentmem_pipeline",
            agent_pool="X-pool",
            stage_pool="L1",
        )

        self.dual_memory.add_to_x_pool(piece)
