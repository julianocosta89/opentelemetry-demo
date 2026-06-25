"""
Post-merge residual-OTel cleanup.

A git merge carries our de-otelification forward, but it can't catch OTel that
upstream ADDED in a region we never touched — those changes merge cleanly, no
conflict. This pass scans the whole merged tree with the shared OTel rules and
cleans whatever the conflict resolvers didn't already handle:

  * files under a deleted telemetry directory  → git rm (stay removed)
  * any other file still tripping a content rule → focused LLM cleanup

It loops until the tree is clean or a round makes no progress, so a cleanup that
exposes nothing new terminates immediately.
"""

from __future__ import annotations

from pathlib import Path

import gitops
import otel_rules
from resolvers import k8s, source_llm
from resolvers.common import ResolveResult


def _rel(path: Path, worktree: Path) -> str:
    return str(path.relative_to(worktree))


def _clean_one(rel: str, worktree: Path, theirs_ref: str, base_sha: str) -> ResolveResult:
    """Clean residual OTel from one clean-merged file.

    The kubernetes manifest is too large and too structured for the LLM — it gets
    the same deterministic transform a k8s conflict would. Everything else (source,
    config) goes to the LLM cleanup.
    """
    if rel.startswith("kubernetes/") and rel.endswith((".yaml", ".yml")):
        full = worktree / rel
        try:
            full.write_text(
                k8s.transform_kubernetes(full.read_text(encoding="utf-8"), k8s.read_init_sql(worktree)),
                encoding="utf-8",
            )
            gitops.stage(rel, worktree)
            return ResolveResult(rel, "k8s-cleanup", True, "deterministic k8s OTel strip (clean-merge re-introduction)")
        except Exception as exc:  # noqa: BLE001
            return ResolveResult(rel, "k8s-cleanup", False, f"k8s transform failed: {exc}", needs_review=True)
    return source_llm.cleanup_file(rel, worktree, theirs_ref, base_sha)


def clean_residual(
    worktree,
    already_handled: set[str],
    theirs_ref: str,
    base_sha: str,
    max_rounds: int = 3,
) -> list[ResolveResult]:
    worktree = Path(worktree)
    handled = set(already_handled)
    results: list[ResolveResult] = []

    for _round in range(max_rounds):
        # 1. Files under a deleted observability directory → remove.
        forbidden = [
            _rel(p, worktree)
            for p in otel_rules.find_forbidden_paths(worktree)
            if _rel(p, worktree) not in handled
        ]
        # 2. Other files still containing OTel (content rules), excluding the
        #    synthetic forbidden-path rule and anything already dealt with.
        tree = otel_rules.scan_tree(worktree)
        otel_files: set[str] = set()
        for rule_id, files in tree.items():
            if rule_id == "forbidden_telemetry_path":
                continue
            for path in files:
                rel = _rel(path, worktree)
                if rel not in handled:
                    otel_files.add(rel)

        if not forbidden and not otel_files:
            break

        for rel in forbidden:
            gitops.remove_path(rel, worktree)
            handled.add(rel)
            results.append(ResolveResult(rel, "residual-rm", True, "telemetry-dir file removed (clean-merge re-introduction)"))

        for rel in sorted(otel_files):
            result = _clean_one(rel, worktree, theirs_ref, base_sha)
            handled.add(rel)
            results.append(result)

    return results
