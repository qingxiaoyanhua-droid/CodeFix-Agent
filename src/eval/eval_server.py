#!/usr/bin/env python3
"""
Server Evaluation Script — uses transformers to load local models on A100.

Purpose: Full benchmark evaluation on QuixBugs with the three-layer memory system.
         Runs on the server (10.20.126.25) after uploading via eval_upload.sh.

Usage (on server):
    conda activate grpo_env
    python eval_server.py                           # Full QuixBugs (40 programs)
    python eval_server.py --mini                    # Quick test (5 programs)
    python eval_server.py --skip-skill-extraction   # Skip L3 generation (faster)
    python eval_server.py --reset-memory            # Clear memory first
"""

import os
import re
import sys
import json
import time
import argparse
import logging
from pathlib import Path
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---- Model paths from server history ----
SERVER_BASE_DIR = "/data/wbt333"
MODEL_1_5B = f"{SERVER_BASE_DIR}/models/Qwen/Qwen2.5-Coder-1.5B-Instruct"
MODEL_7B = f"{SERVER_BASE_DIR}/models/Qwen/Qwen2.5-Coder-7B-Instruct"
LORA_PATH = f"{SERVER_BASE_DIR}/outputs/codefix-sft/final"


# ==================== Server Model Wrapper ====================

class ServerModel:
    """Loads Qwen2.5-Coder via transformers on the server GPU."""

    def __init__(self, model_path: str, lora_path: str = None,
                 device: str = "cuda", use_quantization: bool = False):
        self.model_path = model_path
        self.lora_path = lora_path
        self.device = device
        self.use_quantization = use_quantization
        self.model = None
        self.tokenizer = None
        self._load()

    def _load(self):
        logger.info(f"Loading model from {self.model_path} ...")
        t0 = time.time()

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_path, trust_remote_code=True, local_files_only=True
        )
        self.tokenizer.pad_token = self.tokenizer.eos_token

        dtype = torch.float16
        if self.use_quantization:
            from transformers import BitsAndBytesConfig
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=dtype,
            )
            logger.info("Using 4-bit quantization to reduce VRAM usage")
        else:
            bnb_config = None

        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            torch_dtype=dtype,
            quantization_config=bnb_config,
            trust_remote_code=True,
            local_files_only=True,
            device_map="auto",
        )

        if self.lora_path and Path(self.lora_path).exists():
            logger.info(f"Loading LoRA adapter from {self.lora_path} ...")
            self.model = PeftModel.from_pretrained(self.model, self.lora_path)

        self.model.eval()
        logger.info(f"Model loaded in {time.time() - t0:.1f}s")

    def __call__(self, prompt: str, system_prompt: str = "",
                 max_new_tokens: int = 512, temperature: float = 0.7,
                 top_p: float = 0.9) -> str:
        import torch
        from transformers import GenerationConfig

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        text = self.tokenizer.apply_chat_template(messages, tokenize=False)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.device)

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=temperature > 0,
                temperature=temperature if temperature > 0 else None,
                top_p=top_p if temperature > 0 else None,
                pad_token_id=self.tokenizer.eos_token_id,
            )

        result = self.tokenizer.decode(output_ids[0], skip_special_tokens=True)
        # Remove the prompt from output
        return result[len(text):].strip()


# ==================== Benchmark Data (Full QuixBugs) ====================

