"""
Multi-Agent System: 去中心化协作 Agent 架构
============================================
所有 Agent 的统一入口。

文件结构：
  base_agent.py      — Agent 基类、消息协议、SharedContext
  memory_agent.py    — Memory Agent: 三层记忆 + RAG + 双池调度
  reviewer_agent.py  — Reviewer Agent: 编译验证 + 7维度审查 + 软验证Nudge
  solver_agent.py    — Solver Agent: CoT推理 + AST精确替换 + Symbol Pre-Search
  codefixer.py       — CodeFixer: 小大模型协同修复（原子操作）

multi_agent_orchestrator.py — 调度中枢: 协调多个 Agent 的执行顺序
"""

from agents.base_agent import (
    BaseAgent,
    AgentProfile,
    AgentCapability,
    AgentMessage,
    MessageType,
    SharedContext,
)
from agents.memory_agent import MemoryAgent, MemoryQuery, MemoryResult
from agents.reviewer_agent import (
    ReviewerAgent,
    ReviewResult,
    SevenDimensionBugDetector,
    BugReport,
)
from agents.solver_agent import SolverAgent, CoTParser
from agents.codefixer import (
    CodeFixer,
    FixResult,
    FixStatus,
    create_codefixer_agent,
)
from multi_agent_orchestrator import MultiAgentOrchestrator, OrchestratorResult, TaskPhase

__all__ = [
    # Base
    "BaseAgent",
    "AgentProfile",
    "AgentCapability",
    "AgentMessage",
    "MessageType",
    "SharedContext",
    # Agents
    "MemoryAgent",
    "MemoryQuery",
    "MemoryResult",
    "ReviewerAgent",
    "ReviewResult",
    "SevenDimensionBugDetector",
    "BugReport",
    "SolverAgent",
    "CoTParser",
    "CodeFixer",
    "FixResult",
    "FixStatus",
    "create_codefixer_agent",
    # Orchestrator
    "MultiAgentOrchestrator",
    "OrchestratorResult",
    "TaskPhase",
]
