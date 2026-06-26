"""
Test 4: Verify all services start and reach running state after docker compose up.

Uses the session-scoped compose_stack fixture from conftest.py — the stack is
shared with test_startup_errors.py so it is only started once per test session.

Run individually (starts and tears down the stack):
    pytest tests/test_compose_health.py -v

Run together with startup error tests (stack started only once):
    pytest -m compose -v
"""

import pytest

# Exact service names from docker-compose.yml
EXPECTED_SERVICES = {
    # Core demo services
    "accounting",
    "ad",
    "cart",
    "checkout",
    "currency",
    "email",
    "fraud-detection",
    "frontend",
    "frontend-proxy",
    "image-provider",
    "load-generator",
    "payment",
    "product-catalog",
    "product-reviews",
    "quote",
    "recommendation",
    "shipping",
    # Dependent services
    "flagd",
    "flagd-ui",
    "kafka",
    "llm",
    "postgresql",
    "valkey-cart",
}

# OTel infrastructure services that must NOT be present after de-otelification
OTEL_INFRA_SERVICES = {
    "jaeger",
    "grafana",
    "prometheus",
    "otel-collector",
    "opensearch",
    "opensearch-dashboards",
    "otelcol",
}


@pytest.mark.slow
@pytest.mark.compose
def test_all_expected_services_are_running(compose_stack):
    """
    Assert every service defined in docker-compose.yml reached 'running' state.

    If this fails, check the logs of the named service(s) with:
        docker compose logs <service-name>
    """
    running = set(compose_stack)
    missing = EXPECTED_SERVICES - running
    if missing:
        pytest.fail(
            f"The following services did not reach running state:\n"
            + "\n".join(f"  - {s}" for s in sorted(missing))
            + "\n\nInspect with: docker compose logs "
            + " ".join(sorted(missing))
        )


@pytest.mark.slow
@pytest.mark.compose
def test_no_extra_services_running(compose_stack):
    """
    Warn if unexpected services are running that are not tracked in EXPECTED_SERVICES.
    This catches new services added upstream that need to be added to the test.
    """
    running = set(compose_stack)
    unexpected = running - EXPECTED_SERVICES
    if unexpected:
        # Informational only — new upstream services may be legitimate
        print(
            f"\n[WARNING] Services running but not in EXPECTED_SERVICES:\n"
            + "\n".join(f"  + {s}" for s in sorted(unexpected))
            + "\nIf these are new upstream services, add them to EXPECTED_SERVICES."
        )


@pytest.mark.slow
@pytest.mark.compose
def test_no_otel_infrastructure_running(compose_stack):
    """
    Assert none of the OTel observability infrastructure services are running.
    These should have been removed by the de-otelification pipeline.
    """
    running = set(compose_stack)
    leftover_otel = OTEL_INFRA_SERVICES & running
    if leftover_otel:
        pytest.fail(
            f"OTel infrastructure services are still running after de-otelification:\n"
            + "\n".join(f"  - {s}" for s in sorted(leftover_otel))
        )
