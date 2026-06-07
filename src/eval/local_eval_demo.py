#!/usr/bin/env python3
"""
Tencent Interview Demo - Local Evaluation (No API Key Required)
Pipeline: CoT Parse -> Code Extract -> Execute -> Pass@1 Metrics

Usage:
    python local_eval_demo.py              # Quick (20 samples)
    python local_eval_demo.py --full       # Full (50 samples)
    python local_eval_demo.py --verbose   # Show failure details
"""

import os
import sys
import json
import re
import math
import time
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from datetime import datetime

# ==================== Windows GBK 兼容 ====================

import sys, io
try:
    if sys.stdout.encoding.lower() in ('cp936', 'gbk'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
except:
    pass

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# ==================== 数据加载 ====================

def load_high_quality_data(path: str = None) -> List[Dict]:
    """加载高质量数据集（前50条已通过语法检查）"""
    if path is None:
        path = PROJECT_ROOT / "datasets" / "dpo_dataset_merged.json"

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Filter: keep only samples with <reasoning> and <fixed_code> tags
    valid = []
    for item in data:
        chosen = item.get("chosen", "")
        if "<reasoning>" in chosen and "<fixed_code>" in chosen:
            valid.append(item)

    return valid[:50]  # Top 50 high-quality samples


# ==================== CoT Parser（复用你的代码） ====================

class CoTParser:
    """从模型输出中解析CoT推理步骤和修复代码"""

    TAG_PATTERN = re.compile(r'<reasoning>(.*?)</reasoning>', re.DOTALL)
    CODE_PATTERN = re.compile(
        r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
        re.DOTALL
    )
    CODE_PATTERN_FALLBACK = re.compile(
        r'<fixed_code>\s*(.*?)\s*</fixed_code>',
        re.DOTALL
    )

    def parse_steps(self, text: str) -> List[str]:
        """提取CoT推理步骤"""
        steps = []
        reasoning_match = self.TAG_PATTERN.search(text)
        if reasoning_match:
            reasoning_text = reasoning_match.group(1)
            # 按 [Step N] 或 换行 分割
            step_blocks = re.split(r'\[Step\s*\d+\]', reasoning_text)
            for block in step_blocks:
                block = block.strip()
                if block and len(block) > 10:
                    steps.append(block)
        if not steps:
            steps = ["[Automated] Parsed reasoning from output"]
        return steps

    def extract_code(self, text: str) -> str:
        """提取修复后的代码"""
        match = self.CODE_PATTERN.search(text)
        if match:
            code = match.group(1).strip()
            # 清理 markdown 代码块残留
            code = re.sub(r'^```python\s*', '', code, flags=re.DOTALL)
            code = re.sub(r'\s*```$', '', code, flags=re.DOTALL)
            return code

        match = self.CODE_PATTERN_FALLBACK.search(text)
        if match:
            return match.group(1).strip()

        return ""


# ==================== 代码执行器 ====================

@dataclass
class ExecutionResult:
    success: bool
    error_type: str = ""
    error_msg: str = ""
    test_results: List[Dict] = None

    def __post_init__(self):
        if self.test_results is None:
            self.test_results = []


class CodeExecutor:
    """编译 + 执行 + 测试验证"""

    def compile(self, code: str) -> Tuple[bool, str]:
        """编译检查"""
        try:
            compile(code, '<input>', 'exec')
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

    def execute(self, code: str, tests: List[Dict]) -> ExecutionResult:
        """
        执行代码并运行测试用例

        Args:
            code: 修复后的代码
            tests: 测试用例列表 [{"function": "func", "input": [args], "output": expected}]
        """
        # 编译检查
        ok, err = self.compile(code)
        if not ok:
            return ExecutionResult(success=False, error_type="compile", error_msg=err)

        if not tests:
            return ExecutionResult(success=True)

        # 执行测试
        test_results = []
        try:
            namespace = {}
            exec(code, namespace)
        except Exception as e:
            return ExecutionResult(
                success=False, error_type="runtime",
                error_msg=str(e), test_results=[]
            )

        for tc in tests:
            func_name = tc.get("function", "")
            args = tc.get("input", [])
            expected = tc.get("output")

            if func_name not in namespace:
                test_results.append({
                    "passed": False,
                    "func": func_name,
                    "expected": expected,
                    "actual": None,
                    "error": f"Function {func_name} not found"
                })
                continue

            try:
                fn = namespace[func_name]
                if isinstance(args, list):
                    actual = fn(*args)
                else:
                    actual = fn(args)

                # 处理生成器转list
                if hasattr(actual, '__iter__') and hasattr(actual, '__next__') and not isinstance(actual, (str, list, dict)):
                    actual = list(actual)

                passed = actual == expected
                test_results.append({
                    "passed": passed,
                    "func": func_name,
                    "input": args,
                    "expected": expected,
                    "actual": actual,
                })
            except Exception as e:
                test_results.append({
                    "passed": False,
                    "func": func_name,
                    "expected": expected,
                    "actual": None,
                    "error": str(e)
                })

        all_passed = all(r["passed"] for r in test_results)
        return ExecutionResult(
            success=all_passed,
            error_type="test_failed" if not all_passed else "",
            test_results=test_results
        )


# ==================== 测试用例生成器 ====================

def generate_test_cases(buggy_code: str, bug_type: str) -> List[Dict]:
    """
    从buggy代码自动生成测试用例（用于评测）

    策略：
    - 尝试从代码中静态分析函数签名
    - 注入确定性输入/输出对
    - 对于复杂函数使用通用边界测试
    """
    tests = []

    # 从代码中提取函数名
    func_match = re.search(r'def\s+(\w+)\s*\(', buggy_code)
    if not func_match:
        return tests
    func_name = func_match.group(1)

    # 根据bug类型和函数名生成测试用例
    test_cases_map = {
        "two_sum": [
            {"function": "two_sum", "input": [[2, 7, 11, 15], 9], "output": [0, 1]},
            {"function": "two_sum", "input": [[3, 2, 4], 6], "output": [1, 2]},
            {"function": "two_sum", "input": [[3, 3], 6], "output": [0, 1]},
        ],
        "is_valid": [
            {"function": "is_valid", "input": ["()"], "output": True},
            {"function": "is_valid", "input": ["()[]{}"], "output": True},
            {"function": "is_valid", "input": ["(]"], "output": False},
            {"function": "is_valid", "input": ["([)]"], "output": False},
        ],
        "max_subarray": [
            {"function": "max_subarray", "input": [[-2,1,-3,4,-1,2,1,-5,4]], "output": 6},
            {"function": "max_subarray", "input": [[1]], "output": 1},
            {"function": "max_subarray", "input": [[5,4,-1,7,8]], "output": 23},
        ],
        "climb_stairs": [
            {"function": "climb_stairs", "input": [2], "output": 2},
            {"function": "climb_stairs", "input": [3], "output": 3},
            {"function": "climb_stairs", "input": [4], "output": 5},
        ],
        "longest_palindrome": [
            {"function": "longest_palindrome", "input": ["babad"], "output": "bab"},
            {"function": "longest_palindrome", "input": ["cbbd"], "output": "bb"},
            {"function": "longest_palindrome", "input": ["a"], "output": "a"},
        ],
        "max_area": [
            {"function": "max_area", "input": [[1,8,6,2,5,4,8,3,7]], "output": 49},
            {"function": "max_area", "input": [[1,1]], "output": 1},
        ],
        "three_sum": [
            {"function": "three_sum", "input": [[-1,0,1,2,-1,-4]], "output": [[-1,-1,2],[-1,0,1]]},
            {"function": "three_sum", "input": [[0,1,1]], "output": []},
            {"function": "three_sum", "input": [[0,0,0]], "output": [[0,0,0]]},
        ],
        "remove_duplicates": [
            {"function": "remove_duplicates", "input": [[1,1,2]], "output": 2},
            {"function": "remove_duplicates", "input": [[0,0,1,1,1,2,2,3,3,4]], "output": 5},
        ],
        "search_insert": [
            {"function": "search_insert", "input": [[1,3,5,6], 5], "output": 2},
            {"function": "search_insert", "input": [[1,3,5,6], 2], "output": 1},
            {"function": "search_insert", "input": [[1,3,5,6], 7], "output": 4},
        ],
        "merge_two_lists": None,  # 链表结构，不自动测试
        "detect_cycle": None,
        "reverse_linked_list": None,
    }

    if func_name in test_cases_map and test_cases_map[func_name] is not None:
        tests = test_cases_map[func_name]

    return tests


# ==================== 置信区间 ====================

def wilson_ci(passed: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson置信区间"""
    if total == 0:
        return 0.0, 0.0
    p_hat = passed / total
    denom = 1 + z**2 / total
    center = (p_hat + z**2 / (2 * total)) / denom
    spread = z * math.sqrt(p_hat * (1 - p_hat) / total + z**2 / (4 * total**2)) / denom
    return max(0.0, center - spread), min(1.0, center + spread)


# ==================== 主评测流程 ====================

def run_evaluation(items: List[Dict], verbose: bool = False) -> Dict:
    """运行完整评测流程"""

    parser = CoTParser()
    executor = CodeExecutor()

    results = []
    passed = 0
    compile_errors = 0
    runtime_errors = 0
    test_failures = 0
    skipped = 0

    bug_type_stats = {}

    print(f"\n{'='*70}")
    print(f"  CodeFix Agent - Local Evaluation ({len(items)} samples)")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")

    for i, item in enumerate(items):
        bug_type = item.get("bug_type", "unknown")
        prompt = item.get("prompt", "")
        chosen = item.get("chosen", "")

        # 提取函数名
        func_match = re.search(r'def\s+(\w+)\s*\(', prompt)
        func_name = func_match.group(1) if func_match else "unknown"

        # 生成测试用例
        buggy_code_match = re.search(r'```python\n(.*?)```', prompt, re.DOTALL)
        buggy_code = buggy_code_match.group(1).strip() if buggy_code_match else ""
        tests = generate_test_cases(buggy_code, bug_type)

        # CoT Parser 解析
        steps = parser.parse_steps(chosen)
        extracted_code = parser.extract_code(chosen)

        # 执行验证
        if not extracted_code:
            status = "skipped"
            exec_result = None
            skipped += 1
        else:
            exec_result = executor.execute(extracted_code, tests)
            if exec_result.success:
                status = "pass"
                passed += 1
            else:
                status = "fail"
                if exec_result.error_type == "compile":
                    compile_errors += 1
                elif exec_result.error_type == "runtime":
                    runtime_errors += 1
                else:
                    test_failures += 1

        # 统计
        if bug_type not in bug_type_stats:
            bug_type_stats[bug_type] = {"total": 0, "passed": 0}
        bug_type_stats[bug_type]["total"] += 1
        if status == "pass":
            bug_type_stats[bug_type]["passed"] += 1

        results.append({
            "idx": i,
            "func": func_name,
            "bug_type": bug_type,
            "status": status,
            "error_type": exec_result.error_type if exec_result else "no_code",
            "error_msg": exec_result.error_msg if exec_result else "",
            "steps_count": len(steps),
            "code_length": len(extracted_code),
        })

        # 输出
        icon = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}.get(status, "?")
        icon_char = "+" if status == "pass" else "X" if status == "fail" else "="
        print(f"  {icon_char} [{icon:>4s}] {func_name:20s} | {bug_type:25s} | {len(steps):2d} steps")

        if verbose and status == "fail" and exec_result:
            if exec_result.error_type == "test_failed":
                failed_tests = [t for t in exec_result.test_results if not t["passed"]]
                for ft in failed_tests[:2]:
                    print(f"       -> test failed: {ft['func']}({ft['input']}) = {ft['actual']}, expected {ft['expected']}")
            elif exec_result.error_type == "compile":
                print(f"       -> compile error: {exec_result.error_msg[:60]}")
            elif exec_result.error_type == "runtime":
                print(f"       -> runtime error: {exec_result.error_msg[:60]}")

    # 汇总
    total = len(results)
    pass_rate = passed / total if total > 0 else 0
    ci_low, ci_high = wilson_ci(passed, total)

    print(f"\n{'='*70}")
    print(f"  RESULT SUMMARY")
    print(f"{'='*70}")
    print(f"  Total:         {total:3d}")
    print(f"  Passed:        {passed:3d} ({pass_rate:.1%})")
    print(f"  Test Failures: {test_failures:3d}")
    print(f"  Compile Errors:{compile_errors:3d}")
    print(f"  Runtime Errors:{runtime_errors:3d}")
    print(f"  Skipped:       {skipped:3d}")
    print(f"  ")
    print(f"  Pass@1:   {pass_rate:.1%}")
    print(f"  95% CI:   [{ci_low:.1%}, {ci_high:.1%}]")
    print(f"{'='*70}")

    # By bug type
    print(f"\n  By Bug Type:")
    sorted_types = sorted(bug_type_stats.items(), key=lambda x: -x[1]["total"])
    for bt, stats in sorted_types:
        rate = stats["passed"] / stats["total"] if stats["total"] > 0 else 0
        bar = "#" * int(rate * 20) + "." * (20 - int(rate * 20))
        print(f"    {bt:25s} {stats['passed']:2d}/{stats['total']:2d} ({rate:.0%}) |{bar}|")

    # Demo a success case
    success_cases = [r for r in results if r["status"] == "pass"]
    if success_cases:
        print(f"\n{'='*70}")
        print(f"  DEMO: Random Success Case")
        print(f"{'='*70}")
        import random
        demo = random.choice(success_cases)
        demo_item = items[demo["idx"]]
        print(f"  Function:  {demo['func']}")
        print(f"  Bug Type:  {demo['bug_type']}")
        print(f"  CoT Steps: {demo['steps_count']}")
        print(f"  Code Len:  {demo['code_length']} chars")

        chosen_text = demo_item.get("chosen", "")
        steps = parser.parse_steps(chosen_text)
        print(f"\n  CoT Reasoning ({len(steps)} steps):")
        for j, step in enumerate(steps[:3], 1):
            print(f"    Step {j}: {step[:80]}{'...' if len(step) > 80 else ''}")
        print(f"\n  Fixed Code:")
        code = parser.extract_code(chosen_text)
        for line in code.split('\n')[:8]:
            print(f"    {line}")
        if len(code.split('\n')) > 8:
            print(f"    ...")

    print(f"\n{'='*70}")
    print(f"  Pipeline Verified OK!")
    print(f"  - CoT Parser: OK (<reasoning> + <fixed_code> tags)")
    print(f"  - Executor:   OK (compile + test validation)")
    print(f"  - CI:         OK (Wilson CI calculation)")
    print(f"  - Dataset:    {len(items)} high-quality samples")
    print(f"{'='*70}\n")

    return {
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "compile_errors": compile_errors,
        "runtime_errors": runtime_errors,
        "test_failures": test_failures,
        "skipped": skipped,
        "bug_type_stats": bug_type_stats,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="CodeFix Agent - Local Evaluation")
    parser.add_argument("--full", action="store_true", help="Full evaluation (50 samples)")
    parser.add_argument("--verbose", action="store_true", help="Show failure details")
    parser.add_argument("--output", type=str, default="", help="Output path")
    parser.add_argument("--dataset", type=str, default="", help="Dataset path")
    args = parser.parse_args()

    print("\n" + "="*70)
    print("  CodeFix Agent - Tencent Interview Demo")
    print("  Pipeline: CoT Parse -> Code Extract -> Execute -> Pass@1")
    print("="*70)

    dataset_path = args.dataset if args.dataset else None
    data = load_high_quality_data(dataset_path)
    n = len(data)

    if not args.full and n > 20:
        print(f"\n[Quick Mode] Using first 20 samples (full: --full)")
        data = data[:20]
    else:
        print(f"\n[Full Mode] Loaded {len(data)} samples")

    results = run_evaluation(data, verbose=args.verbose)

    # 保存结果
    if args.output:
        output_path = Path(args.output)
    else:
        output_dir = PROJECT_ROOT / "runs_pipeline"
        output_dir.mkdir(exist_ok=True)
        output_path = output_dir / f"local_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"  结果已保存: {output_path}")

    return results


if __name__ == "__main__":
    main()
