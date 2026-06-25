"""
Propagate upstream file deletions that a git merge silently preserves.

When our fork renames a file that upstream later deletes, git can't connect
"the file they deleted" to "the file we renamed" — no conflict is raised, and our
renamed copy survives, quietly retaining something upstream removed. (Concrete
case: upstream dropped its custom `src/postgres/Dockerfile`; we'd renamed it to
`src/postgresql/Dockerfile`, so the merge kept ours forever.)

Same-path deletions are already handled: a clean merge drops a file we didn't
touch, and a modify/delete conflict goes to the `deleted` resolver. The gap this
closes is the *renamed* survivor — detected via git rename detection between the
merge base and the merged worktree, filtered to files upstream actually deleted.

The result is a reviewable branch/PR; every removal is logged and shown in the
report and diff. `config.KEEP_DESPITE_UPSTREAM_DELETION` exempts files the fork
deliberately keeps.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config
import gitops
from resolvers.common import ResolveResult

log = logging.getLogger("sync.deletions")


def propagate_deletions(worktree, base: str, target: str) -> list[ResolveResult]:
    """Remove files upstream deleted (base→target) that survived the merge as renames.

    Returns one ResolveResult per removal. Files in
    config.KEEP_DESPITE_UPSTREAM_DELETION are left in place.
    """
    worktree = Path(worktree)
    # The worktree shares the object store, so it can resolve base/target too.
    deleted = gitops.deleted_between(base, target, cwd=worktree)
    if not deleted:
        return []

    keep = config.KEEP_DESPITE_UPSTREAM_DELETION
    renames = gitops.renames_in_worktree(base, worktree)
    results: list[ResolveResult] = []

    for old, new in sorted(renames.items()):
        if old not in deleted or new in keep:
            continue
        if not (worktree / new).exists():
            continue
        gitops.remove_path(new, worktree)
        log.info("Removed %s — renamed copy of upstream-deleted %s", new, old)
        results.append(ResolveResult(
            new, "upstream-deleted", True,
            f"upstream deleted `{old}`; removed our renamed copy",
        ))
    return results
