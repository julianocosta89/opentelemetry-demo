"""
Unit tests for propagating upstream deletions a merge kept as renames.

Builds a throwaway git repo so the real rename detection (git diff -M) is exercised,
not a mock: base has a file, "upstream" deletes it, the worktree carries a renamed
(and lightly modified) copy — which the check must remove.
"""

import subprocess
from pathlib import Path

import config
import deletions as deletions_mod
import gitops


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                        "HOME": str(cwd), "PATH": __import__("os").environ["PATH"]})


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q", "-b", "main"], r)
    return r


def _commit(repo: Path, msg: str) -> str:
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", msg], repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()


def test_removes_renamed_copy_of_upstream_deleted_file(tmp_path):
    repo = _repo(tmp_path)
    # base: a Dockerfile exists at the old path
    old = repo / "src/postgres"; old.mkdir(parents=True)
    body = "FROM postgres:16\n" + "\n".join(f"RUN echo line{i}" for i in range(20)) + "\n"
    (old / "Dockerfile").write_text(body)
    base = _commit(repo, "base")

    # "upstream" deletes the Dockerfile on a separate ref
    _git(["checkout", "-q", "-b", "upstream"], repo)
    (old / "Dockerfile").unlink()
    target = _commit(repo, "upstream removes Dockerfile")
    _git(["checkout", "-q", "main"], repo)

    # our fork: the file was renamed (and lightly modified) and committed — so the
    # merge keeps it (it's tracked, no conflict with upstream's delete).
    new = repo / "src/postgresql"; new.mkdir(parents=True)
    (new / "Dockerfile").write_text(body.replace("postgres:16", "postgres:17.6"))
    (old / "Dockerfile").unlink()
    _commit(repo, "our fork: rename postgres dir")

    results = deletions_mod.propagate_deletions(repo, base, target)

    assert len(results) == 1
    assert results[0].path == "src/postgresql/Dockerfile"
    assert results[0].ok
    assert "src/postgres/Dockerfile" in results[0].detail
    assert not (new / "Dockerfile").exists()       # actually removed


def test_no_op_when_we_dont_carry_the_deleted_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "gone.txt").write_text("x\n")
    (repo / "kept.txt").write_text("y\n")
    base = _commit(repo, "base")

    _git(["checkout", "-q", "-b", "upstream"], repo)
    (repo / "gone.txt").unlink()
    target = _commit(repo, "delete gone.txt")
    _git(["checkout", "-q", "main"], repo)

    # We followed the deletion (no renamed survivor) — nothing to propagate.
    (repo / "gone.txt").unlink()

    assert deletions_mod.propagate_deletions(repo, base, target) == []


def test_allowlist_exempts_a_path(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    old = repo / "tool.sh"
    body = "#!/bin/sh\n" + "\n".join(f"echo {i}" for i in range(20)) + "\n"
    old.write_text(body)
    base = _commit(repo, "base")

    _git(["checkout", "-q", "-b", "upstream"], repo)
    old.unlink()
    target = _commit(repo, "delete tool.sh")
    _git(["checkout", "-q", "main"], repo)

    # renamed survivor, committed (so it's tracked / detected as a rename)
    old.unlink()
    (repo / "scripts").mkdir()
    (repo / "scripts/tool.sh").write_text(body + "echo extra\n")
    _commit(repo, "our fork: move tool.sh")

    monkeypatch.setattr(config, "KEEP_DESPITE_UPSTREAM_DELETION", frozenset({"scripts/tool.sh"}))
    assert deletions_mod.propagate_deletions(repo, base, target) == []
    assert (repo / "scripts/tool.sh").exists()      # exempted, not removed
