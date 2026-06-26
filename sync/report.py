"""
Assemble and render the sync report — both a machine-readable JSON artifact and a
human-readable markdown summary for the PR body / local review.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def _ser(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def build(
    *,
    status: str,
    target_tag: str,
    target_commit: str,
    base_commit: str,
    synced_at: str,
    probe: dict,
    resolutions: list,
    residual: list,
    lockfiles: list,
    unresolved: list[str],
    tests: dict | None,
    notes: list[str],
    deletions: list | None = None,
) -> dict:
    return {
        "status": status,                  # dry-run | ready | needs-review | aborted | up-to-date
        "upstream_tag": target_tag,
        "upstream_commit": target_commit,
        "merge_base": base_commit,
        "synced_at": synced_at,
        "conflicts": probe,
        "resolutions": [_ser(r) for r in resolutions],
        "residual_cleanup": [_ser(r) for r in residual],
        "upstream_deletions": [_ser(r) for r in (deletions or [])],
        "lockfiles": [_ser(r) for r in lockfiles],
        "unresolved": unresolved,
        "tests": tests,
        "notes": notes,
    }


def render_markdown(r: dict) -> str:
    L: list[str] = []
    L.append(f"# Upstream sync — `{r['upstream_tag']}`  ·  status: **{r['status']}**")
    L.append("")
    L.append(f"- Upstream commit: `{r['upstream_commit'][:12]}`")
    L.append(f"- Merge base: `{r['merge_base'][:12]}`")
    L.append(f"- Synced at: {r['synced_at']}")
    c = r["conflicts"]
    L.append(
        f"- Conflicts: {c.get('total', 0)} "
        f"({c.get('content', 0)} content, {c.get('modify_delete', 0)} modify/delete, "
        f"{c.get('source', 0)} source)"
    )
    L.append("")

    # Dry run: the "resolutions" entries are a routing plan, not results.
    if r["status"] == "dry-run":
        L.append("## Routing plan (no merge performed)")
        from collections import Counter
        counts = Counter(x.get("resolver", "?") for x in r["resolutions"])
        L.append("- " + ", ".join(f"**{k}**: {v}" for k, v in sorted(counts.items())))
        L.append("")
        for x in r["resolutions"]:
            L.append(f"  - `{x['path']}` ({x.get('code', '?')}) → {x.get('resolver', '?')}")
        L.append("")
        if r["notes"]:
            L.append("## Notes")
            for n in r["notes"]:
                L.append(f"- {n}")
        return "\n".join(L)

    review = [x for x in r["resolutions"] + r["residual_cleanup"]
              if x.get("method") and (x.get("needs_review") or not x.get("ok", True))]
    if r["unresolved"]:
        L.append("## ⚠️ Unresolved conflicts (left with markers — must be resolved by hand)")
        for p in r["unresolved"]:
            L.append(f"- `{p}`")
        L.append("")
    if review:
        L.append("## ⚠️ Needs review")
        for x in review:
            L.append(f"- `{x['path']}` — {x['method']}: {x['detail']}")
        L.append("")

    if r["resolutions"]:
        L.append("## Conflict resolutions")
        by_method: dict[str, list] = {}
        for x in r["resolutions"]:
            by_method.setdefault(x["method"], []).append(x)
        for method, items in sorted(by_method.items()):
            L.append(f"- **{method}** ({len(items)}): " + ", ".join(f"`{i['path']}`" for i in items))
        L.append("")

    if r["residual_cleanup"]:
        L.append("## Residual OTel cleanup (clean-merge re-introductions)")
        for x in r["residual_cleanup"]:
            flag = "" if x["ok"] else " ⚠️"
            L.append(f"- `{x['path']}` — {x['method']}: {x['detail']}{flag}")
        L.append("")

    if r.get("upstream_deletions"):
        L.append("## Propagated upstream deletions")
        L.append("Files upstream removed that the merge kept (our renamed copies) — now deleted:")
        for x in r["upstream_deletions"]:
            flag = "" if x["ok"] else " ⚠️"
            L.append(f"- `{x['path']}` — {x['detail']}{flag}")
        L.append("")

    if r["lockfiles"]:
        L.append("## Lockfile regeneration")
        for x in r["lockfiles"]:
            mark = {"ok": "✓", "failed": "✗", "skipped-no-tool": "—"}.get(x["status"], "?")
            L.append(f"- {mark} `{x['directory']}` ({x['tool']}): {x['detail']}")
        L.append("")

    if r["tests"] is not None:
        t = r["tests"]
        L.append("## Verification (test suite)")
        L.append(f"- `{t.get('command', '')}` → {'PASSED' if t.get('passed') else 'FAILED'} (exit {t.get('returncode')})")
        L.append("")

    if r["notes"]:
        L.append("## Notes")
        for n in r["notes"]:
            L.append(f"- {n}")
        L.append("")

    return "\n".join(L)


def write(report: dict, json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
