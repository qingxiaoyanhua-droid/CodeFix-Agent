#!/usr/bin/env python3
"""
ToolSelector: 工具选择层
====================
职责：根据任务状态和执行结果，动态选择下一个工具

核心设计：
  1. 工具注册表（Tool Registry）
     - 注册所有可用工具及其元信息
     - 工具能力描述（用于动态选择）
     - 工具依赖关系

  2. 动态选择策略（Dynamic Selection）
     - 根据 PlanStep 的 tool 字段直接路由
     - 根据执行结果动态切换（失败时 fallback）
     - 根据中间变量状态选择

  3. 条件路由（Conditional Routing）
     - compile_fail → 选择大模型分析工具
     - syntax_error → 选择语法修复工具
     - rag_miss → 选择降级策略（不用 RAG）
     - loop_detected → 选择回退策略
"""

from __future__ import annotations

import json
import time
import hashlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Callable, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ==================== 工具定义 ====================

class ToolCategory(Enum):
    """工具类别"""
    REASONING   = "reasoning"    # 推理工具
    RETRIEVAL   = "retrieval"    # 检索工具
    EXECUTION   = "execution"    # 执行工具
    MEMORY      = "memory"       # 记忆工具
    ANALYSIS    = "analysis"     # 分析工具


@dataclass
class Tool:
    """工具元信息"""
    name: str
    category: ToolCategory
    description: str

    # 能力描述
    input_schema: Dict[str, Any]   # 接受的输入参数
    output_schema: Dict[str, Any]   # 输出的格式

    # 适用条件
    applicable_intents: List[str] = field(default_factory=list)  # 适用的意图类型
    applicable_complexities: List[int] = field(default_factory=list)  # 适用的复杂度等级

    # 性能信息
    estimated_latency_ms: float = 0.0  # 预估延迟
    estimated_cost: float = 0.0  # 预估成本（大模型token消耗）

    # 依赖
    requires_tools: List[str] = field(default_factory=list)  # 前置工具
    produces_vars: List[str] = field(default_factory=list)  # 产生的变量

    # 实现
    func: Optional[Callable] = None  # 实际执行函数（由 Agent 注入）


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_name: str
    success: bool
    result: Any = None
    error: Optional[str] = None

    # 质量信息
    confidence: float = 1.0   # 结果置信度
    should_retry: bool = False  # 是否应该重试
    fallback_tool: Optional[str] = None  # 推荐的 fallback 工具

    # 性能信息
    latency_ms: float = 0.0
    tokens_used: int = 0

    def to_dict(self) -> Dict:
        return {
            "tool": self.tool_name,
            "success": self.success,
            "result": str(self.result)[:200] if self.result else None,
            "error": self.error,
            "confidence": self.confidence,
            "should_retry": self.should_retry,
            "fallback_tool": self.fallback_tool,
            "latency_ms": self.latency_ms,
        }


# ==================== 工具注册表 ====================

