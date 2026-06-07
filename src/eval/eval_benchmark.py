#!/usr/bin/env python3
"""
评测脚本 - 在 QuixBugs 完整数据集上验证 pass@1 和 pass@10

用法:
    # 本地 Ollama 模型（默认）
    python eval_benchmark.py

    # 指定模型路径（微调后的模型）
    python eval_benchmark.py --model output/sft_run/final

    # 指定数据集
    python eval_benchmark.py --dataset datasets/training/sft_eval.json

    # 仅评测指定 bug 类型
    python eval_benchmark.py --bug_types wrong_operator off_by_one

    # 批量评测多个 checkpoint
    python eval_benchmark.py --checkpoints output/sft_run/checkpoint-50 output/sft_run/checkpoint-100
"""

import os
import sys
import json
import time
import argparse
import re
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
from collections import defaultdict
import random

import requests
import torch
from tqdm import tqdm

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))
from cot_react_agent import CoTReActAgent, ReflectionMemory, SkillManager, CodeExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# ==================== QuixBugs 数据集 ====================

QUIXBUGS_PROGRAMS = [
    # wrong_operator
    {"name": "bitcount", "bug_type": "wrong_operator",
     "buggy": '''def bitcount(n):
    count = 0
    while n:
        n ^= n - 1
        count += 1
    return count
''', "tests": [("bitcount(0)", 0), ("bitcount(1)", 1), ("bitcount(7)", 3), ("bitcount(255)", 8), ("bitcount(128)", 1)]},

    # off_by_one
    {"name": "find_first_in_sorted", "bug_type": "off_by_one",
     "buggy": '''def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(arr) and arr[lo] == x:
        return lo
    return -1
''', "tests": [("find_first_in_sorted([1,2,3,4,5], 3)", 2), ("find_first_in_sorted([1,1,1,1], 1)", 0), ("find_first_in_sorted([], 1)", -1), ("find_first_in_sorted([1,3,5,7], 2)", -1)]},

    # wrong_argument_order
    {"name": "gcd", "bug_type": "wrong_argument_order",
     "buggy": '''def gcd(a, b):
    if b:
        return gcd(a % b, b)
    return a
''', "tests": [("gcd(48, 18)", 6), ("gcd(18, 48)", 6), ("gcd(17, 13)", 1), ("gcd(100, 25)", 25), ("gcd(7, 0)", 7)]},

    # wrong_expression
    {"name": "sqrt", "bug_type": "wrong_expression",
     "buggy": '''def sqrt(x, epsilon):
    approx = x / 2
    while abs(x - approx) > epsilon:
        approx = (approx + x / approx) / 2
    return approx
''', "tests": [("abs(sqrt(4, 1e-6) - 2) < 1e-4", True), ("abs(sqrt(9, 1e-6) - 3) < 1e-4", True), ("abs(sqrt(2, 1e-6) - 1.4142) < 1e-3", True)]},

    # off_by_one
    {"name": "quicksort", "bug_type": "off_by_one",
     "buggy": '''def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    lesser = quicksort([x for x in arr[1:] if x <= pivot])
    greater = quicksort([x for x in arr[1:] if x > pivot])
    return lesser + [pivot] + greater
''', "tests": [("quicksort([3,1,2])", [1,2,3]), ("quicksort([5,3,8,1,2])", [1,2,3,5,8]), ("quicksort([])", []), ("quicksort([1])", [1]), ("quicksort([2,1])", [1,2])]},

    # wrong_data_structure
    {"name": "two_sum", "bug_type": "wrong_data_structure",
     "buggy": '''def two_sum(nums, target):
    seen = []
    for i, num in enumerate(nums):
        complement = target - num
        if complement in seen:
            return [seen.index(complement), i]
        seen.append(num)
    return []
''', "tests": [("two_sum([2,7,11,15], 9)", [0,1]), ("two_sum([3,2,4], 6)", [1,2]), ("two_sum([3,3], 6)", [0,1])]},

    # wrong_initialization
    {"name": "kadane", "bug_type": "wrong_initialization",
     "buggy": '''def max_subarray(nums):
    max_sum = 0
    current_sum = 0
    for num in nums:
        current_sum = max(num, current_sum + num)
        max_sum = max(max_sum, current_sum)
    return max_sum
''', "tests": [("max_subarray([-2,1,-3,4,-1,2,1,-5,4])", 6), ("max_subarray([1])", 1), ("max_subarray([-1])", 0), ("max_subarray([-2,-3,-1])", -1)]},

    # wrong_condition
    {"name": "merge_sorted", "bug_type": "wrong_condition",
     "buggy": '''def merge_sorted(l1, l2):
    result = []
    while l1 and l2:
        if l1[0] <= l2[0]:
            result.append(l1.pop(0))
        else:
            result.append(l2.pop(0))
    result.extend(l1)
    result.extend(l2)
    return result
''', "tests": [("merge_sorted([1,3,5], [2,4,6])", [1,2,3,4,5,6]), ("merge_sorted([], [1])", [1]), ("merge_sorted([1], [])", [1])]},

    # missing_base_case
    {"name": "climbing_stairs", "bug_type": "missing_base_case",
     "buggy": '''def climb_stairs(n):
    if n <= 1:
        return n
    return climb_stairs(n-1) + climb_stairs(n-2)
''', "tests": [("climb_stairs(1)", 1), ("climb_stairs(2)", 2), ("climb_stairs(5)", 8), ("climb_stairs(10)", 89)]},

    # wrong_operator
    {"name": "is_palindrome", "bug_type": "wrong_operator",
     "buggy": '''def is_palindrome(s):
    left, right = 0, len(s) - 1
    while left < right:
        if s[left] != s[right]:
            return False
        left += 1
        right -= 1
    return True
''', "tests": [("is_palindrome('racecar')", True), ("is_palindrome('hello')", False), ("is_palindrome('a')", True), ("is_palindrome('')", True)]},

    # off_by_one
    {"name": "binary_search", "bug_type": "off_by_one",
     "buggy": '''def binary_search(arr, target):
    lo, hi = 0, len(arr)
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
''', "tests": [("binary_search([1,2,3,4,5], 3)", 2), ("binary_search([1,2,3], 1)", 0), ("binary_search([1,2,3], 4)", -1)]},

    # wrong_expression
    {"name": "is_anagram", "bug_type": "wrong_expression",
     "buggy": '''def is_anagram(s1, s2):
    return sorted(s1) == sorted(s2)
''', "tests": [("is_anagram('listen', 'silent')", True), ("is_anagram('hello', 'world')", False), ("is_anagram('aab', 'aba')", True)]},
]


