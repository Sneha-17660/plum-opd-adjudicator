"""Run the supplied acceptance cases (backend/data/test_cases.json) through the policy engine.

Usage (from the repo root):  python scripts/run_test_cases.py

The adapter in backend/tests/acceptance.py turns each case's input_data into
document evidence; expected_output is only used for the comparison column.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.engine.policy_engine import PolicyEngine  # noqa: E402
from tests.acceptance import compare, load_cases, run_case  # noqa: E402


def main() -> int:
    engine, failures = PolicyEngine(), 0
    print(f"{'Case':<7}{'Decision':<15}{'Payable':>10}  Result")
    for case in load_cases():
        result = run_case(case, engine)
        problems = compare(case, result)
        failures += bool(problems)
        print(f"{case['case_id']:<7}{result['decision']:<15}{result['approved_amount']:>10,.0f}  "
              f"{'pass' if not problems else 'FAIL: ' + '; '.join(problems)}")
    total = len(load_cases())
    print(f"\nResult: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
