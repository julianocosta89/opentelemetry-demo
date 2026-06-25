"""
Deterministic resolution of the Kubernetes manifest.

kubernetes/opentelemetry-demo.yaml is a single ~40k-line generated artifact — far
too large for an LLM and not amenable to a 3-way text merge. So on conflict we
take upstream's regenerated manifest and re-apply the structural de-otelification
deterministically:

  1. drop OTel infrastructure resources (jaeger/grafana/prometheus/otel-collector/…)
  2. strip OTEL_* env vars and OTel init containers from surviving pods
  3. keep opentelemetry.io/* labels (the demo's identity labels / selectors — not
     instrumentation), synthesizing a selector only when one is genuinely empty
  4. normalize/synthesize postgres: stock image + init.sql from a ConfigMap
     (upstream ships no postgres in k8s; we add it so it survives every sync)
  5. rewrite prebuilt upstream image refs (ghcr.io/open-telemetry/demo:<ver>-<svc>,
     which still contain OTel) to locally-built de-oteled images so skaffold can
     deploy from source — see config.no_otel_image()

We accept upstream's pod structure as-is (e.g. flagd-ui stays a container in the
flagd pod) rather than splitting services out — matching this fork's de-otel standard.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

import config
import gitops
from resolvers.common import ResolveResult

_OTEL_INFRA_SUBSTRINGS = [
    "jaeger", "grafana", "prometheus", "opensearch",
    "otel-collector", "otelcol", "opentelemetry-collector",
]
_INFRA_KINDS = {
    "Deployment", "Service", "ConfigMap", "DaemonSet", "ClusterRole",
    "ClusterRoleBinding", "ServiceAccount", "Ingress", "StatefulSet",
}


def _is_otel_infra(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    name = (doc.get("metadata") or {}).get("name", "") or ""
    kind = doc.get("kind", "")
    if kind == "Namespace":
        return False
    if kind == "ServiceAccount" and name == "opentelemetry-demo":
        return False
    low = name.lower()
    for infra in _OTEL_INFRA_SUBSTRINGS:
        if low == infra or low.startswith(infra + "-") or low.startswith(infra + ":"):
            return True
        if infra in low and kind in _INFRA_KINDS:
            return True
    return False


def _strip_otel_env(doc: dict) -> None:
    spec = doc.get("spec") or {}
    pod_spec = (spec.get("template") or {}).get("spec", spec)
    for key in ("containers", "initContainers"):
        for container in pod_spec.get(key, []) or []:
            if not isinstance(container, dict):
                continue
            env = container.get("env")
            if not env:
                continue
            def is_otel(e: dict) -> bool:
                if not isinstance(e, dict):
                    return False
                name = str(e.get("name", ""))
                value = str(e.get("value", ""))
                if "OTEL_" in name:
                    return True
                if "OTEL_" in value:
                    return True
                if name == "FLAGD_METRICS_EXPORTER" and value == "otel":
                    return True
                return False
            filtered = [e for e in env if not is_otel(e)]
            if len(filtered) != len(env):
                container["env"] = filtered


def _strip_otel_init_containers(doc: dict) -> None:
    spec = doc.get("spec") or {}
    pod_spec = (spec.get("template") or {}).get("spec", {})
    init = pod_spec.get("initContainers")
    if not init:
        return
    kept = [
        ic for ic in init
        if not (isinstance(ic, dict)
                and ("opentelemetry" in str(ic.get("name", "")).lower()
                     or "opentelemetry" in str(ic.get("image", "")).lower()))
    ]
    if len(kept) != len(init):
        if kept:
            pod_spec["initContainers"] = kept
        else:
            pod_spec.pop("initContainers", None)


_SELECTOR_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}


def _repair_selector(doc: dict) -> None:
    """Synthesize a label selector only when one is genuinely missing/empty.

    This fork keeps `opentelemetry.io/*` labels, so upstream selectors keyed on
    `opentelemetry.io/name` remain valid and are left untouched. We only build a
    selector when there is none — an empty `matchLabels: {}` is invalid for a
    workload, and a null Service `selector` routes to no pods and silently breaks
    inter-service networking.
    """
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return
    name = (doc.get("metadata") or {}).get("name")

    if kind == "Service":
        if spec.get("selector"):      # any non-empty selector is valid — preserve it
            return
        if name:
            spec["selector"] = {"app.kubernetes.io/name": name}
        return

    if kind not in _SELECTOR_KINDS:
        return
    if (spec.get("selector") or {}).get("matchLabels"):
        return  # already valid
    template_labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    name = template_labels.get("app.kubernetes.io/name") or name
    if not name:
        return
    spec["selector"] = {"matchLabels": {"app.kubernetes.io/name": name}}
    template_labels.setdefault("app.kubernetes.io/name", name)
    spec.setdefault("template", {}).setdefault("metadata", {})["labels"] = template_labels


def _rewrite_images(doc: dict) -> None:
    """Rewrite prebuilt upstream demo image refs to locally-built de-oteled images.

    `ghcr.io/open-telemetry/demo:2.1.3-checkout` → `<prefix>/checkout-no-otel`.
    Third-party images (valkey, open-feature/flagd, busybox) are left untouched.
    Without this, skaffold can't map its build artifacts to the manifest and the
    deploy silently pulls upstream's instrumented images.
    """
    spec = doc.get("spec") or {}
    pod_spec = (spec.get("template") or {}).get("spec", spec)
    for key in ("containers", "initContainers"):
        for container in pod_spec.get(key, []) or []:
            if not isinstance(container, dict):
                continue
            service = config.upstream_image_service(str(container.get("image", "")))
            if service:
                container["image"] = config.no_otel_image(service)


def _synthesize_missing(docs: list[dict]) -> list[dict]:
    """Synthesize nothing — accept upstream's manifest structure as-is.

    Our earlier strict standard split flagd-ui out of the flagd pod into its own
    Deployment and added standalone llm / product-reviews Deployments. Juliano's
    no-otel (the standard we follow) keeps upstream's structure instead: flagd-ui
    runs as a *container* inside the flagd pod, and llm / product-reviews aren't
    deployed standalone in k8s. Synthesizing them would diverge from his base and
    duplicate flagd-ui on every sync, so we add nothing.
    """
    return []


def _postgres_deployment_doc() -> dict:
    """A postgres Deployment using the stock image, loading init.sql from a ConfigMap.

    Upstream removed its custom postgres Dockerfile (now a stock image + mounted
    init.sql) and ships no postgres in k8s at all — so we synthesize it ourselves
    the same upstream-aligned way: stock image, init.sql from `POSTGRES_INIT_CONFIGMAP`.
    """
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "postgresql",
            "labels": {
                "app.kubernetes.io/component": "postgresql",
                "app.kubernetes.io/name": "postgresql",
                "app.kubernetes.io/part-of": "opentelemetry-demo",
            },
        },
        "spec": {
            "replicas": 1,
            "revisionHistoryLimit": 10,
            "selector": {"matchLabels": {"app.kubernetes.io/name": "postgresql"}},
            "template": {
                "metadata": {"labels": {
                    "app.kubernetes.io/component": "postgresql",
                    "app.kubernetes.io/name": "postgresql",
                }},
                "spec": {
                    "serviceAccountName": "opentelemetry-demo",
                    "containers": [{
                        "name": "postgresql",
                        "image": config.POSTGRES_IMAGE,
                        "imagePullPolicy": "IfNotPresent",
                        "ports": [{"containerPort": 5432, "name": "service"}],
                        "env": [
                            {"name": "POSTGRES_USER", "value": "root"},
                            {"name": "POSTGRES_PASSWORD", "value": "otel"},
                            {"name": "POSTGRES_DB", "value": "otel"},
                        ],
                        "resources": {"limits": {"memory": "100Mi"}},
                        "volumeMounts": [{"name": "init-sql", "mountPath": "/docker-entrypoint-initdb.d"}],
                    }],
                    "volumes": [{"name": "init-sql", "configMap": {"name": config.POSTGRES_INIT_CONFIGMAP}}],
                },
            },
        },
    }


def _normalize_postgres_deployment(doc: dict) -> None:
    """Repoint an existing postgres Deployment at the stock image + init.sql ConfigMap,
    replacing the custom-built `postgresql-no-otel` image."""
    pod_spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
    for c in pod_spec.get("containers") or []:
        if isinstance(c, dict) and c.get("name") == "postgresql":
            c["image"] = config.POSTGRES_IMAGE
            mounts = [m for m in (c.get("volumeMounts") or [])
                      if isinstance(m, dict) and m.get("name") != "init-sql"]
            mounts.append({"name": "init-sql", "mountPath": "/docker-entrypoint-initdb.d"})
            c["volumeMounts"] = mounts
    volumes = [v for v in (pod_spec.get("volumes") or [])
               if isinstance(v, dict) and v.get("name") != "init-sql"]
    volumes.append({"name": "init-sql", "configMap": {"name": config.POSTGRES_INIT_CONFIGMAP}})
    pod_spec["volumes"] = volumes


def _postgres_init_configmap_yaml(init_sql: str) -> str:
    """The ConfigMap (as YAML text) carrying init.sql, generated from the single source
    src/postgresql/init.sql so it never drifts from the file."""
    body = "\n".join(("    " + line) if line else "" for line in init_sql.splitlines())
    return (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        f"  name: {config.POSTGRES_INIT_CONFIGMAP}\n"
        "data:\n"
        "  init.sql: |\n"
        f"{body}"
    )


def read_init_sql(worktree) -> str | None:
    """Read src/postgresql/init.sql from the worktree, or None if absent."""
    p = Path(worktree) / config.POSTGRES_INIT_SQL_PATH
    return p.read_text(encoding="utf-8") if p.is_file() else None


def transform_kubernetes(content: str, init_sql: str | None = None) -> str:
    """Pure transform: upstream manifest text → de-otelified manifest text.

    When `init_sql` is given, the postgres Deployment is normalized to the stock image
    (synthesized if upstream's conflicting manifest dropped it) and its init.sql
    ConfigMap is regenerated from that source.
    """
    raw_docs = content.split("\n---")
    parsed: list[dict] = []
    kept_yaml: list[str] = []
    saw_postgres = False

    for raw in raw_docs:
        raw = raw.strip()
        if not raw:
            continue
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError:
            kept_yaml.append(raw)
            continue
        if not isinstance(doc, dict):
            kept_yaml.append(raw)
            continue
        if _is_otel_infra(doc):
            continue
        # Drop any previously-generated init.sql ConfigMap — regenerated below from source.
        if (doc.get("kind") == "ConfigMap"
                and (doc.get("metadata") or {}).get("name") == config.POSTGRES_INIT_CONFIGMAP):
            continue
        _strip_otel_env(doc)
        _strip_otel_init_containers(doc)
        # NB: opentelemetry.io/* labels are intentionally KEPT (the demo's identity
        # labels / selectors) — this fork removes instrumentation, not those labels.
        _repair_selector(doc)
        _rewrite_images(doc)
        if (init_sql is not None and doc.get("kind") == "Deployment"
                and (doc.get("metadata") or {}).get("name") == "postgresql"):
            _normalize_postgres_deployment(doc)
            saw_postgres = True
        parsed.append(doc)
        kept_yaml.append(yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())

    # Postgres survives only if present; upstream ships none, so synthesize it when a
    # conflicting manifest (taken from upstream) dropped it. Regenerate its ConfigMap.
    if init_sql is not None:
        if not saw_postgres:
            kept_yaml.append(yaml.dump(_postgres_deployment_doc(), default_flow_style=False,
                                       allow_unicode=True, sort_keys=False).rstrip())
        kept_yaml.append(_postgres_init_configmap_yaml(init_sql))

    for dep in _synthesize_missing(parsed):
        kept_yaml.append(yaml.dump(dep, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip())

    result = "\n---\n".join(kept_yaml)
    if not result.startswith("---"):
        result = "---\n" + result
    return result + "\n"


def resolve(path: str, worktree) -> ResolveResult:
    gitops.checkout_side(path, "theirs", worktree)
    full = Path(worktree) / path
    try:
        transformed = transform_kubernetes(full.read_text(encoding="utf-8"), read_init_sql(worktree))
    except Exception as exc:  # noqa: BLE001 — report any parse/transform failure
        return ResolveResult(path, "k8s", False, f"k8s transform failed: {exc}", needs_review=True)
    full.write_text(transformed, encoding="utf-8")
    gitops.stage(path, worktree)
    return ResolveResult(path, "k8s", True, "took upstream manifest, removed OTel infra/env/labels, synthesized app Deployments")


# ─── Always-on local-deploy normalization ──────────────────────────────────────

_MANIFEST_REL = "kubernetes/opentelemetry-demo.yaml"
_SKAFFOLD_REL = "skaffold.yaml"
# A skaffold build artifact for a postgres image: the `- image: …postgres…` list item
# plus its more-indented continuation lines (context/docker/dockerfile).
_SKAFFOLD_POSTGRES_ARTIFACT = re.compile(
    r"^  - image:[^\n]*postgres[^\n]*\n(?:^    [^\n]*\n)*", re.MULTILINE)


def _normalize_postgres_manifest_text(text: str, init_sql: str) -> str:
    """Return the manifest with ONLY its postgres Deployment normalized to the stock
    image + init.sql ConfigMap. Other documents keep their exact original text."""
    chunks = text.split("\n---\n")
    out: list[str] = []
    seen = False
    for ch in chunks:
        try:
            doc = yaml.safe_load(ch)
        except yaml.YAMLError:
            doc = None
        if isinstance(doc, dict):
            md = doc.get("metadata") or {}
            if doc.get("kind") == "ConfigMap" and md.get("name") == config.POSTGRES_INIT_CONFIGMAP:
                continue  # drop stale; regenerated from source below
            if doc.get("kind") == "Deployment" and md.get("name") == "postgresql":
                _normalize_postgres_deployment(doc)
                seen = True
                out.append(yaml.dump(doc, default_flow_style=False, allow_unicode=True,
                                     sort_keys=False).rstrip())
                continue
        out.append(ch)
    if not seen:
        return text
    return "\n---\n".join(out).rstrip() + "\n---\n" + _postgres_init_configmap_yaml(init_sql).rstrip() + "\n"


def ensure_postgres_deployable(root, init_sql: str | None = None) -> list[str]:
    """Idempotently make postgres deployable from the stock image, independent of
    whether the k8s manifest passed through transform_kubernetes.

    transform_kubernetes normalizes postgres, but it only runs when the manifest
    conflicts or carries residual OTel — a clean adoption of an already-de-oteled
    base (e.g. Juliano's) skips it and inherits that base's postgres deploy, which
    can be broken (manifest deploys a postgresql-no-otel image, skaffold builds it
    from a src/postgres/Dockerfile that no longer exists). This guarantees, on every
    sync, that the manifest's postgres runs the stock image + an init.sql ConfigMap
    and that skaffold doesn't try to build a postgres image (it's pulled).

    Returns human-readable descriptions of what changed (empty if already clean).
    """
    root = Path(root)
    changed: list[str] = []
    if init_sql is None:
        init_sql = read_init_sql(root)

    manifest = root / _MANIFEST_REL
    if init_sql is not None and manifest.is_file():
        original = manifest.read_text(encoding="utf-8")
        updated = _normalize_postgres_manifest_text(original, init_sql)
        if updated != original:
            manifest.write_text(updated, encoding="utf-8")
            changed.append(f"{_MANIFEST_REL}: postgres → stock image + init.sql ConfigMap")

    skaffold = root / _SKAFFOLD_REL
    if skaffold.is_file():
        original = skaffold.read_text(encoding="utf-8")
        updated = _SKAFFOLD_POSTGRES_ARTIFACT.sub("", original)
        if updated != original:
            skaffold.write_text(updated, encoding="utf-8")
            changed.append(f"{_SKAFFOLD_REL}: removed postgres build artifact (postgres is pulled, not built)")

    return changed


# ─── Auto-derive skaffold build artifacts from the manifest ─────────────────────

_ENV_ASSIGN_RE = re.compile(r"^([A-Z0-9_]+)\s*=\s*(.*)$")
_VAR_REF_RE = re.compile(r"^\$\{?(\w+)\}?$")
_COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def _env_values(root: Path) -> dict[str, str]:
    """Parse .env into a NAME→value map (skips comments/blanks)."""
    env = root / ".env"
    values: dict[str, str] = {}
    if not env.is_file():
        return values
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_ASSIGN_RE.match(line)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def _norm_dockerfile(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _service_dockerfile_map(root: Path) -> dict[str, str]:
    """Map service name → Dockerfile path — the canonical build mapping.

    Read from the compose file's `services.<name>.build.dockerfile`, resolving a
    `${VAR}` reference against .env. The compose service name is authoritative, so
    cases where the env-var stem differs from the service resolve correctly
    (`fraud-detection` builds from `${FRAUD_DOCKERFILE}`, not `FRAUD_DETECTION_*`).
    Paths are non-uniform (e.g. cart → `src/cart/src/Dockerfile`), which is exactly
    why we reuse this mapping instead of guessing `src/<svc>/Dockerfile`. Falls back
    to the `<SERVICE>_DOCKERFILE` env-var-name heuristic if no compose file is present.
    """
    env_values = _env_values(root)
    compose = next((root / name for name in _COMPOSE_NAMES if (root / name).is_file()), None)
    mapping: dict[str, str] = {}

    if compose is not None:
        try:
            data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        for service, spec in (data.get("services") or {}).items():
            build = (spec or {}).get("build")
            if not isinstance(build, dict):
                continue
            dockerfile = build.get("dockerfile")
            if not isinstance(dockerfile, str):
                continue
            ref = _VAR_REF_RE.match(dockerfile.strip())
            resolved = env_values.get(ref.group(1), "") if ref else dockerfile.strip()
            resolved = _norm_dockerfile(resolved)
            if resolved:
                mapping[str(service)] = resolved
        if mapping:
            return mapping

    # Fallback: no compose file — derive service from the env-var stem.
    for var, value in env_values.items():
        if var.endswith("_DOCKERFILE"):
            service = var[: -len("_DOCKERFILE")].lower().replace("_", "-")
            mapping[service] = _norm_dockerfile(value)
    return mapping


def _manifest_no_otel_services(root: Path) -> set[str]:
    """Services the manifest deploys as `<prefix>/<svc>-no-otel` images."""
    manifest = root / _MANIFEST_REL
    if not manifest.is_file():
        return set()
    prefix, suffix = config.NO_OTEL_IMAGE_PREFIX + "/", "-no-otel"
    services: set[str] = set()
    for doc in yaml.safe_load_all(manifest.read_text(encoding="utf-8")):
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec") or {}
        pod_spec = (spec.get("template") or {}).get("spec", spec)
        for key in ("containers", "initContainers"):
            for c in pod_spec.get(key) or []:
                img = isinstance(c, dict) and str(c.get("image", "")) or ""
                if img.startswith(prefix) and img.endswith(suffix):
                    services.add(img[len(prefix):-len(suffix)])
    return services


def _split_artifact_items(block_lines: list[str]) -> list[list[str]]:
    """Split the raw lines of a skaffold `artifacts:` list into per-item line blocks."""
    items: list[list[str]] = []
    cur: list[str] | None = None
    for line in block_lines:
        if line.startswith("  - "):
            if cur is not None:
                items.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        items.append(cur)
    return items


def _artifact_image(item_lines: list[str]) -> str | None:
    """The `image:` of one raw artifact item block (parsed, so field order is irrelevant)."""
    try:
        doc = yaml.safe_load("\n".join(line[2:] for line in item_lines))
    except yaml.YAMLError:
        return None
    if isinstance(doc, list) and doc and isinstance(doc[0], dict):
        img = doc[0].get("image")
        return str(img) if img else None
    return None


def ensure_skaffold_artifacts(root) -> list[str]:
    """Reconcile skaffold.yaml's build artifacts to exactly the manifest's de-oteled
    services, idempotently, on every sync.

    Coverage was hand-maintained, so a new service that gains a k8s Deployment had to
    be added by hand or the deploy-consistency gate failed. Instead, derive the artifact
    set from the manifest: build every `<prefix>/<svc>-no-otel` image the manifest
    deploys, using the service→Dockerfile mapping from .env. Existing matching artifacts
    are kept verbatim (no loss of any hand-tuning); only the `artifacts:` block is
    rewritten, so the rest of skaffold.yaml (build settings, profiles, deploy,
    port-forward, comments) is preserved.

    A manifest service with no .env Dockerfile entry (or a missing file) is NOT given a
    fabricated artifact — it's reported for review (and the gate then flags it). Returns
    human-readable change descriptions (empty if already correct).
    """
    root = Path(root)
    skaffold = root / _SKAFFOLD_REL
    if not skaffold.is_file():
        return []
    text = skaffold.read_text(encoding="utf-8")
    lines = text.split("\n")

    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "  artifacts:")
    except StopIteration:
        return []
    end = start + 1
    while end < len(lines) and (lines[end].startswith("  - ") or lines[end].startswith("    ")):
        end += 1
    block_lines = lines[start + 1:end]

    items = _split_artifact_items(block_lines)
    prefix, suffix = config.NO_OTEL_IMAGE_PREFIX + "/", "-no-otel"

    def service_of(image: str | None) -> str | None:
        if image and image.startswith(prefix) and image.endswith(suffix):
            return image[len(prefix):-len(suffix)]
        return None

    by_service: dict[str, list[str]] = {}
    other_items: list[list[str]] = []          # non-`-no-otel` artifacts: leave untouched
    for item in items:
        svc = service_of(_artifact_image(item))
        if svc is None:
            other_items.append(item)
        else:
            by_service[svc] = item

    desired = _manifest_no_otel_services(root)
    dfmap = _service_dockerfile_map(root)
    changes: list[str] = []

    kept_or_added: list[list[str]] = []
    for svc in sorted(desired):
        if svc in by_service:
            kept_or_added.append(by_service[svc])      # keep verbatim
            continue
        df = dfmap.get(svc)
        if not df or not (root / df).is_file():
            changes.append(f"skaffold: {svc} has no buildable Dockerfile (env/file missing) — needs review")
            continue
        kept_or_added.append([
            f"  - image: {config.no_otel_image(svc)}",
            "    context: .",
            "    docker:",
            f"      dockerfile: {df}",
        ])
        changes.append(f"{_SKAFFOLD_REL}: added build artifact for {svc} ({df})")

    for svc in sorted(by_service):
        if svc not in desired:
            changes.append(f"{_SKAFFOLD_REL}: removed orphan build artifact for {svc} (not deployed by manifest)")

    new_block = [line for item in other_items for line in item] + \
                [line for item in kept_or_added for line in item]
    new_lines = lines[:start + 1] + new_block + lines[end:]
    new_text = "\n".join(new_lines)

    if new_text != text:
        skaffold.write_text(new_text, encoding="utf-8")
    return changes