class ToolRegistry:
    """
    工具注册表（工具选择层的数据核心）
    """

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._tool_aliases: Dict[str, str] = {}  # 别名 → 正式名

        # 注册所有内置工具
        self._register_builtin_tools()

    def _register_builtin_tools(self):
        """注册内置工具"""

        tools = [
            # ===== 推理工具 =====
            Tool(
                name="cot_generate",
                category=ToolCategory.REASONING,
                description="CoT 四步推理生成（定位→根因→策略→验证）",
                input_schema={
                    "code": "str: buggy code",
                    "intent": "str: intent type",
                    "bug_type": "str: specific bug type",
                    "context": "dict: optional L2/L3 context",
                },
                output_schema={"cot_reasoning": "str", "fixed_code": "str"},
                applicable_intents=["bug_fix", "crash_fix", "semantic_fix"],
                applicable_complexities=[1, 2, 3, 4, 5],
                estimated_latency_ms=3000,
                estimated_cost=500,
                produces_vars=["cot_reasoning", "fixed_code", "step_outputs"],
            ),
            Tool(
                name="large_model_analyze",
                category=ToolCategory.REASONING,
                description="大模型分析 CoT 错误步骤（只给 hint，不亲自修）",
                input_schema={
                    "error": "str: compile/test error",
                    "cot_reasoning": "str: previous reasoning",
                    "buggy_code": "str: original buggy code",
                },
                output_schema={"hint": "str", "error_step": "int"},
                applicable_intents=["bug_fix", "crash_fix", "semantic_fix"],
                applicable_complexities=[2, 3, 4, 5],
                estimated_latency_ms=5000,
                estimated_cost=2000,
                requires_tools=["cot_generate"],
                produces_vars=["hint", "error_step"],
            ),

            # ===== 检索工具 =====
            Tool(
                name="rag_retrieve",
                category=ToolCategory.RETRIEVAL,
                description="从 596 条 bug 修复案例库检索相似案例",
                input_schema={
                    "query": "str: bug description + keywords",
                    "top_k": "int: number of results (default 3)",
                    "mode": "str: bm25 | dense | hybrid",
                },
                output_schema={"cases": "List[dict]", "scores": "List[float]"},
                applicable_intents=["bug_fix", "crash_fix", "semantic_fix"],
                applicable_complexities=[2, 3, 4, 5],
                estimated_latency_ms=50,
                estimated_cost=0,
                produces_vars=["retrieved_cases", "rag_scores"],
            ),
            Tool(
                name="memory_l2_retrieve",
                category=ToolCategory.MEMORY,
                description="从失败教训库检索相关 L2 记忆",
                input_schema={
                    "keywords": "List[str]",
                    "bug_type": "str",
                    "top_k": "int",
                },
                output_schema={"reflections": "List[dict]"},
                applicable_intents=["bug_fix", "crash_fix"],
                applicable_complexities=[3, 4, 5],
                estimated_latency_ms=30,
                estimated_cost=0,
                produces_vars=["l2_reflections"],
            ),
            Tool(
                name="memory_l3_retrieve",
                category=ToolCategory.MEMORY,
                description="从技能库检索相关 L3 记忆",
                input_schema={
                    "keywords": "List[str]",
                    "complexity": "int",
                    "top_k": "int",
                },
                output_schema={"skills": "List[dict]"},
                applicable_intents=["bug_fix", "crash_fix"],
                applicable_complexities=[3, 4, 5],
                estimated_latency_ms=30,
                estimated_cost=0,
                produces_vars=["l3_skills"],
            ),

            # ===== 执行工具 =====
            Tool(
                name="file_search",
                category=ToolCategory.EXECUTION,
                description="在仓库中搜索文件（支持 glob 模式和正则）",
                input_schema={
                    "repo_path": "str: 仓库根目录",
                    "pattern": "str: 搜索模式（函数名/文件名/关键词）",
                    "file_types": "List[str]: 限定文件类型（如 .py）",
                },
                output_schema={"matches": "List[dict{file, line, context}]"},
                applicable_intents=["bug_fix", "crash_fix"],
                applicable_complexities=[3, 4, 5],
                estimated_latency_ms=50,
                estimated_cost=0,
                produces_vars=["candidate_files", "rg_matches"],
            ),
            Tool(
                name="ast_locate",
                category=ToolCategory.EXECUTION,
                description="使用 Tree-sitter 精确定位函数（字节偏移）",
                input_schema={
                    "code": "str: source code",
                    "functions": "List[str]: function names",
                    "language": "str: python | java | javascript",
                },
                output_schema={"functions": "List[FunctionInfo]"},
                applicable_intents=["bug_fix", "crash_fix", "syntax_fix", "semantic_fix"],
                applicable_complexities=[1, 2, 3, 4, 5],
                estimated_latency_ms=20,
                estimated_cost=0,
                produces_vars=["function_infos", "byte_offsets"],
            ),
            Tool(
                name="harness_execute",
                category=ToolCategory.EXECUTION,
                description="三阶段验证：compile() → exec() → 测试用例",
                input_schema={
                    "code": "str: code to test",
                    "function_name": "str: expected function name",
                    "test_cases": "List[dict]: test cases",
                },
                output_schema={
                    "status": "PASS | FAIL",
                    "error": "str: error message if failed",
                    "failed_case": "dict: which test case failed",
                },
                applicable_intents=["bug_fix", "crash_fix", "syntax_fix", "semantic_fix"],
                applicable_complexities=[1, 2, 3, 4, 5],
                estimated_latency_ms=100,
                estimated_cost=0,
                produces_vars=["harness_result", "error_message"],
            ),
            Tool(
                name="syntax_fix",
                category=ToolCategory.EXECUTION,
                description="直接修复语法错误（不走 CoT，轻量）",
                input_schema={"code": "str: buggy code"},
                output_schema={"fixed_code": "str"},
                applicable_intents=["syntax_fix"],
                applicable_complexities=[1, 2],
                estimated_latency_ms=50,
                estimated_cost=50,
                produces_vars=["fixed_code"],
            ),

            # ===== 分析工具 =====
            Tool(
                name="error_analysis",
                category=ToolCategory.ANALYSIS,
                description="从错误栈提取关键信息（文件名、行号、异常类型）",
                input_schema={"error_traceback": "str: full traceback"},
                output_schema={
                    "exception_type": "str",
                    "file": "str",
                    "line": "int",
                    "message": "str",
                },
                applicable_intents=["crash_fix"],
                applicable_complexities=[2, 3, 4, 5],
                estimated_latency_ms=10,
                estimated_cost=0,
                produces_vars=["error_info"],
            ),
            Tool(
                name="rag_fallback",
                category=ToolCategory.RETRIEVAL,
                description="RAG 检索失败时的降级策略（用关键词匹配代替向量检索）",
                input_schema={
                    "query": "str",
                    "fallback_mode": "str: keyword | random | none",
                },
                output_schema={"cases": "List[dict]"},
                applicable_intents=["bug_fix", "crash_fix"],
                applicable_complexities=[1, 2, 3, 4, 5],
                estimated_latency_ms=10,
                estimated_cost=0,
                produces_vars=["fallback_cases"],
            ),
        ]

        for t in tools:
            self.register(t)

    def register(self, tool: Tool):
        """注册一个工具"""
        self._tools[tool.name] = tool
        logger.debug(f"[ToolRegistry] Registered tool: {tool.name}")

    def register_function(self, name: str, func: Callable,
                          category: ToolCategory = ToolCategory.REASONING):
        """快速注册一个函数为工具"""
        tool = Tool(
            name=name,
            category=category,
            description=func.__doc__ or f"Tool: {name}",
            input_schema={},
            output_schema={},
            func=func,
        )
        self.register(tool)

    def get(self, name: str) -> Optional[Tool]:
        """获取工具定义"""
        # 支持别名
        canonical_name = self._tool_aliases.get(name, name)
        return self._tools.get(canonical_name)

    def list_by_category(self, category: ToolCategory) -> List[Tool]:
        """按类别列出工具"""
        return [t for t in self._tools.values() if t.category == category]

    def list_by_intent(self, intent: str) -> List[Tool]:
        """按意图列出适用工具"""
        return [t for t in self._tools.values()
                if intent in t.applicable_intents or not t.applicable_intents]


