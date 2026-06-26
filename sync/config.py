"""
Central configuration for the de-otelification sync pipeline.

Everything tunable lives here so the rest of the pipeline reads, never hardcodes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ─── Repository layout ────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_DIR = REPO_ROOT / "sync"
LAST_SYNCED_FILE = SYNC_DIR / "last_synced.json"

# The merge happens in a throwaway git worktree, a sibling of the repo, so the
# user's working tree is never touched and a failed run leaves no mess.
WORKTREE_DIR = REPO_ROOT.parent / f"{REPO_ROOT.name}-sync-worktree"

# ─── Upstream ─────────────────────────────────────────────────────────────────

UPSTREAM_REMOTE = "upstream"
# Canonical upstream. Re-point once at adoption with:
#   git remote set-url upstream https://github.com/open-telemetry/opentelemetry-demo.git
UPSTREAM_URL = "https://github.com/open-telemetry/opentelemetry-demo.git"

# Releases are tagged like 2.2.0 (canonical upstream dropped the leading "v"; older
# v1.x.y tags are pre-rename relics). The "v" is optional so both forms parse. We sync
# against tags, not main HEAD.
RELEASE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# ─── Local-deploy image naming ────────────────────────────────────────────────
# Upstream's k8s manifest pulls prebuilt images (ghcr.io/open-telemetry/demo:<ver>-<svc>)
# that still contain OTel. The sync pipeline rewrites those refs to locally-built
# de-oteled images so skaffold can deploy from source. Convention:
# <prefix>/<service>-no-otel — must match skaffold.yaml artifacts and
# .github/workflows/build-push-ghr-no-otel.yml.

UPSTREAM_IMAGE_REPO = "ghcr.io/open-telemetry/demo"
NO_OTEL_IMAGE_PREFIX = os.environ.get("NO_OTEL_IMAGE_PREFIX", "ghcr.io/julianocosta89")

# Postgres runs from the stock upstream image (upstream dropped its custom postgres
# Dockerfile — see sync/resolvers/k8s.py _normalize_postgres). In k8s, init.sql is
# loaded from a ConfigMap generated from src/postgresql/init.sql, the single source.
POSTGRES_IMAGE = os.environ.get("POSTGRES_IMAGE", "postgres:17.6")
POSTGRES_INIT_CONFIGMAP = "postgresql-init-sql"
POSTGRES_INIT_SQL_PATH = "src/postgresql/init.sql"

# Optional: rewrite Dockerfile base images (FROM docker.io/...) to a pull-through
# mirror so builds don't hit Docker Hub's anonymous rate limit (useful when you
# can't `docker login`). Empty = disabled. e.g. "mirror.gcr.io".
BASE_IMAGE_MIRROR = os.environ.get("BASE_IMAGE_MIRROR", "")


def no_otel_image(service: str) -> str:
    """The local de-oteled image name for a service (untagged; skaffold supplies the tag)."""
    return f"{NO_OTEL_IMAGE_PREFIX}/{service}-no-otel"


def upstream_image_service(image: str) -> str | None:
    """If `image` is an upstream prebuilt demo image, return its service, else None.

    e.g. "ghcr.io/open-telemetry/demo:2.1.3-product-catalog" -> "product-catalog"
         "ghcr.io/open-telemetry/demo:latest-llm"            -> "llm"
    The tag is "<version>-<service>" and the version contains no "-".
    """
    prefix = UPSTREAM_IMAGE_REPO + ":"
    if not image.startswith(prefix):
        return None
    tag = image[len(prefix):]
    parts = tag.split("-", 1)
    return parts[1] if len(parts) == 2 and parts[1] else None


# ─── LLM ──────────────────────────────────────────────────────────────────────

# Current, most-capable model. Source-conflict resolution is correctness-critical
# and low-volume, so we use the strongest model at high effort.
MODEL = os.environ.get("SYNC_MODEL", "claude-opus-4-8")
LLM_EFFORT = os.environ.get("SYNC_LLM_EFFORT", "high")
# Validation retries: re-prompt when the model's output fails validation
# (conflict markers left, residual OTel, gofmt failure).
LLM_MAX_RETRIES = 3
# Transport retries: how many times the SDK retries a transient API failure
# (429 rate-limit, 529 overloaded, 5xx) with exponential backoff + jitter before
# giving up. The default of 2 is too few during a sustained overload window — a
# single overloaded response then aborts a whole file. Low-volume run, so be patient.
LLM_API_MAX_RETRIES = int(os.environ.get("SYNC_LLM_API_MAX_RETRIES", "8"))

# ─── Robustness thresholds ────────────────────────────────────────────────────
# If a merge is noisier than this, abort before spending LLM tokens and let a
# human look first. Overridable via env for unusual upstream jumps.

MAX_CONFLICT_FILES = int(os.environ.get("SYNC_MAX_CONFLICT_FILES", "60"))
MAX_SOURCE_CONFLICT_FILES = int(os.environ.get("SYNC_MAX_SOURCE_CONFLICT_FILES", "10"))

# Files this fork deliberately keeps even though upstream deleted them. The deletion
# check (sync/deletions.py) removes our (possibly renamed) copies of upstream-deleted
# files; anything listed here is exempt. Empty by default — add a path only with a
# documented reason.
KEEP_DESPITE_UPSTREAM_DELETION: frozenset[str] = frozenset()

# ─── File classification ──────────────────────────────────────────────────────

# Extensions that are program source code → routed to the LLM resolver when they
# conflict (everything else is handled deterministically or kept-deleted).
SOURCE_EXTENSIONS = {
    ".go", ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".cs", ".java", ".kt", ".rs", ".rb", ".php",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".ex", ".exs",
}

# Dependency / build manifests → deterministic deps resolver.
DEP_FILENAMES = {
    "go.mod", "package.json", "composer.json", "requirements.txt",
    "mix.exs", "Cargo.toml", "Gemfile", "pom.xml",
}
DEP_SUFFIXES = {".csproj", ".gradle"}            # *.csproj, *.gradle
DEP_COMPOUND_SUFFIXES = {".gradle.kts"}          # build.gradle.kts

# Lockfiles → never hand-edited; regenerated from their manifest.
LOCKFILE_NAMES = {
    "go.sum", "package-lock.json", "yarn.lock", "Cargo.lock",
    "composer.lock", "Gemfile.lock", "mix.lock", "poetry.lock",
}

# Requirements variants (requirements-test.txt etc.)
def is_requirements_file(name: str) -> bool:
    return name.startswith("requirements") and name.endswith(".txt")


def is_source_path(path: str) -> bool:
    return Path(path).suffix in SOURCE_EXTENSIONS


def is_lockfile(path: str) -> bool:
    return Path(path).name in LOCKFILE_NAMES


def is_dependency_file(path: str) -> bool:
    name = Path(path).name
    if name in DEP_FILENAMES or is_requirements_file(name):
        return True
    if Path(path).suffix in DEP_SUFFIXES:
        return True
    if any(name.endswith(sfx) for sfx in DEP_COMPOUND_SUFFIXES):
        return True
    return False
