"""
Test 5: Verify no new error logs appear in services after startup.

Compares error log counts against a committed baseline captured from the
upstream (with-OTel) version of the demo. A service that had 0 errors in
the upstream baseline must have 0 errors in the de-otelified version.

Normal test run (compare against baseline):
    pytest tests/test_startup_errors.py -m compose -v

Update baseline from the current de-otelified stack:
    pytest tests/test_startup_errors.py -m compose --update-baseline -v

Capture baseline from the upstream (with-OTel) images:
    pytest tests/test_startup_errors.py::test_capture_upstream_baseline \\
        --update-baseline --compose-timeout=300 --error-wait=60
"""

import datetime
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ─── Error detection patterns ─────────────────────────────────────────────────

# Log lines matching any of these are classified as errors
_ERROR_PATTERNS = [
    re.compile(r"\bERROR\b"),
    re.compile(r"\[ERROR\]"),
    re.compile(r"\bEXCEPTION\b", re.IGNORECASE),
    re.compile(r"\bPANIC\b"),
    re.compile(r"\bFATAL\b"),
]

# Lines matching any of these are NOT counted as errors even if they match above
# (avoids common false positives in structured/Go/Rust logs)
_ERROR_EXCLUDE_PATTERNS = [
    re.compile(r'err\s*=\s*(nil|<nil>|null|None|"")'),   # err = nil/null/None
    re.compile(r"no error"),
    re.compile(r"error_count\s*=\s*0"),
    re.compile(r'"level"\s*:\s*"(info|debug|warn)"'),    # structured JSON non-error
    re.compile(r'"severity"\s*:\s*"(INFO|DEBUG|WARN)"'),
]

UPSTREAM_RAW_URL = (
    "https://raw.githubusercontent.com/open-telemetry/opentelemetry-demo"
    "/v{version}/docker-compose.yml"
)


