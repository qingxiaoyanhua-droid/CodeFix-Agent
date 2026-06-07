#!/usr/bin/env python3
"""
StateManager: 状态管理层
======================
职责：多轮执行上下文维护、历史轨迹记录、中间变量管理

核心设计：
  1. 执行轨迹（Execution Trace）
     - 每个 step 的 input / output / status 完整记录
     - 支持回溯和重放
     - 可序列化为 JSON 用于持久化

  2. 上下文窗口管理
     - 自动压缩历史（保留关键决策点）
     - 动态注入相关历史（减少注意力涣散）
     - 最大 token 预算控制

  3. 中间变量存储
     - 每个 step 的输出作为后续 step 的输入
     - 变量命名空间隔离
     - 支持变量引用和条件分支

  4. 多轮循环检测
     - 检测重复执行模式
     - 防止死循环
     - 最大重试次数限制
"""

from __future__ import annotations

import json
import copy
import uuid
import time
import hashlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from enum import Enum
from collections import deque
import logging

logger = logging.getLogger(__name__)


# ==================== 状态枚举 ====================

class StepStatus(Enum):
    """单步执行状态"""
    PENDING   = "pending"    # 未执行
    RUNNING   = "running"    # 执行中
    SUCCESS   = "success"    # 成功
    FAILED    = "failed"     # 失败
    SKIPPED   = "skipped"    # 跳过（如条件不满足）
    RETRYING  = "retrying"   # 重试中


class TaskStatus(Enum):
    """整体任务状态"""
    PLANNING    = "planning"    # 规划中
    EXECUTING   = "executing"   # 执行中
    SUCCEEDED   = "succeeded"   # 成功完成
    FAILED      = "failed"      # 最终失败
    MAX_RETRIES = "max_retries" # 达到最大重试次数


# ==================== 核心数据结构 ====================

@dataclass
class StepOutput:
    """单步执行输出"""
    tool: str
    status: StepStatus
    result: Any          # 工具返回的实际结果
    error: Optional[str] = None

    # 时间信息
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration_ms(self) -> float:
        if self.end_time and self.start_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    @property
    def is_success(self) -> bool:
        return self.status == StepStatus.SUCCESS


@dataclass
class ExecutionStep:
    """执行历史中的单个步骤"""
    step_id: int
    name: str
    tool: str

    # 输入
    input_snapshot: Dict[str, Any]  # 该 step 接收到的输入

    # 输出
    output: Optional[StepOutput] = None

    # 状态
    status: StepStatus = StepStatus.PENDING

    # 依赖关系
    depends_on: List[int] = field(default_factory=list)  # 依赖哪些 step_id

    # 重试信息
    retry_count: int = 0
    max_retries: int = 3
    last_error: Optional[str] = None

    # 中间变量（该 step 产生的变量，可供后续 step 使用）
    produced_vars: Dict[str, Any] = field(default_factory=dict)

    def mark_success(self, result: Any, duration: float = 0.0):
        self.status = StepStatus.SUCCESS
        self.output = StepOutput(
            tool=self.tool,
            status=StepStatus.SUCCESS,
            result=result,
            start_time=time.time() - duration / 1000,
            end_time=time.time(),
        )

    def mark_failed(self, error: str, retry: bool = False):
        self.status = StepStatus.FAILED if not retry else StepStatus.RETRYING
        self.last_error = error
        self.retry_count += 1
        self.output = StepOutput(
            tool=self.tool,
            status=StepStatus.FAILED,
            result=None,
            error=error,
            start_time=0,
            end_time=time.time(),
        )

    def to_dict(self) -> Dict:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "tool": self.tool,
            "status": self.status.value,
            "input_snapshot": self.input_snapshot,
            "output": {
                "status": self.output.status.value if self.output else None,
                "result": str(self.output.result)[:200] if self.output and self.output.result else None,
                "error": self.output.error if self.output else None,
                "duration_ms": self.output.duration_ms if self.output else None,
            } if self.output else None,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "produced_vars": {k: str(v)[:100] for k, v in self.produced_vars.items()},
        }


