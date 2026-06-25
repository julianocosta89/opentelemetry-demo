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
