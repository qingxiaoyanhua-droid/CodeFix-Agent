#!/usr/bin/env python3
"""
Multi-Agent Orchestrator: 去中心化协作调度中枢
=============================================
职责：
  - 任务生命周期管理（init → solving → reviewing → memory → done/failed）
  - 协调 Solver / Reviewer / Memory / CodeFixer 四个 Agent 的执行顺序
  - 管理 SharedContext 的读写
  - 控制重试循环（max 3 轮）+ 循环检测（多维度 stall 检测）
  - 收集轨迹用于 GRPO 训练数据
  - 去中心化记忆：每个 Agent 有独立 DualPoolMemory，通过共享存储层同步

两种模式：
  1. 经典模式：SolverAgent → ReviewerAgent → 循环重试（多轮）
  2. CodeFixer 模式：CodeFixer 原子操作（封装小大模型协同循环）

去中心化设计原则（参考 MAC 论文）：
  - 多个 Agent 各自独立，有明确边界
  - Orchestrator 只负责协调，不做具体生成/审查/记忆操作
  - Agent 之间通过 SharedContext 通信，不直接调用彼此
  - 每个 Agent 有独立的 DualPoolMemory（去中心化记忆）
  - 记忆通过共享存储层（SharedMemoryStore）实现跨 Agent 同步
  - 支持异步消息队列（未来扩展）

Classic 协作流程（Solver + Reviewer 循环）：
  Round 0:
    Orchestrator ──→ MemoryAgent: 检索记忆
              ←── memory_output

    Orchestrator ──→ SolverAgent: 首轮生成
              ←── solver_output

    Orchestrator ──→ ReviewerAgent: 审查
              ←── reviewer_output

    Reviewer ✅ → Orchestrator: 写轨迹 → done

    Reviewer ❌ → Orchestrator: 写教训到 MemoryAgent → 下一轮

  Round N (N > 0):
    Orchestrator ──→ SolverAgent: 重试（注入审查反馈）
              ←── solver_output

    Orchestrator ──→ ReviewerAgent: 审查
              ←── reviewer_output
    ...
"""

from __future__ import annotations

import time
import json
import logging
import hashlib
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Set

from agents.base_agent import (
    BaseAgent, AgentProfile, AgentCapability,
    SharedContext, AgentMessage, MessageType
)
from agents.codefixer import CodeFixer, FixResult, FixStatus

logger = logging.getLogger(__name__)


# ==================== 循环检测 ====================


@dataclass
class CycleDetectionResult:
    """循环检测结果"""
    is_stuck: bool
    reason: str
    stall_rounds: int
    suggestions: List[str] = field(default_factory=list)


