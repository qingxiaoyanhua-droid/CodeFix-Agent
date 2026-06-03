from evaluate_on_benchmark import execute_and_test, QUIXBUGS_PROGRAMS, _extract_code
import re

# Test all programs with their fixed code (oracle)
print("=" * 60)
print("  QuixBugs Oracle Test (using fixed code)")
print("=" * 60)

total = 0
passed = 0
failed_list = []

for prog in QUIXBUGS_PROGRAMS:
    if not prog.get("tests"):
        continue
    fixed = prog["fixed"]
    ok, err = execute_and_test(fixed, prog["tests"])
    total += 1
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed_list.append(prog["name"])
    print(f"  [{status}] {prog['name']} ({prog['bug_type']})")
    if not ok:
        print(f"         Error: {err[:100]}")

print()
print(f"Results: {passed}/{total} passed")
if passed == total:
    print("All tests pass - benchmark data is correct!")
else:
    print(f"Failed: {failed_list}")

print()
print("=" * 60)
print("  Dummy Model Test (returns buggy code)")
print("=" * 60)

from evaluate_on_benchmark import evaluate_model

def dummy_model(buggy_code, bug_desc):
    return f'<fixed_code>\n```python\n{buggy_code}\n```\n</fixed_code>'

result = evaluate_model(dummy_model, benchmark="quixbugs")
print(f"Baseline pass@1: {result.passed}/{result.total} = {result.pass_at_1:.1%}")
print(f"95% CI: [{result.confidence_interval_95[0]:.1%}, {result.confidence_interval_95[1]:.1%}]")
