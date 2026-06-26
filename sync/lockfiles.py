"""
Regenerate lockfiles after dependency manifests were de-otelified.

We never hand-merge a lockfile — we take upstream's, then regenerate it from the
de-oteled manifest so transitive deps that existed only to satisfy OTel drop out.
Each ecosystem's regen is gated on its toolchain being installed; a missing tool
is reported (and fails the gate) rather than silently leaving a stale lockfile.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# manifest filename → (tool executable, regen command run in the manifest's dir)
_ECOSYSTEMS = {
    "go.mod": ("go", ["go", "mod", "tidy"]),
    "package.json": ("npm", ["npm", "install", "--package-lock-only"]),
    "composer.json": ("composer", ["composer", "update", "--no-install"]),
    "Cargo.toml": ("cargo", ["cargo", "generate-lockfile"]),
    "mix.exs": ("mix", ["mix", "deps.get"]),
}


@dataclass
class LockfileResult:
    directory: str
    tool: str
    status: str   # "ok" | "failed" | "skipped-no-tool"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def regenerate(worktree, dependency_paths: list[str], timeout: int = 600) -> list[LockfileResult]:
    """Regenerate lockfiles for every manifest among `dependency_paths`."""
    worktree = Path(worktree)
    # Unique (directory, manifest-name) pairs that have a known regen recipe.
    jobs: dict[tuple[str, str], None] = {}
    for rel in dependency_paths:
        name = Path(rel).name
        if name in _ECOSYSTEMS:
            jobs[(str(Path(rel).parent), name)] = None

    results: list[LockfileResult] = []
    for (directory, name) in sorted(jobs):
        tool, command = _ECOSYSTEMS[name]
        workdir = worktree / directory
        if shutil.which(tool) is None:
            results.append(LockfileResult(directory, tool, "skipped-no-tool",
                                          f"{tool} not on PATH — lockfile not regenerated"))
            continue
        try:
            proc = subprocess.run(
                command, cwd=str(workdir), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            results.append(LockfileResult(directory, tool, "failed", f"{tool} timed out after {timeout}s"))
            continue
        if proc.returncode == 0:
            results.append(LockfileResult(directory, tool, "ok", f"{' '.join(command)} succeeded"))
        else:
            results.append(LockfileResult(directory, tool, "failed",
                                          (proc.stderr or proc.stdout).strip()[:500]))
    return results