class CycleDetector:
    """
    多维度循环 / stall 检测器。

    检测维度：
    1. 重复结果循环 — Solver 连续生成相似代码（编辑距离 < 阈值）
    2. 审查失败循环 — Reviewer 连续报告同类错误
    3. 进展停滞循环 — 连续 N 轮质量分无提升
    4. 栈套娃循环 — SharedContext message_history 中检测 Agent 间互相调用
    """

    def __init__(
        self,
        max_consecutive_same_bug: int = 2,
        max_consecutive_quality_plateau: int = 3,
        max_message_chain_depth: int = 5,
    ):
        self.max_consecutive_same_bug = max_consecutive_same_bug
        self.max_consecutive_quality_plateau = max_consecutive_quality_plateau
        self.max_message_chain_depth = max_message_chain_depth

        self._reset()

    def _reset(self):
        self._consecutive_same_code: int = 0
        self._consecutive_same_bug: int = 0
        self._consecutive_quality_plateau: int = 0
        self._last_quality: float = 0.0
        self._last_code_hash: str = ""
        self._failed_bug_types: List[str] = []

    @staticmethod
    def _code_hash(code: str) -> str:
        return hashlib.md5(code.encode()).hexdigest()[:16]

    def detect(self, context: SharedContext, current_round: int) -> CycleDetectionResult:
        """
        对当前轮次执行循环检测。

        四个维度，任一触发阈值即返回 stuck。
        """
        suggestions = []
        stall_rounds = 0
        is_stuck = False
        reason = ""

        # 维度1：重复代码循环
        if context.solver_output:
            current_code = context.solver_output.get("fixed_code", "")
            current_hash = self._code_hash(current_code)
            if self._last_code_hash and current_hash == self._last_code_hash:
                self._consecutive_same_code += 1
            else:
                self._consecutive_same_code = 1
            self._last_code_hash = current_hash

            if self._consecutive_same_code >= self.max_consecutive_same_bug:
                is_stuck = True
                reason = (
                    f"重复代码循环：Solver 连续 {self._consecutive_same_code} 轮生成相同代码"
                )
                suggestions.append(
                    "注入不同的 bug 特征提示，或强制切换到 X-pool 探索模式"
                )

        # 维度2：同类审查失败循环
        if context.reviewer_output:
            reviewer_out = context.reviewer_output
            if reviewer_out.get("compile_error"):
                failed_bug_type = "compile_error"
            elif reviewer_out.get("test_error"):
                failed_bug_type = "test_error"
            elif reviewer_out.get("nudge_triggered"):
                failed_bug_type = "nudge"
            elif reviewer_out.get("outcome") == "failed":
                failed_bug_type = "general_failure"
            else:
                failed_bug_type = ""

            if failed_bug_type:
                if self._failed_bug_types and self._failed_bug_types[-1] == failed_bug_type:
                    self._consecutive_same_bug += 1
                else:
                    self._consecutive_same_bug = 1
                self._failed_bug_types.append(failed_bug_type)

                if self._consecutive_same_bug >= self.max_consecutive_same_bug:
                    is_stuck = True
                    if not reason:
                        reason = (
                            f"同类审查失败循环：Reviewer 连续 {self._consecutive_same_bug} 轮报告 "
                            f"同类错误 [{failed_bug_type}]"
                        )
                    suggestions.append(
                        f"强制触发大模型接管（Big Model Takeover），绕过小模型直接修复 [{failed_bug_type}]"
                    )
            else:
                self._consecutive_same_bug = 0
                self._failed_bug_types = []

        # 维度3：质量分停滞
        current_quality = (
            context.reviewer_output.get("quality_score", 0.0)
            if context.reviewer_output else 0.0
        )
        if current_round > 1:
            if abs(current_quality - self._last_quality) < 0.05:
                self._consecutive_quality_plateau += 1
            else:
                self._consecutive_quality_plateau = 0
            self._last_quality = current_quality

            if self._consecutive_quality_plateau >= self.max_consecutive_quality_plateau:
                if not is_stuck:
                    is_stuck = True
                    reason = (
                        f"质量分停滞：连续 {self._consecutive_quality_plateau} 轮质量分变化 < 0.05"
                    )
                suggestions.append(
                    "触发 OnlineRouter 权重调整，降低 E-pool 置信度，增加 X-pool 探索概率"
                )
                stall_rounds = max(stall_rounds, self._consecutive_quality_plateau)

        # 维度4：Agent 套娃循环（message_history 检测）
        message_history = getattr(context, "message_history", [])
        if len(message_history) >= self.max_message_chain_depth:
            recent = message_history[-self.max_message_chain_depth:]
            agents_in_chain = [msg.sender for msg in recent]
            unique_agents = set(agents_in_chain)
            if len(unique_agents) <= 2:
                is_stuck = True
                if not reason:
                    reason = (
                        f"Agent 套娃循环：message_history 检测到 {len(agents_in_chain)} 次 "
                        f"反复调用 [{', '.join(unique_agents)}]"
                    )
                suggestions.append("终止循环，回退到上一轮有效结果")
                stall_rounds = max(stall_rounds, len(agents_in_chain))

        if is_stuck:
            logger.warning(
                f"[CycleDetector] STUCK @ round {current_round}: {reason}"
            )

        return CycleDetectionResult(
            is_stuck=is_stuck,
            reason=reason,
            stall_rounds=stall_rounds,
            suggestions=suggestions,
        )


# ==================== 去中心化记忆共享存储层 ====================


