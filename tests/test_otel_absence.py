"""
Test 1: Verify no OpenTelemetry code remains after de-otelification.

The rule set lives in sync/otel_rules.py — the single source of truth shared with
the sync pipeline's residual scan, so "what the pipeline removes" and "what this
test checks for" can never drift apart.

Each rule is parametrized so failures are reported per language/file-type,
and individual rules can be re-run with: pytest tests/test_otel_absence.py -k py_imports
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# Make the shared rule module importable without installing the sync package.
sys.path.insert(0, str(PROJECT_ROOT / "sync"))

from otel_rules import (  # noqa: E402
    RULES,
    find_forbidden_paths,
    scan_rule,
)


@pytest.mark.fast
@pytest.mark.parametrize(
    "rule_id, glob_pattern, patterns, description",
    RULES,
    ids=[r[0] for r in RULES],
)
def test_no_otel_references(rule_id, glob_pattern, patterns, description):
    """
    For each file-type rule, scan all matching files and assert none contain
    OpenTelemetry references matching the given patterns.

    Reports ALL violating files and lines — not just the first match.
    """
    violations = scan_rule(rule_id, glob_pattern, patterns, PROJECT_ROOT)

    if violations:
        lines = [f"\nRule '{rule_id}': {description}"]
        lines.append(f"Patterns checked: {patterns}")
        lines.append(f"\nFound OTel references in {len(violations)} file(s):\n")
        for path, hits in sorted(violations.items()):
            rel = path.relative_to(PROJECT_ROOT)
            lines.append(f"  {rel}:")
            for lineno, content in hits[:10]:
                lines.append(f"    line {lineno:4d}: {content}")
            if len(hits) > 10:
                lines.append(f"    ... and {len(hits) - 10} more lines")
        pytest.fail("\n".join(lines))


@pytest.mark.fast
def test_no_forbidden_telemetry_paths():
    """
    The observability infrastructure directories (grafana, jaeger, otel-collector,
    prometheus, opensearch) were deleted during de-otelification and must stay gone.

    This guards a gap that content scanning cannot: a clean upstream ADD into one of
    these deleted directories merges without a conflict, silently re-creating
    telemetry config that no per-file regex would catch.
    """
    forbidden = find_forbidden_paths(PROJECT_ROOT)
    if forbidden:
        lines = ["\nForbidden observability files present (should have been removed):\n"]
        for path in forbidden:
            lines.append(f"  {path.relative_to(PROJECT_ROOT)}")
        pytest.fail("\n".join(lines))
