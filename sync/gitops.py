"""
Thin, well-typed wrappers around the git commands the sync pipeline needs.

Design notes:
  * Every git call runs under LC_ALL=C so messages are English and parseable
    regardless of the operator's locale (the original tooling broke on a German
    locale because it parsed "CONFLICT (modify/delete)" message strings).
  * Conflict classification after a real merge uses `git status --porcelain` XY
    codes, which are locale-independent. The pre-merge probe parses merge-tree
    `--messages` output, which is why LC_ALL=C matters there too.
  * All mutating operations target a throwaway worktree, never the user's tree.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from config import REPO_ROOT, RELEASE_TAG_RE, UPSTREAM_REMOTE, UPSTREAM_URL

# Environment that forces portable, non-interactive git behaviour.
_GIT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"}


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path = REPO_ROOT, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_GIT_ENV,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} (cwd={cwd}) failed [{result.returncode}]:\n{result.stderr.strip()}"
        )
    return result


# ─── Remote / tags ────────────────────────────────────────────────────────────


def ensure_upstream_remote(cwd: Path = REPO_ROOT) -> None:
    """Make sure an `upstream` remote pointing at the canonical repo exists."""
    remotes = _git(["remote"], cwd).stdout.split()
    if UPSTREAM_REMOTE not in remotes:
        _git(["remote", "add", UPSTREAM_REMOTE, UPSTREAM_URL], cwd)


def fetch_upstream(cwd: Path = REPO_ROOT) -> None:
    _git(["fetch", UPSTREAM_REMOTE, "--tags", "--prune"], cwd)


def list_release_tags(cwd: Path = REPO_ROOT) -> list[str]:
    """Return release tags (vX.Y.Z) sorted ascending by semantic version."""
    out = _git(["tag", "--list"], cwd).stdout.splitlines()
    versioned: list[tuple[tuple[int, int, int], str]] = []
    for tag in out:
        tag = tag.strip()
        m = RELEASE_TAG_RE.match(tag)
        if m:
            versioned.append(((int(m[1]), int(m[2]), int(m[3])), tag))
    versioned.sort()
    return [tag for _v, tag in versioned]


def latest_release_tag(cwd: Path = REPO_ROOT) -> str:
    tags = list_release_tags(cwd)
    if not tags:
        raise GitError("No release tags (vX.Y.Z) found on upstream — did you fetch --tags?")
    return tags[-1]


def resolve_target_tag(requested: str | None, cwd: Path = REPO_ROOT) -> str:
    """Resolve the tag to sync to: explicit request, or the latest release."""
    if requested:
        if requested not in list_release_tags(cwd) and not rev_parse(requested, cwd, check=False):
            raise GitError(f"Requested tag/ref '{requested}' not found")
        return requested
    return latest_release_tag(cwd)


# ─── Revisions ────────────────────────────────────────────────────────────────


def rev_parse(ref: str, cwd: Path = REPO_ROOT, check: bool = True) -> str | None:
    result = _git(["rev-parse", "--verify", "--quiet", ref], cwd, check=False)
    sha = result.stdout.strip()
    if not sha:
        if check:
            raise GitError(f"Cannot resolve ref '{ref}'")
        return None
    return sha


def merge_base(a: str, b: str, cwd: Path = REPO_ROOT) -> str:
    return _git(["merge-base", a, b], cwd).stdout.strip()


def show(ref: str, path: str, cwd: Path = REPO_ROOT) -> str | None:
    """Content of `path` at `ref`, or None if it does not exist there."""
    result = _git(["show", f"{ref}:{path}"], cwd, check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def deleted_between(base: str, target: str, cwd: Path = REPO_ROOT) -> set[str]:
    """Paths that `target` deleted relative to `base`.

    `-M` so a file upstream *renamed* counts as a rename (R), not a deletion — we
    only want files genuinely removed.
    """
    out = _git(["diff", "-M", "--diff-filter=D", "--name-only", base, target], cwd).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def renames_in_worktree(base: str, worktree: Path) -> dict[str, str]:
    """Map old→new for files renamed between `base` and the worktree's current tree.

    Compares `base` against the working tree (the merged, not-yet-committed state),
    with rename detection so a renamed-and-lightly-modified file is still matched.
    """
    out = _git(["diff", "-M", "--diff-filter=R", "--name-status", base], worktree).stdout
    renames: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith("R"):
            renames[parts[1]] = parts[2]
    return renames


# ─── Pre-merge probe (no mutation) ────────────────────────────────────────────


@dataclass
class MergeProbe:
    tree_oid: str
    conflicted_paths: list[str] = field(default_factory=list)
    modify_delete_paths: list[str] = field(default_factory=list)
    rename_paths: list[str] = field(default_factory=list)

    @property
    def content_paths(self) -> list[str]:
        special = set(self.modify_delete_paths)
        return [p for p in self.conflicted_paths if p not in special]


def merge_tree_probe(ours: str, theirs: str, cwd: Path = REPO_ROOT) -> MergeProbe | None:
    """
    Compute the merge of `theirs` into `ours` WITHOUT touching the working tree.

    Returns a MergeProbe, or None if the merge is clean (no conflicts).

    Output format of `git merge-tree --write-tree --name-only --messages`:
        <tree-oid>
        <conflicted-path-1>
        ...
        <conflicted-path-N>
        <blank line>
        <informational messages: "CONFLICT (modify/delete): <path> ...", etc.>
    """
    result = _git(
        ["merge-tree", "--write-tree", "--name-only", "--messages", ours, theirs],
        cwd,
        check=False,
    )
    lines = result.stdout.splitlines()
    if not lines:
        raise GitError(f"merge-tree produced no output:\n{result.stderr}")

    tree_oid = lines[0]
    if result.returncode == 0:
        return None  # clean merge

    # Split into the conflicted-paths section and the messages section.
    try:
        blank = lines.index("", 1)
    except ValueError:
        blank = len(lines)
    conflicted = [ln for ln in lines[1:blank] if ln]
    messages = lines[blank + 1 :]

    probe = MergeProbe(tree_oid=tree_oid, conflicted_paths=conflicted)
    for msg in messages:
        if "CONFLICT (modify/delete):" in msg:
            # "CONFLICT (modify/delete): <path> deleted in HEAD and modified in ..."
            after = msg.split("CONFLICT (modify/delete):", 1)[1].strip()
            path = after.split(" deleted in ", 1)[0].split(" modified in ", 1)[0].strip()
            if path:
                probe.modify_delete_paths.append(path)
        elif "CONFLICT (rename" in msg:
            probe.rename_paths.append(msg.strip())
    return probe


# ─── Worktree lifecycle ───────────────────────────────────────────────────────


def worktree_add(path: Path, branch: str, base: str, cwd: Path = REPO_ROOT) -> None:
    """Create a fresh worktree at `path` on a new `branch` based on `base`.

    Idempotent: any pre-existing worktree/branch is removed first, so a re-run
    starts from a clean slate.
    """
    worktree_remove(path, cwd)
    _git(["worktree", "prune"], cwd)
    # -B resets the branch if it already exists.
    _git(["worktree", "add", "-B", branch, str(path), base], cwd)


def worktree_remove(path: Path, cwd: Path = REPO_ROOT) -> None:
    _git(["worktree", "remove", "--force", str(path)], cwd, check=False)
    if path.exists():
        # Fall back to a manual cleanup if git refused (e.g. dir not a worktree).
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    _git(["worktree", "prune"], cwd)


# ─── Merge + conflict inspection (in the worktree) ────────────────────────────


def merge_no_commit(ref: str, worktree: Path) -> subprocess.CompletedProcess:
    """Start the merge; expected to stop with conflicts (non-zero) or clean (zero)."""
    return _git(["merge", "--no-commit", "--no-ff", ref], worktree, check=False)


def unmerged_paths(worktree: Path) -> list[str]:
    out = _git(["diff", "--name-only", "--diff-filter=U"], worktree).stdout
    return [ln for ln in out.splitlines() if ln]


def conflict_status(worktree: Path) -> dict[str, str]:
    """
    Map every conflicted path to its porcelain XY code (locale-independent).

    Codes of interest:
      DU = deleted by us,   modified by them  → we removed it, keep removed
      UD = modified by us,  deleted by them   → upstream removed; accept deletion
      AU/UA = added by one side only
      AA/DD/UU = both-added / both-deleted / both-modified content conflict
    """
    out = _git(["status", "--porcelain=v1", "-z"], worktree).stdout
    codes: dict[str, str] = {}
    for entry in out.split("\0"):
        if len(entry) < 4:
            continue
        xy = entry[:2]
        path = entry[3:]
        if xy in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
            codes[path] = xy
    return codes


def stage(path: str, worktree: Path) -> None:
    _git(["add", "--", path], worktree)


def remove_path(path: str, worktree: Path) -> None:
    _git(["rm", "-f", "--", path], worktree, check=False)


def stage_all(worktree: Path) -> None:
    """Stage every change in the worktree (adds, mods, deletes).

    Steps after conflict resolution (lockfile regeneration, residual cleanup)
    modify files without re-staging them, so commit only what was staged would
    omit them — and the test gate runs against the working tree, so the commit
    must match it. The worktree is entirely pipeline-controlled output, so staging
    everything guarantees committed state == tested state.
    """
    _git(["add", "-A"], worktree)


def checkout_side(path: str, side: str, worktree: Path) -> None:
    """Resolve a path by taking one side wholesale: side in {'ours', 'theirs'}."""
    _git(["checkout", f"--{side}", "--", path], worktree)


def write_resolved(path: str, content: str, worktree: Path) -> None:
    full = worktree / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    stage(path, worktree)


def commit(message: str, worktree: Path) -> None:
    _git(["commit", "--no-verify", "-m", message], worktree)


def abort_merge(worktree: Path) -> None:
    _git(["merge", "--abort"], worktree, check=False)
