"""
Unit tests for the modify/delete conflict resolver, incl. the gitignored-path case
that crashed a 3.0.0 sync (upstream tracks src/react-native-app/ios/*, the fork
gitignores it → `git add` of the conflict path errored).
"""

import subprocess
from pathlib import Path

import gitops
from resolvers import deleted


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                        "HOME": str(cwd), "PATH": __import__("os").environ["PATH"]})


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q", "-b", "main"], r)
    (r / ".gitignore").write_text("ignored/\n")
    (r / ".gitkeep").write_text("")
    _git(["add", "-A"], r)
    _git(["commit", "-q", "-m", "base"], r)
    return r


def test_gitignored_conflict_path_kept_untracked(tmp_path):
    repo = _repo(tmp_path)
    # A merge leaves the path IN THE INDEX (the other side staged it), even though
    # the fork gitignores it — force-add to reproduce that tracked-conflict state.
    (repo / "ignored").mkdir()
    (repo / "ignored" / "build.lock").write_text("from upstream\n")
    _git(["add", "-f", "ignored/build.lock"], repo)
    # is_ignored must see through the index (via --no-index) and still report ignored.
    assert gitops.is_ignored("ignored/build.lock", repo)

    # Previously this hit `git add` and crashed; now it's recognized as ignored.
    res = deleted.resolve("ignored/build.lock", "UD", repo)
    assert res.ok
    assert res.method == "deleted-gitignored"
    assert not res.needs_review


def test_non_ignored_paths_resolve_normally(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("our version\n")   # "ours" exists for UD keep-ours
    assert not gitops.is_ignored("src/foo.py", repo)
    assert deleted.resolve("src/foo.py", "DU", repo).method == "deleted-stay-removed"
    ud = deleted.resolve("src/foo.py", "UD", repo)
    assert ud.method == "deleted-keep-ours" and ud.needs_review
