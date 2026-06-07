#!/usr/bin/env python3
"""
Local Evaluation Script — uses Ollama API for quick validation.

Purpose: Verify the three-layer memory system (Reflection + Skill + Embedding)
         works correctly before uploading to the server.

Requirements:
    pip install requests sentence-transformers
    ollama pull qwen2.5-coder:1.5b

Usage:
    python eval_local.py                    # Quick test (5 programs)
    python eval_local.py --full             # Full QuixBugs (40 programs)
    python eval_local.py --no-embedding     # Skip sentence-transformers
    python eval_local.py --reset-memory     # Clear reflection/skill memory first
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

# ---- Ollama API ----
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---- Project imports ----
sys.path.insert(0, str(Path(__file__).parent))
from cot_react_agent import (
    CoTReActAgent, ReflectionMemory, SkillManager,
    Reflection, Skill,
)
from enhanced_agent import BugFixRetriever

# ==================== Ollama Model Wrapper ====================

class OllamaModel:
    """Wrapper around Ollama REST API — no GPU needed locally."""

    def __init__(self, model_name: str = "qwen2.5-coder:1.5b",
                 base_url: str = "http://localhost:11434",
                 timeout: int = 120):
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._verify_connection()

    def _verify_connection(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            if self.model_name not in models:
                logger.warning(
                    f"Model '{self.model_name}' not found in Ollama. "
                    f"Available: {models}. Run: ollama pull {self.model_name}"
                )
            else:
                logger.info(f"Ollama connected, model '{self.model_name}' available.")
        except Exception as e:
            logger.error(f"Cannot connect to Ollama at {self.base_url}: {e}")
            raise

    def __call__(self, prompt: str, system_prompt: str = "") -> str:
        """Single chat completion via Ollama API."""
        payload = {
            "model": self.model_name,
            "messages": [
                *( [{"role": "system", "content": system_prompt}] if system_prompt else [] ),
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": 0.7,
                "top_p": 0.9,
                "num_predict": 4096,
            }
        }
        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except requests.exceptions.Timeout:
            logger.warning("Ollama request timed out, returning empty string.")
            return ""
        except Exception as e:
            logger.error(f"Ollama API error: {e}")
            return ""


# ==================== Benchmark Data (QuixBugs subset) ====================

QUIXBUGS_MINI = [
    {
        "name": "bitcount",
        "buggy": "def bitcount(n):\n    count = 0\n    while n:\n        n ^= n - 1\n        count += 1\n    return count",
        "fixed": "def bitcount(n):\n    count = 0\n    while n:\n        n &= n - 1\n        count += 1\n    return count",
        "bug_type": "wrong_operator",
        "description": "The bitcount function counts set bits in a number. The buggy code uses XOR (^) instead of AND (&).",
        "tests": [
            {"function": "bitcount", "input": [127], "output": 7},
            {"function": "bitcount", "input": [0], "output": 0},
            {"function": "bitcount", "input": [1], "output": 1},
            {"function": "bitcount", "input": [255], "output": 8},
        ]
    },
    {
        "name": "find_first_in_sorted",
        "buggy": "def find_first_in_sorted(arr, x):\n    lo = 0\n    hi = len(arr)\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):\n            return mid\n        elif x <= arr[mid]:\n            hi = mid\n        else:\n            lo = mid + 1\n    return -1",
        "fixed": "def find_first_in_sorted(arr, x):\n    lo = 0\n    hi = len(arr) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):\n            return mid\n        elif x <= arr[mid]:\n            hi = mid - 1\n        else:\n            lo = mid + 1\n    return -1",
        "bug_type": "off_by_one",
        "description": "Binary search for first occurrence of x in sorted array. Bug: hi should be len(arr)-1, and hi=mid-1 instead of hi=mid.",
        "tests": [
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 3], "output": 2},
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 5], "output": -1},
            {"function": "find_first_in_sorted", "input": [[1, 1, 2, 3], 1], "output": 0},
            {"function": "find_first_in_sorted", "input": [[2, 2, 2], 2], "output": 0},
        ]
    },
    {
        "name": "gcd",
        "buggy": "def gcd(a, b):\n    if b:\n        return gcd(a % b, b)\n    else:\n        return a",
        "fixed": "def gcd(a, b):\n    if b:\n        return gcd(b, a % b)\n    else:\n        return a",
        "bug_type": "wrong_argument_order",
        "description": "Greatest common divisor using Euclidean algorithm. Arguments are swapped in recursive call.",
        "tests": [
            {"function": "gcd", "input": [12, 8], "output": 4},
            {"function": "gcd", "input": [17, 5], "output": 1},
            {"function": "gcd", "input": [100, 25], "output": 25},
            {"function": "gcd", "input": [48, 18], "output": 6},
        ]
    },
    {
        "name": "sqrt",
        "buggy": "def sqrt(x, epsilon):\n    approx = x / 2\n    while abs(x - approx) > epsilon:\n        approx = 0.5 * (approx + x / approx)\n    return approx",
        "fixed": "def sqrt(x, epsilon):\n    approx = x / 2\n    while abs(approx ** 2 - x) > epsilon:\n        approx = 0.5 * (approx + x / approx)\n    return approx",
        "bug_type": "wrong_expression",
        "description": "Newton's method for square root. Bug: convergence check should be abs(approx**2 - x), not abs(x - approx).",
        "tests": [
            {"function": "sqrt", "input": [4, 0.01], "output": 2.0},
            {"function": "sqrt", "input": [9, 0.001], "output": 3.0},
            {"function": "sqrt", "input": [2, 0.0001], "output": 1.4142},
        ]
    },
    {
        "name": "quicksort",
        "buggy": "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[0]\n    lesser = quicksort([x for x in arr[1:] if x < pivot])\n    greater = quicksort([x for x in arr[1:] if x > pivot])\n    return lesser + [pivot] + greater",
        "fixed": "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[0]\n    lesser = quicksort([x for x in arr[1:] if x < pivot])\n    greater = quicksort([x for x in arr[1:] if x >= pivot])\n    return lesser + [pivot] + greater",
        "bug_type": "off_by_one",
        "description": "QuickSort partitioning. Bug: elements equal to pivot are dropped (should use x >= pivot for the greater partition).",
        "tests": [
            {"function": "quicksort", "input": [[3, 1, 2]], "output": [1, 2, 3]},
            {"function": "quicksort", "input": [[5, 3, 3, 1]], "output": [1, 3, 3, 5]},
            {"function": "quicksort", "input": [[]], "output": []},
            {"function": "quicksort", "input": [[2, 2, 2]], "output": [2, 2, 2]},
        ]
    },
]

# ==================== Code Execution ====================

def execute_and_test(code: str, test_cases: List[Dict]) -> tuple[bool, str]:
    """Execute code and run test cases."""
    try:
        ns = {}
        exec(code, ns)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    except Exception as e:
        return False, f"CompileError: {e}"

    for tc in test_cases:
        fname = tc["function"]
        args = tc["input"]
        expected = tc["output"]
        if fname not in ns:
            return False, f"Function '{fname}' not defined"
        try:
            result = ns[fname](*args) if isinstance(args, list) else ns[fname](args)
            if result != expected:
                return False, f"{fname}({args}) = {result}, expected {expected}"
        except Exception as e:
            return False, f"RuntimeError: {e}"
    return True, ""


# ==================== Three-Layer Memory Status Reporter ====================

def report_memory_stats(rmem: ReflectionMemory, sman: SkillManager):
    """Print current state of L2 and L3 memory."""
    print("\n" + "=" * 60)
    print("  THREE-LAYER MEMORY STATUS")
    print("=" * 60)
    print(f"  L2 ReflectionMemory: {rmem.size} entries")
    for r in rmem.reflections:
        print(f"    [{r.buggy_code_hash}] {r.bug_pattern}")
        print(f"      usefulness={r.usefulness_score:.0%}  "
              f"verified={r.verification_count}x  "
              f"helpful={r.helpful_count}x")

    print(f"\n  L3 SkillManager: {sman.active_count} active, {sman.archived_count} archived")
    for s in sman.skills:
        status = "ARCHIVED" if s.is_stale else "active"
        print(f"    [{status}] {s.name} (v{s.version})")
        print(f"      usefulness={s.usefulness:.0%}  "
              f"success={s.success_count}  fail={s.failure_count}")
    print("=" * 60 + "\n")


# ==================== Main Evaluation ====================

def evaluate(
    agent: CoTReActAgent,
    programs: List[Dict],
    memory_log_path: str = "./runs/local_eval_memory.json",
) -> Dict:
    """
    Evaluate the agent on a list of programs.
    Returns a structured result dict for analysis.
    """
    results = []
    total_time = 0.0

    for i, prog in enumerate(programs):
        print(f"\n[{i+1}/{len(programs)}] Evaluating: {prog['name']} ({prog['bug_type']})")
        print(f"  Buggy code ({len(prog['buggy'].splitlines())} lines):")
        for line in prog["buggy"].splitlines()[:3]:
            print(f"    {line}")
        print("  ...")

        bug_desc = f"Fix the bug in the {prog['name']} function. {prog.get('description', '')}"
        buggy_code = prog["buggy"]
        test_cases = prog["tests"]

        start = time.time()
        agent_result = agent.fix_bug(bug_desc, buggy_code, test_cases)
        elapsed = (time.time() - start) * 1000
        total_time += elapsed

        # Execute the generated fix against ground-truth tests
        if agent_result.fixed_code:
            passed, err = execute_and_test(agent_result.fixed_code, test_cases)
        else:
            passed, err = False, "No fixed code produced"

        result = {
            "program": prog["name"],
            "bug_type": prog["bug_type"],
            "agent_success": agent_result.success,
            "test_passed": passed,
            "error": err,
            "iterations": agent_result.total_iterations,
            "llm_calls": agent_result.large_model_calls,
            "used_reflections": agent_result.used_past_reflections,
            "used_skills": agent_result.used_skills,
            "skill_extracted": agent_result.skill_extracted,
            "time_ms": elapsed,
            "compute_savings": agent_result.compute_savings,
        }
        results.append(result)

        status = "PASS" if passed else "FAIL"
        print(f"  Result: {status} | "
              f"iter={agent_result.total_iterations} | "
              f"llm_calls={agent_result.large_model_calls} | "
              f"refls={agent_result.used_past_reflections} | "
              f"skills={agent_result.used_skills} | "
              f"{elapsed:.0f}ms")

        if not passed:
            print(f"  Error: {err[:100]}")

    # ---- Summary ----
    total = len(results)
    passed = sum(1 for r in results if r["test_passed"])
    pass_rate = passed / total if total > 0 else 0.0

    print("\n" + "=" * 60)
    print(f"  LOCAL EVALUATION SUMMARY")
    print("=" * 60)
    print(f"  Programs:     {total}")
    print(f"  Passed:       {passed}/{total} ({pass_rate:.1%})")
    print(f"  Total time:   {total_time/1000:.1f}s")
    print(f"  Avg per bug:  {total_time/total:.0f}ms")

    # Memory effectiveness
    refls_used = sum(1 for r in results if r["used_reflections"] > 0)
    skills_used = sum(1 for r in results if r["used_skills"])
    skills_extracted = sum(1 for r in results if r["skill_extracted"])
    print(f"\n  Memory effectiveness:")
    print(f"    Reflections retrieved: {refls_used}/{total} ({refls_used/total:.0%})")
    print(f"    Skills retrieved:     {skills_used}/{total} ({skills_used/total:.0%})")
    print(f"    Skills extracted:     {skills_extracted}/{total}")

    # By bug type
    by_type: Dict[str, Dict] = {}
    for r in results:
        bt = r["bug_type"]
        if bt not in by_type:
            by_type[bt] = {"total": 0, "passed": 0}
        by_type[bt]["total"] += 1
        if r["test_passed"]:
            by_type[bt]["passed"] += 1

    print(f"\n  By bug type:")
    for bt, stats in sorted(by_type.items()):
        print(f"    {bt:20s}  {stats['passed']}/{stats['total']}  "
              f"({stats['passed']/stats['total']:.0%})")

    print("=" * 60)

    # Save detailed results
    out = {
        "summary": {
            "total": total, "passed": passed, "pass_rate": pass_rate,
            "total_time_s": total_time / 1000,
        },
        "results": results,
    }
    out_path = Path("./runs/local_eval_results.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n  Detailed results saved to: {out_path}")

    return out


# ==================== Entry Point ====================

def main():
    parser = argparse.ArgumentParser(description="Local evaluation with Ollama")
    parser.add_argument("--full", action="store_true", help="Run full QuixBugs (not mini)")
    parser.add_argument("--no-embedding", action="store_true",
                        help="Disable sentence-transformers (keyword fallback)")
    parser.add_argument("--reset-memory", action="store_true",
                        help="Clear reflection/skill memory before running")
    parser.add_argument("--ollama-url", default="http://localhost:11434",
                        help="Ollama API base URL")
    parser.add_argument("--model", default="qwen2.5-coder:1.5b",
                        help="Ollama model name")
    parser.add_argument("--max-iterations", type=int, default=3,
                        help="Max agent iterations per bug")
    args = parser.parse_args()

    # ---- Determine programs ----
    programs = QUIXBUGS_MINI
    benchmark_name = "quixbugs_mini"

    # ---- Reset memory if requested ----
    mem_base = Path("./runs")
    if args.reset_memory:
        for f in ["reflection_memory.json", "skills/skills.json"]:
            p = mem_base / f
            if p.exists():
                p.unlink()
                logger.info(f"Deleted: {p}")

    # ---- Check Ollama availability ----
    try:
        small_model = OllamaModel(model_name=args.model, base_url=args.ollama_url)
    except Exception as e:
        logger.error(f"Cannot connect to Ollama: {e}")
        logger.info("Make sure Ollama is running: ollama serve")
        logger.info("And the model is pulled: ollama pull qwen2.5-coder:1.5b")
        sys.exit(1)

    # ---- Large model: same Ollama (for local testing, we use the same model) ----
    # In production, small=1.5B, large=7B. Here both are the same for simplicity.
    large_model = small_model

    # ---- Initialize memory components ----
    embedding_model = None if args.no_embedding else "all-MiniLM-L6-v2"
    reflection_memory = ReflectionMemory(
        memory_path="./runs/reflection_memory.json",
        embedding_model_name=embedding_model or "all-MiniLM-L6-v2",
    )
    skill_manager = SkillManager(
        skills_dir="./runs/skills",
        embedding_model_name=embedding_model or "all-MiniLM-L6-v2",
    )

    # ---- Bug Knowledge Base RAG ----
    rag_retriever = BugFixRetriever(knowledge_base_path="./runs/bug_fixes.json")

    # ---- Report initial state ----
    report_memory_stats(reflection_memory, skill_manager)

    # ---- Build agent ----
    agent = CoTReActAgent(
        small_model_fn=small_model,
        large_model_fn=large_model,
        rag_retriever=rag_retriever,
        max_iterations=args.max_iterations,
        reflection_memory=reflection_memory,
        skill_manager=skill_manager,
    )

    # ---- Run evaluation ----
    print(f"\nStarting local evaluation on {len(programs)} programs...")
    print(f"  Ollama model:    {args.model}")
    print(f"  Embedding:       {'disabled' if args.no_embedding else 'all-MiniLM-L6-v2'}")
    print(f"  Max iterations:  {args.max_iterations}")

    results = evaluate(agent, programs)

    # ---- Final memory state ----
    report_memory_stats(reflection_memory, skill_manager)

    return results


if __name__ == "__main__":
    main()
