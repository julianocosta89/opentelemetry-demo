"""Shared result type for conflict resolvers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResolveResult:
    """Outcome of resolving a single conflicted path."""

    path: str
    method: str          # "deps" | "deleted-stay-removed" | "deleted-keep-ours" | "k8s" | "llm" | "llm-cleanup" | "unhandled"
    ok: bool
    detail: str = ""
    needs_review: bool = False   # surfaced prominently in the report for a human

    def __str__(self) -> str:
        flag = " [REVIEW]" if self.needs_review else ""
        state = "ok" if self.ok else "FAILED"
        return f"{self.path}: {self.method} ({state}){flag} — {self.detail}"
