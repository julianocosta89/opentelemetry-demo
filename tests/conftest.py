"""
Shared fixtures and configuration for the de-otelification pipeline test suite.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


# ─── CLI options ──────────────────────────────────────────────────────────────


def pytest_addoption(parser):
    parser.addoption(
        "--no-cache",
        action="store_true",
        default=False,
        help="Pass --no-cache to docker compose build",
    )
    parser.addoption(
        "--update-baseline",
        action="store_true",
        default=False,
        help="Overwrite tests/baseline/startup_errors.json with current run",
    )
    parser.addoption(
        "--compose-timeout",
        type=int,
        default=300,
        help="Seconds to wait for all containers to reach running state (default: 300)",
    )
    parser.addoption(
        "--build-timeout",
        type=int,
        default=1800,
        help="Seconds to allow docker compose build to complete (default: 1800)",
    )
    parser.addoption(
        "--error-wait",
        type=int,
        default=60,
        help="Seconds to wait after containers are running before capturing error logs (default: 60)",
    )


# ─── Path fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def project_root():
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def compose_file(project_root):
    p = project_root / "docker-compose.yml"
    assert p.exists(), f"docker-compose.yml not found at {p}"
    return p


@pytest.fixture(scope="session")
def baseline_file(project_root):
    return project_root / "tests" / "baseline" / "startup_errors.json"


@pytest.fixture(scope="session")
def compose_cmd(compose_file):
    """Base docker compose command list. docker compose auto-loads .env from the compose file's directory."""
    return ["docker", "compose", "-f", str(compose_file)]


# ─── Docker compose stack (shared by Tests 4 and 5) ──────────────────────────


@pytest.fixture(scope="session")
def compose_stack(request, compose_cmd, project_root):
    """
    Starts 'docker compose up -d', waits for all containers to reach running
    state, yields the list of running service names, then tears down.

    Shared between test_compose_health.py and test_startup_errors.py so the
    stack is started only once per test session.
    """
    timeout = request.config.getoption("--compose-timeout")

    subprocess.run(
        compose_cmd + ["up", "-d", "--remove-orphans"],
        check=True,
        cwd=project_root,
    )

    deadline = time.monotonic() + timeout
    containers = []

    try:
        while time.monotonic() < deadline:
            result = subprocess.run(
                compose_cmd + ["ps", "--format", "json"],
                capture_output=True,
                text=True,
                cwd=project_root,
            )
            containers = _parse_compose_ps_json(result.stdout)

            running = [c for c in containers if c.get("State") == "running"]
            health_starting = [c for c in containers if c.get("Health") == "starting"]

            if (
                len(running) == len(containers)
                and len(containers) > 0
                and not health_starting
            ):
                break

            time.sleep(5)
        # Whether we timed out or all services are up, yield the running set.
        # Individual tests are responsible for asserting which services they need.
        # Calling pytest.fail() here would register as an ERROR (fixture setup
        # failure) rather than a test FAILURE, hiding the problem from the
        # orchestrator's test result parser.

        running_services = [
            c.get("Service", c.get("Name", "?"))
            for c in containers
            if c.get("State") == "running"
        ]
        yield running_services

    finally:
        subprocess.run(
            compose_cmd + ["down", "--remove-orphans", "--volumes"],
            cwd=project_root,
        )


def _parse_compose_ps_json(raw: str) -> list[dict]:
    """
    Parse 'docker compose ps --format json' output.
    Handles both JSON array format and one-JSON-object-per-line format.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Try as a JSON array first (newer docker compose versions)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        pass

    # Fall back to one JSON object per line (older versions)
    containers = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return containers
