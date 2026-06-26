"""
Deterministic resolution of dependency / build manifests.

Strategy: take upstream's version of the file wholesale (it carries every
legitimate version bump and any new non-OTel deps), then subtract the OTel
entries. This is provably "absorb upstream + re-remove OTel" — exactly the manual
de-otelification, reproduced. Transitive churn (deps that existed only to satisfy
OTel) is settled afterwards by lockfile regeneration (`go mod tidy`, `npm install`,
…), which is why we never hand-edit lockfiles.

The transform_* functions are pure (str -> str) so they unit-test without git.
resolve() wires them to the worktree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import config
import gitops
from resolvers.common import ResolveResult

_MARKER_RE = re.compile(r"^(<{7}|={7}|>{7}|\|{7})")


def _drop_markers(content: str) -> str:
    """Defensive: strip any stray git conflict-marker lines."""
    return "\n".join(ln for ln in content.splitlines() if not _MARKER_RE.match(ln))


def _join(lines: list[str], trailing_newline: bool = True) -> str:
    text = "\n".join(lines)
    if trailing_newline and not text.endswith("\n"):
        text += "\n"
    return text


# ─── Per-filetype transforms ──────────────────────────────────────────────────


def transform_go_mod(content: str) -> str:
    """Drop direct go.opentelemetry.io requires; keep `// indirect` ones for tidy."""
    out = []
    for line in _drop_markers(content).splitlines():
        if "go.opentelemetry.io" in line and "// indirect" not in line:
            continue
        out.append(line)
    return _join(out)


def transform_requirements(content: str) -> str:
    out = [ln for ln in _drop_markers(content).splitlines() if "opentelemetry" not in ln.lower()]
    return _join(out)


def transform_csproj(content: str) -> str:
    out = [ln for ln in _drop_markers(content).splitlines() if "opentelemetry" not in ln.lower()]
    return _join(out)


def transform_cargo_toml(content: str) -> str:
    out = [ln for ln in _drop_markers(content).splitlines() if "opentelemetry" not in ln.lower()]
    return _join(out)


def transform_mix_exs(content: str) -> str:
    out = [ln for ln in _drop_markers(content).splitlines() if "opentelemetry" not in ln.lower()]
    return _join(out)


# Genuine OTel artifacts: dependency coordinates, the javaagent, the OTel gradle
# instrumentation plugin, and the named SDK/instrumentation modules. Deliberately
# NOT matched: the demo's own identity (group/namespace/applicationId/project name
# legitimately live under io.opentelemetry), copyright headers, and the
# `opentelemetry-demo` repo/path name — those are kept verbatim.
_GRADLE_OTEL_ARTIFACT = re.compile(
    r"io\.opentelemetry:opentelemetry-"          # maven coordinate, e.g. io.opentelemetry:opentelemetry-api
    r"|io\.opentelemetry\.instrumentation"       # the OTel gradle instrumentation plugin / group
    r"|opentelemetry-(?:api|sdk|instrumentation|javaagent|exporter|bom|semconv|extension|context|annotations)",
    re.IGNORECASE,
)


def transform_gradle(content: str) -> str:
    """
    Drop genuine OTel artifact lines (dependencies, the javaagent, the OTel gradle
    plugin) and leave everything else untouched.

    This fork's de-otel standard removes instrumentation but keeps the demo's own
    identity: its group/namespace/applicationId/project name live under
    io.opentelemetry and must not be renamed or deleted (that breaks the build).
    """
    out = [
        line for line in _drop_markers(content).splitlines()
        if not _GRADLE_OTEL_ARTIFACT.search(line)
    ]
    return _join(out)


def _strip_json_otel_keys(obj: dict, prefixes: tuple[str, ...]) -> int:
    """Remove dependency keys with any of `prefixes` from common dep sections."""
    removed = 0
    for section in (
        "dependencies", "devDependencies", "peerDependencies",
        "optionalDependencies", "require", "require-dev",
    ):
        deps = obj.get(section)
        if isinstance(deps, dict):
            for key in [k for k in deps if any(k.startswith(p) for p in prefixes)]:
                del deps[key]
                removed += 1
    return removed


_NODE_OTEL_FLAG = re.compile(r"\s*--(?:require|loader|import)[ =]\S*opentelemetry\S*")


def _clean_npm_scripts(scripts: dict) -> None:
    """Strip OTel auto-instrumentation flags from npm script commands.

    e.g. `node --require @opentelemetry/auto-instrumentations-node/register index.js`
         → `node index.js`. The --require loads instrumentation at startup, so it's
    removed like any other instrumentation, not kept as config.
    """
    for name, cmd in list(scripts.items()):
        if isinstance(cmd, str):
            cleaned = _NODE_OTEL_FLAG.sub("", cmd).strip()
            if cleaned != cmd:
                scripts[name] = cleaned


def transform_package_json(content: str) -> str:
    obj = json.loads(_drop_markers(content))
    # Remove any dependency whose package name contains "opentelemetry" — covers the
    # @opentelemetry/* scope AND unscoped OTel packages (e.g. pino-opentelemetry-
    # transport) — across dependency-like sections including overrides/resolutions.
    for section in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies", "overrides", "resolutions"):
        deps = obj.get(section)
        if isinstance(deps, dict):
            for key in [k for k in deps if "opentelemetry" in k.lower()]:
                del deps[key]
            if not deps and section in ("overrides", "resolutions"):
                del obj[section]   # drop a now-empty overrides/resolutions block
    if isinstance(obj.get("scripts"), dict):
        _clean_npm_scripts(obj["scripts"])
    return json.dumps(obj, indent=2) + "\n"


def transform_composer_json(content: str) -> str:
    obj = json.loads(_drop_markers(content))
    _strip_json_otel_keys(obj, ("open-telemetry/",))
    return json.dumps(obj, indent=2) + "\n"


def transform_pom_xml(content: str) -> str:
    """Remove whole <dependency>…</dependency> blocks that mention opentelemetry."""
    content = _drop_markers(content)

    def keep(match: re.Match) -> str:
        return "" if "opentelemetry" in match.group(0).lower() else match.group(0)

    return re.sub(r"<dependency>.*?</dependency>\s*", keep, content, flags=re.DOTALL)


# ─── Dispatch by filename ─────────────────────────────────────────────────────


def transform_for(path: str, content: str) -> str:
    name = Path(path).name
    if name == "go.mod":
        return transform_go_mod(content)
    if name == "package.json":
        return transform_package_json(content)
    if name == "composer.json":
        return transform_composer_json(content)
    if name == "pom.xml":
        return transform_pom_xml(content)
    if name == "Cargo.toml":
        return transform_cargo_toml(content)
    if name == "mix.exs":
        return transform_mix_exs(content)
    if config.is_requirements_file(name):
        return transform_requirements(content)
    if name.endswith(".csproj"):
        return transform_csproj(content)
    if name.endswith(".gradle") or name.endswith(".gradle.kts"):
        return transform_gradle(content)
    # Unknown dep file: line-strip as a safe default.
    return _join([ln for ln in _drop_markers(content).splitlines() if "opentelemetry" not in ln.lower()])


def resolve(path: str, worktree) -> ResolveResult:
    # Absorb upstream's version (all bumps + new deps), then re-remove OTel.
    gitops.checkout_side(path, "theirs", worktree)
    full = Path(worktree) / path
    try:
        original = full.read_text(encoding="utf-8")
        transformed = transform_for(path, original)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ResolveResult(
            path, "deps", False, f"deterministic transform failed: {exc}", needs_review=True
        )
    full.write_text(transformed, encoding="utf-8")
    gitops.stage(path, worktree)
    return ResolveResult(path, "deps", True, "took upstream version, removed OTel deps (lockfile regen pending)")
