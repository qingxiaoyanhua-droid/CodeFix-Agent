#!/usr/bin/env python3
"""
TaskPlanner: 任务规划层
===================
职责：意图识别、任务拆解、规划生成

输入: buggy_code + bug_description
输出: TaskPlan（包含意图类型、复杂度、拆解步骤、工具序列）

核心设计：
  1. 意图识别（Intent Classification）
     - BUG_FIX: 代码能跑但结果错 → CoT推理+编译验证
     - CRASH_FIX: 代码直接崩溃 → 先exec()捕获错误栈
     - SYNTAX_FIX: 语法错误 → 简单修复
     - RUNTIME_OPT: 性能问题 → Profile工具

  2. 任务拆解（Task Decomposition）
     - 识别需要修复的函数数量
     - 判断是否涉及多文件
     - 评估依赖复杂度

  3. 规划生成（Plan Generation）
     - 生成有序的执行步骤
     - 每步指定 tool + expected_output
     - 标注条件分支（if compile_fail → use_large_model）
"""

from __future__ import annotations

import re
import ast
import hashlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ==================== Intent & Plan 定义 ====================

class IntentType(Enum):
    """任务意图类型"""
    BUG_FIX       = "bug_fix"       # 代码能跑但结果错
    CRASH_FIX     = "crash_fix"     # 代码运行崩溃
    SYNTAX_FIX    = "syntax_fix"    # 语法错误
    RUNTIME_OPT   = "runtime_opt"   # 运行时优化
    SEMANTIC_FIX  = "semantic_fix"  # 语义/逻辑错误
    REPO_LEVEL    = "repo_level"    # 仓库级：需大模型协调 grep+AST 定位
    CODE_FIX      = "code_fix"      # 代码修复（使用小大模型协同模式）


class Complexity(Enum):
    """任务复杂度"""
    TRIVIAL   = 1   # 单行修改，语法级
    SIMPLE    = 2   # 单函数，逻辑清晰
    MODERATE  = 3   # 多函数，需分析依赖
    COMPLEX   = 4   # 多文件，跨模块
    VERY_COMPLEX = 5  # 架构级重构


@dataclass
class IntentResult:
    """意图识别结果"""
    intent: IntentType
    confidence: float          # 0.0 - 1.0
    reasoning: str             # 为什么这么判断
    bug_type: str              # 具体 bug 类型（如 off-by-one, null-pointer）
    keywords: List[str]        # 关键词列表（用于 L2/L3 检索）
    affected_functions: List[str]  # 涉及的函数名


@dataclass
class PlanStep:
    """单个规划步骤"""
    step_id: int
    name: str                  # 如 "AST_定位函数" "小模型推理" "编译验证"
    tool: str                  # 如 "ast_locate" "cot_generate" "compile"
    input_requirements: Dict[str, Any]   # 该步骤需要的输入
    expected_output: str       # 期望的输出格式
    condition: Optional[str] = None   # 前置条件，如 "compile_fail"
    fallback: Optional[str] = None    # 失败时的备选工具
    description: str = ""


@dataclass
class TaskPlan:
    """完整任务规划"""
    task_id: str
    intent: IntentResult
    complexity: Complexity

    # 拆解出的子任务
    steps: List[PlanStep]

    # 元信息
    needs_large_model: bool    # 是否需要大模型介入
    needs_rag: bool           # 是否需要 RAG 检索
    needs_l2_retrieval: bool  # 是否需要 L2 教训检索
    needs_l3_retrieval: bool  # 是否需要 L3 技能检索

    # 预估
    estimated_attempts: int    # 预估尝试次数
    estimated_tools: List[str]  # 预计调用的工具序列

    # 原始输入（用于溯源）
    buggy_code_hash: str
    bug_description: str


# ==================== 意图识别 ====================

