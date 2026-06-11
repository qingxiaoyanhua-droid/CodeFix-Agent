#!/usr/bin/env python3
"""
Multi-Agent Base Classes and Message Protocol
=============================================
所有Agent 的共同接口、消息类型和共享数据结构。
"""

from __future__ import annotations

import time
import uuid
import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Set, Callable
from enum import Enum
from abc import ABC, abstractmethod


logger = logging.getLogger(__name__)


# ==================== 消息类型 ====================

class MessageType(Enum):
    """Agent 间通信的消息类型"""
    REQUEST  = "request"   # 请求某个 Agent 执行任务
    RESPONSE = "response"  # 对请求的响应
    BROADCAST = "broadcast"  # 广播（如广播记忆更新）
    ALERT    = "alert"    # 警告（如检测到循环、危险模式）
    ACK      = "ack"      # 确认收到


@dataclass
class AgentMessage:
    """
    Agent 间传递的统一消息格式。

    设计参考MAC 论文的协作模式：每个 Agent 只负责自己的职责，
    通过消息队列进行通信，避免单点中央化调度。
    """
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    msg_type: MessageType = MessageType.REQUEST
    sender: str = ""       # 发送方Agent 名称
    recipient: str = ""     # 接收方Agent 名称，"* 表示广播
    content: Dict[str, Any] = field(default_factory=dict)

    # 元信息
    timestamp: float = field(default_factory=time.time)
    related_task_id: str = ""  # 关联的任务ID
    in_reply_to: str = ""      # 回复的消息ID（用于追踪对话）

    # 执行结果（用于RESPONSE）
    status: str = "ok"    # ok | error | partial
    result: Any = None
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type.value,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "timestamp": self.timestamp,
            "related_task_id": self.related_task_id,
            "in_reply_to": self.in_reply_to,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


# ==================== Agent 能力标签 ====================

class AgentCapability(Enum):
    """Agent 的能力标签，用于 Orchestrator 路由"""
    SOLVE       = "solve"        # 生成修复代码
    REVIEW      = "review"       # 审查修复质量
    MEMORY_READ = "memory_read"  # 读取记忆
    MEMORY_WRITE = "memory_write" # 写入记忆
    VERIFY      = "verify"       # 编译/测试验证
    PLAN        = "plan"         # 任务规划
    CODE_FIX    = "code_fix"     # 小大模型协同代码修复（原子操作）


@dataclass
class AgentProfile:
    """Agent 的能力配置"""
    name: str
    capabilities: Set[AgentCapability]
    description: str = ""
    max_concurrent: int = 1   # 最大并发处理任务数
    timeout_seconds: float = 30.0

    def can_handle(self, capability: AgentCapability) -> bool:
        return capability in self.capabilities


# ==================== 共享上下文 ====================

@dataclass
class SharedContext:
    """
    所有Agent 共享的任务上下文。

    通过 Orchestrator 管理，Solver/Reviewer/MemoryAgent 都可以读写，
    每个任务有独立的上下文实例。
    """
    task_id: str
    buggy_code: str = ""
    bug_description: str = ""
    error_message: str = ""
    language: str = "python"
    test_cases: List[Dict] = field(default_factory=list)

    # 各Agent 的输出
    solver_output: Optional[Dict] = None    # Solver 生成的代码和 CoT
    reviewer_output: Optional[Dict] = None  # Reviewer 的审查结果
    memory_output: Optional[Dict] = None    # Memory Agent 检索的记忆

    # 执行状态
    current_round: int = 0
    max_rounds: int = 3
    status: str = "init"  # init | planning | solving | reviewing | memory | done | failed

    # 轨迹（用于GRPO 训练数据收集）
    trajectory: List[Dict] = field(default_factory=list)

    # 协作追踪（所有路由的消息）
    message_history: List[AgentMessage] = field(default_factory=list)

    def record_step(self, agent: str, action: str, result: Any):
        self.trajectory.append({
            "agent": agent,
            "action": action,
            "result": str(result)[:200],
            "round": self.current_round,
            "timestamp": time.time(),
        })

    def task_hash(self) -> str:
        return hashlib.md5(
            f"{self.task_id}{self.buggy_code}".encode()
        ).hexdigest()[:16]


# ==================== A2A 路由权限 ====================

# 允许的 A2A 通信路径（supervisor 监管下的最小集合）
A2A_ALLOWED_ROUTES: Set[tuple[str, str]] = {
    # solver -> memory: memory query / result
    ("solver", "memory"),
    # reviewer -> solver: review feedback / response or ack
    ("reviewer", "solver"),
}


