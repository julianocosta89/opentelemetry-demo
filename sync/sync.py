#!/usr/bin/env python3
"""
De-otelification upstream sync — git-merge based.

    python sync/sync.py [--tag vX.Y.Z] [--dry-run] [--no-tests] [--push]

Flow: fetch upstream → pick the release tag → probe the merge (abort if too noisy)
→ merge into a throwaway worktree → resolve conflicts (deterministic where
mechanical, LLM for entangled source) → clean residual OTel → regenerate lockfiles
→ run the test gate → commit. The user's working tree is never touched; the result
is a `sync/upstream-<tag>` branch + a review report.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sibling modules importable whether run as `python sync/sync.py` or `-m`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import base_image_mirror
import config
import deletions as deletions_mod
import gitops
import lockfiles
import report
import residual_scan
from resolvers import dispatch, k8s, source_llm

log = logging.getLogger("sync")


def _classify_all(probe: gitops.MergeProbe) -> dict[str, str]:
    """Synthesize porcelain-style codes from a probe for dry-run routing."""
    codes = {p: "DU" for p in probe.modify_delete_paths}
    codes.update({p: "UU" for p in probe.content_paths})
    return codes


def _run_tests(worktree: Path, marker: str = "fast") -> dict:
    cmd = [sys.executable, "-m", "pytest", "tests/", "-m", marker, "-q"]
    proc = subprocess.run(cmd, cwd=str(worktree), capture_output=True, text=True)
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-15:])
    return {
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "output_tail": tail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="De-otelification upstream sync (git-merge based)")
    parser.add_argument("--tag", help="Upstream ref to sync to (default: latest vX.Y.Z release tag)")
    parser.add_argument("--dry-run", action="store_true", help="Probe + report only; no merge")
    parser.add_argument("--no-tests", action="store_true", help="Skip the verification test gate")
    parser.add_argument("--push", action="store_true", help="Push the sync branch and open a PR (CI)")
    parser.add_argument("--keep-worktree", action="store_true", help="Keep the worktree even on a clean success")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    synced_at = datetime.now(timezone.utc).isoformat()
    notes: list[str] = []

    report_json = config.SYNC_DIR / "last_sync_report.json"
    report_md = config.SYNC_DIR / "last_sync_report.md"

    # 1. Fetch upstream and choose the target tag.
    gitops.ensure_upstream_remote()
    log.info("Fetching upstream %s ...", config.UPSTREAM_REMOTE)
    gitops.fetch_upstream()
    target = gitops.resolve_target_tag(args.tag)
    target_commit = gitops.rev_parse(target)
    base = gitops.merge_base("HEAD", target)
    log.info("Target=%s (%s)  merge-base=%s", target, target_commit[:12], base[:12])

    if target_commit == base:
        log.info("Already up to date with %s — nothing to merge.", target)
        return 0

    # 2. Probe the merge without touching the tree.
    probe = gitops.merge_tree_probe("HEAD", target)
    if probe is None:
        conflict_codes_preview: dict[str, str] = {}
        probe_stats = {"total": 0, "content": 0, "modify_delete": 0, "source": 0}
        notes.append("Merge is conflict-free; only clean-merge residual OTel (if any) needs cleanup.")
    else:
        conflict_codes_preview = _classify_all(probe)
        source = [p for p in probe.content_paths if config.is_source_path(p)]
        probe_stats = {
            "total": len(probe.conflicted_paths),
            "content": len(probe.content_paths),
            "modify_delete": len(probe.modify_delete_paths),
            "source": len(source),
            "rename_messages": probe.rename_paths,
        }
        log.info("Conflicts: %d total, %d source", probe_stats["total"], probe_stats["source"])

    # 3. Robustness guard — abort a too-noisy merge before spending LLM tokens.
    if probe is not None:
        src = probe_stats["source"]
        if probe_stats["total"] > config.MAX_CONFLICT_FILES or src > config.MAX_SOURCE_CONFLICT_FILES:
            notes.append(
                f"ABORTED: {probe_stats['total']} conflicts ({src} source) exceed thresholds "
                f"({config.MAX_CONFLICT_FILES}/{config.MAX_SOURCE_CONFLICT_FILES}). Review by hand."
            )
            rep = report.build(status="aborted", target_tag=target, target_commit=target_commit,
                               base_commit=base, synced_at=synced_at, probe=probe_stats,
                               resolutions=[], residual=[], lockfiles=[], unresolved=[],
                               tests=None, notes=notes)
            report.write(rep, report_json, report_md)
            log.error("Merge too noisy — aborted. See %s", report_md)
            return 2

    # 4. Dry run: report the routing plan and stop.
    if args.dry_run:
        plan = [{"path": p, "code": c, "resolver": dispatch.classify(p, c)}
                for p, c in sorted(conflict_codes_preview.items())]
        rep = report.build(status="dry-run", target_tag=target, target_commit=target_commit,
                           base_commit=base, synced_at=synced_at, probe=probe_stats,
                           resolutions=plan, residual=[], lockfiles=[], unresolved=[],
                           tests=None, notes=notes + ["Dry run — no merge performed."])
        report.write(rep, report_json, report_md)
        log.info("Dry run complete. Routing plan written to %s", report_md)
        return 0

    # 5. Merge into a fresh worktree.
    branch = f"sync/upstream-{target}"
    wt = config.WORKTREE_DIR
    log.info("Creating worktree %s on branch %s", wt, branch)
    gitops.worktree_add(wt, branch, "HEAD")
    gitops.merge_no_commit(target, wt)

    conflict_codes = gitops.conflict_status(wt)
    log.info("Resolving %d conflicted paths ...", len(conflict_codes))
    llm_resolve = source_llm.make_resolver(target, base)
    resolutions = dispatch.resolve_all(conflict_codes, wt, llm_resolve)

    # 6. Any conflicts the resolvers could not finish?
    unresolved = gitops.unmerged_paths(wt)

    # 7. Residual OTel cleanup over the whole merged tree (clean-merge re-introductions).
    log.info("Scanning for residual OTel ...")
    residual = residual_scan.clean_residual(wt, set(conflict_codes), target, base)

    # 7a. Propagate upstream deletions a merge kept (our renamed copies of files
    #     upstream removed — e.g. a renamed Dockerfile upstream deleted).
    deleted_propagated = deletions_mod.propagate_deletions(wt, base, target)
    if deleted_propagated:
        log.info("Removed %d file(s) upstream deleted but our merge kept", len(deleted_propagated))
        notes.append(f"Removed {len(deleted_propagated)} file(s) upstream deleted (kept by merge as renames).")

    # 7b. Optionally route Dockerfile base images through a pull-through mirror
    #     (avoids Docker Hub anonymous rate limits when you can't `docker login`).
    if config.BASE_IMAGE_MIRROR:
        mirrored = base_image_mirror.rewrite_tree(wt, config.BASE_IMAGE_MIRROR)
        for m in mirrored:
            gitops.stage(m, wt)
        if mirrored:
            log.info("Rewrote %d Dockerfile(s) to base-image mirror %s", len(mirrored), config.BASE_IMAGE_MIRROR)
            notes.append(f"Rewrote {len(mirrored)} Dockerfile base image(s) to {config.BASE_IMAGE_MIRROR}.")

    # 7c. Guarantee postgres is deployable from the stock image (+ init.sql ConfigMap)
    #     and not a dangling skaffold build — even on a clean merge that skipped the
    #     k8s transform (e.g. adopting an already-de-oteled base with a broken deploy).
    pg_changed = k8s.ensure_postgres_deployable(wt)
    for c in pg_changed:
        gitops.stage(c.split(":", 1)[0], wt)
    if pg_changed:
        log.info("Normalized postgres deploy: %s", "; ".join(pg_changed))
        notes.append("Normalized postgres deploy (stock image + init.sql ConfigMap; no postgres build artifact).")

    # 7d. Auto-derive skaffold build artifacts from the manifest, so local-deploy
    #     coverage tracks the manifest: a service that gains a Deployment is built,
    #     an orphaned one is dropped — using the canonical service→Dockerfile map in .env.
    sk_changed = k8s.ensure_skaffold_artifacts(wt)
    if sk_changed:
        gitops.stage("skaffold.yaml", wt)
        log.info("Reconciled skaffold artifacts: %s", "; ".join(sk_changed))
        notes.append("Reconciled skaffold build artifacts to the manifest's deployed services.")

    # 8. Regenerate lockfiles for de-oteled dependency manifests.
    dep_paths = [p for p in conflict_codes if dispatch.classify(p, conflict_codes[p]) in ("deps", "lockfile")]
    lock_results = lockfiles.regenerate(wt, dep_paths)
    for lr in lock_results:
        if not lr.ok:
            notes.append(f"Lockfile not regenerated: {lr.directory} ({lr.tool}) — {lr.detail}")

    # 9. Decide status and (if clean) commit.
    review_items = [r for r in resolutions + residual if r.needs_review or not r.ok]
    lock_failed = any(lr.status == "failed" for lr in lock_results)
    tests_result: dict | None = None

    if unresolved:
        status = "needs-review"
        notes.append(f"{len(unresolved)} conflict(s) left with markers — resolve by hand in {wt}.")
    else:
        # Validate the working tree, then (if clean) stage all of it and commit.
        if not args.no_tests:
            log.info("Running verification gate (pytest -m fast) ...")
            tests_result = _run_tests(wt)
        tests_ok = tests_result is None or tests_result["passed"]

        if review_items or lock_failed or not tests_ok:
            status = "needs-review"
        else:
            status = "ready"
            # Record the snapshot point and commit the merge.
            snapshot = {
                "upstream": config.UPSTREAM_URL,
                "tag": target,
                "tag_commit": target_commit,
                "merge_base": base,
                "synced_at": synced_at,
            }
            (wt / "sync" / "last_synced.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
            # Stage the full worktree so the commit matches exactly what the test
            # gate validated — lockfile regeneration and residual cleanup modify
            # files after the per-path staging done during conflict resolution.
            gitops.stage_all(wt)
            message = (
                f"chore(sync): merge upstream {target} with de-otelification\n\n"
                f"Upstream-Sync: open-telemetry/opentelemetry-demo@{target}\n"
            )
            gitops.commit(message, wt)
            log.info("Committed merge on branch %s", branch)

    # 10. Report.
    rep = report.build(status=status, target_tag=target, target_commit=target_commit,
                       base_commit=base, synced_at=synced_at, probe=probe_stats,
                       resolutions=resolutions, residual=residual, lockfiles=lock_results,
                       unresolved=unresolved, tests=tests_result, notes=notes,
                       deletions=deleted_propagated)
    report.write(rep, report_json, report_md)

    # 11. Push + PR (CI) on a ready sync.
    if status == "ready" and args.push:
        log.info("Pushing %s and opening a PR ...", branch)
        gitops._git(["push", "-u", "origin", branch], wt)
        subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--base", "main",
             "--title", f"Sync upstream {target} (de-otelified)",
             "--body-file", str(report_md)],
            cwd=str(config.REPO_ROOT), check=False,
        )

    if status == "ready" and not args.keep_worktree and not args.push:
        log.info("Sync ready. Review branch '%s' (worktree kept at %s for inspection).", branch, wt)
    log.info("Status: %s. Report: %s", status.upper(), report_md)
    return 0 if status in ("ready", "up-to-date") else 1


if __name__ == "__main__":
    raise SystemExit(main())
