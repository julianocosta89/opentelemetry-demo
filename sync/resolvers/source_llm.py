"""
LLM resolver for the handful of conflicts that need real judgment.

Routed here: source-code conflicts (where upstream's change entangles a non-OTel
improvement with OTel plumbing) and the mixed infra/config files (docker-compose,
.env, Dockerfile) whose 3-way merge our local divergences make non-mechanical.

The model gets all three sides — merge-base, ours, theirs — plus the conflicted
file with markers and the de-otelification policy, and returns the complete
resolved file. Every candidate is validated (no markers left, no OTel per the
shared rules, syntax-checks where cheap); failures retry with the errors fed back.
A file that never validates is left with its markers and flagged for a human —
never committed broken.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import config
import gitops
import llm
import otel_rules
from resolvers.common import ResolveResult

_START = "<<<RESOLVED_FILE>>>"
_END = "<<<END_RESOLVED_FILE>>>"
_MARKERS = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")

_SYSTEM = """You resolve git merge conflicts for a fork of the OpenTelemetry demo \
that has ALL OpenTelemetry instrumentation removed ("de-otelification"). Upstream \
keeps evolving; you are merging an upstream release into our de-otelified branch.

Your job for the file you are given:
1. Resolve EVERY conflict marker — no <<<<<<<, =======, or >>>>>>> may remain.
2. Remove all OpenTelemetry instrumentation: OTel imports/packages, tracer/meter \
setup, span creation and attributes, context propagation, metrics, and .NET \
ActivitySource/Activity tracing. Remove OTEL_* environment variables and \
observability services/config.
3. PRESERVE upstream's non-OTel improvements. When upstream refactored code in a \
way that is entangled with OTel (e.g. switching to an explicit request object to \
thread a context), keep the structural improvement but drop the OTel parts — and \
adapt it to OUR types. Do not reference fields/helpers that exist only on upstream's \
OTel-carrying version (e.g. a shared client our struct doesn't have); use what our \
side actually defines.
4. Preserve all business logic, comments, and non-OTel configuration.

You are given the merge base (common ancestor), OUR version (de-otelified), \
THEIRS (upstream), and the conflicted file. Use OUR version to see what we \
deliberately changed.

Output ONLY the complete resolved file content, wrapped exactly like:
<<<RESOLVED_FILE>>>
...entire file...
<<<END_RESOLVED_FILE>>>
No explanation, no markdown fences."""


def _extract(text: str) -> str | None:
    if _START not in text or _END not in text:
        return None
    body = text.split(_START, 1)[1].split(_END, 1)[0]
    # Drop a single leading/trailing newline introduced by the sentinels.
    if body.startswith("\n"):
        body = body[1:]
    if body.endswith("\n"):
        body = body[:-1]
    return body


def _gofmt_error(content: str) -> str | None:
    """Return a gofmt error string for Go content, or None if it parses / gofmt absent."""
    try:
        proc = subprocess.run(
            ["gofmt", "-e"], input=content, capture_output=True, text=True
        )
    except FileNotFoundError:
        return None  # toolchain absent — defer to the test gate
    return proc.stderr.strip() or None


def _validate(path: str, content: str) -> list[str]:
    problems: list[str] = []
    leftover = [m for m in _MARKERS if any(line.startswith(m) for line in content.splitlines())]
    if leftover:
        problems.append(f"conflict markers still present: {leftover}")
    otel = otel_rules.scan_text(path, content)
    if otel:
        shown = "; ".join(f"line {ln}: {txt}" for _rid, ln, txt in otel[:8])
        problems.append(f"OpenTelemetry references remain ({len(otel)}): {shown}")
    if path.endswith(".go"):
        err = _gofmt_error(content)
        if err:
            problems.append(f"gofmt rejects the result: {err.splitlines()[0]}")
    return problems


def _build_user(path: str, base: str | None, ours: str | None, theirs: str | None,
                conflicted: str, prior_problems: list[str]) -> str:
    def section(title: str, body: str | None) -> str:
        return f"### {title}\n```\n{body if body is not None else '(absent)'}\n```\n"

    parts = [
        f"File: `{path}`\n",
        section("MERGE BASE (common ancestor)", base),
        section("OURS (current de-otelified version)", ours),
        section("THEIRS (incoming upstream version)", theirs),
        section("CONFLICTED FILE (resolve this — markers included)", conflicted),
    ]
    if prior_problems:
        parts.append(
            "Your previous attempt was rejected for these reasons — fix them:\n"
            + "\n".join(f"- {p}" for p in prior_problems)
            + "\n"
        )
    parts.append(f"Return the resolved `{path}` wrapped in {_START} / {_END}.")
    return "\n".join(parts)


def make_resolver(theirs_ref: str, base_sha: str) -> Callable[[str, object], ResolveResult]:
    """Build a resolver bound to this sync's upstream ref and merge base."""

    def resolve(path: str, worktree) -> ResolveResult:
        worktree = Path(worktree)
        conflicted = (worktree / path).read_text(encoding="utf-8", errors="replace")
        base = gitops.show(base_sha, path, worktree)
        ours = gitops.show("HEAD", path, worktree)
        theirs = gitops.show(theirs_ref, path, worktree)

        problems: list[str] = []
        for attempt in range(1, config.LLM_MAX_RETRIES + 1):
            user = _build_user(path, base, ours, theirs, conflicted, problems)
            try:
                raw = llm.complete(_SYSTEM, user)
            except llm.LLMRefusal:
                return ResolveResult(path, "llm", False, "model refused — left conflict for review", needs_review=True)
            except llm.LLMError as exc:
                return ResolveResult(path, "llm", False, f"LLM error: {exc}", needs_review=True)

            content = _extract(raw)
            if content is None:
                problems = ["output was not wrapped in the required sentinels"]
                continue
            problems = _validate(path, content)
            if not problems:
                gitops.write_resolved(path, content, worktree)
                detail = "resolved by LLM" + (f" (attempt {attempt})" if attempt > 1 else "")
                return ResolveResult(path, "llm", True, detail)

        return ResolveResult(
            path, "llm", False,
            f"unresolved after {config.LLM_MAX_RETRIES} attempts: {problems[0] if problems else 'unknown'}",
            needs_review=True,
        )

    return resolve


def cleanup_file(path: str, worktree, theirs_ref: str, base_sha: str) -> ResolveResult:
    """
    Clean residual OTel from a file that merged cleanly (no conflict) but still
    trips the OTel scan — e.g. upstream added a span in a region we didn't touch.
    Reuses the same validate/retry machinery, but there are no conflict markers.
    """
    resolver = make_resolver(theirs_ref, base_sha)
    result = resolver(path, worktree)
    result.method = "llm-cleanup"
    return result
