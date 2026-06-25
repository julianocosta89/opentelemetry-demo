# Upstream sync

Re-applies de-otelification when upstream releases a new version, by merging upstream
into this fork and resolving the conflicts. Produces a `sync/upstream-<tag>` branch and
a report for review.

## Run a sync

```bash
source sync/.venv/bin/activate
pip install -r sync/requirements-sync.txt

# preview — what would change, no edits:
python sync/sync.py --dry-run --tag 2.2.0

# do it (needs ANTHROPIC_API_KEY for the few LLM-resolved files):
ANTHROPIC_API_KEY=$(cat ~/.anthropic) python sync/sync.py --tag 2.2.0
```

- Release tags are `X.Y.Z` (e.g. `2.2.0`). A leading `v` is also accepted for the
  older `v1.x` tags. Omit `--tag` to target the latest release tag.
- The merge runs in a throwaway worktree (`../otelfree-demo-sync-worktree`) on branch
  `sync/upstream-<tag>` — **your working tree is untouched**.
- Output: `sync/last_sync_report.md` (what was resolved, what needs review) and, on a
  clean run, a commit recording the synced tag in `sync/last_synced.json`.
- **Exit codes:** `0` ready · `1` needs review (markers left, failed validation, or red
  test gate — see the report) · `2` aborted (too many conflicts).

Then review the branch and merge it. To run the resulting demo locally, see
[`../SKAFFOLD.md`](../SKAFFOLD.md).

## Configure

All knobs are in `sync/config.py` (most also overridable via env): upstream URL,
release-tag pattern, LLM model/effort, conflict-abort thresholds, the image prefix
(`NO_OTEL_IMAGE_PREFIX`), and the base-image mirror (`BASE_IMAGE_MIRROR`).

First-time adoption — point `upstream` at the canonical repo (the pipeline also does
this if the remote is missing):

```bash
git remote set-url upstream https://github.com/open-telemetry/opentelemetry-demo.git
```

## Tests

```bash
python -m pytest sync/tests/    # resolvers, routing, rules, LLM resolver (mocked)
python -m pytest tests/ -m fast # the de-otelification verification gate
```

## CI

`.github/workflows/sync-upstream.yml` runs weekly (and on demand) and opens a PR for
review — it never auto-merges. Schedule + `workflow_dispatch` only (no `pull_request`
trigger), with the API key in the protected `llm-sync` environment.

---

## How it works

Background — not needed to run a sync.

This fork shares git history with upstream (our de-otelification is commits on top of an
upstream commit), so a sync is a **`git merge`**, not a from-scratch re-transformation.
The manual de-otelification already lives in history and is carried forward by the merge;
the pipeline only settles conflicts and cleans up anything upstream newly instrumented.
It's deterministic where it can be, reviewable (output is a diff), and uses the LLM only
for the few genuinely entangled files.

```
fetch upstream → pick release tag → merge-tree probe (abort if too noisy)
  → git merge into a throwaway worktree
  → resolve conflicts (resolvers/dispatch.py routes each path):
       modify/delete we deleted   → stay removed              (deleted.py)
       dependency manifests       → take upstream, strip OTel  (deps.py)
       lockfiles                  → take upstream, regenerate
       kubernetes manifest        → deterministic OTel-strip + image rewrite (k8s.py)
       source / compose / .env / Dockerfile → 3-way LLM resolution (source_llm.py)
  → residual scan: clean OTel that merged in without a conflict (residual_scan.py)
  → optional: rewrite base images to a mirror (base_image_mirror.py)
  → regenerate lockfiles (go mod tidy / npm / composer / cargo / mix)  (lockfiles.py)
  → test gate (pytest tests/ -m fast) → commit + report → (CI) open a PR
```

Deterministic resolvers take **upstream's** version of a file (keeping legitimate version
bumps) and subtract OTel; lockfile regeneration drops deps that existed only for OTel.
Entangled files go to the LLM with all three sides (base/ours/theirs); the result is
validated (no conflict markers, no OTel, `gofmt` for Go) and retried, and a file that
never validates is left with markers and flagged — never committed broken.

`sync/otel_rules.py` is the single definition of "what counts as OTel", imported by both
the residual scan and `tests/test_otel_absence.py`, so removal and verification can't drift.

> A previous attempt re-derived every file via an LLM and added a self-modifying
> multi-agent loop that rewrote the pipeline to chase green tests — unreproducible, and
> removed.