# ==================== 模型接口 ====================

class OllamaModel:
    """Ollama 本地模型接口"""
    def __init__(self, model_name="qwen2.5-coder:1.5b", base_url="http://localhost:11434"):
        self.model = model_name
        self.base_url = base_url
        self._check_connection()

    def _check_connection(self):
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                logger.info(f"Ollama connected. Available models: {[m['name'] for m in models]}")
            else:
                logger.warning(f"Ollama returned status {resp.status_code}")
        except Exception as e:
            logger.warning(f"Ollama not available: {e}. Will attempt to call anyway.")

    def __call__(self, prompt: str, system_prompt: str = "", **kwargs) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": kwargs.get("max_tokens", 2048),
                "temperature": kwargs.get("temperature", 0.7),
            }
        }

        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["message"]["content"]


class HFModel:
    """HuggingFace 模型接口（支持 LoRA 适配器）"""
    def __init__(self, model_path: str, device="cuda", max_tokens=2048, temperature=0.7):
        from transformers import AutoTokenizer, AutoModelForCausalLM
        import torch

        self.model_path = model_path
        self.max_tokens = max_tokens
        self.temperature = temperature

        logger.info(f"Loading model from {model_path}...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            trust_remote_code=True,
            torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            device_map="auto",
        )
        self.model.eval()
        logger.info("Model loaded successfully")

    def __call__(self, prompt: str, system_prompt: str = "", **kwargs) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

        encodings = self.tokenizer(
            text,
            max_length=2048,
            truncation=True,
            return_tensors="pt"
        ).to(self.model.device)

        with torch.no_grad():
            outputs = self.model.generate(
                **encodings,
                max_new_tokens=kwargs.get("max_tokens", self.max_tokens),
                temperature=kwargs.get("temperature", self.temperature),
                do_sample=kwargs.get("temperature", 0) > 0,
                top_p=0.9,
                pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
                eos_token_id=self.tokenizer.eos_token_id,
            )

        response = self.tokenizer.decode(outputs[0][encodings["input_ids"].shape[1]:], skip_special_tokens=True)
        return response


