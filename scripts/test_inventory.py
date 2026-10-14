#!/usr/bin/env python3
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

loader = unittest.TestLoader()
test_files = sorted([f for f in os.listdir(os.path.join(ROOT, "tests")) if f.startswith("test_") and f.endswith(".py")])

total = 0
rows = []
for f in test_files:
    mod_name = "tests." + f[:-3]
    try:
        suite = loader.loadTestsFromName(mod_name)
        cnt = suite.countTestCases()
        total += cnt
        rows.append((f, cnt))
    except Exception as exc:
        rows.append((f, f"ERROR: {exc}"))

print(f"{'File':<44} | {'Test Count':<10}")
print("-" * 44 + "-+-" + "-" * 10)
for f, cnt in rows:
    print(f"{f:<44} | {cnt:<10}")
print("=" * 44 + "=+=" + "=" * 10)
print(f"{'TOTAL':<44} | {total:<10}")
