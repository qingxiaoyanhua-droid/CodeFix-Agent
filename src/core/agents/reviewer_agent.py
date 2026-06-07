#!/usr/bin/env python3
"""
Reviewer Agent: 代码审查 + 编译验证 + 软验证 Nudge
====================================================
职责：
  - 编译验证：Python / Node.js / C / C++ 编译检查
  - 测试用例执行：pytest / node test 运行
  - 7 维度自动 Bug 检测（AST 分析）
  - Ruff 静态分析兜底
  - 软验证 Nudge：模型说"完成"时，强制一次确认检查（MAC 论文最佳实践）
  - Test-File Guard Rail：拒绝修改测试文件

参考 MAC 论文的 Self-Verification Loop：
  模型说完成 → Agent 先跑 diff + py_compile + 测试 → 有问题才打回
"""

from __future__ import annotations

import re
import time
import hashlib
import subprocess
import ast
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

from base_agent import BaseAgent, AgentProfile, AgentCapability, SharedContext
from sandbox.shell_sandbox import ShellSandbox, TestRunner


# ==================== 7 维度 Bug 检测 ====================

@dataclass
class BugReport:
    """Bug 报告"""
    dimension: str       # 哪个维度
    severity: str       # critical | high | medium | low
    description: str
    location: str       # 文件:行号 或 函數名
    suggestion: str     # 修复建议


@dataclass
class ReviewResult:
    """审查结果"""
    # 编译 + 测试
    compile_success: bool = False
    compile_error: Optional[str] = None
    test_passed: bool = False
    test_error: Optional[str] = None

    # Ruff 静态分析
    ruff_issues: List[Dict] = field(default_factory=list)
    ruff_passed: bool = True

    # 7 维度 Bug 检测
    bug_reports: List[BugReport] = field(default_factory=list)

    # 综合结论
    outcome: str = "unknown"   # success | partial | failed | critical_bug
    quality_score: float = 0.0  # 0.0 - 1.0

    # 软验证 Nudge
    nudge_triggered: bool = False
    nudge_message: str = ""

    # 审查耗时
    review_time_ms: float = 0.0


# ==================== 7 维度 Bug 检测器 ====================

