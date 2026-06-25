"""
Route each conflicted path to the resolver that should handle it.

Routing (checked in order):
  modify/delete (DU/UD)           → deleted        (stay-removed / keep-ours)
  lockfile (go.sum, *-lock.json…) → lockfile        (take upstream, regenerate later)
  dependency manifest             → deps            (deterministic strip)
  kubernetes/*.yaml               → k8s             (deterministic transform)
  everything else (source code,   → llm             (3-way LLM resolution)
    docker-compose, .env, Dockerfile, other config/text)

The genuinely-entangled infra/config files (docker-compose.yml, .env, Dockerfile)
go to the LLM rather than a deterministic strip: our HEAD carries rich non-OTel
local divergences (healthcheck, custom base images, the postgres rename) that a
"take upstream, strip OTel" pass would silently discard. The LLM sees base/ours/
theirs and produces a correct 3-way resolution. The k8s manifest is the one infra
file that stays deterministic — it's too large for an LLM and is a generated
artifact, so "take upstream, de-otelify structurally" is the right model.
"""

from __future__ import annotations

from typing import Callable

import config
import gitops
from resolvers import deleted, deps, k8s
from resolvers.common import ResolveResult

LlmResolver = Callable[[str, object], ResolveResult]

_MODIFY_DELETE_CODES = {"DU", "UD"}


def classify(path: str, code: str) -> str:
    if code in _MODIFY_DELETE_CODES:
        return "deleted"
    if config.is_lockfile(path):
        return "lockfile"
    if config.is_dependency_file(path):
        return "deps"
    if path.startswith("kubernetes/") and path.endswith((".yaml", ".yml")):
        return "k8s"
    return "llm"


def _resolve_lockfile(path: str, worktree) -> ResolveResult:
    # Lockfiles are never hand-merged. Take upstream's and let regeneration (run
    # after source/dep resolution) bring it into sync with the de-oteled manifest.
    gitops.checkout_side(path, "theirs", worktree)
    gitops.stage(path, worktree)
    return ResolveResult(path, "lockfile", True, "took upstream lockfile; pending regeneration")


def resolve_all(
    conflict_codes: dict[str, str],
    worktree,
    llm_resolve: LlmResolver,
) -> list[ResolveResult]:
    """Resolve every conflicted path; return one ResolveResult per path."""
    results: list[ResolveResult] = []
    for path in sorted(conflict_codes):
        code = conflict_codes[path]
        category = classify(path, code)
        if category == "deleted":
            results.append(deleted.resolve(path, code, worktree))
        elif category == "lockfile":
            results.append(_resolve_lockfile(path, worktree))
        elif category == "deps":
            results.append(deps.resolve(path, worktree))
        elif category == "k8s":
            results.append(k8s.resolve(path, worktree))
        else:
            results.append(llm_resolve(path, worktree))
    return results
