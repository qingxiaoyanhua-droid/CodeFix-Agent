#!/usr/bin/env python3
"""快速验证QuixBugs数据正确性"""
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from server_eval import execute_and_test, QUIXBUGS_DATA

print("=" * 60)
print("  QuixBugs Oracle Test")
print("=" * 60)

total = 0
passed = 0
failed = []

for prog in QUIXBUGS_DATA:
    if not prog.get("tests"):
        continue
    total += 1
    ok, err = execute_and_test(prog["fixed"], prog["tests"])
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed.append(prog["name"])
    print(f"  [{status}] {prog['name']:30s} ({prog['bug_type']})")

print()
print(f"Results: {passed}/{total} passed")
if failed:
    print(f"Failed: {failed}")
else:
    print("All oracle tests pass!")