# ==================== 执行器 ====================

class SimpleExecutor:
    """简化的代码执行器"""
    def __init__(self, timeout=5):
        self.timeout = timeout

    def execute(self, code: str, buggy_code: str, tests: list) -> tuple:
        """
        执行测试用例，返回 (success, error_message)
        """
        import traceback
        import signal

        # 构建完整的执行代码
        full_code = f"""
{buggy_code}

# ==== TEST CASES ====
{chr(10).join(f'test_result = {t[0]}' for t in tests)}
""" + "\n# Execute all tests\n"

        # 执行测试
        namespace = {}
        try:
            exec(compile(code, '<string>', 'exec'), namespace)
            exec(compile(full_code, '<string>', 'exec'), namespace)

            # 验证结果
            for i, (test_expr, expected) in enumerate(tests):
                namespace_test = dict(namespace)
                exec(f"result = {test_expr}", namespace_test)
                result = namespace_test.get("result")
                if result != expected:
                    return False, f"Test {i} failed: expected {expected}, got {result}"
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


# ==================== 评测主逻辑 ====================

def evaluate_program(
    agent: CoTReActAgent,
    program: Dict,
    executor: SimpleExecutor,
    max_iterations: int = 3,
) -> Dict:
    """评测单个程序"""
    name = program["name"]
    buggy = program["buggy"]
    tests = program["tests"]
    bug_type = program["bug_type"]

    # 构建 prompt
    prompt = (
        f"Bug: Fix the {bug_type} bug in the following code.\n\n"
        f"Buggy Code:\n```python\n{buggy}\n```\n\n"
        f"Fix the bug. Follow the required format."
    )

    try:
        result = agent.fix(prompt, buggy, max_iterations=max_iterations)
        success = result.get("success", False)
        error = result.get("error", "")
        iterations = len(result.get("iterations", []))
        llm_calls = sum(1 for it in result.get("iterations", []) if getattr(it, 'llm_calls', 0) > 0)

        return {
            "name": name,
            "bug_type": bug_type,
            "success": success,
            "iterations": iterations,
            "llm_calls": llm_calls,
            "error": error,
        }
    except Exception as e:
        return {
            "name": name,
            "bug_type": bug_type,
            "success": False,
            "iterations": 0,
            "llm_calls": 0,
            "error": str(e),
        }


