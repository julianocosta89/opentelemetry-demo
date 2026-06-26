"""Tests for the deterministic Kubernetes manifest transform."""

import yaml

import config
from resolvers import k8s

MANIFEST = """\
apiVersion: v1
kind: Namespace
metadata:
  name: opentelemetry-demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: jaeger
  labels:
    opentelemetry.io/name: jaeger
spec:
  template:
    spec:
      containers:
        - name: jaeger
          image: jaegertracing/all-in-one
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  labels:
    opentelemetry.io/name: checkout
    app.kubernetes.io/name: checkout
spec:
  template:
    spec:
      initContainers:
        - name: wait-for-otelcol
          image: busybox:opentelemetry
      containers:
        - name: checkout
          image: ghcr.io/checkout
          env:
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: http://otel-collector:4317
            - name: CHECKOUT_PORT
              value: "5050"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flagd
spec:
  template:
    spec:
      containers:
        - name: flagd
          image: ghcr.io/flagd
        - name: flagd-ui
          image: ghcr.io/flagd-ui
"""


def _by_name(docs):
    return {d["metadata"]["name"]: d for d in docs if isinstance(d, dict) and "metadata" in d}


def test_transform_kubernetes_removes_infra_keeps_structure():
    out = k8s.transform_kubernetes(MANIFEST)
    docs = list(yaml.safe_load_all(out))
    names = _by_name(docs)

    # OTel infra resource removed
    assert "jaeger" not in names
    # Namespace and app deployments survive
    assert "opentelemetry-demo" in names
    assert "checkout" in names
    # Upstream structure kept, nothing synthesized: flagd-ui stays a container in the
    # flagd pod (no standalone Deployment), and llm/product-reviews aren't added.
    assert "flagd" in names
    flagd_containers = {c["name"] for c in names["flagd"]["spec"]["template"]["spec"]["containers"]}
    assert "flagd-ui" in flagd_containers
    assert "flagd-ui" not in names
    assert "llm" not in names
    assert "product-reviews" not in names

    checkout = names["checkout"]
    containers = checkout["spec"]["template"]["spec"]["containers"]
    env_names = [e["name"] for e in containers[0].get("env", [])]
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env_names   # OTEL_ env stripped
    assert "CHECKOUT_PORT" in env_names                      # business env kept
    # OTel init container removed
    assert "initContainers" not in checkout["spec"]["template"]["spec"]

    # opentelemetry.io/* labels are KEPT (the demo's identity labels), while OTEL_
    # env vars are still stripped.
    assert names["checkout"]["metadata"]["labels"].get("opentelemetry.io/name") == "checkout"
    assert "OTEL_" not in out


IMAGE_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
spec:
  template:
    spec:
      containers:
        - name: checkout
          image: ghcr.io/open-telemetry/demo:2.1.3-checkout
        - name: valkey
          image: valkey/valkey:8.1.3-alpine
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: product-catalog
spec:
  template:
    spec:
      containers:
        - name: product-catalog
          image: ghcr.io/open-telemetry/demo:latest-product-catalog
"""


def test_image_rewrite_and_thirdparty_preserved():
    out = k8s.transform_kubernetes(IMAGE_MANIFEST)
    names = _by_name(list(yaml.safe_load_all(out)))

    # upstream prebuilt demo images → local de-otel images
    checkout_imgs = [c["image"] for c in names["checkout"]["spec"]["template"]["spec"]["containers"]]
    assert config.no_otel_image("checkout") in checkout_imgs
    assert "valkey/valkey:8.1.3-alpine" in checkout_imgs          # third-party untouched
    assert config.no_otel_image("product-catalog") in \
        [c["image"] for c in names["product-catalog"]["spec"]["template"]["spec"]["containers"]]

    # no upstream prebuilt refs survive anywhere
    assert config.UPSTREAM_IMAGE_REPO + ":" not in out


SELECTOR_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: load-generator
spec:
  selector:
    matchLabels:
      opentelemetry.io/name: load-generator
  template:
    metadata:
      labels:
        app.kubernetes.io/name: load-generator
        opentelemetry.io/name: load-generator
    spec:
      containers:
        - name: load-generator
          image: ghcr.io/open-telemetry/demo:2.1.3-load-generator
---
apiVersion: v1
kind: Service
metadata:
  name: kafka
spec:
  selector:
    opentelemetry.io/name: kafka
  ports:
    - port: 9092
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flagd
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: flagd
  template:
    metadata:
      labels:
        app.kubernetes.io/name: flagd
    spec:
      containers:
        - name: flagd
          image: ghcr.io/open-feature/flagd:v0.12.8
        - name: flagd-ui
          image: ghcr.io/open-telemetry/demo:2.1.3-flagd-ui
          volumeMounts:
            - mountPath: /app/data
              name: config-rw
"""


