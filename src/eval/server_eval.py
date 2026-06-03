#!/usr/bin/env python3
"""
服务器端完整评测脚本
用于在服务器上运行QuixBugs基准评测

使用方法:
    python server_eval.py                    # 快速评测（基线）
    python server_eval.py --model <路径>   # 评测指定模型
    python server_eval.py --full            # 完整评测（需GPU）
"""

import os
import sys
import json
import time
import argparse
import re
import math
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple
import types
import math
from collections.abc import Generator

# ==================== 数据 ====================

QUIXBUGS_DATA = [
    {
        "name": "bitcount", "bug_type": "wrong_operator",
        "buggy": """def bitcount(n):
    count = 0
    while n:
        n ^= n - 1
        count += 1
    return count""",
        "fixed": """def bitcount(n):
    count = 0
    while n:
        n &= n - 1
        count += 1
    return count""",
        "tests": [
            {"function": "bitcount", "input": [127], "output": 7},
            {"function": "bitcount", "input": [0], "output": 0},
            {"function": "bitcount", "input": [1], "output": 1},
        ]
    },
    {
        "name": "find_first_in_sorted", "bug_type": "off_by_one",
        "buggy": """def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr)
    while lo <= hi:
        mid = (lo + hi) // 2
        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid
        elif x <= arr[mid]:
            hi = mid
        else:
            lo = mid + 1
    return -1""",
        "fixed": """def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid
        elif x <= arr[mid]:
            hi = mid - 1
        else:
            lo = mid + 1
    return -1""",
        "tests": [
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 3], "output": 2},
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 5], "output": -1},
            {"function": "find_first_in_sorted", "input": [[1, 1, 2, 3], 1], "output": 0},
        ]
    },
    {
        "name": "flatten", "bug_type": "wrong_variable",
        "buggy": """def flatten(arr):
    for x in arr:
        if isinstance(x, list):
            for y in flatten(x):
                yield y
        else:
            yield flatten(x)""",
        "fixed": """def flatten(arr):
    for x in arr:
        if isinstance(x, list):
            for y in flatten(x):
                yield y
        else:
            yield x""",
        "tests": [
            {"function": "flatten", "input": [[[1, [2]], 3]], "output": [1, 2, 3]},
            {"function": "flatten", "input": [[[1, 2, 3, 4]]], "output": [1, 2, 3, 4]},
        ]
    },
    {
        "name": "gcd", "bug_type": "wrong_argument_order",
        "buggy": """def gcd(a, b):
    if b:
        return gcd(a % b, b)
    else:
        return a""",
        "fixed": """def gcd(a, b):
    if b:
        return gcd(b, a % b)
    else:
        return a""",
        "tests": [
            {"function": "gcd", "input": [12, 8], "output": 4},
            {"function": "gcd", "input": [17, 5], "output": 1},
            {"function": "gcd", "input": [100, 25], "output": 25},
        ]
    },
    {
        "name": "is_valid_parenthesization", "bug_type": "missing_check",
        "buggy": """def is_valid_parenthesization(parens):
    depth = 0
    for paren in parens:
        if paren == '(':
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                return False
    return True""",
        "fixed": """def is_valid_parenthesization(parens):
    depth = 0
    for paren in parens:
        if paren == '(':
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0""",
        "tests": [
            {"function": "is_valid_parenthesization", "input": ["(())"], "output": True},
            {"function": "is_valid_parenthesization", "input": ["(()"], "output": False},
            {"function": "is_valid_parenthesization", "input": [")("], "output": False},
            {"function": "is_valid_parenthesization", "input": [""], "output": True},
        ]
    },
    {
        "name": "max_sublist_sum", "bug_type": "wrong_initialization",
        "buggy": """def max_sublist_sum(arr):
    max_ending_here = 0
    max_so_far = 0
    for x in arr:
        max_ending_here = max(0, max_ending_here + x)
        max_so_far = max(max_so_far, max_ending_here)
    return max_so_far""",
        "fixed": """def max_sublist_sum(arr):
    if not arr:
        return 0
    max_ending_here = arr[0]
    max_so_far = arr[0]
    for x in arr[1:]:
        max_ending_here = max(0, max_ending_here + x)
        max_so_far = max(max_so_far, max_ending_here)
    return max_so_far""",
        "tests": [
            {"function": "max_sublist_sum", "input": [[4, -5, 2, 1, -1, 3]], "output": 5},
            {"function": "max_sublist_sum", "input": [[-1, -2, -3]], "output": 0},
            {"function": "max_sublist_sum", "input": [[1, 2, 3]], "output": 6},
        ]
    },
    {
        "name": "sqrt", "bug_type": "wrong_expression",
        "buggy": """def sqrt(x, epsilon):
    approx = x / 2
    while abs(x - approx) > epsilon:
        approx = 0.5 * (approx + x / approx)
    return approx""",
        "fixed": """def sqrt(x, epsilon):
    approx = x / 2
    while abs(approx * approx - x) > epsilon:
        approx = 0.5 * (approx + x / approx)
    return approx""",
        "tests": [
            {"function": "sqrt", "input": [4, 0.01], "output": 2.0},
            {"function": "sqrt", "input": [9, 0.01], "output": 3.0},
        ]
    },
    {
        "name": "quicksort", "bug_type": "off_by_one",
        "buggy": """def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    lesser = quicksort([x for x in arr[1:] if x < pivot])
    greater = quicksort([x for x in arr[1:] if x > pivot])
    return lesser + [pivot] + greater""",
        "fixed": """def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    lesser = quicksort([x for x in arr[1:] if x < pivot])
    greater = quicksort([x for x in arr[1:] if x >= pivot])
    return lesser + [pivot] + greater""",
        "tests": [
            {"function": "quicksort", "input": [[3, 1, 2]], "output": [1, 2, 3]},
            {"function": "quicksort", "input": [[5, 3, 3, 1]], "output": [1, 3, 3, 5]},
            {"function": "quicksort", "input": [[]], "output": []},
        ]
    },
    {
        "name": "reverse_linked_list", "bug_type": "wrong_variable",
        "buggy": """def reverse_linked_list(node):
    prevnode = None
    while node:
        nextnode = node.next
        node.next = prevnode
        node = nextnode
        prevnode = nextnode
    return prevnode""",
        "fixed": """def reverse_linked_list(node):
    prevnode = None
    while node:
        nextnode = node.next
        node.next = prevnode
        prevnode = node
        node = nextnode
    return prevnode""",
        "tests": []  # 需要链表结构
    },
    {
        "name": "detect_cycle", "bug_type": "wrong_condition",
        "buggy": """def detect_cycle(node):
    if not node:
        return None
    slow = node
    fast = node
    while fast and fast.next:
        slow = slow.next
        fast = fast.next.next
        if slow == fast:
            return slow
    return None""",
        "fixed": """def detect_cycle(node):
    if not node or not node.next:
        return None
    slow = node
    fast = node.next
    while fast and fast.next:
        slow = slow.next
        fast = fast.next.next
        if slow == fast:
            return slow
    return None""",
        "tests": []  # 需要链表结构
    },
]