# ==================== 条件路由 ====================

class ConditionalRouter:
    """
    条件路由：根据执行结果动态选择工具

    核心决策规则：
      compile_fail → large_model_analyze（概率性触发）
      syntax_error → syntax_fix
      rag_miss → rag_fallback
      loop_detected → 终止或降级
      l2_hit → 增强 prompt
      l3_hit → 注入 SOP
    """

    # 工具别名（便于记忆）
    TOOL_ALIASES = {
        "analyze": "large_model_analyze",
        "fix": "cot_generate",
        "retrieve": "rag_retrieve",
        "compile": "harness_execute",
        "test": "harness_execute",
        "search": "rag_retrieve",
        "remember": "memory_l2_retrieve",
        "skill": "memory_l3_retrieve",
        "locate": "ast_locate",
    }

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def resolve(self, tool_name: str) -> Optional[Tool]:
        """解析工具名（支持别名）"""
        canonical = self.TOOL_ALIASES.get(tool_name, tool_name)
        return self.registry.get(canonical)

    def select_next(self,
                   current_step: Any,
                   harness_result: Optional[ToolResult] = None,
                   loop_detected: bool = False,
                   retry_count: int = 0) -> Tuple[Optional[str], str]:
        """
        根据当前状态选择下一个工具

        Args:
            current_step: 当前的 PlanStep
            harness_result: Harness 执行结果（如果有）
            loop_detected: 是否检测到循环
            retry_count: 当前重试次数

        Returns:
            (tool_name, reason): 选中的工具名和选择原因
        """
        # 1. 循环检测：强制终止
        if loop_detected:
            logger.warning("[Router] Loop detected, selecting termination")
            return None, "loop_detected"

        # 2. 编译失败：条件性调用大模型
        if harness_result and not harness_result.success:
            error_msg = harness_result.error or ""

            if "syntax" in error_msg.lower():
                return "syntax_fix", "syntax_error_detected"

            # 编译失败 → 大模型分析（概率随重试次数增加）
            escalate_prob = min(0.3 + retry_count * 0.2, 0.9)
            import random
            if random.random() < escalate_prob:
                return "large_model_analyze", f"compile_fail (prob={escalate_prob:.1f})"

            return "cot_generate", "compile_fail_retry"

        # 3. 按计划执行
        if current_step and current_step.tool:
            return current_step.tool, "planned_step"

        # 4. 默认
        return "cot_generate", "default"

    def select_fallback(self, failed_tool: str,
                       error: str,
                       state: Any) -> Tuple[Optional[str], str]:
        """
        当工具执行失败时，选择 fallback 工具

        Args:
            failed_tool: 失败的工具名
            error: 错误信息
            state: 当前状态管理器

        Returns:
            (fallback_tool, reason)
        """
        # RAG 失败 → fallback
        if failed_tool == "rag_retrieve":
            logger.warning("[Router] RAG failed, using fallback")
            return "rag_fallback", "rag_unavailable"

        # 大模型失败 → 降级到小模型重试
        if failed_tool == "large_model_analyze":
            logger.warning("[Router] Large model failed, falling back to cot_generate")
            return "cot_generate", "llm_unavailable"

        # CoT 生成失败 → 简化 prompt 重试
        if failed_tool == "cot_generate":
            logger.warning("[Router] CoT generation failed, retrying")
            return "cot_generate", "retry_with_simpler_prompt"

        # Harness 失败 → 可能是代码完全跑不了，尝试语法修复
        if failed_tool == "harness_execute":
            return "syntax_fix", "harness_cannot_execute"

        # 默认：重试原工具
        return failed_tool, "default_retry"