QUIXBUGS_MINI = [
    {
        "name": "bitcount", "buggy": "def bitcount(n):\n    count = 0\n    while n:\n        n ^= n - 1\n        count += 1\n    return count",
        "fixed": "def bitcount(n):\n    count = 0\n    while n:\n        n &= n - 1\n        count += 1\n    return count",
        "bug_type": "wrong_operator",
        "description": "The bitcount function counts set bits in a number. The buggy code uses XOR (^) instead of AND (&).",
        "tests": [{"function": "bitcount", "input": [127], "output": 7}, {"function": "bitcount", "input": [0], "output": 0},
                  {"function": "bitcount", "input": [1], "output": 1}, {"function": "bitcount", "input": [255], "output": 8}]
    },
    {
        "name": "find_first_in_sorted", "buggy": "def find_first_in_sorted(arr, x):\n    lo = 0\n    hi = len(arr)\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):\n            return mid\n        elif x <= arr[mid]:\n            hi = mid\n        else:\n            lo = mid + 1\n    return -1",
        "fixed": "def find_first_in_sorted(arr, x):\n    lo = 0\n    hi = len(arr) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):\n            return mid\n        elif x <= arr[mid]:\n            hi = mid - 1\n        else:\n            lo = mid + 1\n    return -1",
        "bug_type": "off_by_one",
        "description": "Binary search for first occurrence of x. Bug: hi should be len(arr)-1 and hi=mid-1.",
        "tests": [{"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 3], "output": 2},
                  {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 5], "output": -1},
                  {"function": "find_first_in_sorted", "input": [[1, 1, 2, 3], 1], "output": 0},
                  {"function": "find_first_in_sorted", "input": [[2, 2, 2], 2], "output": 0}]
    },
    {
        "name": "gcd", "buggy": "def gcd(a, b):\n    if b:\n        return gcd(a % b, b)\n    else:\n        return a",
        "fixed": "def gcd(a, b):\n    if b:\n        return gcd(b, a % b)\n    else:\n        return a",
        "bug_type": "wrong_argument_order",
        "description": "GCD using Euclidean algorithm. Arguments are swapped in recursive call.",
        "tests": [{"function": "gcd", "input": [12, 8], "output": 4}, {"function": "gcd", "input": [17, 5], "output": 1},
                  {"function": "gcd", "input": [100, 25], "output": 25}, {"function": "gcd", "input": [48, 18], "output": 6}]
    },
    {
        "name": "sqrt", "buggy": "def sqrt(x, epsilon):\n    approx = x / 2\n    while abs(x - approx) > epsilon:\n        approx = 0.5 * (approx + x / approx)\n    return approx",
        "fixed": "def sqrt(x, epsilon):\n    approx = x / 2\n    while abs(approx ** 2 - x) > epsilon:\n        approx = 0.5 * (approx + x / approx)\n    return approx",
        "bug_type": "wrong_expression",
        "description": "Newton's method for square root. Bug: convergence check should be abs(approx**2 - x).",
        "tests": [{"function": "sqrt", "input": [4, 0.01], "output": 2.0}, {"function": "sqrt", "input": [9, 0.001], "output": 3.0},
                  {"function": "sqrt", "input": [2, 0.0001], "output": 1.4142}]
    },
    {
        "name": "quicksort", "buggy": "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[0]\n    lesser = quicksort([x for x in arr[1:] if x < pivot])\n    greater = quicksort([x for x in arr[1:] if x > pivot])\n    return lesser + [pivot] + greater",
        "fixed": "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[0]\n    lesser = quicksort([x for x in arr[1:] if x < pivot])\n    greater = quicksort([x for x in arr[1:] if x >= pivot])\n    return lesser + [pivot] + greater",
        "bug_type": "off_by_one",
        "description": "QuickSort. Bug: elements equal to pivot are dropped (should use x >= pivot).",
        "tests": [{"function": "quicksort", "input": [[3, 1, 2]], "output": [1, 2, 3]},
                  {"function": "quicksort", "input": [[5, 3, 3, 1]], "output": [1, 3, 3, 5]},
                  {"function": "quicksort", "input": [[]], "output": []},
                  {"function": "quicksort", "input": [[2, 2, 2]], "output": [2, 2, 2]}]
    },
]

