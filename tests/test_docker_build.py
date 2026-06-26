"""
Test 2: Verify the de-otelified repo builds successfully with docker compose build.

Only tests the root docker-compose.yml — not test variants or override files.

Run individually:
    pytest tests/test_docker_build.py -v
    pytest tests/test_docker_build.py --no-cache -v   # full clean build
"""

import subprocess

import pytest


@pytest.mark.slow
@pytest.mark.build
def test_docker_compose_build(request, project_root, compose_cmd):
    """
    Runs 'docker compose build' and asserts it exits with code 0.

    Streams output directly to the terminal (no capture) so build errors
    are visible in CI logs without needing to dig through pytest output.
    """
    no_cache = request.config.getoption("--no-cache")
    timeout = request.config.getoption("--build-timeout")

    cmd = compose_cmd + ["build"]
    if no_cache:
        cmd.append("--no-cache")

    result = subprocess.run(
        cmd,
        cwd=project_root,
        timeout=timeout,
        # No capture_output: stream directly so progress is visible
    )

    if result.returncode != 0:
        pytest.fail(
            f"docker compose build failed (exit code {result.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"Check the output above for the failing service and error."
        )