def test_otel_labels_and_selectors_preserved():
    """This fork keeps opentelemetry.io/* labels (the demo's identity labels, used as
    selectors) — they survive the transform unchanged rather than being stripped."""
    docs = _by_name(list(yaml.safe_load_all(k8s.transform_kubernetes(SELECTOR_MANIFEST))))
    sel = docs["load-generator"]["spec"]["selector"]["matchLabels"]
    assert sel == {"opentelemetry.io/name": "load-generator"}     # preserved as-is
    labels = docs["load-generator"]["spec"]["template"]["metadata"]["labels"]
    assert labels.get("opentelemetry.io/name") == "load-generator"   # template label kept


def test_service_otel_selector_preserved():
    """A Service keyed on opentelemetry.io/name keeps that selector — it's the demo's
    identity label, not instrumentation, so routing stays intact."""
    docs = list(yaml.safe_load_all(k8s.transform_kubernetes(SELECTOR_MANIFEST)))
    svc = next(d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"] == "kafka")
    assert svc["spec"]["selector"] == {"opentelemetry.io/name": "kafka"}


def test_empty_selector_still_repaired():
    """A genuinely empty/missing selector is still synthesized (invalid otherwise)."""
    manifest = """\
apiVersion: v1
kind: Service
metadata:
  name: orphan
spec:
  ports:
    - port: 80
"""
    docs = list(yaml.safe_load_all(k8s.transform_kubernetes(manifest)))
    svc = next(d for d in docs if d.get("kind") == "Service")
    assert svc["spec"]["selector"] == {"app.kubernetes.io/name": "orphan"}


POSTGRES_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgresql
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: postgresql
  template:
    metadata:
      labels:
        app.kubernetes.io/name: postgresql
    spec:
      containers:
      - name: postgresql
        image: ghcr.io/julianocosta89/postgresql-no-otel
        volumeMounts: null
      volumes: null
"""

_INIT_SQL = "CREATE USER otelu WITH PASSWORD 'otelp';\n\nCREATE SCHEMA catalog;\n"


def test_postgres_normalized_to_stock_image_and_configmap():
    """An existing custom-built postgres image is repointed at the stock image and
    loads init.sql from the generated ConfigMap."""
    docs = list(yaml.safe_load_all(k8s.transform_kubernetes(POSTGRES_MANIFEST, _INIT_SQL)))
    dep = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "postgresql")
    pod = dep["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == config.POSTGRES_IMAGE
    assert "no-otel" not in container["image"]
    mount = next(m for m in container["volumeMounts"] if m["name"] == "init-sql")
    assert mount["mountPath"] == "/docker-entrypoint-initdb.d"
    vol = next(v for v in pod["volumes"] if v["name"] == "init-sql")
    assert vol["configMap"]["name"] == config.POSTGRES_INIT_CONFIGMAP


def test_postgres_init_configmap_generated_from_source():
    """The init.sql ConfigMap content comes verbatim from the provided source."""
    docs = list(yaml.safe_load_all(k8s.transform_kubernetes(POSTGRES_MANIFEST, _INIT_SQL)))
    cms = [d for d in docs if d.get("kind") == "ConfigMap"
           and d["metadata"]["name"] == config.POSTGRES_INIT_CONFIGMAP]
    assert len(cms) == 1                       # exactly one, no duplicates
    assert cms[0]["data"]["init.sql"].strip() == _INIT_SQL.strip()


def test_postgres_synthesized_when_upstream_manifest_drops_it():
    """Upstream ships no postgres in k8s; if a conflicting manifest is taken from
    upstream, postgres must be synthesized back (stock image + ConfigMap)."""
    no_pg = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
spec:
  template:
    spec:
      containers:
      - name: frontend
        image: ghcr.io/open-telemetry/demo:2.2.0-frontend
"""
    docs = list(yaml.safe_load_all(k8s.transform_kubernetes(no_pg, _INIT_SQL)))
    names = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in docs if d}
    assert ("Deployment", "postgresql") in names
    assert ("ConfigMap", config.POSTGRES_INIT_CONFIGMAP) in names


def test_postgres_untouched_without_init_sql():
    """With no init.sql available, postgres is left as-is rather than half-migrated."""
    docs = list(yaml.safe_load_all(k8s.transform_kubernetes(POSTGRES_MANIFEST, None)))
    dep = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "postgresql")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/julianocosta89/postgresql-no-otel"
    assert not [d for d in docs if d.get("kind") == "ConfigMap"]