def _parse_compose_ps_json(raw: str) -> list[dict]:
    """Parse 'docker compose ps --format json' output (array or one-obj-per-line)."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        pass
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


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _is_error_line(line: str) -> bool:
    """Return True if the log line represents an error."""
    if any(rx.search(line) for rx in _ERROR_EXCLUDE_PATTERNS):
        return False
    return any(rx.search(line) for rx in _ERROR_PATTERNS)


def _capture_service_errors(
    compose_cmd: list[str],
    service: str,
    project_root: Path,
) -> list[dict]:
    """Fetch logs for a service and return error lines as dicts."""
    result = subprocess.run(
        compose_cmd + ["logs", "--no-color", service],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    errors = []
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if _is_error_line(line):
            errors.append({"line": line.strip(), "source": "stdout"})
    return errors


def _get_image_version(project_root: Path) -> str:
    """Read IMAGE_VERSION from .env file."""
    env_file = project_root / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("IMAGE_VERSION="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"IMAGE_VERSION not found in {env_file}")


def _write_baseline(
    baseline_file: Path,
    services_errors: dict[str, dict],
    project_root: Path,
    *,
    source: str = "de-otelified",
) -> None:
    """Write baseline JSON file with error counts per service."""
    version = _get_image_version(project_root)
    data = {
        "meta": {
            "captured_at": datetime.datetime.utcnow().isoformat() + "Z",
            "upstream_version": version,
            "upstream_image_prefix": f"ghcr.io/open-telemetry/demo:{version}",
            "source": source,
            "wait_seconds_after_running": 60,
        },
        "services": services_errors,
    }
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    baseline_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"\nBaseline written to: {baseline_file}")


# ─── Main regression test ─────────────────────────────────────────────────────


@pytest.mark.slow
@pytest.mark.compose
def test_startup_errors_within_baseline(
    request, compose_stack, compose_cmd, project_root, baseline_file
):
    """
    After the de-otelified stack is running (via compose_stack fixture),
    compare error logs per service against the committed baseline.

    Key rule: a service with 0 baseline errors must have 0 errors here.
    A service with N>0 baseline errors is allowed up to N errors (some
    infrastructure services like Kafka have known startup warnings).
    """
    wait_seconds = request.config.getoption("--error-wait")
    update_baseline = request.config.getoption("--update-baseline")

    print(f"\nWaiting {wait_seconds}s for services to stabilize...")
    time.sleep(wait_seconds)

    services = compose_stack

    # Capture current error counts
    current: dict[str, dict] = {}
    for service in services:
        errors = _capture_service_errors(compose_cmd, service, project_root)
        current[service] = {"error_count": len(errors), "errors": errors}

    if update_baseline:
        _write_baseline(baseline_file, current, project_root, source="de-otelified")
        pytest.skip(
            "Baseline updated from current de-otelified stack.\n"
            "Re-run without --update-baseline to compare."
        )
        return

    if not baseline_file.exists():
        pytest.fail(
            f"Baseline file not found: {baseline_file}\n"
            f"Create it with:\n"
            f"  pytest tests/test_startup_errors.py -m compose --update-baseline\n"
            f"Or capture from upstream:\n"
            f"  pytest tests/test_startup_errors.py::test_capture_upstream_baseline "
            f"--update-baseline"
        )

    baseline_data = json.loads(baseline_file.read_text(encoding="utf-8"))
    baseline_services = baseline_data.get("services", {})

    failures = []
    for service, cur in current.items():
        bl = baseline_services.get(service, {"error_count": 0, "errors": []})
        bl_count = bl.get("error_count", 0)
        cur_count = cur["error_count"]

        if bl_count == 0 and cur_count > 0:
            sample = cur["errors"][:5]
            failures.append(
                f"  '{service}': baseline=0 errors, current={cur_count} errors\n"
                + "\n".join(f"    {e['line']}" for e in sample)
                + (f"\n    ... and {cur_count - 5} more" if cur_count > 5 else "")
            )
        elif cur_count > bl_count:
            new_count = cur_count - bl_count
            sample = cur["errors"][:5]
            failures.append(
                f"  '{service}': baseline={bl_count}, current={cur_count} "
                f"(+{new_count} new errors)\n"
                + "\n".join(f"    {e['line']}" for e in sample)
                + (f"\n    ... and {cur_count - 5} more" if cur_count > 5 else "")
            )

    if failures:
        pytest.fail(
            f"Startup error regression in {len(failures)} service(s):\n\n"
            + "\n\n".join(failures)
        )


# ─── Upstream baseline capture ────────────────────────────────────────────────


@pytest.mark.slow
@pytest.mark.compose
def test_capture_upstream_baseline(request, project_root, baseline_file):
    """
    One-off test to capture error baseline from the upstream (with-OTel) images.

    Downloads the upstream docker-compose.yml at the pinned IMAGE_VERSION tag,
    starts the stack using pre-built GHCR images (no local build required),
    waits for stabilization, captures errors, and writes the baseline file.

    Invoke as:
        pytest tests/test_startup_errors.py::test_capture_upstream_baseline \\
            --update-baseline --compose-timeout=300 --error-wait=60

    This is a one-time operation. Re-run after each upstream version bump.
    """
    if not request.config.getoption("--update-baseline"):
        pytest.skip(
            "This test only runs with --update-baseline.\n"
            "Use it to capture a fresh upstream baseline after a version bump."
        )

    version = _get_image_version(project_root)
    timeout = request.config.getoption("--compose-timeout")
    wait_seconds = request.config.getoption("--error-wait")
    tmpdir = Path(tempfile.mkdtemp(prefix="otel-upstream-baseline-"))

    try:
        # Download upstream docker-compose.yml at the pinned version
        url = UPSTREAM_RAW_URL.format(version=version)
        print(f"\nFetching upstream docker-compose.yml from: {url}")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                compose_content = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            pytest.fail(
                f"Failed to fetch upstream docker-compose.yml (HTTP {e.code}).\n"
                f"URL: {url}\n"
                f"Check that IMAGE_VERSION={version} exists as a git tag in the upstream repo."
            )

        upstream_compose = tmpdir / "docker-compose-upstream.yml"
        upstream_compose.write_text(compose_content, encoding="utf-8")

        upstream_cmd = [
            "docker", "compose",
            "-f", str(upstream_compose),
            "--project-name", "otel-upstream-baseline",
        ]

        print("Pulling upstream images and starting stack...")
        subprocess.run(
            upstream_cmd + ["pull"],
            check=True,
            cwd=tmpdir,
        )
        subprocess.run(
            upstream_cmd + ["up", "-d", "--remove-orphans"],
            check=True,
            cwd=tmpdir,
        )

        # Wait for containers to reach running state
        deadline = time.monotonic() + timeout
        containers = []
        while time.monotonic() < deadline:
            result = subprocess.run(
                upstream_cmd + ["ps", "--format", "json"],
                capture_output=True,
                text=True,
                cwd=tmpdir,
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

        print(f"Waiting {wait_seconds}s for services to stabilize...")
        time.sleep(wait_seconds)

        # Capture service names from running containers
        services = sorted({
            c.get("Service", c.get("Name", "?"))
            for c in containers
            if c.get("State") == "running"
        })

        services_errors: dict[str, dict] = {}
        for service in services:
            errors = _capture_service_errors(upstream_cmd, service, tmpdir)
            services_errors[service] = {
                "error_count": len(errors),
                "errors": errors,
            }

        _write_baseline(
            baseline_file,
            services_errors,
            project_root,
            source="upstream-with-otel",
        )

    finally:
        subprocess.run(
            ["docker", "compose",
             "-f", str(tmpdir / "docker-compose-upstream.yml"),
             "--project-name", "otel-upstream-baseline",
             "down", "--remove-orphans", "--volumes"],
            cwd=tmpdir,
        )
        shutil.rmtree(tmpdir, ignore_errors=True)
