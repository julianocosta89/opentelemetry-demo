"""
Rewrite Dockerfile base images (`FROM docker.io/...`) to a pull-through mirror.

Docker Hub rate-limits anonymous pulls (100 / 6h per IP). A daemon registry-mirror
fixes image *layer* pulls, but tooling like skaffold inspects base-image metadata
via direct `index.docker.io` calls that bypass the daemon mirror — so `skaffold dev`
still trips the limit. Rewriting the `FROM` ref to an explicit mirror registry
(e.g. `mirror.gcr.io`) routes *every* base-image access through the mirror.

Only docker.io images are rewritten. Other registries (gcr.io, ghcr.io,
mcr.microsoft.com, quay.io, …), `scratch`, and multi-stage build-stage aliases
(`FROM builder`) are left untouched. Idempotent: already-mirrored refs are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

_FROM_RE = re.compile(r"(?i)^(\s*FROM\s+)(--platform=\S+\s+)?(\S+)(\s+AS\s+\S+)?\s*$")
_AS_RE = re.compile(r"(?i)^\s*FROM\s+.*\bAS\s+(\S+)\s*$")

# First path component identifies an explicit registry when it looks like a host.
_DOCKER_HUB_REGISTRIES = {"docker.io", "index.docker.io"}


def mirror_image_ref(ref: str, mirror: str) -> str | None:
    """Return the mirrored ref for a docker.io image, or None to leave it unchanged.

    Examples (mirror="mirror.gcr.io"):
      python:3.12                      -> mirror.gcr.io/library/python:3.12
      eclipse-temurin:21-jre           -> mirror.gcr.io/library/eclipse-temurin:21-jre
      docker.io/library/node:22        -> mirror.gcr.io/library/node:22
      nginxinc/nginx-unprivileged:1.27 -> mirror.gcr.io/nginxinc/nginx-unprivileged:1.27
      mcr.microsoft.com/dotnet/sdk:10  -> None  (non-docker registry)
      gcr.io/distroless/static         -> None
      scratch                          -> None
      mirror.gcr.io/library/python     -> None  (already mirrored)
    """
    if not mirror or ref == "scratch":
        return None
    parts = ref.split("/")
    if len(parts) == 1:
        # bare image → docker.io/library/<ref>
        return f"{mirror}/library/{ref}"
    first = parts[0]
    if "." in first or ":" in first or first == "localhost":
        # explicit registry host
        if first in _DOCKER_HUB_REGISTRIES:
            rest = parts[1:]
            # docker.io/debian → official image, needs the library/ namespace
            if len(rest) == 1:
                return f"{mirror}/library/{rest[0]}"
            return f"{mirror}/{'/'.join(rest)}"  # docker.io/org/repo
        return None  # some other registry (incl. the mirror itself) — leave it
    # no explicit registry, has a slash → docker.io org image (org/repo)
    return f"{mirror}/{ref}"


_FROM_VAR_RE = re.compile(r"^\$\{?(\w+)\}?$")
_ARG_RE = re.compile(r"^(\s*ARG\s+(\w+)\s*=\s*)(.+?)\s*$")


def rewrite_dockerfile(text: str, mirror: str) -> str:
    """Rewrite `FROM` base images (and the ARG defaults they reference) to the mirror.

    A `FROM ${BUILDER_IMAGE}` references a build ARG, not a literal image — rewriting
    the FROM line would corrupt it, so instead we rewrite the docker.io default on the
    matching `ARG BUILDER_IMAGE=...` line.
    """
    if not mirror:
        return text
    lines = text.splitlines()
    stages = {m.group(1) for line in lines if (m := _AS_RE.match(line))}

    # ARG names used as `FROM ${NAME}` — their defaults get mirrored instead.
    image_args: set[str] = set()
    for line in lines:
        m = _FROM_RE.match(line)
        if m and (mv := _FROM_VAR_RE.match(m.group(3))):
            image_args.add(mv.group(1))

    out: list[str] = []
    for line in lines:
        m = _FROM_RE.match(line)
        if m:
            prefix, platform, image, as_clause = m.group(1), m.group(2) or "", m.group(3), m.group(4) or ""
            # Skip variable refs (handled via their ARG) and earlier build stages.
            if "$" not in image and image not in stages:
                new = mirror_image_ref(image, mirror)
                if new:
                    line = f"{prefix}{platform}{new}{as_clause}"
            out.append(line)
            continue
        am = _ARG_RE.match(line)
        if am and am.group(2) in image_args:
            pre, value = am.group(1), am.group(3)
            quote = ""
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                quote, value = value[0], value[1:-1]
            new = mirror_image_ref(value, mirror)
            if new:
                line = f"{pre}{quote}{new}{quote}"
        out.append(line)
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


def rewrite_tree(root, mirror: str) -> list[str]:
    """Rewrite all Dockerfiles under `root`; return the list of changed relative paths."""
    if not mirror:
        return []
    root = Path(root)
    changed: list[str] = []
    for path in root.rglob("Dockerfile*"):
        if not path.is_file() or "node_modules" in path.parts:
            continue
        original = path.read_text(encoding="utf-8")
        rewritten = rewrite_dockerfile(original, mirror)
        if rewritten != original:
            path.write_text(rewritten, encoding="utf-8")
            changed.append(str(path.relative_to(root)))
    return changed