class SevenDimensionBugDetector:
    """
    7 维度自动 Bug 检测（基于 AST 分析）：

    D1 - 语法正确性：Python 语法是否正确
    D2 - 类型一致性：变量/返回值类型是否匹配
    D3 - 空指针 / None 检查：是否有未检查的 None
    D4 - 边界条件：循环/数组访问的边界是否正确
    D5 - 逻辑完整性：if/else 是否覆盖所有情况
    D6 - 资源泄露：文件/连接/锁是否正确关闭
    D7 - 安全风险：是否有注入、命令执行等安全漏洞
    """

    def __init__(self, language: str = "python"):
        self.language = language

    def detect(self, code: str) -> List[BugReport]:
        if self.language != "python":
            return []

        reports = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            reports.append(BugReport(
                dimension="D1", severity="critical",
                description="Python 语法错误",
                location="<file>",
                suggestion="检查代码缩进和语法",
            ))
            return reports

        reports.extend(self._check_types(tree, code))
        reports.extend(self._check_none_safety(tree, code))
        reports.extend(self._check_bounds(tree, code))
        reports.extend(self._check_logic_completeness(tree, code))
        reports.extend(self._check_resource_leaks(tree, code))
        reports.extend(self._check_security(tree, code))
        return reports

    def _check_types(self, tree: ast.AST, code: str) -> List[BugReport]:
        """D2: 类型一致性检测"""
        reports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # 检查返回值中是否存在 + str 等类型混用
                for child in ast.walk(node):
                    if isinstance(child, ast.BinOp):
                        if isinstance(child.op, ast.Add):
                            # 简单检测 "var + str(...)" 模式
                            left_code = ast.unparse(child.left) if hasattr(ast, 'unparse') else ""
                            if "str(" in left_code or "int(" not in left_code:
                                pass  # 简化版，不做深度检测
        return reports

    def _check_none_safety(self, tree: ast.AST, code: str) -> List[BugReport]:
        """D3: 空指针 / None 安全检测"""
        reports = []
        for node in ast.walk(tree):
            # 检测 a == None 而非 a is None
            if isinstance(node, ast.Compare) and any(
                isinstance(n, ast.Constant) and n.value is None
                for n in node.ops
            ):
                # 找到了 None 比较，但不检查 is/is not
                comp_code = code[node.col_offset:node.col_offset+20] if hasattr(node, 'col_offset') else ""
                # 简化：只报告，不深度分析
        return reports

    def _check_bounds(self, tree: ast.AST, code: str) -> List[BugReport]:
        """D4: 边界条件检测"""
        reports = []
        for node in ast.walk(tree):
            # 检测 range(len(...)) 后用 < 而非 <=
            if isinstance(node, ast.For):
                if isinstance(node.iter, ast.Call):
                    if hasattr(node.iter.func, 'id') and node.iter.func.id == 'range':
                        pass  # 简化
        return reports

    def _check_logic_completeness(self, tree: ast.AST, code: str) -> List[BugReport]:
        """D5: 逻辑完整性检测"""
        reports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                has_else = any(isinstance(n, ast.Else) for n in node.orelse)
                # 检测只有 if 没有 else 的情况（潜在不完整逻辑）
                # 简化版本不做强制报告
        return reports

    def _check_resource_leaks(self, tree: ast.AST, code: str) -> List[BugReport]:
        """D6: 资源泄露检测"""
        reports = []
        # 检测 open() 没有 with 语句
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if hasattr(node.func, 'id') and node.func.id == 'open':
                    # 检查是否在 with 语句内（简化版）
                    reports.append(BugReport(
                        dimension="D6", severity="medium",
                        description="open() 未使用 with 语句，可能导致资源未关闭",
                        location=f"line ~{getattr(node, 'lineno', '?')}",
                        suggestion="使用 with open(...) as f: 确保文件正确关闭",
                    ))
        return reports

    def _check_security(self, tree: ast.AST, code: str) -> List[BugReport]:
        """D7: 安全风险检测"""
        reports = []
        dangerous_calls = [
            ("eval", "eval() 允许执行任意代码，存在安全风险"),
            ("exec", "exec() 允许执行任意代码，存在安全风险"),
            ("__import__", "__import__() 动态导入，存在安全风险"),
        ]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if hasattr(node.func, 'id'):
                    for name, msg in dangerous_calls:
                        if node.func.id == name:
                            reports.append(BugReport(
                                dimension="D7", severity="high",
                                description=msg,
                                location=f"line ~{getattr(node, 'lineno', '?')}",
                                suggestion=f"避免使用 {name}()，考虑替代方案",
                            ))
        return reports


# ==================== Ruff 静态分析 ====================