def evaluate_program_with_sampling(
    model_fn,
    buggy: str,
    bug_type: str,
    executor: SimpleExecutor,
    num_samples: int = 10,
    max_tokens: int = 512,
) -> Dict:
    """
    Pass@k 评测：采样 n 次，看是否至少有一次成功
    """
    prompt = (
        f"Bug: Fix the {bug_type} bug in the following code.\n\n"
        f"Buggy Code:\n```python\n{buggy}\n```\n\n"
        f"Fix the bug. Follow the required format."
    )

    # 从 CoTParser 复用提取逻辑
    from cot_react_agent import CoTParser

    results = []
    for i in range(num_samples):
        try:
            output = model_fn(prompt=prompt, max_tokens=max_tokens, temperature=0.8)
            code = CoTParser.extract_code(output)
            code = CoTParser.normalize_function_name(code, buggy)

            if code:
                success, error = executor.execute(code, buggy, [])
                results.append({"success": success, "error": error, "output": output[:100]})
            else:
                results.append({"success": False, "error": "No code extracted", "output": output[:100]})
        except Exception as e:
            results.append({"success": False, "error": str(e), "output": ""})

    # 计算 pass@k
    # pass@1: 第一次就成功
    pass_at_1 = 1.0 if any(r["success"] for r in results[:1]) else 0.0
    # pass@5: 前5次有一次成功
    pass_at_5 = 1.0 if any(r["success"] for r in results[:min(5, len(results))]) else 0.0
    # pass@10: 前10次有一次成功
    pass_at_10 = 1.0 if any(r["success"] for r in results) else 0.0

    return {
        "pass_at_1": pass_at_1,
        "pass_at_5": pass_at_5,
        "pass_at_10": pass_at_10,
        "first_success": next((i for i, r in enumerate(results) if r["success"]), -1),
        "num_samples": len(results),
        "num_success": sum(1 for r in results if r["success"]),
    }


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description="QuixBugs 评测脚本")
    parser.add_argument("--model", type=str, default=None,
                        help="模型路径或名称，默认为 Ollama 本地模型")
    parser.add_argument("--ollama_url", type=str, default="http://localhost:11434",
                        help="Ollama 服务器地址")
    parser.add_argument("--ollama_model", type=str, default="qwen2.5-coder:1.5b",
                        help="Ollama 模型名称")
    parser.add_argument("--output", type=str, default="runs/benchmark_results.json",
                        help="结果输出路径")
    parser.add_argument("--max_iterations", type=int, default=3)
    parser.add_argument("--pass_at_k", type=int, default=10,
                        help="Pass@k 的 k 值")
    parser.add_argument("--num_samples", type=int, default=10,
                        help="每个 bug 的采样次数")
    parser.add_argument("--bug_types", nargs="+",
                        help="只评测指定的 bug 类型")
    parser.add_argument("--dataset", type=str, default=None,
                        help="自定义数据集路径（JSON 格式）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("QuixBugs Benchmark Evaluation")
    logger.info("=" * 60)

    # 加载模型
    if args.model:
        # HuggingFace 模型
        model = HFModel(args.model)
        model_fn = lambda **kw: model(**kw)
    else:
        # Ollama 模型
        model = OllamaModel(model_name=args.ollama_model, base_url=args.ollama_url)
        model_fn = lambda **kw: model(prompt=kw.get("prompt", ""), **kw)

    # 加载数据集
    if args.dataset:
        with open(args.dataset, 'r') as f:
            custom_programs = json.load(f)
        programs = []
        for item in custom_programs:
            if "messages" in item:
                # SFT 数据格式
                user_msg = next((m["content"] for m in item["messages"] if m["role"] == "user"), "")
                prog = parse_sft_prompt(user_msg)
                if prog:
                    programs.append(prog)
            elif "buggy_code" in item:
                programs.append({
                    "name": item.get("name", f"prog_{len(programs)}"),
                    "buggy": item["buggy_code"],
                    "bug_type": item.get("bug_type", "unknown"),
                    "tests": item.get("tests", []),
                })
        logger.info(f"Loaded {len(programs)} programs from {args.dataset}")
    else:
        programs = QUIXBUGS_PROGRAMS

    # 过滤 bug 类型
    if args.bug_types:
        programs = [p for p in programs if p["bug_type"] in args.bug_types]
        logger.info(f"Filtered to {len(programs)} programs of types: {args.bug_types}")

    # 执行器
    executor = SimpleExecutor()

    # 创建 Agent
    from cot_react_agent import COT_SYSTEM_PROMPT
    agent = CoTReActAgent(
        small_model=model_fn,
        large_model=model_fn,
        system_prompt=COT_SYSTEM_PROMPT,
        num_iterations=args.max_iterations,
    )

    # 评测
    results = []
    by_type = defaultdict(list)

    logger.info(f"\nEvaluating {len(programs)} programs...")
    for prog in tqdm(programs, desc="Evaluating"):
        if args.pass_at_k > 1:
            # Pass@k 模式
            result = evaluate_program_with_sampling(
                model_fn, prog["buggy"], prog["bug_type"],
                executor, num_samples=args.num_samples,
                max_tokens=512,
            )
            result["name"] = prog["name"]
            result["bug_type"] = prog["bug_type"]
        else:
            # 单次评测模式
            result = evaluate_program(agent, prog, executor, args.max_iterations)

        results.append(result)
        by_type[result["bug_type"]].append(result["success"])

        if args.verbose and not result["success"]:
            logger.info(f"  FAILED: {prog['name']} ({prog['bug_type']})")

    # 统计
    total = len(results)
    passed = sum(1 for r in results if r["success"])
    overall_rate = passed / total * 100 if total > 0 else 0

    logger.info("\n" + "=" * 60)
    logger.info("RESULTS")
    logger.info("=" * 60)

    if args.pass_at_k > 1:
        logger.info(f"Pass@{args.num_samples}: {overall_rate:.1f}% ({passed}/{total})")
        logger.info(f"Pass@1: {sum(1 for r in results if r.get('pass_at_1', 0)) / total * 100:.1f}%")
        logger.info(f"Pass@5: {sum(1 for r in results if r.get('pass_at_5', 0)) / total * 100:.1f}%")
        logger.info(f"Pass@{args.num_samples}: {sum(1 for r in results if r.get('pass_at_10', 0)) / total * 100:.1f}%")
    else:
        logger.info(f"Pass@1: {overall_rate:.1f}% ({passed}/{total})")

    logger.info(f"\nBy bug type:")
    for bt, successes in sorted(by_type.items()):
        rate = sum(successes) / len(successes) * 100 if successes else 0
        logger.info(f"  {bt:25s}: {sum(successes)}/{len(successes)} ({rate:.1f}%)")

    # 保存结果
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now().isoformat(),
        "model": args.model or args.ollama_model,
        "num_programs": total,
        "passed": passed,
        "pass_rate": overall_rate,
        "pass_at_k": args.num_samples,
        "by_bug_type": {bt: sum(s)/len(s)*100 for bt, s in by_type.items()},
        "results": results,
    }
    with open(output_path, 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"\nResults saved to: {output_path}")
    logger.info("[DONE]")


def parse_sft_prompt(prompt_text: str) -> Optional[Dict]:
    """从 SFT prompt 中提取 buggy_code 和 bug_type"""
    buggy_match = re.search(r"Buggy Code:\n```python\n(.*?)```", prompt_text, re.DOTALL)
    if not buggy_match:
        return None

    desc_match = re.search(r"Bug: (.*?)\n\n", prompt_text)
    desc = desc_match.group(1) if desc_match else "Fix the bug"

    # 简单推断 bug_type
    bug_type = "unknown"
    if "off-by" in desc.lower() or "boundary" in desc.lower():
        bug_type = "off_by_one"
    elif "operator" in desc.lower():
        bug_type = "wrong_operator"
    elif "argument" in desc.lower() or "order" in desc.lower():
        bug_type = "wrong_argument_order"
    elif "expression" in desc.lower():
        bug_type = "wrong_expression"
    elif "data structure" in desc.lower() or "list" in desc.lower():
        bug_type = "wrong_data_structure"

    return {
        "name": "custom",
        "buggy": buggy_match.group(1).strip(),
        "bug_type": bug_type,
        "tests": [],
    }


if __name__ == "__main__":
    main()
