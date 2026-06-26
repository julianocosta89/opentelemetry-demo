"""
Test 7: Go unit tests for business logic in the checkout service.

Runs src/checkout/money/money_test.go which tests currency arithmetic:
  - Money validation (units, nanos, currency codes)
  - Arithmetic (sum, negate, comparison)

These are pure unit tests — no Docker, no network, no OTel dependency.
They run in seconds and provide a sanity check that Go service code is intact.

Run individually:
    pytest tests/test_go_unit.py -v

Skipped if Go is not installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKOUT_DIR = Path(__file__).parent.parent / "src" / "checkout"


@pytest.mark.fast
def test_checkout_money_unit_tests(project_root):
    """
    Runs Go unit tests for the checkout service's money package.

    Tests currency arithmetic business logic (validation, addition, negation)
    that is core to the checkout flow. These tests have zero OTel dependency.
    """
    go = shutil.which("go")
    if not go:
        pytest.skip("go not found in PATH — install Go to run checkout unit tests")

    if not CHECKOUT_DIR.exists():
        pytest.skip(f"Checkout service directory not found: {CHECKOUT_DIR}")

    result = subprocess.run(
        [go, "test", "./money/...", "-v"],
        cwd=CHECKOUT_DIR,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        pytest.fail(
            f"Go unit tests failed in {CHECKOUT_DIR}/money:\n\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    # Print test output for visibility even on pass
    if result.stdout:
        print(f"\n{result.stdout}")
