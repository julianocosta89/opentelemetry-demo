"""
Resolve modify/delete conflicts.

When one side deleted a file the other side modified, git cannot auto-merge.
De-otelification deleted a lot of observability infrastructure, so the common
case is "we deleted it, upstream kept editing it" — which resolves to "stay
deleted". The mirror case (upstream deleted something we edited) is rarer and is
kept-ours plus a review flag, since our edit might be load-bearing.
"""

from __future__ import annotations

import gitops
from resolvers.common import ResolveResult


def resolve(path: str, code: str, worktree) -> ResolveResult:
    if gitops.is_ignored(path, worktree):
        # This fork's .gitignore covers the path (e.g. build artifacts upstream
        # tracks but we don't, like src/react-native-app/ios/). It must not be
        # tracked — drop whatever the merge left, keeping it untracked. Staging it
        # would fail ("paths are ignored"); keeping ours would re-track it.
        gitops.remove_path(path, worktree)
        return ResolveResult(
            path, "deleted-gitignored", True,
            "path is gitignored in this fork — kept untracked",
        )
    if code == "DU":
        # Deleted by us, modified by them → we removed this (e.g. otel-collector
        # config); keep it removed. Git left upstream's version in the tree.
        gitops.remove_path(path, worktree)
        return ResolveResult(
            path, "deleted-stay-removed", True,
            "we deleted it during de-otelification; upstream modified it — kept removed",
        )
    if code == "UD":
        # Modified by us, deleted by them → upstream removed a file we still edit.
        # Keep ours (the working copy is our version) but flag it: upstream may
        # have intentionally retired this file.
        gitops.stage(path, worktree)
        return ResolveResult(
            path, "deleted-keep-ours", True,
            "upstream deleted it but we modified it — kept ours",
            needs_review=True,
        )
    return ResolveResult(
        path, "unhandled", False,
        f"unexpected modify/delete status code {code!r}",
        needs_review=True,
    )