@dataclass
class Variable:
    """命名的中间变量"""
    name: str
    value: Any
    produced_by: int       # 由哪个 step_id 产生
    consumed_by: List[int] = field(default_factory=list)  # 被哪些 step_id 消费
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "value": str(self.value)[:200],
            "produced_by": self.produced_by,
            "consumed_by": self.consumed_by,
            "age_seconds": time.time() - self.created_at if self.created_at else 0,
        }


# ==================== 循环检测 ====================

@dataclass
class LoopPattern:
    """检测到的循环模式"""
    pattern_hash: str       # 循环模式的哈希
    repeat_count: int       # 重复次数
    last_seen: float        # 上次出现时间
    detected_at: float      # 检测时间
    affected_steps: List[int] = field(default_factory=list)


class LoopDetector:
    """
    检测重复执行模式，防止死循环

    策略：
      - 跟踪最近 N 次执行的 (tool, result_hash) 序列
      - 如果同一序列重复出现超过阈值，触发警告
      - 统计同一 tool 的调用次数，过多则标记
    """

    def __init__(self, history_depth: int = 10, repeat_threshold: int = 3):
        self.history_depth = history_depth
        self.repeat_threshold = repeat_threshold
        self._history: deque = deque(maxlen=history_depth)
        self._tool_call_count: Dict[str, int] = {}
        self._detected_loops: List[LoopPattern] = []

    def record(self, tool: str, result: Any, step_id: int):
        """记录一次执行"""
        result_repr = str(result)[:100] if result else "None"
        pattern = hashlib.md5(f"{tool}:{result_repr}".encode()).hexdigest()[:8]
        self._history.append({"tool": tool, "pattern": pattern, "step_id": step_id, "ts": time.time()})
        self._tool_call_count[tool] = self._tool_call_count.get(tool, 0) + 1

    def check(self, tool: str) -> Tuple[bool, Optional[str]]:
        """
        检查是否可能陷入循环

        Returns:
            (is_loop, warning_message)
        """
        # 检查单个 tool 调用次数过多
        call_count = self._tool_call_count.get(tool, 0)
        if call_count > self.repeat_threshold:
            return True, f"工具 '{tool}' 已调用 {call_count} 次，疑似循环"

        # 检查重复模式
        recent = list(self._history)
        if len(recent) < 3:
            return False, None

        # 检查最近3步是否形成重复
        for pattern_len in [2, 3]:
            if len(recent) < pattern_len * 2:
                continue
            first = [h["pattern"] for h in recent[-pattern_len*2:-pattern_len]]
            second = [h["pattern"] for h in recent[-pattern_len:]]
            if first == second:
                return True, f"检测到重复执行模式（长度={pattern_len}）"

        return False, None


# ==================== StateManager 主类 ====================

