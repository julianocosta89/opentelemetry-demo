"""
LLM resolver tests with the model call mocked, so the validate/retry machinery
is exercised deterministically (no network, no API key).
"""

import llm
import gitops
from resolvers import source_llm

GOOD_GO = "package main\n\nfunc main() {}\n"
OTEL_GO = 'package main\n\nimport "go.opentelemetry.io/otel"\n\nfunc main() {}\n'


def _wrap(content: str) -> str:
    return f"prose\n{source_llm._START}\n{content}\n{source_llm._END}\nmore prose"


def _setup(monkeypatch, tmp_path, completions):
    """Wire a fake worktree + a scripted sequence of llm.complete() responses."""
    path = "src/checkout/main.go"
    full = tmp_path / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("<<<<<<< HEAD\n=======\n>>>>>>> upstream\n")

    monkeypatch.setattr(gitops, "show", lambda ref, p, cwd=None: "base/ours/theirs stub")

    written = {}

    def fake_write(p, content, worktree):
        (tmp_path / p).write_text(content)
        written[p] = content

    monkeypatch.setattr(gitops, "write_resolved", fake_write)
    monkeypatch.setattr(gitops, "stage", lambda p, worktree: None)

    seq = iter(completions)
    monkeypatch.setattr(llm, "complete", lambda system, user, **kw: next(seq))

    resolver = source_llm.make_resolver("vX.Y.Z", "basesha")
    return resolver, path, written


def test_happy_path(monkeypatch, tmp_path):
    resolver, path, written = _setup(monkeypatch, tmp_path, [_wrap(GOOD_GO)])
    result = resolver(path, tmp_path)
    assert result.ok and not result.needs_review
    assert written[path] == GOOD_GO


def test_retries_then_succeeds(monkeypatch, tmp_path):
    bad = _wrap("<<<<<<< still has markers\n=======\n>>>>>>> x")
    resolver, path, written = _setup(monkeypatch, tmp_path, [bad, _wrap(GOOD_GO)])
    result = resolver(path, tmp_path)
    assert result.ok
    assert "attempt 2" in result.detail


def test_persistent_markers_flagged(monkeypatch, tmp_path):
    bad = _wrap("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> up")
    resolver, path, written = _setup(monkeypatch, tmp_path, [bad, bad, bad])
    result = resolver(path, tmp_path)
    assert not result.ok and result.needs_review
    assert path not in written  # a broken file is never written/staged


def test_residual_otel_rejected(monkeypatch, tmp_path):
    # gofmt accepts it (valid Go), but the OTel import must be caught and retried.
    resolver, path, written = _setup(monkeypatch, tmp_path, [_wrap(OTEL_GO)] * 3)
    result = resolver(path, tmp_path)
    assert not result.ok and result.needs_review


def test_missing_sentinels_flagged(monkeypatch, tmp_path):
    resolver, path, written = _setup(monkeypatch, tmp_path, ["just prose, no sentinels"] * 3)
    result = resolver(path, tmp_path)
    assert not result.ok and result.needs_review


def test_refusal_flagged(monkeypatch, tmp_path):
    resolver, path, written = _setup(monkeypatch, tmp_path, [_wrap(GOOD_GO)])

    def refuse(system, user, **kw):
        raise llm.LLMRefusal("nope")

    monkeypatch.setattr(llm, "complete", refuse)
    result = resolver(path, tmp_path)
    assert not result.ok and result.needs_review