# ==================== ToolSelector 主类 ====================

class ToolSelector:
    """
    工具选择器（工具选择层核心）

    职责：
      1. 维护工具注册表
      2. 根据规划执行工具序列
      3. 处理条件路由（执行结果 → 工具切换）
      4. 管理工具执行（调用、结果封装）
      5. 性能追踪

    使用方式：
        selector = ToolSelector()

        # 注册实际执行函数
        selector.register_tool("cot_generate", my_cot_function)
        selector.register_tool("harness_execute", my_harness_function)

        # 执行规划
        tool_name, reason = selector.select_next(plan_step, harness_result, ...)
        result = selector.execute(tool_name, inputs, state)
    """

    def __init__(self):
        self.registry = ToolRegistry()
        self.router = ConditionalRouter(self.registry)
        self._execution_count: Dict[str, int] = {}

    # ==================== 注册 ====================

    def register_tool(self, name: str, func: Callable,
                     category: ToolCategory = ToolCategory.REASONING):
        """注册工具实现函数"""
        self.registry.register_function(name, func, category)

    # ==================== 选择 ====================

    def select_next(self,
                   plan_step: Any = None,
                   harness_result: Optional[ToolResult] = None,
                   loop_detected: bool = False,
                   retry_count: int = 0) -> Tuple[Optional[str], str]:
        """
        选择下一个要执行的工具

        Returns:
            (tool_name, reason)
        """
        tool_name, reason = self.router.select_next(
            current_step=plan_step,
            harness_result=harness_result,
            loop_detected=loop_detected,
            retry_count=retry_count,
        )
        logger.info(f"[ToolSelector] Selected: {tool_name} (reason: {reason})")
        return tool_name, reason

    def select_fallback(self, failed_tool: str,
                       error: str,
                       state: Any) -> Tuple[Optional[str], str]:
        """选择 fallback 工具"""
        return self.router.select_fallback(failed_tool, error, state)

    # ==================== 执行 ====================

    def execute(self, tool_name: str,
               inputs: Dict[str, Any],
               state: Optional[Any] = None) -> ToolResult:
        """
        执行工具并封装结果

        Args:
            tool_name: 工具名
            inputs: 输入参数
            state: 状态管理器（用于变量读写）

        Returns:
            ToolResult
        """
        if tool_name is None:
            return ToolResult(
                tool_name="None",
                success=False,
                error="No tool selected (loop detected or task complete)",
            )

        tool_def = self.registry.get(tool_name)
        if not tool_def:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Tool '{tool_name}' not found in registry",
            )

        # 更新执行计数
        self._execution_count[tool_name] = self._execution_count.get(tool_name, 0) + 1

        # 执行
        start_time = time.time()
        try:
            if tool_def.func:
                result = tool_def.func(**inputs)
                return ToolResult(
                    tool_name=tool_name,
                    success=True,
                    result=result,
                    latency_ms=(time.time() - start_time) * 1000,
                    confidence=1.0,
                )
            else:
                # 无实现函数（工具定义存在但未注入）
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=f"Tool '{tool_name}' registered but no implementation function provided",
                    should_retry=False,
                )
        except Exception as e:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=str(e),
                latency_ms=(time.time() - start_time) * 1000,
                should_retry=True,
                fallback_tool=self.router.select_fallback(tool_name, str(e), state)[0],
            )

    # ==================== 工具信息 ====================

    def list_tools(self, category: Optional[ToolCategory] = None) -> List[str]:
        """列出所有工具"""
        if category:
            return [t.name for t in self.registry.list_by_category(category)]
        return list(self.registry._tools.keys())

    def get_stats(self) -> Dict[str, Any]:
        """获取工具执行统计"""
        return {
            "total_tools": len(self.registry._tools),
            "execution_count": self._execution_count,
            "by_category": {
                cat.value: len(self.registry.list_by_category(cat))
                for cat in ToolCategory
            },
        }
