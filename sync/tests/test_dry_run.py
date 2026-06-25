"""
Integration guard: probe a real merge against a pinned upstream commit and assert
every conflicted path routes to a known resolver. Catches upstream drift that would
introduce an unroutable file type. Skips when the pinned commit isn't fetched.
"""

import pytest

import gitops
from resolvers import dispatch

# Feb-2026 upstream commit — the ~36-conflict delta the pipeline was designed against.
PINNED = "5155119584b8d71d0858d5efe50d35a903dc0ab3"


def _available() -> bool:
    try:
        return gitops.rev_parse(PINNED, check=False) is not None
    except gitops.GitError:
        return False


@pytest.mark.skipif(not _available(), reason="pinned upstream commit not fetched")
def test_every_probed_conflict_routes():
    probe = gitops.merge_tree_probe("HEAD", PINNED)
    if probe is None:
        pytest.skip("merge is conflict-free against HEAD")
    codes = {p: "DU" for p in probe.modify_delete_paths}
    codes.update({p: "UU" for p in probe.content_paths})
    unrouted = [p for p, c in codes.items() if dispatch.classify(p, c) == "unhandled"]
    assert not unrouted, f"unroutable conflict paths: {unrouted}"