# ==================== 核心函数 ====================

def execute_and_test(code: str, tests: List[Dict]) -> Tuple[bool, str]:
    """执行代码并测试"""
    if not tests:
        try:
            compile(code, '<test>', 'exec')
            return True, ""
        except SyntaxError as e:
            return False, str(e)

    try:
        namespace = {}
        exec(code, namespace)
    except Exception as e:
        return False, f"Execution error: {e}"

    for tc in tests:
        func_name = tc["function"]
        args = tc["input"]
        expected = tc["output"]
        if func_name not in namespace:
            return False, f"Function {func_name} not defined"

        try:
            if isinstance(args, list):
                result = namespace[func_name](*args)
            else:
                result = namespace[func_name](args)

            if isinstance(result, Generator):
                result = list(result)

            # Float comparison with tolerance
            if isinstance(expected, float) and isinstance(result, (int, float)):
                if abs(float(result) - expected) < 0.01:
                    result = expected  # treat as equal

            if result != expected:
                return False, f"{func_name}({args}) = {result}, expected {expected}"
        except Exception as e:
            return False, f"Runtime error: {e}"

    return True, ""


def extract_code(text: str) -> str:
    """从模型输出中提取代码"""
    patterns = [
        r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
        r'<fixed_code>\s*(.*?)\s*</fixed_code>',
        r'```python\s*(.*?)\s*```',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
    return text.strip()


def wilson_ci(passed: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson置信区间"""
    if total == 0:
        return (0.0, 0.0)
    p_hat = passed / total
    denom = 1 + z**2 / total
    center = (p_hat + z**2 / (2 * total)) / denom
    spread = z * math.sqrt(p_hat * (1 - p_hat) / total + z**2 / (4 * total**2)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


# ==================== 模型调用 ====================

def load_model(model_path: str = None):
    """加载模型（如果指定）"""
    if not model_path:
        return None

    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        print(f"Loading model from {model_path}...")
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        print("Model loaded successfully!")
        return model, tokenizer
    except Exception as e:
        print(f"Failed to load model: {e}")
        return None


def generate_fix(model_info, buggy_code: str, bug_desc: str) -> str:
    """使用模型生成修复"""
    if model_info is None:
        return None

    model, tokenizer = model_info

    prompt = f"""Bug: {bug_desc}

Buggy Code:
```python
{buggy_code}
```

Fix the bug. Return the corrected code in <fixed_code> tags.
"""

    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    outputs = model.generate(
        **inputs,
        max_new_tokens=256,
        temperature=0.2,
        do_sample=True,
    )
    response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    return extract_code(response)


# ==================== 评测 ====================

def evaluate(model_info, programs: List[Dict], desc: str = "", use_oracle: bool = False) -> Dict:
    """评测模型"""
    results = []
    passed = 0

    print(f"\n{'='*60}")
    print(f"  Evaluating: {desc}")
    print(f"{'='*60}")

    for prog in programs:
        if not prog.get("tests"):
            continue

        bug_desc = f"Fix the {prog['name']} function"
        start = time.time()

        if model_info:
            generated = generate_fix(model_info, prog["buggy"], bug_desc)
            code = generated if generated else prog["buggy"]
        elif use_oracle:
            # Oracle: use the pre-computed fixed code
            code = prog.get("fixed", prog["buggy"])
        else:
            # Baseline: use buggy code
            code = prog["buggy"]

        ok, err = execute_and_test(code, prog["tests"])
        elapsed = (time.time() - start) * 1000

        results.append({
            "name": prog["name"],
            "bug_type": prog["bug_type"],
            "passed": ok,
            "error": err[:100] if err else "",
            "time_ms": elapsed,
        })

        if ok:
            passed += 1

        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {prog['name']} ({prog['bug_type']}) {elapsed:.0f}ms")
        if not ok:
            print(f"         {err[:80]}")

    total = len(results)
    pass_rate = passed / total if total > 0 else 0
    ci = wilson_ci(passed, total)

    print(f"\n{'='*60}")
    print(f"  Results: {passed}/{total} = {pass_rate:.1%}")
    print(f"  95% CI: [{ci[0]:.1%}, {ci[1]:.1%}]")
    print(f"{'='*60}")

    # 按bug类型统计
    by_type = {}
    for r in results:
        bt = r["bug_type"]
        if bt not in by_type:
            by_type[bt] = {"total": 0, "passed": 0}
        by_type[bt]["total"] += 1
        if r["passed"]:
            by_type[bt]["passed"] += 1

    print("\n  By bug type:")
    for bt, stats in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
        rate = stats["passed"] / stats["total"] if stats["total"] > 0 else 0
        print(f"    {bt:25s} {stats['passed']}/{stats['total']} ({rate:.0%})")

    return {
        "desc": desc,
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
        "ci_low": ci[0],
        "ci_high": ci[1],
        "by_type": by_type,
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description="QuixBugs基准评测")
    parser.add_argument("--model", type=str, default="",
                        help="模型路径（不指定则使用基线）")
    parser.add_argument("--output", type=str, default="./runs/quixbugs_results.json",
                        help="结果保存路径")
    parser.add_argument("--oracle", action="store_true",
                        help="运行Oracle测试（使用正确代码）")
    args = parser.parse_args()

    programs = [p for p in QUIXBUGS_DATA if p.get("tests")]

    if args.oracle:
        # Oracle: use fixed code to validate test data correctness
        print("Running Oracle test (using fixed code)...")
        model_info = None
        desc = "Oracle (fixed code)"
        result = evaluate(None, programs, desc, use_oracle=True)
    else:
        model_info = load_model(args.model) if args.model else None
        desc = "Baseline" if model_info is None else f"Model: {args.model}"
        result = evaluate(model_info, programs, desc, use_oracle=False)

    # 保存结果
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to: {args.output}")

    return result


if __name__ == "__main__":
    main()
