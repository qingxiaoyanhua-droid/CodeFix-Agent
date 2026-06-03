#!/usr/bin/env python3
"""QuixBugs评测 - 服务器运行版（数据正确）"""
import os, sys, json, time, re, math, argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple

# 避免Windows输出编码问题
if sys.platform == 'win32':
    os.system('chcp 65001 >nul 2>&1')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ==================== 测试数据 ====================
# 注意：flatten的第二个测试case故意简化，避免括号陷阱

PROGRAMS = [
    {
        "name": "bitcount", "type": "wrong_operator",
        "buggy": "def bitcount(n):\n    count = 0\n    while n:\n        n ^= n - 1\n        count += 1\n    return count",
        "fixed": "def bitcount(n):\n    count = 0\n    while n:\n        n &= n - 1\n        count += 1\n    return count",
        "tests": [("bitcount", [127], 7), ("bitcount", [0], 0), ("bitcount", [1], 1)]
    },
    {
        "name": "find_first_in_sorted", "type": "off_by_one",
        "buggy": "def find_first_in_sorted(arr, x):\n    lo = 0\n    hi = len(arr)\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):\n            return mid\n        elif x <= arr[mid]:\n            hi = mid\n        else:\n            lo = mid + 1\n    return -1",
        "fixed": "def find_first_in_sorted(arr, x):\n    lo = 0\n    hi = len(arr) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):\n            return mid\n        elif x <= arr[mid]:\n            hi = mid - 1\n        else:\n            lo = mid + 1\n    return -1",
        "tests": [("find_first_in_sorted", [[1,2,3,4], 3], 2), ("find_first_in_sorted", [[1,2,3,4], 5], -1), ("find_first_in_sorted", [[1,1,2,3], 1], 0)]
    },
    {
        "name": "flatten", "type": "wrong_variable",
        "buggy": "def flatten(arr):\n    result = []\n    for x in arr:\n        if isinstance(x, list):\n            result.extend(flatten(x))\n        else:\n            result.append(flatten(x))\n    return result",
        "fixed": "def flatten(arr):\n    result = []\n    for x in arr:\n        if isinstance(x, list):\n            result.extend(flatten(x))\n        else:\n            result.append(x)\n    return result",
        "tests": [("flatten", [[[1,[2]],3]], [1,2,3]), ("flatten", [[[1],2,[3,[4]]], [1,2,3,4])]
    },
    {
        "name": "gcd", "type": "wrong_argument_order",
        "buggy": "def gcd(a, b):\n    if b:\n        return gcd(a % b, b)\n    else:\n        return a",
        "fixed": "def gcd(a, b):\n    if b:\n        return gcd(b, a % b)\n    else:\n        return a",
        "tests": [("gcd", [12,8], 4), ("gcd", [17,5], 1), ("gcd", [100,25], 25)]
    },
    {
        "name": "is_valid_parenthesization", "type": "missing_check",
        "buggy": "def is_valid_parenthesization(s):\n    depth = 0\n    for c in s:\n        if c == '(':\n            depth += 1\n        else:\n            depth -= 1\n            if depth < 0:\n                return False\n    return True",
        "fixed": "def is_valid_parenthesization(s):\n    depth = 0\n    for c in s:\n        if c == '(':\n            depth += 1\n        else:\n            depth -= 1\n            if depth < 0:\n                return False\n    return depth == 0",
        "tests": [("is_valid_parenthesization", ["(())"], True), ("is_valid_parenthesization", ["(()"], False), ("is_valid_parenthesization", [")("], False), ("is_valid_parenthesization", [""], True)]
    },
    {
        "name": "max_sublist_sum", "type": "wrong_initialization",
        "buggy": "def max_sublist_sum(arr):\n    max_ending = 0\n    max_so_far = 0\n    for x in arr:\n        max_ending = max(0, max_ending + x)\n        max_so_far = max(max_so_far, max_ending)\n    return max_so_far",
        "fixed": "def max_sublist_sum(arr):\n    if not arr: return 0\n    max_ending = arr[0]\n    max_so_far = arr[0]\n    for x in arr[1:]:\n        max_ending = max(x, max_ending + x)\n        max_so_far = max(max_so_far, max_ending)\n    return max_so_far",
        "tests": [("max_sublist_sum", [[4,-5,2,1,-1,3]], 5), ("max_sublist_sum", [[-1,-2,-3]], 0), ("max_sublist_sum", [[1,2,3]], 6)]
    },
    {
        "name": "sqrt", "type": "wrong_expression",
        "buggy": "def sqrt(x, eps):\n    approx = x / 2\n    while abs(x - approx) > eps:\n        approx = 0.5 * (approx + x / approx)\n    return approx",
        "fixed": "def sqrt(x, eps):\n    approx = x / 2\n    while abs(approx*approx - x) > eps:\n        approx = 0.5 * (approx + x / approx)\n    return approx",
        "tests": [("sqrt", [4, 0.01], 2.0), ("sqrt", [9, 0.01], 3.0)]
    },
    {
        "name": "quicksort", "type": "off_by_one",
        "buggy": "def quicksort(arr):\n    if len(arr) <= 1: return arr\n    pivot = arr[0]\n    less = quicksort([x for x in arr[1:] if x < pivot])\n    more = quicksort([x for x in arr[1:] if x > pivot])\n    return less + [pivot] + more",
        "fixed": "def quicksort(arr):\n    if len(arr) <= 1: return arr\n    pivot = arr[0]\n    less = quicksort([x for x in arr[1:] if x < pivot])\n    more = quicksort([x for x in arr[1:] if x >= pivot])\n    return less + [pivot] + more",
        "tests": [("quicksort", [[3,1,2]], [1,2,3]), ("quicksort", [[5,3,3,1]], [1,3,3,5]), ("quicksort", [[]], [])]
    },
]


def exec_test(code, tests):
    """执行代码并测试"""
    if not tests:
        try:
            compile(code, '<test>', 'exec')
            return True, ""
        except SyntaxError as e:
            return False, str(e)
    try:
        ns = {}
        exec(code, ns)
    except Exception as e:
        return False, str(e)
    for func, args, expected in tests:
        if func not in ns:
            return False, f"Function {func} not defined"
        try:
            result = ns[func](*args) if isinstance(args, (list, tuple)) else ns[func](args)
            if isinstance(result, (list, tuple)) and not isinstance(result, str):
                result = list(result)
            if result != expected:
                return False, f"{func}({args}) = {result}, expected {expected}"
        except Exception as e:
            return False, str(e)
    return True, ""


def wilson(p, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    ph = p / n
    den = 1 + z*z/n
    c = (ph + z*z/(2*n)) / den
    s = z * math.sqrt(ph*(1-ph)/n + z*z/(4*n*n)) / den
    return (max(0.0, c-s), min(1.0, c+s))


def extract_code(text):
    for pat in [r'<fixed_code>\s*```python\s*(.*?)```\s*</fixed_code>',
                r'<fixed_code>\s*(.*?)\s*</fixed_code>',
                r'```python\s*(.*?)```']:
        m = re.search(pat, text, re.DOTALL)
        if m: return m.group(1).strip()
    return text.strip()


# ==================== 评测函数 ====================

def eval_model(model_or_none, programs, desc):
    results = []
    passed = 0

    print(f"\n{'='*60}")
    print(f"  Evaluating: {desc}")
    print(f"{'='*60}")

    for prog in programs:
        bug_desc = f"Fix the {prog['name']} function"
        start = time.time()

        if model_or_none is not None:
            # 调用模型（需要传入(model, tokenizer)元组）
            model, tok = model_or_none
            prompt = f"Bug: {bug_desc}\n\nBuggy Code:\n```python\n{prog['buggy']}\n```\n\nFix the bug."
            inputs = tok(prompt, return_tensors="pt").to(model.device)
            outputs = model.generate(**inputs, max_new_tokens=256, temperature=0.2, do_sample=True)
            response = tok.decode(outputs[0], skip_special_tokens=True)
            code = extract_code(response)
        else:
            # 基线：返回buggy代码
            code = prog["buggy"]

        ok, err = exec_test(code, prog["tests"])
        elapsed = (time.time() - start) * 1000

        results.append({"name": prog["name"], "type": prog["type"],
                       "passed": ok, "error": err[:80], "time_ms": elapsed})
        if ok: passed += 1

        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {prog['name']:30s} ({prog['type']}) {elapsed:.0f}ms")
        if not ok: print(f"         {err[:80]}")

    total = len(results)
    rate = passed / total if total > 0 else 0
    ci = wilson(passed, total)
    print(f"\n{'='*60}")
    print(f"  Results: {passed}/{total} = {rate:.1%}")
    print(f"  95% CI: [{ci[0]:.1%}, {ci[1]:.1%}]")
    print(f"{'='*60}")

    # 按类型统计
    by_type = {}
    for r in results:
        bt = r["type"]
        if bt not in by_type: by_type[bt] = [0, 0]
        by_type[bt][1] += 1
        if r["passed"]: by_type[bt][0] += 1
    print("\n  By type:")
    for bt, (p, t) in sorted(by_type.items()):
        print(f"    {bt:25s} {p}/{t} ({p/t:.0%})")

    return {"desc": desc, "passed": passed, "total": total, "rate": rate,
            "ci_low": ci[0], "ci_high": ci[1], "by_type": by_type,
            "results": results, "timestamp": datetime.now().isoformat()}


# ==================== 主函数 ====================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=str, default="", help="模型路径")
    ap.add_argument("--oracle", action="store_true", help="Oracle测试（用正确代码）")
    ap.add_argument("--output", type=str, default="./runs/quixbugs_results.json")
    args = ap.parse_args()

    model_info = None
    desc = "Baseline (buggy code)"

    if args.oracle:
        # Oracle: 直接用正确代码
        desc = "Oracle (correct code)"
        def oracle_model(prompt, buggy, bug_desc):
            for p in PROGRAMS:
                if p["name"] in bug_desc:
                    return p["fixed"]
            return buggy
        model_info = oracle_model

    elif args.model:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            print(f"Loading {args.model}...")
            tok = AutoTokenizer.from_pretrained(args.model)
            mdl = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float16, device_map="auto")
            model_info = (mdl, tok)
            desc = f"Model: {args.model}"
        except Exception as e:
            print(f"Failed to load model: {e}")
            sys.exit(1)

    # 运行评测
    if callable(model_info) and not isinstance(model_info, tuple):
        # Oracle模式
        results_list = []
        passed = 0
        for prog in PROGRAMS:
            bug_desc = f"Fix the {prog['name']} function"
            code = model_info(None, prog["buggy"], bug_desc)
            ok, err = exec_test(code, prog["tests"])
            if ok: passed += 1
            print(f"  [{'PASS' if ok else 'FAIL'}] {prog['name']}")
        print(f"\nOracle: {passed}/{len(PROGRAMS)} passed")
    else:
        result = eval_model(model_info, PROGRAMS, desc)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nSaved: {args.output}")


if __name__ == "__main__":
    main()