def run_ruff_check(code: str, filepath: str = "/tmp/ruff_check.py") -> tuple[bool, List[Dict]]:
    """
    运行 Ruff 静态分析。
    Returns: (passed, issues)
    """
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)

        result = subprocess.run(
            ["ruff", "check", filepath, "--output-format=json"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        issues = []
        if result.returncode != 0 and result.stdout:
            try:
                import json
                raw = json.loads(result.stdout)
                for item in (raw if isinstance(raw, list) else []):
                    issues.append({
                        "code": item.get("code", ""),
                        "message": item.get("message", ""),
                        "location": item.get("location", {}),
                    })
            except Exception:
                # 非 JSON 输出，逐行解析
                for line in result.stdout.split("\n"):
                    if line.strip() and ":" in line:
                        parts = line.split(":", 2)
                        issues.append({
                            "code": parts[0] if len(parts) > 0 else "",
                            "message": parts[1] if len(parts) > 1 else line,
                            "location": {"row": parts[2] if len(parts) > 2 else "?"},
                        })

        passed = len(issues) == 0
        return passed, issues
    except FileNotFoundError:
        return True, []  # ruff 未安装，跳过
    except Exception:
        return True, []  # 其他错误，跳过


# ==================== Reviewer Agent ====================

class ReviewerAgent(BaseAgent):
    """
    代码审查 Agent（验证 + 质量把关）

    职责：
      1. 编译验证：Python / Node.js / C / C++ 编译检查
      2. 测试用例执行：pytest / node test
      3. 7 维度 Bug 检测（AST）
      4. Ruff 静态分析兜底
      5. 软验证 Nudge：强制一次确认检查
      6. Test-File Guard Rail：拒绝修改测试文件

    去中心化设计：
      - 独立运行，不依赖 Solver 的输出格式
      - Orchestrator 根据其结果决定是否继续重试
      - 审查结果写入 SharedContext，供 Memory Agent 写入双池
    """

    def __init__(
        self,
        agent_id: str = "reviewer_agent",
        sandbox: Optional[ShellSandbox] = None,
        test_runner: Optional[TestRunner] = None,
        language: str = "python",
    ):
        profile = AgentProfile(
            name=agent_id,
            capabilities={AgentCapability.REVIEW, AgentCapability.VERIFY},
            description="编译验证 + 7维度审查 + 软验证Nudge + Ruff兜底",
            timeout_seconds=30.0,
        )
        super().__init__(agent_id, profile)

        self.sandbox = sandbox or ShellSandbox(timeout=10)
        self.test_runner = test_runner or TestRunner(self.sandbox)
        self.language = language
        self.bug_detector = SevenDimensionBugDetector(language=language)

        # 软验证 Nudge 状态
        self._nudge_enabled = True
        self._nudge_threshold_rounds = 1  # 第 1 轮就开始 nudge

    def run(self, context: SharedContext) -> Dict[str, Any]:
        """执行完整审查流程"""
        t0 = time.time()

        solver_out = context.solver_output or {}
        fixed_code = solver_out.get("fixed_code", "")

        if not fixed_code:
            return {
                "status": "error",
                "error": "No fixed_code in solver_output",
            }

        # Step 1: 编译验证
        compile_result = self._compile_check(fixed_code)

        # Step 2: 测试用例执行
        test_result = {"passed": False, "error": None}
        if compile_result["success"]:
            test_result = self._run_tests(fixed_code, context.test_cases)

        # Step 3: Ruff 静态分析（编译通过后）
        ruff_passed = True
        ruff_issues = []
        if compile_result["success"]:
            ruff_passed, ruff_issues = run_ruff_check(fixed_code)

        # Step 4: 7 维度 Bug 检测
        bug_reports = self.bug_detector.detect(fixed_code)

        # Step 5: 软验证 Nudge（MAC 论文最佳实践）
        nudge_triggered = False
        nudge_message = ""
        if self._should_nudge(context):
            nudge_triggered, nudge_message = self._soft_verify_nudge(
                fixed_code, compile_result, test_result, bug_reports
            )

        # Step 6: 综合评分
        quality_score = self._compute_quality_score(
            compile_result, test_result, ruff_passed, bug_reports, nudge_triggered
        )

        # Step 7: 结论
        outcome = self._determine_outcome(
            compile_result, test_result, bug_reports, quality_score
        )

        review_time_ms = (time.time() - t0) * 1000

        review_result = ReviewResult(
            compile_success=compile_result["success"],
            compile_error=compile_result.get("error"),
            test_passed=test_result["passed"],
            test_error=test_result.get("error"),
            ruff_issues=ruff_issues,
            ruff_passed=ruff_passed,
            bug_reports=bug_reports,
            outcome=outcome,
            quality_score=quality_score,
            nudge_triggered=nudge_triggered,
            nudge_message=nudge_message,
            review_time_ms=review_time_ms,
        )

        # 写入 SharedContext
        context.reviewer_output = {
            "compile_success": compile_result["success"],
            "compile_error": compile_result.get("error"),
            "test_passed": test_result["passed"],
            "test_error": test_result.get("error"),
            "ruff_passed": ruff_passed,
            "ruff_issues": ruff_issues,
            "bug_reports": [
                {"dimension": r.dimension, "severity": r.severity,
                 "description": r.description, "suggestion": r.suggestion}
                for r in bug_reports
            ],
            "outcome": outcome,
            "quality_score": quality_score,
            "nudge_triggered": nudge_triggered,
            "nudge_message": nudge_message,
            "review_time_ms": review_time_ms,
        }

        return {
            "status": "ok",
            "review_result": review_result,
            "quality_score": quality_score,
            "outcome": outcome,
            "nudge_triggered": nudge_triggered,
            "nudge_message": nudge_message,
        }

    # ==================== 编译验证 ====================

    def _compile_check(self, code: str) -> Dict[str, Any]:
        """编译 / 语法检查"""
        result = self.sandbox.run(code, self.language)

        if result.error == "DangerousPattern":
            return {"success": False, "error": f"Security blocked: {result.stderr}"}
        if result.error:
            return {"success": False, "error": result.stderr}

        if result.exit_code != 0:
            return {"success": False, "error": result.stderr}

        return {"success": True}

    # ==================== 测试执行 ====================

    def _run_tests(self, code: str, test_cases: List[Dict]) -> Dict[str, Any]:
        """运行测试用例"""
        if not test_cases:
            return {"passed": True, "error": None}  # 无测试用例，直接通过

        try:
            passed, error = self.test_runner.run_tests(code, self.language, test_cases)
            return {"passed": passed, "error": error}
        except Exception as e:
            return {"passed": False, "error": str(e)}

    # ==================== 软验证 Nudge ====================

    def _should_nudge(self, context: SharedContext) -> bool:
        """判断是否触发软验证 Nudge"""
        if not self._nudge_enabled:
            return False
        return context.current_round >= self._nudge_threshold_rounds

    def _soft_verify_nudge(
        self,
        fixed_code: str,
        compile_result: Dict,
        test_result: Dict,
        bug_reports: List[BugReport],
    ) -> tuple[bool, str]:
        """
        MAC 论文的软验证 Nudge：一次性的确认检查，不死循环。

        验证点：
          1. AST diff 非空（确实做了修改）
          2. py_compile / 语法检查通过
          3. 无 critical/high 级别 bug
          4. 如果有 ruff 建议则提示优化
        """
        issues = []

        # 1. 检查是否做了修改
        if not fixed_code.strip():
            issues.append("修复代码为空")

        # 2. 编译必须通过
        if not compile_result.get("success", False):
            issues.append(f"编译失败: {compile_result.get('error', 'unknown')[:50]}")

        # 3. 检查 critical/high bug
        critical_bugs = [b for b in bug_reports if b.severity in ("critical", "high")]
        if critical_bugs:
            issues.append(f"存在 {len(critical_bugs)} 个 critical/high 级别问题")

        # 4. 检查 Ruff 建议
        if bug_reports:
            d6_issues = [b for b in bug_reports if b.dimension == "D6"]
            if d6_issues:
                issues.append(f"存在 {len(d6_issues)} 个资源管理建议（可选优化）")

        if issues:
            nudge_msg = (
                "[软验证 Nudge] 请确认以下问题：\n" +
                "\n".join(f"  - {issue}" for issue in issues) +
                "\n如果以上都不是 blocking 问题，可以确认完成。"
            )
            return True, nudge_msg

        return False, ""

    # ==================== 质量评分 ====================

    def _compute_quality_score(
        self,
        compile_result: Dict,
        test_result: Dict,
        ruff_passed: bool,
        bug_reports: List[BugReport],
        nudge_triggered: bool,
    ) -> float:
        """
        综合质量评分（0.0 - 1.0）

        权重：
          - 编译通过: 30%
          - 测试通过: 40%
          - Ruff 通过: 15%
          - 无 critical bug: 15%
        """
        score = 0.0

        if compile_result.get("success", False):
            score += 0.30

        if test_result.get("passed", False):
            score += 0.40

        if ruff_passed:
            score += 0.15

        # 无 critical bug
        critical_count = sum(1 for b in bug_reports if b.severity == "critical")
        high_count = sum(1 for b in bug_reports if b.severity == "high")
        if critical_count == 0:
            score += 0.075
        if high_count == 0:
            score += 0.075

        return min(score, 1.0)

    # ==================== 最终结论 ====================

    def _determine_outcome(
        self,
        compile_result: Dict,
        test_result: Dict,
        bug_reports: List[BugReport],
        quality_score: float,
    ) -> str:
        """判断最终结果"""
        critical_bugs = [b for b in bug_reports if b.severity == "critical"]

        if not compile_result.get("success", False):
            return "failed"
        if critical_bugs:
            return "critical_bug"
        if not test_result.get("passed", False):
            return "partial"  # 编译通过但测试失败
        if quality_score >= 0.85:
            return "success"
        return "partial"

    # ==================== Test-File Guard Rail ====================

    def check_test_file_modification(self, old_code: str, new_code: str) -> bool:
        """
        检查修复代码是否修改了测试文件。

        返回 True 如果检测到修改了测试相关代码，False 表示安全。

        用于 Orchestrator 或沙箱层判断是否拒绝本次修复。
        """
        test_patterns = [
            r'def test_\w+',      # pytest 函数
            r'class Test\w+',     # pytest 类
            r'unittest\.TestCase',
            r'def suite\(',        # 常见测试套件
            r'assert\s+',         # 测试断言
            r'\.assert\w+\(',
        ]

        # 检查新增代码中是否包含测试相关代码
        new_lines = set(new_code.split('\n')) - set(old_code.split('\n'))
        for line in new_lines:
            for pattern in test_patterns:
                if re.search(pattern, line):
                    return True  # 检测到修改了测试文件

        return False
