#!/usr/bin/env python3
"""
QuixBugs Benchmark 测试用例生成

分析每个 buggy/fixed 代码对的实际行为，生成能区分两者的测试用例。
"""

import json
from pathlib import Path


def get_expected_output(program_id: str, buggy_code: str, fixed_code: str) -> list:
    tests = []

    if program_id == "bitcount":
        return [
            {"function": "bitcount", "input": [0], "output": 0},
            {"function": "bitcount", "input": [1], "output": 1},
            {"function": "bitcount", "input": [7], "output": 3},
            {"function": "bitcount", "input": [255], "output": 8},
            {"function": "bitcount", "input": [128], "output": 1},
            {"function": "bitcount", "input": [1023], "output": 10},
        ]

    elif program_id == "find_first_in_sorted":
        return [
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4, 5], 3], "output": 2},
            {"function": "find_first_in_sorted", "input": [[1, 1, 1, 1], 1], "output": 0},
            {"function": "find_first_in_sorted", "input": [[1, 3, 5, 7, 9], 5], "output": 2},
            {"function": "find_first_in_sorted", "input": [[], 1], "output": -1},
            {"function": "find_first_in_sorted", "input": [[2, 4, 6, 8], 5], "output": -1},
            {"function": "find_first_in_sorted", "input": [[1, 2, 3], 1], "output": 0},
        ]

    elif program_id == "flatten":
        return [
            {"function": "flatten", "input": [[[1, [2]], 3]], "output": [1, 2, 3]},
            {"function": "flatten", "input": [[[1], 2, [3, [4]]]], "output": [1, 2, 3, 4]},
            {"function": "flatten", "input": [[[[1]]]], "output": [1]},
            {"function": "flatten", "input": [[[1, [2, [3]]]]], "output": [1, 2, 3]},
        ]

    elif program_id == "gcd":
        return [
            {"function": "gcd", "input": [12, 8], "output": 4},
            {"function": "gcd", "input": [48, 18], "output": 6},
            {"function": "gcd", "input": [17, 13], "output": 1},
            {"function": "gcd", "input": [100, 25], "output": 25},
            {"function": "gcd", "input": [7, 0], "output": 7},
            {"function": "gcd", "input": [1, 1], "output": 1},
        ]

    elif program_id == "is_valid_parenthesization":
        return [
            {"function": "is_valid_parenthesization", "input": ["(())"], "output": True},
            {"function": "is_valid_parenthesization", "input": ["(()"], "output": False},
            {"function": "is_valid_parenthesization", "input": [")("], "output": False},
            {"function": "is_valid_parenthesization", "input": [""], "output": True},
            {"function": "is_valid_parenthesization", "input": ["()"], "output": True},
            {"function": "is_valid_parenthesization", "input": [")()("], "output": False},
            {"function": "is_valid_parenthesization", "input": ["((())"], "output": False},
            {"function": "is_valid_parenthesization", "input": ["()()"], "output": True},
        ]

    elif program_id == "max_sublist_sum":
        return [
            {"function": "max_sublist_sum", "input": [[-2, 1, -3, 4, -1, 2, 1, -5, 4]], "output": 6},
            {"function": "max_sublist_sum", "input": [[-1, -2, -3]], "output": -1},
            {"function": "max_sublist_sum", "input": [[1, 2, 3]], "output": 6},
            {"function": "max_sublist_sum", "input": [[-1]], "output": -1},
            {"function": "max_sublist_sum", "input": [[5]], "output": 5},
            {"function": "max_sublist_sum", "input": [[-3, 2, -1]], "output": 2},
        ]

    elif program_id == "quicksort":
        return [
            {"function": "quicksort", "input": [[3, 1, 2]], "output": [1, 2, 3]},
            {"function": "quicksort", "input": [[5, 3, 8, 1, 2]], "output": [1, 2, 3, 5, 8]},
            {"function": "quicksort", "input": [[]], "output": []},
            {"function": "quicksort", "input": [[1]], "output": [1]},
            {"function": "quicksort", "input": [[2, 1]], "output": [1, 2]},
            {"function": "quicksort", "input": [[5, 3, 3, 1]], "output": [1, 3, 3, 5]},
        ]

    elif program_id == "shortest_path_lengths":
        return [
            {"function": "shortest_path_lengths",
             "input": [{"A": [("B", 1), ("C", 4)], "B": [("C", 2), ("D", 5)], "C": [("D", 1)], "D": []}, "A"],
             "output": {"A": 0, "B": 1, "C": 3, "D": 4}},
            {"function": "shortest_path_lengths",
             "input": [{"A": [("B", 3)], "B": [("C", 1)], "C": []}, "A"],
             "output": {"A": 0, "B": 3, "C": 4}},
        ]

    elif program_id == "maximum_subarray":
        return [
            {"function": "maximum_subarray", "input": [], "output": 0},
        ]

    elif program_id == "sqrt":
        return []

    elif program_id == "kmp":
        return []

    return tests


def main():
    output_dir = Path(__file__).parent / "bushu" / "datasets" / "quixbugs"
    output_dir.mkdir(parents=True, exist_ok=True)

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from quixbugs_data import QUIXBUGS_PROGRAMS as raw_data
        print("Loaded %d programs from quixbugs_data.py" % len(raw_data))
    except ImportError:
        print("ERROR: Cannot load quixbugs_data.py")
        return

    programs = []
    for item in raw_data:
        name = item.get("name", "unknown")
        buggy = item.get("buggy_code", item.get("buggy", ""))
        fixed = item.get("fixed_code", item.get("fixed", ""))
        bug_type = item.get("bug_type", "unknown")
        tests = get_expected_output(name, buggy, fixed)

        programs.append({
            "name": name,
            "buggy_code": buggy,
            "fixed_code": fixed,
            "bug_type": bug_type,
            "tests": tests,
        })

    with_tests = sum(1 for p in programs if p["tests"])
    print("Programs with test cases: %d/%d" % (with_tests, len(programs)))

    output_path = output_dir / "quixbugs_benchmark.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(programs, f, ensure_ascii=False, indent=2)

    print("Saved to %s" % output_path)
    print("")
    for p in programs:
        status = "(%d tests)" % len(p["tests"]) if p["tests"] else "(no tests)"
        print("  [%s] %s %s" % (p["bug_type"][:15], p["name"], status))


if __name__ == "__main__":
    main()