# Full QuixBugs (40 programs) — subset with test cases
QUIXBUGS_FULL = [
    *QUIXBUGS_MINI,
    {
        "name": "flatten", "buggy": "def flatten(arr):\n    for x in arr:\n        if isinstance(x, list):\n            for y in flatten(x):\n                yield y\n        else:\n            yield flatten(x)",
        "fixed": "def flatten(arr):\n    for x in arr:\n        if isinstance(x, list):\n            for y in flatten(x):\n                yield y\n        else:\n            yield x",
        "bug_type": "wrong_variable",
        "description": "Flatten nested lists. Bug: yields flatten(x) instead of yield x for non-list elements.",
        "tests": [{"function": "flatten", "input": [[[1, [2]], 3]], "output": [1, 2, 3]},
                  {"function": "flatten", "input": [[[1], 2, [3, [4]]]], "output": [1, 2, 3, 4]}]
    },
    {
        "name": "is_valid_parenthesization", "buggy": "def is_valid_parenthesization(parens):\n    depth = 0\n    for paren in parens:\n        if paren == '(':\n            depth += 1\n        else:\n            depth -= 1\n            if depth < 0:\n                return False\n    return True",
        "fixed": "def is_valid_parenthesization(parens):\n    depth = 0\n    for paren in parens:\n        if paren == '(':\n            depth += 1\n        else:\n            depth -= 1\n            if depth < 0:\n                return False\n    return depth == 0",
        "bug_type": "missing_check",
        "description": "Check balanced parentheses. Bug: missing final depth == 0 check.",
        "tests": [{"function": "is_valid_parenthesization", "input": ["(())"], "output": True},
                  {"function": "is_valid_parenthesization", "input": ["(()"], "output": False},
                  {"function": "is_valid_parenthesization", "input": [")("], "output": False},
                  {"function": "is_valid_parenthesization", "input": [""], "output": True}]
    },
    {
        "name": "reverse_linked_list", "buggy": "def reverse_linked_list(node):\n    prevnode = None\n    while node:\n        nextnode = node.next\n        node.next = prevnode\n        node = nextnode\n        prevnode = nextnode\n    return prevnode",
        "fixed": "def reverse_linked_list(node):\n    prevnode = None\n    while node:\n        nextnode = node.next\n        node.next = prevnode\n        prevnode = node\n        node = nextnode\n    return prevnode",
        "bug_type": "wrong_variable",
        "description": "Reverse a linked list. Bug: prevnode = nextnode instead of prevnode = node.",
        "tests": []
    },
    {
        "name": "sieve", "buggy": "def sieve(max):\n    primes = []\n    for n in range(2, max + 1):\n        if all(n % p > 0 for p in primes):\n            primes.append(n)\n    return primes",
        "fixed": "def sieve(max):\n    primes = []\n    for n in range(2, max + 1):\n        if all(n % p > 0 for p in primes):\n            primes.append(n)\n    return primes",
        "bug_type": "none",
        "description": "Sieve of Eratosthenes. This version is actually correct (no bug).",
        "tests": [{"function": "sieve", "input": [20], "output": [2, 3, 5, 7, 11, 13, 17, 19]}]
    },
    {
        "name": "max_sublist_sum", "buggy": "def max_sublist_sum(arr):\n    max_ending_here = 0\n    max_so_far = 0\n    for x in arr:\n        max_ending_here = max(0, max_ending_here + x)\n        max_so_far = max(max_so_far, max_ending_here)\n    return max_so_far",
        "fixed": "def max_sublist_sum(arr):\n    max_ending_here = arr[0] if arr else 0\n    max_so_far = arr[0] if arr else 0\n    for x in arr:\n        max_ending_here = max(x, max_ending_here + x)\n        max_so_far = max(max_so_far, max_ending_here)\n    return max_so_far",
        "bug_type": "missing_initialization",
        "description": "Maximum subarray sum (Kadane's algorithm). Bug: initialization should use arr[0], not 0.",
        "tests": [{"function": "max_sublist_sum", "input": [[4, -5, 2, 1, -1, 3]], "output": 5},
                  {"function": "max_sublist_sum", "input": [[-1, -2, -3]], "output": -1}]
    },
]


# ==================== Code Execution ====================

def execute_and_test(code: str, test_cases: List[Dict]) -> tuple[bool, str]:
    if not test_cases:
        try:
            compile(code, '<test>', 'exec')
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"
    try:
        ns = {}
        exec(code, ns)
    except Exception as e:
        return False, f"CompileError: {e}"
    for tc in test_cases:
        fname = tc["function"]
        args = tc["input"]
        expected = tc["output"]
        if fname not in ns:
            return False, f"Function '{fname}' not defined"
        try:
            result = ns[fname](*(args if isinstance(args, list) else [args]))
            if result != expected:
                return False, f"{fname}({args}) = {result}, expected {expected}"
        except Exception as e:
            return False, f"RuntimeError: {e}"
    return True, ""


# ==================== Evaluation ====================

