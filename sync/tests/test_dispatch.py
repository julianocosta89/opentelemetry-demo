"""Routing tests — every conflicted path must map to a known resolver."""

import pytest

from resolvers import dispatch

CASES = [
    # (path, porcelain-code, expected resolver)
    ("src/grafana/dashboards/x.json", "DU", "deleted"),
    (".github/workflows/run-integration-tests.yml", "DU", "deleted"),
    ("some/file.txt", "UD", "deleted"),
    ("src/checkout/go.sum", "UU", "lockfile"),
    ("src/frontend/package-lock.json", "UU", "lockfile"),
    ("src/checkout/go.mod", "UU", "deps"),
    ("src/frontend/package.json", "UU", "deps"),
    ("src/accounting/Accounting.csproj", "UU", "deps"),
    ("src/ad/build.gradle", "UU", "deps"),
    ("src/fraud-detection/build.gradle.kts", "UU", "deps"),
    ("src/quote/composer.json", "UU", "deps"),
    ("src/flagd-ui/mix.exs", "UU", "deps"),          # dep file, not source, despite .exs
    ("src/recommendation/requirements.txt", "UU", "deps"),
    ("kubernetes/opentelemetry-demo.yaml", "UU", "k8s"),
    ("src/checkout/main.go", "UU", "llm"),
    ("src/accounting/Consumer.cs", "UU", "llm"),
    ("docker-compose.yml", "UU", "llm"),
    (".env", "UU", "llm"),
    ("src/frontend/Dockerfile", "UU", "llm"),
]


@pytest.mark.parametrize("path, code, expected", CASES, ids=[c[0] for c in CASES])
def test_classify(path, code, expected):
    assert dispatch.classify(path, code) == expected


def test_no_path_is_unhandled():
    for path, code, _ in CASES:
        assert dispatch.classify(path, code) != "unhandled"