class SharedMemoryStore:
    """
    去中心化记忆的共享存储层。

    设计原则：
    - 每个 Agent 有独立的 DualPoolMemory 实例（per-agent 本地记忆）
    - 通过 SharedMemoryStore 实现跨 Agent 记忆同步
    - 同步策略：任务完成后，Agent 将高质量记忆碎片写入共享存储
    - 其他 Agent 下次检索时，从共享存储拉取最新碎片
    - 使用文件锁保证多进程写入安全

    对应 DECENTMEM 论文 Section 4.3：
    "记忆碎片通过共享存储层实现跨 Agent 传播，同时保留 Agent 私有记忆"
    """

    def __init__(self, shared_dir: str = "runs/shared_memory"):
        import os
        from pathlib import Path
        self.shared_dir = Path(shared_dir)
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.shared_e_path = self.shared_dir / "shared_e_pool.jsonl"
        self.shared_x_path = self.shared_dir / "shared_x_pool.jsonl"
        self._lock_file = self.shared_dir / ".sync.lock"

    def _acquire_lock(self) -> bool:
        """简单文件锁，防止多进程同时写入"""
        import os
        import time
        for _ in range(10):
            try:
                fd = os.open(str(self._lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return True
            except FileExistsError:
                time.sleep(0.1)
        return False

    def _release_lock(self):
        import os
        try:
            os.remove(str(self._lock_file))
        except FileNotFoundError:
            pass

    def publish(self, piece: Dict, pool: str = "E"):
        """Agent 任务完成后，将记忆碎片发布到共享存储"""
        if not self._acquire_lock():
            logger.warning("[SharedMemory] Failed to acquire lock, skipping publish")
            return
        try:
            path = self.shared_e_path if pool == "E" else self.shared_x_path
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(piece, ensure_ascii=False) + "\n")
        finally:
            self._release_lock()

    def pull_new(self, last_read_line: int = 0) -> tuple[List[Dict], int]:
        """
        从共享存储拉取新碎片（自 last_read_line 之后的行）。

        Returns:
            (new_pieces, last_read_line)
        """
        if not self.shared_e_path.exists():
            return [], last_read_line

        pieces = []
        try:
            with open(self.shared_e_path, encoding="utf-8") as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if i >= last_read_line:
                    stripped = line.strip()
                    if stripped:
                        try:
                            pieces.append(json.loads(stripped))
                        except json.JSONDecodeError:
                            pass

            new_last = len(lines)
            return pieces, new_last
        except (IOError, OSError):
            return [], last_read_line

    def merge_to_agent_pool(self, agent_e_pool: List, agent_x_pool: List,
                             e_weight: float = 0.7) -> tuple[int, int]:
        """
        将共享存储中的碎片合并到 Agent 本地池。

        基于权重决定加入 E-pool 还是 X-pool：
        - quality >= e_weight → E-pool（成功经验）
        - quality < e_weight → X-pool（候选策略）
        """
        new_e, new_x = 0, 0
        pieces, _ = self.pull_new(last_read_line=0)

        for piece in pieces:
            quality = piece.get("quality_score", 0.5)
            if quality >= e_weight:
                agent_e_pool.append(piece)
                new_e += 1
            else:
                agent_x_pool.append(piece)
                new_x += 1

        return new_e, new_x


# ==================== 任务状态 ====================

class TaskPhase(Enum):
    """任务执行阶段"""
    INIT          = "init"
    MEMORY_RETRIEVE = "memory_retrieve"
    SOLVING       = "solving"
    REVIEWING     = "reviewing"
    MEMORY_WRITE  = "memory_write"
    DONE          = "done"
    FAILED        = "failed"


# ==================== 调度结果 ====================

@dataclass
class OrchestratorResult:
    """Orchestrator 最终输出"""
    success: bool
    task_id: str
    phase: str
    final_code: str = ""
    quality_score: float = 0.0
    total_rounds: int = 0
    total_time_ms: float = 0.0
    trajectory: List[Dict] = field(default_factory=list)

    # 各 Agent 统计
    solver_calls: int = 0
    reviewer_calls: int = 0
    memory_retrievals: int = 0
    memory_writes: int = 0

    # CodeFixer 统计（当使用 CodeFixer 模式时）
    codefixer_calls: int = 0
    small_model_calls: int = 0
    large_model_calls: int = 0
    sandbox_runs: int = 0
    large_model_took_over: bool = False

    # 错误信息
    error: str = ""

    # FixStatus（来自 CodeFixer）
    fix_status: str = ""

    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "phase": self.phase,
            "final_code": self.final_code,
            "quality_score": self.quality_score,
            "total_rounds": self.total_rounds,
            "total_time_ms": self.total_time_ms,
            "trajectory": self.trajectory,
            "solver_calls": self.solver_calls,
            "reviewer_calls": self.reviewer_calls,
            "memory_retrievals": self.memory_retrievals,
            "memory_writes": self.memory_writes,
            "codefixer_calls": self.codefixer_calls,
            "small_model_calls": self.small_model_calls,
            "large_model_calls": self.large_model_calls,
            "sandbox_runs": self.sandbox_runs,
            "large_model_took_over": self.large_model_took_over,
            "error": self.error,
            "fix_status": self.fix_status,
        }