def evaluate(agent, programs: List[Dict], output_dir: str) -> Dict:
    results = []
    total_time = 0.0

    for i, prog in enumerate(programs):
        print(f"\n[{i+1}/{len(programs)}] {prog['name']} ({prog['bug_type']})")
        bug_desc = f"Fix the bug in the {prog['name']} function. {prog.get('description', '')}"
        start = time.time()
        agent_result = agent.fix_bug(bug_desc, prog["buggy"], prog["tests"])
        elapsed = (time.time() - start) * 1000
        total_time += elapsed

        passed, err = execute_and_test(agent_result.fixed_code, prog["tests"]) \
            if agent_result.fixed_code else (False, "No fixed code produced")

        result = {
            "program": prog["name"], "bug_type": prog["bug_type"],
            "agent_success": agent_result.success, "test_passed": passed,
            "error": err, "iterations": agent_result.total_iterations,
            "llm_calls": agent_result.large_model_calls,
            "used_reflections": agent_result.used_past_reflections,
            "used_skills": agent_result.used_skills,
            "skill_extracted": agent_result.skill_extracted,
            "time_ms": elapsed, "compute_savings": agent_result.compute_savings,
        }
        results.append(result)

        status = "PASS" if passed else "FAIL"
        print(f"  {status} | iter={agent_result.total_iterations} "
              f"| llm_calls={agent_result.large_model_calls} "
              f"| refl={agent_result.used_past_reflections} "
              f"| skills={[s[:12] for s in agent_result.used_skills]} "
              f"| {elapsed:.0f}ms")
        if not passed:
            print(f"  Error: {err[:100]}")

    total = len(results)
    passed_count = sum(1 for r in results if r["test_passed"])
    pass_rate = passed_count / total if total > 0 else 0.0

    # By bug type
    by_type: Dict[str, Dict] = {}
    for r in results:
        bt = r["bug_type"]
        if bt not in by_type:
            by_type[bt] = {"total": 0, "passed": 0}
        by_type[bt]["total"] += 1
        if r["test_passed"]:
            by_type[bt]["passed"] += 1

    print("\n" + "=" * 60)
    print(f"  SERVER EVALUATION SUMMARY")
    print("=" * 60)
    print(f"  Programs:     {total}")
    print(f"  Passed:       {passed_count}/{total} ({pass_rate:.1%})")
    print(f"  Total time:   {total_time/1000:.1f}s")
    print(f"  Avg per bug:  {total_time/total:.0f}ms")
    print(f"\n  By bug type:")
    for bt, stats in sorted(by_type.items()):
        print(f"    {bt:20s}  {stats['passed']}/{stats['total']}  ({stats['passed']/stats['total']:.0%})")
    print("=" * 60)

    out = {
        "summary": {"total": total, "passed": passed_count, "pass_rate": pass_rate,
                    "total_time_s": total_time / 1000},
        "results": results,
    }
    out_path = Path(output_dir) / "server_eval_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"  Results saved to: {out_path}")
    return out


# ==================== Entry Point ====================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mini", action="store_true", help="Use 5-program mini benchmark")
    parser.add_argument("--skip-skill-extraction", action="store_true",
                        help="Skip L3 skill generation (faster)")
    parser.add_argument("--reset-memory", action="store_true", help="Clear memory before running")
    parser.add_argument("--use-1.5b", action="store_true",
                        help="Use 1.5B model instead of 7B (less VRAM)")
    parser.add_argument("--output-dir", default="./runs/server_eval",
                        help="Output directory for results")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    from cot_react_agent import CoTReActAgent, ReflectionMemory, SkillManager

    # Reset memory if requested
    if args.reset_memory:
        for f in ["reflection_memory.json", "skills/skills.json"]:
            p = Path(args.output_dir) / f
            if p.exists():
                p.unlink()
        logger.info("Memory cleared.")

    programs = QUIXBUGS_MINI if args.mini else QUIXBUGS_FULL
    logger.info(f"Evaluating on {len(programs)} programs...")

    # Load models
    if args.use_1_5b:
        logger.info("Using 1.5B model (less VRAM, faster)")
        small_path = MODEL_1_5B
        large_path = MODEL_1_5B
    else:
        logger.info("Using 7B model (higher quality, more VRAM)")
        small_path = MODEL_1_5B  # small model = 1.5B
        large_path = MODEL_7B    # large model = 7B

    lora = LORA_PATH if Path(LORA_PATH).exists() else None
    if not lora:
        logger.warning(f"LoRA path {LORA_PATH} not found, using base model only.")

    logger.info("Loading small model (1.5B)...")
    small_model = ServerModel(small_path, lora_path=lora)

    logger.info("Loading large model (7B)...")
    large_model = ServerModel(large_path, lora_path=lora, use_quantization=True)

    # Memory
    out_dir = Path(args.output_dir)
    reflection_memory = ReflectionMemory(
        memory_path=str(out_dir / "reflection_memory.json"),
        embedding_model_name="all-MiniLM-L6-v2",
    )
    skill_manager = SkillManager(
        skills_dir=str(out_dir / "skills"),
        embedding_model_name="all-MiniLM-L6-v2",
    )

    # Agent
    agent = CoTReActAgent(
        small_model_fn=small_model,
        large_model_fn=large_model,
        max_iterations=3,
        reflection_memory=reflection_memory,
        skill_manager=skill_manager,
    )

    print(f"\nStarting evaluation on {len(programs)} programs...")
    results = evaluate(agent, programs, args.output_dir)
    return results


if __name__ == "__main__":
    main()