class A2ARouter:
    """
    Supervisor-controlled A2A message router.

    Responsibilities:
      - Permission enforcement: only allow explicitly registered routes
      - Message injection: append routed messages to SharedContext.message_history
      - Routing: forward messages to target agent's handle_message()

    This is the minimal supervised A2A layer for interview/demo value.
    It does NOT replace orchestrator-driven phase ordering; it only enables
    controlled direct communication for specific flows.
    """

    def __init__(self):
        self._handlers: Dict[str, Callable[[AgentMessage], Optional[AgentMessage]]] = {}

    def register(self, agent_name: str,
                 handler: Callable[[AgentMessage], Optional[AgentMessage]]) -> None:
        """Register an agent handler for A2A routing."""
        self._handlers[agent_name] = handler

    def _is_route_allowed(self, sender: str, recipient: str) -> bool:
        """Check if sender -> recipient route is permitted."""
        return (sender, recipient) in A2A_ALLOWED_ROUTES

    def route(self, message: AgentMessage,
              context: Optional[SharedContext] = None) -> Optional[AgentMessage]:
        """
        Route a message from sender to recipient if permitted.

        Args:
            message: The A2A message to route.
            context: SharedContext for message_history tracking.

        Returns:
            Response from target agent, or None if not allowed / not found.
        """
        sender = message.sender
        recipient = message.recipient

        if not self._is_route_allowed(sender, recipient):
            logger.warning(
                f"[A2ARouter] Blocked A2A route: {sender} -> {recipient}"
            )
            return None

        handler = self._handlers.get(recipient)
        if handler is None:
            logger.warning(f"[A2ARouter] No handler for agent: {recipient}")
            return None

        response = handler(message)

        # Track routed message in history
        if context is not None:
            context.message_history.append(message)

        logger.debug(
            f"[A2ARouter] Routed {message.msg_type.value} {message.msg_id}: "
            f"{sender} -> {recipient}"
        )
        return response

    def unregister(self, agent_name: str) -> None:
        """Unregister an agent handler."""
        self._handlers.pop(agent_name, None)


# ==================== Agent 基类 ====================

class BaseAgent(ABC):
    """
    所有Agent 的抽象基类。

    每个 Agent 有：
    - name: 名称
    - profile: 能力配置
    - handle(message): 处理消息
    - run(context): 直接运行（Orchestrator 调用）

    Agent 之间通过 A2ARouter 路由消息，不直接 call each other.
    """

    def __init__(self, name: str, profile: AgentProfile):
        self.name = name
        self.profile = profile
        self._pending_messages: List[AgentMessage] = []

    @abstractmethod
    def run(self, context: SharedContext) -> Dict[str, Any]:
        """
        执行 Agent 的核心逻辑。

        Args:
            context: 共享上下文（包含任务信息和各 Agent 输出）

        Returns:
            执行结果字典，供 Orchestrator 路由
        """
        raise NotImplementedError

    def send_message(self, to: str, content: Dict[str, Any],
                     msg_type: MessageType = MessageType.REQUEST,
                     in_reply_to: str = "",
                     related_task_id: str = "") -> AgentMessage:
        """Construct a message to another agent (does NOT send directly)."""
        return AgentMessage(
            msg_type=msg_type,
            sender=self.name,
            recipient=to,
            content=content,
            in_reply_to=in_reply_to,
            related_task_id=related_task_id,
        )

    def route_message(
        self,
        message: AgentMessage,
        router: A2ARouter,
        context: Optional[SharedContext] = None,
    ) -> Optional[AgentMessage]:
        """
        Route a message via the A2ARouter.

        This is the supervised A2A entry point: the router enforces permissions
        and tracks the message in context.message_history.
        """
        return router.route(message, context)

    def handle_message(self, message: AgentMessage) -> Optional[AgentMessage]:
        """
        Handle an incoming A2A message. Subclasses can override to implement
        custom message processing.
        """
        self._pending_messages.append(message)
        return None

    def pop_messages(self) -> List[AgentMessage]:
        """Retrieve and clear pending messages."""
        messages = self._pending_messages
        self._pending_messages = []
        return messages

    def __repr__(self) -> str:
        caps = [c.value for c in self.profile.capabilities]
        return f"<{self.__class__.__name__}(name={self.name}, caps={caps})>"