class IntentClassifier:
    """
    基于规则的意图分类器
    生产环境可替换为微调的分类模型
    """

    # Bug 类型关键词映射
    BUG_KEYWORDS = {
        "off-by-one": ["off-by-one", "off by one", "boundary", "range", "len()", "index", "最后一个", "索引越界"],
        "null-pointer": ["null", "None", "NoneType", "attribute error", "can't multiply", "is none", "is not none", "undefined"],
        "infinite-loop": ["infinite", "loop", "死循环", "timeout", "hang", "不终止", "停止条件"],
        "logic-error": ["logic", "逻辑", "条件", "判断", "if", "else", "错", "wrong", "incorrect"],
        "type-error": ["type error", "类型错误", "int str", "float str", "cannot concat", "+ str"],
        "name-error": ["nameerror", "name error", "未定义", "not defined", "undefined name"],
        "syntax-error": ["syntax", "语法", "expected", "invalid syntax", "parse error"],
        "index-error": ["index error", "索引错误", "list index", "out of range", "index out"],
        "initialization": ["init", "初始", "reset", "total", "sum", "累加", "accumulate", "初始值"],
        "recursion": ["recursion", "递归", "maximum", "depth", "栈溢出", "stack overflow"],
    }

    # 崩溃相关模式（代码能跑但崩溃）
    CRASH_PATTERNS = [
        r"traceback \(most recent call last\)",
        r"error:", r"exception", r"failed",
        r"zerodivisionerror", r"overflowerror",
        r"maximum recursion depth exceeded",
    ]

    def classify(self, buggy_code: str, bug_description: str = "",
                 error_message: str = "") -> IntentResult:
        """
        综合分析输入，判断任务意图

        Args:
            buggy_code: 有 bug 的代码
            bug_description: bug 描述（用户输入或从代码注释提取）
            error_message: 编译/运行时的错误信息（如果有）

        Returns:
            IntentResult，包含意图类型、置信度、推理过程
        """
        bug_type = self._detect_bug_type(bug_description, buggy_code)
        keywords = self._extract_keywords(bug_description, buggy_code)
        affected_funcs = self._extract_functions(buggy_code)

        # 优先级1：语法错误
        if error_message and "syntax" in error_message.lower():
            return IntentResult(
                intent=IntentType.SYNTAX_FIX,
                confidence=0.95,
                reasoning=f"错误信息包含 'syntax' 关键字: {error_message[:100]}",
                bug_type=bug_type,
                keywords=keywords,
                affected_functions=affected_funcs,
            )

        # 优先级2：崩溃错误
        if any(re.search(p, error_message, re.IGNORECASE) for p in self.CRASH_PATTERNS):
            return IntentResult(
                intent=IntentType.CRASH_FIX,
                confidence=0.90,
                reasoning=f"错误信息显示运行时崩溃: {error_message[:100]}",
                bug_type=bug_type,
                keywords=keywords,
                affected_functions=affected_funcs,
            )

        # 优先级3：语法错误（静态检测）
        if self._has_syntax_error(buggy_code):
            return IntentResult(
                intent=IntentType.SYNTAX_FIX,
                confidence=0.85,
                reasoning="Python compile() 检测到语法错误",
                bug_type="syntax_error",
                keywords=keywords,
                affected_functions=affected_funcs,
            )

        # 优先级4：逻辑/语义错误
        if any(kw in bug_description.lower() for kw in
               ["wrong", "incorrect", "result", "result", "logic", "逻辑", "结果", "错"]):
            return IntentResult(
                intent=IntentType.BUG_FIX,
                confidence=0.80,
                reasoning=f"用户描述表明结果错误: {bug_description[:80]}",
                bug_type=bug_type,
                keywords=keywords,
                affected_functions=affected_funcs,
            )

        # 优先级5：性能问题
        if any(kw in bug_description.lower() for kw in
               ["slow", "performance", "优化", "效率", "超时", "timeout", "complexity"]):
            return IntentResult(
                intent=IntentType.RUNTIME_OPT,
                confidence=0.75,
                reasoning=f"用户描述表明性能问题: {bug_description[:80]}",
                bug_type="performance",
                keywords=keywords,
                affected_functions=affected_funcs,
            )

        # 优先级6：仓库级判断（通过输入特征）
        # 多文件导入、跨包依赖、外部包名 → 需要大模型协调 grep+AST
        repo_level_indicators = self._detect_repo_level(buggy_code, bug_description, error_message)
        if repo_level_indicators:
            return IntentResult(
                intent=IntentType.REPO_LEVEL,
                confidence=0.75,
                reasoning=repo_level_indicators,
                bug_type=bug_type,
                keywords=keywords,
                affected_functions=affected_funcs,
            )

        # 默认：BUG_FIX
        return IntentResult(
            intent=IntentType.BUG_FIX,
            confidence=0.60,
            reasoning="无法精确分类，默认为 BUG_FIX",
            bug_type=bug_type,
            keywords=keywords,
            affected_functions=affected_funcs,
        )

    def _detect_bug_type(self, description: str, code: str) -> str:
        """从描述和代码中检测具体 bug 类型"""
        combined = f"{description} {code}".lower()

        for bug_type, keywords in self.BUG_KEYWORDS.items():
            if any(kw.lower() in combined for kw in keywords):
                return bug_type

        # 尝试从代码模式推断
        if "range(" in code and any(op in code for op in ["<", ">", "<=", ">="]):
            return "off-by-one"
        if "None" in code and ("==" in code or "!=" in code):
            return "null-pointer"
        if "while" in code and "break" not in code:
            return "infinite-loop"
        if "return" not in code:
            return "missing-return"

        return "general_bug"

    def _extract_keywords(self, description: str, code: str) -> List[str]:
        """提取关键词（用于 L2/L3 检索）"""
        text = f"{description} {code}"
        # 提取函数名、变量名、数字常量
        funcs = re.findall(r'def\s+(\w+)', text)
        funcs += re.findall(r'(\w+)\s*\(', text)  # 函数调用
        funcs += re.findall(r'(\w+)\s*[=!<>]', text)  # 变量
        keywords = [f for f in funcs if len(f) > 2 and f not in
                    {"def", "for", "while", "if", "else", "return", "import", "None", "True", "False"}]
        return list(set(keywords))[:20]  # 最多20个

    def _extract_functions(self, code: str) -> List[str]:
        """提取代码中定义的函数名"""
        try:
            tree = ast.parse(code)
            return [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        except SyntaxError:
            return []

    def _detect_repo_level(self, code: str, description: str,
                          error_message: str) -> Optional[str]:
        """
        检测是否为仓库级任务（需要大模型协调 grep+AST 定位）

        仓库级特征：
        - 外部包导入（requests, flask, django, numpy...）
        - 跨文件路径引用（from x import, os.path...）
        - 多文件结构描述（src/, tests/, package/...）
        - pytest 报错（带路径的测试失败）
        """
        combined = f"{code} {description} {error_message}".lower()

        # 外部包导入 → 仓库级
        external_packages = [
            "import requests", "import flask", "import django", "import numpy",
            "import pandas", "import torch", "import tensorflow", "from requests",
            "from flask", "from django", "from numpy", "from pandas",
            "import os", "import sys", "import pathlib",
        ]
        if any(pkg in combined for pkg in external_packages):
            return "检测到外部包导入，需仓库级定位"

        # pytest 路径报错 → 仓库级
        if any(p in combined for p in ["pytest", "test_", "_test.py", "tests/"]):
            return "检测到 pytest 测试路径，需仓库级上下文"

        # 跨文件路径 → 仓库级
        if any(p in combined for p in ["from .", "from ..", "sys.path", "os.path.join"]):
            return "检测到跨文件导入，需仓库级上下文"

        # 代码行数过多 → 仓库级
        lines = [l for l in code.split('\n') if l.strip() and not l.strip().startswith('#')]
        if len(lines) > 100:
            return f"代码超过100行（{len(lines)}行），需仓库级处理"

        return None

    def _has_syntax_error(self, code: str) -> bool:
        """静态检测是否有语法错误"""
        try:
            compile(code, "<string>", "exec")
            return False
        except SyntaxError:
            return True


# ==================== 复杂度评估 ====================

class ComplexityEstimator:
    """基于代码特征的复杂度评估器"""

    def estimate(self, buggy_code: str, intent: IntentResult) -> Complexity:
        """
        评估任务复杂度

        维度：
          - 代码行数
          - 函数数量
          - 嵌套深度
          - 是否涉及 imports / 跨文件
          - 是否有递归
        """
        lines = buggy_code.strip().split('\n')
        num_lines = len([l for l in lines if l.strip() and not l.strip().startswith('#')])

        num_funcs = len(intent.affected_functions)
        has_recursion = 'recursion' in intent.bug_type or 'def ' in buggy_code
        has_imports = 'import ' in buggy_code
        has_nested = buggy_code.count('    ') >= 8  # 简单嵌套检测

        # 综合评分
        score = 0
        score += min(num_lines / 20, 2)     # 超过20行开始加分
        score += min(num_funcs / 3, 1.5)    # 超过3个函数加分
        score += 1.0 if has_recursion else 0  # 递归
        score += 0.5 if has_imports else 0    # import
        score += 0.5 if has_nested else 0     # 嵌套

        # 映射到复杂度等级
        if score <= 1.0:
            return Complexity.TRIVIAL
        elif score <= 2.0:
            return Complexity.SIMPLE
        elif score <= 3.5:
            return Complexity.MODERATE
        elif score <= 5.0:
            return Complexity.COMPLEX
        else:
            return Complexity.VERY_COMPLEX


# ==================== 规划生成器 ====================

class PlanGenerator:
    """
    根据意图和复杂度生成执行规划
    """

    # 意图 → 工具序列模板
    TOOL_CHAINS: Dict[IntentType, List[str]] = {
        IntentType.SYNTAX_FIX: [
            "ast_locate", "syntax_fix", "compile",
        ],
        IntentType.CRASH_FIX: [
            "ast_locate", "error_analysis", "cot_generate", "compile",
        ],
        IntentType.BUG_FIX: [
            "ast_locate", "rag_retrieve", "cot_generate", "compile", "test",
        ],
        IntentType.CODE_FIX: [
            # CodeFixer 模式：直接调用原子操作
            "codefixer",
        ],
        IntentType.REPO_LEVEL: [
            # 仓库级：大模型协调 grep + AST 定位，再小模型修复
            "file_search", "ast_locate", "cot_generate", "harness_execute",
        ],
        IntentType.RUNTIME_OPT: [
            "ast_locate", "profile", "optimize", "benchmark",
        ],
        IntentType.SEMANTIC_FIX: [
            "ast_locate", "rag_retrieve", "semantic_analysis", "cot_generate", "compile", "test",
        ],
    }

    # 复杂度 → 是否需要大模型
    NEEDS_LARGE_MODEL: Dict[Complexity, float] = {
        Complexity.TRIVIAL: 0.0,
        Complexity.SIMPLE: 0.1,
        Complexity.MODERATE: 0.3,
        Complexity.COMPLEX: 0.6,
        Complexity.VERY_COMPLEX: 0.9,
    }

    def generate(self, intent: IntentResult, complexity: Complexity,
                buggy_code: str) -> List[PlanStep]:
        """
        生成有序的执行计划

        Args:
            intent: 意图识别结果
            complexity: 任务复杂度
            buggy_code: 原始 buggy 代码

        Returns:
            PlanStep 列表，有序执行
        """
        base_chain = self.TOOL_CHAINS.get(intent.intent, self.TOOL_CHAINS[IntentType.BUG_FIX])

        steps = []
        step_id = 1

        # Step 1: AST 定位（几乎所有任务都需要）
        if intent.intent != IntentType.SYNTAX_FIX:
            steps.append(PlanStep(
                step_id=step_id,
                name="AST_函数定位",
                tool="ast_locate",
                input_requirements={"code": buggy_code, "functions": intent.affected_functions},
                expected_output="FunctionInfo list with byte offsets",
                description=f"使用 Tree-sitter 定位 {len(intent.affected_functions)} 个函数",
            ))
            step_id += 1

        # Step 2: RAG 检索（MODERATE 及以上）
        if complexity.value >= Complexity.MODERATE.value:
            steps.append(PlanStep(
                step_id=step_id,
                name="RAG_案例检索",
                tool="rag_retrieve",
                input_requirements={
                    "query": f"{intent.bug_type} {intent.keywords}",
                    "top_k": 3,
                },
                expected_output="Top-3 similar bug fix cases",
                condition="complexity >= MODERATE",
                description="从 596 条案例库检索相似修复",
            ))
            step_id += 1

        # Step 3: L2/L3 记忆检索（COMPLEX 及以上）
        if complexity.value >= Complexity.COMPLEX.value:
            if intent.needs_rag is not False:
                steps.append(PlanStep(
                    step_id=step_id,
                    name="L2_教训检索",
                    tool="memory_l2_retrieve",
                    input_requirements={
                        "keywords": intent.keywords,
                        "bug_type": intent.bug_type,
                        "top_k": 2,
                    },
                    expected_output="Top-2 relevant failure lessons",
                    condition="complexity >= COMPLEX",
                    description="从失败教训库检索相关 L2 记忆",
                ))
                step_id += 1

                steps.append(PlanStep(
                    step_id=step_id,
                    name="L3_技能检索",
                    tool="memory_l3_retrieve",
                    input_requirements={
                        "keywords": intent.keywords,
                        "complexity": complexity.value,
                        "top_k": 1,
                    },
                    expected_output="Top-1 relevant skill SOP",
                    condition="complexity >= COMPLEX",
                    description="从技能库检索相关 L3 记忆",
                ))
                step_id += 1

        # Step 4: 核心推理（根据意图选择）
        if intent.intent in (IntentType.BUG_FIX, IntentType.CRASH_FIX, IntentType.SEMANTIC_FIX):
            steps.append(PlanStep(
                step_id=step_id,
                name="CoT_推理生成",
                tool="cot_generate",
                input_requirements={
                    "intent": intent.intent.value,
                    "bug_type": intent.bug_type,
                    "affected_functions": intent.affected_functions,
                },
                expected_output="CoT reasoning (4 steps) + <fixed_code>",
                condition="compile_fail → escalate_to_large_model",
                fallback="large_model_analyze",
                description="小模型生成四步 CoT 推理和修复代码",
            ))
            step_id += 1

            # Step 5: 编译验证
            steps.append(PlanStep(
                step_id=step_id,
                name="Harness_编译测试",
                tool="harness_execute",
                input_requirements={"code": "<from previous step>"},
                expected_output="PASS | FAIL + error_message",
                description="三阶段验证：compile → exec → test cases",
            ))
            step_id += 1

        # Step 4'（CODE_FIX 专用）：小大模型协同修复
        elif intent.intent == IntentType.CODE_FIX:
            steps.append(PlanStep(
                step_id=step_id,
                name="CodeFixer_小大模型协同",
                tool="codefixer",
                input_requirements={
                    "buggy_code": buggy_code,
                    "bug_description": intent.bug_type,
                    "test_cases": "optional",
                },
                expected_output="FixResult: success + final_code + trajectory",
                description="小模型生成 → 大模型给 hint → 小模型重试 → 大模型亲自下场",
            ))

        # Step 4'（REPO_LEVEL 专用）：大模型协调 grep+AST 定位
        elif intent.intent == IntentType.REPO_LEVEL:
            # 4a: 文件搜索
            steps.append(PlanStep(
                step_id=step_id,
                name="文件搜索_grep",
                tool="file_search",
                input_requirements={
                    "pattern": f"{intent.keywords[0] if intent.keywords else intent.bug_type}",
                    "file_types": [".py"],
                },
                expected_output="候选文件列表（文件名 + 行号 + 上下文）",
                description="大模型协调 grep 搜索候选文件",
            ))
            step_id += 1

            # 4b: AST 精确定位
            steps.append(PlanStep(
                step_id=step_id,
                name="AST_函数字节定位",
                tool="ast_locate",
                input_requirements={
                    "functions": intent.affected_functions,
                    "language": "python",
                },
                expected_output="FunctionInfo list with byte offsets",
                description="Tree-sitter 精确定位函数字节偏移（不依赖行号）",
            ))
            step_id += 1

            # 4c: 小模型细粒度修复
            steps.append(PlanStep(
                step_id=step_id,
                name="CoT_推理生成",
                tool="cot_generate",
                input_requirements={
                    "intent": intent.intent.value,
                    "bug_type": intent.bug_type,
                    "affected_functions": intent.affected_functions,
                    "context": "<from file_search + ast_locate>",
                },
                expected_output="CoT reasoning (4 steps) + <fixed_code>",
                condition="compile_fail → escalate_to_large_model",
                fallback="large_model_analyze",
                description="小模型基于 grep+AST 定位结果修复目标函数",
            ))
            step_id += 1

            # 4d: Harness 验证
            steps.append(PlanStep(
                step_id=step_id,
                name="Harness_编译测试",
                tool="harness_execute",
                input_requirements={"code": "<from previous step>"},
                expected_output="PASS | FAIL + error_message",
                description="三阶段验证：compile → exec → test cases",
            ))

        elif intent.intent == IntentType.SYNTAX_FIX:
            steps.append(PlanStep(
                step_id=step_id,
                name="语法修复",
                tool="syntax_fix",
                input_requirements={"code": buggy_code},
                expected_output="Fixed code with syntax errors corrected",
                description="直接修复语法错误，不走 CoT",
            ))
            step_id += 1

        # Step 6: 大模型介入（编译失败时）
        large_model_prob = self.NEEDS_LARGE_MODEL.get(complexity, 0.3)
        steps.append(PlanStep(
            step_id=step_id,
            name="大模型_错误分析",
            tool="large_model_analyze",
            input_requirements={
                "error": "<from harness>",
                "cot_reasoning": "<from cot_generate>",
                "mode": "hint_only",  # 只给 hint，不亲自修
            },
            expected_output="Hint: which step of CoT is wrong",
            condition=f"compile_fail AND random() < {large_model_prob}",
            description=f"大模型分析 CoT 步骤错误（触发概率 {large_model_prob*100:.0f}%）",
        ))

        return steps


# ==================== TaskPlanner 主类 ====================

class TaskPlanner:
    """
    任务规划器（决策层核心）

    输入: buggy_code + bug_description + error_message
    输出: TaskPlan（包含意图、复杂度、规划步骤）

    设计原则：
      1. 意图识别 → 决定后续工具序列
      2. 复杂度评估 → 决定是否需要大模型/L2/L3
      3. 规划生成 → 产生可执行的有序步骤
      4. 所有决策都是可解释的（有 reasoning 字段）
    """

    def __init__(self):
        self.intent_classifier = IntentClassifier()
        self.complexity_estimator = ComplexityEstimator()
        self.plan_generator = PlanGenerator()

    def plan(self, buggy_code: str,
             bug_description: str = "",
             error_message: str = "") -> TaskPlan:
        """
        完整规划流程

        Args:
            buggy_code: 有 bug 的代码
            bug_description: 可选的 bug 描述
            error_message: 可选的编译/运行错误信息

        Returns:
            TaskPlan: 完整的任务规划
        """
        # 1. 意图识别
        intent = self.intent_classifier.classify(
            buggy_code=buggy_code,
            bug_description=bug_description,
            error_message=error_message,
        )
        logger.info(f"[TaskPlanner] Intent: {intent.intent.value} "
                    f"(conf={intent.confidence:.2f}) "
                    f"BugType: {intent.bug_type}")

        # 2. 复杂度评估
        complexity = self.complexity_estimator.estimate(buggy_code, intent)
        logger.info(f"[TaskPlanner] Complexity: {complexity.name} "
                    f"(score={complexity.value})")

        # 3. 生成规划步骤
        steps = self.plan_generator.generate(intent, complexity, buggy_code)
        logger.info(f"[TaskPlanner] Generated {len(steps)} steps: "
                    f"{[s.tool for s in steps]}")

        # 4. 决定辅助需求
        needs_large_model = complexity.value >= Complexity.MODERATE.value
        needs_rag = complexity.value >= Complexity.SIMPLE.value
        needs_l2 = complexity.value >= Complexity.COMPLEX.value
        needs_l3 = complexity.value >= Complexity.COMPLEX.value

        # 5. 生成预估
        estimated_attempts = min(3, max(1, complexity.value))
        estimated_tools = [s.tool for s in steps]

        return TaskPlan(
            task_id=self._generate_task_id(buggy_code),
            intent=intent,
            complexity=complexity,
            steps=steps,
            needs_large_model=needs_large_model,
            needs_rag=needs_rag,
            needs_l2_retrieval=needs_l2,
            needs_l3_retrieval=needs_l3,
            estimated_attempts=estimated_attempts,
            estimated_tools=estimated_tools,
            buggy_code_hash=self._hash_code(buggy_code),
            bug_description=bug_description,
        )

    def _generate_task_id(self, code: str) -> str:
        return hashlib.md5(code.encode()).hexdigest()[:12]

    def _hash_code(self, code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()[:16]

    def replan(self, task_plan: TaskPlan,
               failed_step: int,
               reason: str) -> TaskPlan:
        """
        当规划中的某一步失败时，重新规划

        Args:
            task_plan: 原始规划
            failed_step: 失败的步骤 ID
            reason: 失败原因

        Returns:
            新的 TaskPlan（替换失败步骤，保留已成功的步骤）
        """
        logger.warning(f"[TaskPlanner] Step {failed_step} failed: {reason}. Replanning...")

        # 使用 fallback 工具替换失败步骤
        new_steps = []
        for step in task_plan.steps:
            if step.step_id == failed_step and step.fallback:
                new_steps.append(PlanStep(
                    step_id=step.step_id,
                    name=f"{step.name}（降级）",
                    tool=step.fallback,
                    input_requirements=step.input_requirements,
                    expected_output=step.expected_output,
                    condition=step.condition,
                    fallback=None,  # 防止无限 fallback
                    description=f"降级工具（原始失败: {reason}）",
                ))
            else:
                new_steps.append(step)

        return TaskPlan(
            task_id=f"{task_plan.task_id}_r{failed_step}",
            intent=task_plan.intent,
            complexity=task_plan.complexity,
            steps=new_steps,
            needs_large_model=True,  # 重试时更可能需要大模型
            needs_rag=task_plan.needs_rag,
            needs_l2_retrieval=True,  # 重试时检索更多 L2
            needs_l3_retrieval=task_plan.needs_l3_retrieval,
            estimated_attempts=task_plan.estimated_attempts - 1,
            estimated_tools=[s.tool for s in new_steps],
            buggy_code_hash=task_plan.buggy_code_hash,
            bug_description=task_plan.bug_description,
        )