# ==================== MultiAgentOrchestrator ====================

class MultiAgentOrchestrator:
    """
    去中心化 Multi-Agent 调度中枢

    核心设计：
      1. 支持两种模式：
         - 经典模式：Solver + Reviewer + Memory 三 Agent 并行注册
         - CodeFixer 模式：CodeFixer 原子操作（封装小大模型协同）
      2. 状态机驱动：每个 phase 对应一个 Agent 调用
      3. SharedContext 作为唯一真数据源
      4. 轨迹收集：用于 GRPO 训练数据

    使用方式（经典模式）：
      orchestrator = MultiAgentOrchestrator(
          solver_agent=solver,
          reviewer_agent=reviewer,
          memory_agent=memory,
      )
      result = orchestrator.run(buggy_code="...", bug_description="...")

    使用方式（CodeFixer 模式）：
      codefixer = create_codefixer_agent(
          small_model_fn=small_fn,
          large_model_fn=large_fn,
      )
      orchestrator = MultiAgentOrchestrator(
          codefixer_agent=codefixer,
          memory_agent=memory,
      )
      result = orchestrator.run(buggy_code="...", bug_description="...")
    """

    def __init__(
        self,
        solver_agent: Optional[BaseAgent] = None,
        reviewer_agent: Optional[BaseAgent] = None,
        memory_agent: Optional[BaseAgent] = None,
        codefixer_agent: Optional[BaseAgent] = None,
        max_rounds: int = 3,
        enable_memory_write: bool = True,
        use_codefixer_mode: bool = False,
        shared_memory_dir: str = "runs/shared_memory",
        human_intervention_fn: Optional[callable] = None,
    ):
        self.solver_agent = solver_agent
        self.reviewer_agent = reviewer_agent
        self.memory_agent = memory_agent
        self.codefixer_agent = codefixer_agent
        self.max_rounds = max_rounds
        self.enable_memory_write = enable_memory_write
        self.use_codefixer_mode = use_codefixer_mode

        # 循环检测器（每个任务实例独立）
        self._cycle_detector: Optional[CycleDetector] = None
        self._last_valid_result: Optional[Dict] = None  # stall 时回退用

        # 去中心化记忆共享存储层
        self._shared_memory = SharedMemoryStore(shared_dir=shared_memory_dir)

        # 人工介入（stuck 时调用，传入上下文，返回 (code, ok)）
        # code: 用户修复后的代码（空字符串表示跳过人工介入）
        # ok: True=成功修复，False=放弃
        self._human_intervention_fn = (
            human_intervention_fn or self._default_cli_intervention
        )

        # 统计
        self._stats = {
            "solver_calls": 0,
            "reviewer_calls": 0,
            "memory_retrievals": 0,
            "memory_writes": 0,
            "codefixer_calls": 0,
            "small_model_calls": 0,
            "large_model_calls": 0,
            "sandbox_runs": 0,
        }

    # ==================== 主入口 ====================

    def run(
        self,
        buggy_code: str,
        bug_description: str = "",
        error_message: str = "",
        language: str = "python",
        test_cases: List[Dict] = None,
        task_id: str = "",
    ) -> OrchestratorResult:
        """
        完整的多 Agent 协作流程。

        根据 use_codefixer_mode 决定使用哪种模式：
          - False（默认）：经典模式，Solver + Reviewer 循环
          - True：CodeFixer 原子操作模式

        每个任务经历：
          1. 初始化 SharedContext
          2. 记忆检索（Memory Agent）
          3. 代码修复（Solver+Reviewer 或 CodeFixer）
          4. 记忆写入（任务结束后）
        """
        t0 = time.time()

        # 每个任务独立的循环检测器
        self._cycle_detector = CycleDetector()
        self._last_valid_result = None

        # 初始化
        context = self._init_context(
            task_id=task_id or f"task_{int(t0)}",
            buggy_code=buggy_code,
            bug_description=bug_description,
            error_message=error_message,
            language=language,
            test_cases=test_cases or [],
        )

        try:
            if self.use_codefixer_mode or self.codefixer_agent is not None:
                self._execute_codefixer_mode(context)
            else:
                self._execute_task(context)
        except Exception as e:
            context.status = "failed"
            logger.error(f"Task {context.task_id} failed: {e}")

        elapsed_ms = (time.time() - t0) * 1000

        # 提取结果
        final_code = ""
        quality_score = 0.0
        fix_status = ""
        large_model_took_over = False

        if context.reviewer_output:
            quality_score = context.reviewer_output.get("quality_score", 0.0)
        if context.solver_output:
            final_code = context.solver_output.get("fixed_code", "")

        # CodeFixer 结果
        codefixer_result = getattr(context, "_codefixer_result", None)
        if codefixer_result:
            final_code = codefixer_result.final_code
            quality_score = codefixer_result.quality_score
            fix_status = codefixer_result.status.value
            large_model_took_over = codefixer_result.large_model_took_over

        return OrchestratorResult(
            success=context.status == "done",
            task_id=context.task_id,
            phase=context.status,
            final_code=final_code,
            quality_score=quality_score,
            total_rounds=context.current_round,
            total_time_ms=elapsed_ms,
            trajectory=context.trajectory,
            solver_calls=self._stats["solver_calls"],
            reviewer_calls=self._stats["reviewer_calls"],
            memory_retrievals=self._stats["memory_retrievals"],
            memory_writes=self._stats["memory_writes"],
            codefixer_calls=self._stats["codefixer_calls"],
            small_model_calls=self._stats["small_model_calls"],
            large_model_calls=self._stats["large_model_calls"],
            sandbox_runs=self._stats["sandbox_runs"],
            large_model_took_over=large_model_took_over,
            error=getattr(context, "_error", ""),
            fix_status=fix_status,
        )

    # ==================== CodeFixer 模式执行 ====================

    def _execute_codefixer_mode(self, context: SharedContext):
        """执行 CodeFixer 原子操作模式"""
        if self.codefixer_agent is None:
            logger.error("[Orchestrator] No CodeFixer agent configured")
            context.status = "failed"
            return

        # Phase 1: 记忆检索
        self._phase_memory_retrieve(context)

        # Phase 2: CodeFixer 执行
        logger.info(f"[Orchestrator] Executing CodeFixer for task {context.task_id}")

        try:
            result = self.codefixer_agent.run(context)
            self._stats["codefixer_calls"] += 1

            # 提取 CodeFixer 统计
            if isinstance(result, dict):
                self._stats["small_model_calls"] += result.get("small_model_calls", 0)
                self._stats["large_model_calls"] += result.get("large_model_calls", 0)
                self._stats["sandbox_runs"] += result.get("sandbox_runs", 0)

            # 保存 FixResult
            fix_result = result.get("fix_result")
            context._codefixer_result = fix_result

            if result.get("status") == "ok":
                logger.info(
                    f"[Orchestrator] CodeFixer done: "
                    f"success={result.get('success')}, "
                    f"status={result.get('status')}, "
                    f"rounds={result.get('total_rounds')}, "
                    f"large_took_over={result.get('large_model_took_over')}, "
                    f"code_len={len(result.get('final_code', ''))}"
                )

                # 根据结果决定状态
                if result.get("success"):
                    context.status = "done"
                    context.solver_output = {
                        "fixed_code": result.get("final_code", ""),
                        "quality_score": result.get("quality_score", 0.0),
                    }
                    self._phase_memory_write(context)
                else:
                    context.status = "failed"
                    context._error = result.get("error", "")
                    # 失败也写入记忆
                    self._phase_memory_write(context)
            else:
                logger.warning(f"[Orchestrator] CodeFixer returned: {result.get('error')}")
                context.status = "failed"
                context._error = result.get("error", "")

        except Exception as e:
            logger.error(f"[Orchestrator] CodeFixer exception: {e}")
            context.status = "failed"
            context._error = str(e)

    # ==================== 任务执行流程 ====================

    def _execute_task(self, context: SharedContext):
        """执行任务主流程（带循环检测）"""
        # Phase 1: 记忆检索（每轮只做一次）
        self._phase_memory_retrieve(context)

        # 循环重试
        while context.current_round < self.max_rounds:
            context.current_round += 1
            context.status = f"round_{context.current_round}"

            # Phase 2: Solver 生成
            self._phase_solve(context)

            # Phase 3: Reviewer 审查
            self._phase_review(context)

            # 记录本轮有效结果（用于 stall 时回退）
            if context.solver_output or context.reviewer_output:
                self._last_valid_result = {
                    "code": context.solver_output.get("fixed_code", "") if context.solver_output else "",
                    "quality": context.reviewer_output.get("quality_score", 0.0) if context.reviewer_output else 0.0,
                    "round": context.current_round,
                }

            # 判断是否继续
            reviewer_out = context.reviewer_output or {}
            outcome = reviewer_out.get("outcome", "failed")

            if outcome == "success":
                context.status = "done"
                self._phase_memory_write(context)
                return

            # ---- 循环检测（每轮失败后执行）----
            cycle_result = self._cycle_detector.detect(context, context.current_round)
            if cycle_result.is_stuck:
                self._handle_stuck(context, cycle_result)
                return

            # 失败：记录到 Memory Agent
            if self.memory_agent:
                failure_reason = (
                    reviewer_out.get("compile_error") or
                    reviewer_out.get("test_error") or
                    f"Review outcome: {outcome}"
                )
                try:
                    self.memory_agent.record_failure(context, failure_reason)
                except Exception:
                    pass

            # 达到最大轮数
            if context.current_round >= self.max_rounds:
                context.status = "failed"
                self._phase_memory_write(context)
                return

        context.status = "failed"

    def _handle_stuck(self, context: SharedContext, cycle_result: CycleDetectionResult):
        """
        处理循环 / stall 情况：
        1. 询问人工介入
        2. 人工有修复 → 用人工代码验证
        3. 无人工介入 → 回退到上一轮有效结果
        4. 无有效结果 → 失败
        5. 将 stall 写入记忆（X-pool）
        """
        context.trajectory.append({
            "agent": "orchestrator",
            "action": "cycle_detected",
            "reason": cycle_result.reason,
            "stall_rounds": cycle_result.stall_rounds,
            "suggestions": cycle_result.suggestions,
            "round": context.current_round,
            "timestamp": time.time(),
        })

        # ---- 1. 尝试人工介入 ----
        user_code, human_ok = self._human_intervention_fn(context, cycle_result)
        if human_ok and user_code:
            logger.info(f"[Orchestrator] Human intervened with {len(user_code)} chars of code")
            context.trajectory.append({
                "agent": "human",
                "action": "manual_fix",
                "code_len": len(user_code),
                "round": context.current_round,
                "timestamp": time.time(),
            })
            # 用人工代码替换 solver_output，触发一次审查验证
            context.solver_output = {"fixed_code": user_code}
            self._phase_review(context)

            reviewer_out = context.reviewer_output or {}
            if reviewer_out.get("outcome") == "success":
                context.status = "done"
                logger.info("[Orchestrator] Human fix passed review!")
            else:
                context.status = "done"  # 人工确认了就结束，不再反复
                logger.warning("[Orchestrator] Human fix did not pass review, but accepted as-is")

            self._phase_memory_write(context)
            return

        logger.info("[Orchestrator] No human intervention, falling back")

        # ---- 2. 无人工介入 → 回退策略 ----
        if self._last_valid_result:
            logger.warning(
                f"[Orchestrator] Stall: {cycle_result.reason}. "
                f"Rolling back to round {self._last_valid_result['round']} "
                f"(quality={self._last_valid_result['quality']:.2f})"
            )
            context.solver_output = {"fixed_code": self._last_valid_result["code"]}
            context.status = "done"
        else:
            logger.error(f"[Orchestrator] Stall with no fallback: {cycle_result.reason}")
            context.status = "failed"

        # 3. 写入 X-pool
        context._cycle_stall_reason = cycle_result.reason
        context._cycle_suggestions = cycle_result.suggestions

        if self.memory_agent:
            try:
                self.memory_agent.record_failure(
                    context,
                    f"[STALL] {cycle_result.reason}"
                )
            except Exception:
                pass

        self._phase_memory_write(context)

    # ==================== Phase 1: 记忆检索 ====================

    def _phase_memory_retrieve(self, context: SharedContext):
        """Memory Agent 检索相关记忆"""
        if self.memory_agent is None:
            return

        logger.info(f"[Orchestrator] Round 0: Memory retrieve for task {context.task_id}")

        try:
            result = self.memory_agent.run(context)
            if result.get("status") == "ok":
                self._stats["memory_retrievals"] += 1
                logger.info(
                    f"[Orchestrator] Memory retrieved in {result.get('retrieval_time_ms', 0):.0f}ms"
                )
        except Exception as e:
            logger.warning(f"[Orchestrator] Memory retrieve failed: {e}")

    # ==================== Phase 2: Solver 生成 ====================

    def _phase_solve(self, context: SharedContext):
        """Solver Agent 生成修复代码"""
        if self.solver_agent is None:
            logger.error("[Orchestrator] No SolverAgent configured")
            context.status = "failed"
            return

        logger.info(
            f"[Orchestrator] Round {context.current_round}: "
            f"Solver generating (reviewer outcome: "
            f"{(context.reviewer_output or {}).get('outcome', 'init')})"
        )

        try:
            result = self.solver_agent.run(context)
            self._stats["solver_calls"] += 1

            if result.get("status") == "ok":
                logger.info(
                    f"[Orchestrator] Solver done: "
                    f"model={result.get('model_used', '?')}, "
                    f"code_len={len(result.get('fixed_code', ''))}"
                )
            else:
                logger.warning(f"[Orchestrator] Solver returned: {result.get('error')}")
        except Exception as e:
            logger.error(f"[Orchestrator] Solver exception: {e}")

    # ==================== Phase 3: Reviewer 审查 ====================

    def _phase_review(self, context: SharedContext):
        """Reviewer Agent 审查修复代码"""
        if self.reviewer_agent is None:
            logger.error("[Orchestrator] No ReviewerAgent configured")
            return

        logger.info(
            f"[Orchestrator] Round {context.current_round}: "
            f"Reviewer reviewing"
        )

        try:
            result = self.reviewer_agent.run(context)
            self._stats["reviewer_calls"] += 1

            if result.get("status") == "ok":
                review_result = result.get("review_result")
                logger.info(
                    f"[Orchestrator] Review done: "
                    f"outcome={review_result.outcome}, "
                    f"quality={review_result.quality_score:.2f}, "
                    f"ruff={review_result.ruff_passed}, "
                    f"nudge={review_result.nudge_triggered}, "
                    f"time={review_result.review_time_ms:.0f}ms"
                )
            else:
                logger.warning(f"[Orchestrator] Reviewer returned: {result.get('error')}")
        except Exception as e:
            logger.error(f"[Orchestrator] Reviewer exception: {e}")

    # ==================== Phase 4: 记忆写入 ====================

    def _phase_memory_write(self, context: SharedContext):
        """任务结束后，Memory Agent 写入记忆并同步到共享存储层"""
        if not self.enable_memory_write or self.memory_agent is None:
            return

        if context.reviewer_output is None:
            return

        quality_score = context.reviewer_output.get("quality_score", 0.0)

        try:
            self.memory_agent.write_to_pool(context, quality_score)
            self._stats["memory_writes"] += 1

            # 尝试提取 SOP
            skill = self.memory_agent.extract_skill(context)
            if skill:
                self.memory_agent.l3_skills.append(skill)
                logger.info(f"[Orchestrator] Skill extracted: {skill.get('bug_type')}")

            # ---- 去中心化记忆同步：发布到共享存储层 ----
            if hasattr(self.memory_agent, "dual_pool"):
                piece = self.memory_agent.dual_pool.e_pool  # 本地写入后取最新碎片
                # 发布到共享存储供其他 Agent 拉取
                pool_type = "E" if context.status == "done" else "X"
                if hasattr(piece, "to_dict"):
                    shared_piece = piece.to_dict()
                else:
                    shared_piece = dict(piece)
                self._shared_memory.publish(shared_piece, pool=pool_type)
                logger.debug(
                    f"[Orchestrator] Published memory piece to shared storage (pool={pool_type})"
                )

            logger.info(
                f"[Orchestrator] Memory written: quality={quality_score:.2f}, "
                f"outcome={context.reviewer_output.get('outcome')}"
            )
        except Exception as e:
            logger.warning(f"[Orchestrator] Memory write failed: {e}")

    # ==================== 初始化 ====================

    def _init_context(
        self,
        task_id: str,
        buggy_code: str,
        bug_description: str,
        error_message: str,
        language: str,
        test_cases: List[Dict],
    ) -> SharedContext:
        """初始化 SharedContext"""
        context = SharedContext(
            task_id=task_id,
            buggy_code=buggy_code,
            bug_description=bug_description,
            error_message=error_message,
            language=language,
            test_cases=test_cases,
        )
        context.status = "init"
        logger.info(f"[Orchestrator] Task {task_id} initialized, code len={len(buggy_code)}")
        return context

    # ==================== 人工介入 ====================

    def _default_cli_intervention(self, context: SharedContext,
                                   cycle_result: CycleDetectionResult) -> tuple[str, bool]:
        """
        默认 CLI 人工介入：打印当前状态，等待用户粘贴修复代码。

        Returns:
            (fixed_code, success)
            - fixed_code: 用户粘贴的修复代码（空字符串=跳过人工介入）
            - success: True=用户认为修好了，False=放弃
        """
        print("\n" + "=" * 60)
        print("  [HITL] Agent Stuck — 需要人工介入")
        print("=" * 60)
        print(f"  Task: {context.task_id}")
        print(f"  原因: {cycle_result.reason}")
        print(f"  当前轮次: Round {context.current_round}")
        print(f"  建议: {cycle_result.suggestions}")
        print("-" * 60)

        if context.buggy_code:
            print(f"  [原始代码片段]\n{context.buggy_code[:300]}")
        print("-" * 60)

        if context.solver_output:
            latest_code = context.solver_output.get("fixed_code", "")
            print(f"  [最新尝试代码片段]\n{latest_code[:300]}")
        print("-" * 60)

        if cycle_result.suggestions:
            print(f"  [Agent 建议]")
            for i, s in enumerate(cycle_result.suggestions, 1):
                print(f"    {i}. {s}")
        print("=" * 60)
        print("  输入你的修复代码（直接粘贴，按 Ctrl+Z 回车结束输入）")
        print("  或直接回车跳过，由 Agent 回退到上一轮有效结果")
        print("  或输入 'skip' 放弃人工介入")
        print("=" * 60)

        import sys
        try:
            lines = []
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                lines.append(line)
                if line.startswith('\x1a'):  # Ctrl+Z
                    lines = lines[:-1]
                    break
        except KeyboardInterrupt:
            lines = []

        user_input = "".join(lines).strip()

        if user_input.lower() in ("skip", "s", "q", "exit", ""):
            return "", False

        return user_input, True

    # ==================== 统计 ====================

    def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)