class StateManager:
    """
    状态管理器（状态维护层核心）

    职责：
      1. 管理执行轨迹（execution trace）
      2. 维护变量命名空间（intermediate variables）
      3. 控制上下文窗口（context window management）
      4. 检测循环（loop detection）
      5. 提供执行历史摘要（用于 prompt 组装）

    设计原则：
      - 所有状态变化都有日志
      - 变量有版本控制（支持回滚）
      - 历史可压缩（防止 token 爆炸）
      - 循环检测是硬约束
    """

    # 上下文压缩参数
    MAX_TRACE_LENGTH = 20       # 最多保留20步历史
    MAX_VARIABLE_COUNT = 50    # 最多50个活跃变量
    MAX_HISTORY_TOKENS = 2000  # 历史摘要最多2000 tokens
    CRITICAL_STEP_KEEP = 3     # 失败/成功步骤多保留

    def __init__(self, task_id: str):
        self.task_id = task_id

        # 执行轨迹
        self.steps: List[ExecutionStep] = []
        self._step_counter = 0

        # 变量命名空间
        self.variables: Dict[str, Variable] = {}

        # 上下文状态
        self.task_status: TaskStatus = TaskStatus.PLANNING
        self.current_plan: Optional[Any] = None  # TaskPlan
        self.current_step_index = 0

        # 循环检测
        self.loop_detector = LoopDetector(history_depth=10, repeat_threshold=4)

        # 时间戳
        self.created_at = time.time()
        self.last_updated = time.time()

        # 会话元数据
        self.metadata: Dict[str, Any] = {}

        logger.info(f"[StateManager] Task {task_id} initialized")

    # ==================== Step 管理 ====================

    def add_step(self, name: str, tool: str,
                 input_snapshot: Dict[str, Any],
                 depends_on: List[int] = None) -> ExecutionStep:
        """
        添加一个新的执行步骤
        """
        self._step_counter += 1
        step = ExecutionStep(
            step_id=self._step_counter,
            name=name,
            tool=tool,
            input_snapshot=input_snapshot,
            depends_on=depends_on or [],
            max_retries=3,
        )
        self.steps.append(step)
        logger.info(f"[StateManager] Step {self._step_counter}: {name} ({tool}) added")
        return step

    def complete_step(self, step_id: int, result: Any, duration_ms: float = 0.0):
        """标记步骤成功完成"""
        step = self._get_step(step_id)
        if step:
            step.mark_success(result, duration_ms)
            self.loop_detector.record(step.tool, result, step_id)
            self.last_updated = time.time()
            logger.info(f"[StateManager] Step {step_id} completed in {duration_ms:.0f}ms")

    def fail_step(self, step_id: int, error: str, can_retry: bool = True):
        """标记步骤失败"""
        step = self._get_step(step_id)
        if step:
            step.mark_failed(error, retry=can_retry)
            self.loop_detector.record(step.tool, error, step_id)
            self.last_updated = time.time()
            logger.warning(f"[StateManager] Step {step_id} failed: {error[:100]}")

    def _get_step(self, step_id: int) -> Optional[ExecutionStep]:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    # ==================== 变量管理 ====================

    def set_var(self, name: str, value: Any, step_id: int):
        """
        设置中间变量（供后续 step 使用）
        """
        now = time.time()
        if name in self.variables:
            # 版本更新
            self.variables[name].value = value
            self.variables[name].updated_at = now
            self.variables[name].produced_by = step_id
        else:
            # 新建
            self.variables[name] = Variable(
                name=name,
                value=value,
                produced_by=step_id,
                created_at=now,
                updated_at=now,
            )

        # 记录哪个 step 消费了这个变量
        for s in self.steps:
            if s.step_id == step_id:
                if name not in s.produced_vars:
                    s.produced_vars[name] = value

        logger.debug(f"[StateManager] Var '{name}' = {str(value)[:50]} (by step {step_id})")

    def get_var(self, name: str) -> Optional[Any]:
        """获取变量值"""
        return self.variables.get(name)

    def get_vars_for_step(self, step_id: int) -> Dict[str, Any]:
        """
        获取某个 step 可以使用的所有变量
        （该 step 之前所有 step 产生的变量）
        """
        available = {}
        for s in self.steps:
            if s.step_id < step_id and s.output and s.output.is_success:
                for var_name, var_value in s.produced_vars.items():
                    available[var_name] = var_value
        return available

    def consume_var(self, var_name: str, by_step_id: int):
        """记录某个 step 消费了某个变量"""
        if var_name in self.variables:
            var = self.variables[var_name]
            if by_step_id not in var.consumed_by:
                var.consumed_by.append(by_step_id)

    # ==================== 上下文管理 ====================

    def build_context_summary(self) -> str:
        """
        构建上下文摘要（用于注入到 prompt）

        格式：
        ## 当前执行状态
        Task: {task_id} | Status: {status}
        Step: {current_step}/{total_steps}

        ## 最近执行历史
        1. [SUCCESS] ast_locate → found 3 functions
        2. [SUCCESS] cot_generate → 4-step reasoning generated
        3. [FAILED] harness_execute → SyntaxError: invalid syntax (retrying...)

        ## 当前中间变量
        - fixed_code: "def add(a, b):\\n    return a + b"
        - error_msg: "TypeError: unsupported operand type(s) for + and 'NoneType'"

        ## 循环警告
        (无 / 检测到重复执行模式)
        """
        lines = []
        lines.append(f"## 当前执行状态")
        lines.append(f"Task: {self.task_id} | Status: {self.task_status.value}")
        lines.append(f"Step: {self._step_counter}")

        # 执行历史
        if self.steps:
            lines.append(f"\n## 最近执行历史")
            for s in self._get_recent_steps():
                status_icon = {
                    StepStatus.SUCCESS: "✅",
                    StepStatus.FAILED: "❌",
                    StepStatus.RUNNING: "🔄",
                    StepStatus.PENDING: "⏳",
                    StepStatus.RETRYING: "🔁",
                }.get(s.status, "❓")

                result_preview = ""
                if s.output and s.output.result:
                    result_str = str(s.output.result)
                    result_preview = f" → {result_str[:60]}"
                elif s.last_error:
                    result_preview = f" → ❌ {s.last_error[:60]}"

                lines.append(f"{status_icon} Step-{s.step_id}: {s.name} [{s.tool}]{result_preview}")

        # 中间变量
        if self.variables:
            lines.append(f"\n## 当前中间变量")
            for name, var in list(self.variables.items())[-5:]:  # 最多显示5个
                val_preview = str(var.value)[:80]
                lines.append(f"- {name}: {val_preview}")

        # 循环警告
        is_loop, warning = self.loop_detector.check("")
        if is_loop:
            lines.append(f"\n## ⚠️ 循环警告")
            lines.append(warning)

        return "\n".join(lines)

    def build_full_trace(self) -> List[Dict]:
        """
        构建完整执行轨迹（用于调试和分析）
        """
        return [s.to_dict() for s in self.steps]

    def _get_recent_steps(self) -> List[ExecutionStep]:
        """获取最近的步骤（带压缩）"""
        if len(self.steps) <= self.MAX_TRACE_LENGTH:
            return self.steps

        # 优先保留：失败步骤、关键成功步骤、最近步骤
        critical_ids = set()
        for s in self.steps:
            if s.status in (StepStatus.FAILED, StepStatus.RETRYING):
                critical_ids.add(s.step_id)

        # 保留最近的 CRITICAL_STEP_KEEP 个成功步骤
        recent_success = [s for s in self.steps if s.status == StepStatus.SUCCESS][-self.CRITICAL_STEP_KEEP:]
        critical_ids.update(s.step_id for s in recent_success)

        # 保留最近的一半步骤
        recent_half = self.steps[len(self.steps)//2:]
        critical_ids.update(s.step_id for s in recent_half)

        result = [s for s in self.steps if s.step_id in critical_ids]
        result.sort(key=lambda s: s.step_id)
        return result

    # ==================== 任务状态控制 ====================

    def set_plan(self, plan: Any):
        """设置任务规划"""
        self.current_plan = plan
        self.task_status = TaskStatus.EXECUTING
        logger.info(f"[StateManager] Plan set, {len(plan.steps)} steps")

    def mark_succeeded(self):
        """标记任务成功"""
        self.task_status = TaskStatus.SUCCEEDED
        self.metadata["completed_at"] = time.time()
        self.metadata["total_duration_s"] = time.time() - self.created_at
        logger.info(f"[StateManager] Task SUCCEEDED")

    def mark_failed(self, reason: str):
        """标记任务失败"""
        self.task_status = TaskStatus.FAILED
        self.metadata["failed_reason"] = reason
        self.metadata["completed_at"] = time.time()
        logger.warning(f"[StateManager] Task FAILED: {reason}")

    def should_retry(self, step_id: int) -> bool:
        """判断某步骤是否应该重试"""
        step = self._get_step(step_id)
        if not step:
            return False
        return step.retry_count < step.max_retries

    # ==================== 序列化 ====================

    def to_json(self) -> str:
        """序列化为 JSON（用于持久化）"""
        return json.dumps({
            "task_id": self.task_id,
            "task_status": self.task_status.value,
            "metadata": self.metadata,
            "steps_summary": [s.to_dict() for s in self.steps],
            "variables": {k: v.to_dict() for k, v in self.variables.items()},
            "created_at": self.created_at,
            "last_updated": self.last_updated,
        }, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "StateManager":
        """从 JSON 反序列化"""
        data = json.loads(json_str)
        sm = cls(task_id=data["task_id"])
        sm.task_status = TaskStatus[data["task_status"]]
        sm.metadata = data.get("metadata", {})
        sm.created_at = data.get("created_at", time.time())
        sm.last_updated = data.get("last_updated", time.time())
        return sm
