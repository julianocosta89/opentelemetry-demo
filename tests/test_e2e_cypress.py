"""
Test 6: End-to-end business logic verification via Cypress.

Runs the upstream Cypress test suite (src/frontend/cypress/e2e/) against the
running de-otelified stack. These tests verify that core user flows work
correctly WITHOUT testing any OTel instrumentation:

  - Home.cy.ts      — 10 products load, currency switching works
  - ProductDetail.cy.ts — product detail, recommendations, reviews, add-to-cart
  - Checkout.cy.ts  — full checkout flow with two items

Cypress hits http://localhost:8080 (via frontend-proxy/Envoy) and validates
actual API responses (/api/products, /api/cart, /api/checkout, etc.).

Run after docker compose up (shares the compose_stack fixture with Tests 4+5):
    pytest -m compose -v                    # runs Tests 4, 5, and 6 together
    pytest tests/test_e2e_cypress.py -v     # standalone (starts its own stack)

Skipped if Node.js/npm is not installed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).parent.parent / "src" / "frontend"


@pytest.mark.slow
@pytest.mark.compose
@pytest.mark.e2e
def test_cypress_e2e(request, compose_stack, project_root):
    """
    Runs Cypress E2E tests against the running de-otelified stack.

    Verifies business logic end-to-end:
    - Home page: products render, currency switching works
    - Product detail: API data loads (recommendations, reviews, ads)
    - Checkout: cart add, order placement, redirect flow

    The compose_stack fixture ensures the full stack is running before Cypress
    starts. Cypress targets http://localhost:8080 (frontend-proxy/Envoy).
    """
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        pytest.skip("node/npm not found in PATH — install Node.js to run Cypress tests")

    if not FRONTEND_DIR.exists():
        pytest.skip(f"Frontend directory not found: {FRONTEND_DIR}")

    # Install Cypress and frontend dependencies (cached after first run)
    install_result = subprocess.run(
        [npm, "install", "--prefer-offline"],
        cwd=FRONTEND_DIR,
        capture_output=True,
        text=True,
    )
    if install_result.returncode != 0:
        pytest.fail(
            f"npm install failed in {FRONTEND_DIR}:\n{install_result.stderr}"
        )

    # Run Cypress in headless mode
    # NODE_ENV is intentionally NOT set to 'production' so Cypress uses
    # http://localhost:${FRONTEND_PORT} (default: localhost:8080 via Envoy proxy)
    env = {**os.environ}
    env.pop("NODE_ENV", None)  # ensure non-production mode

    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx not found in PATH")

    cypress_result = subprocess.run(
        [npx, "cypress", "run"],
        cwd=FRONTEND_DIR,
        capture_output=False,  # stream output so CI shows Cypress progress
        env=env,
    )

    if cypress_result.returncode != 0:
        pytest.fail(
            "Cypress E2E tests failed — business logic verification did not pass.\n"
            "Check the output above for which spec file and assertion failed.\n"
            "Video/screenshots may be in src/frontend/cypress/videos/ and "
            "src/frontend/cypress/screenshots/"
        )
