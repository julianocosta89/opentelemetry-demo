"""Unit test for k8s.ensure_postgres_deployable — the always-on normalization that
makes postgres deployable from the stock image regardless of whether the manifest
went through transform_kubernetes."""

import yaml

import config
from resolvers import k8s

_MANIFEST = """\
apiVersion: v1
kind: Namespace
metadata:
  name: opentelemetry-demo
---
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
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
spec:
  template:
    spec:
      containers:
      - name: frontend
        image: ghcr.io/julianocosta89/frontend-no-otel
"""

_SKAFFOLD = """\
apiVersion: skaffold/v3
kind: Config
build:
  artifacts:
  - image: ghcr.io/julianocosta89/frontend-no-otel
    context: .
    docker:
      dockerfile: src/frontend/Dockerfile
  - image: ghcr.io/julianocosta89/postgresql-no-otel
    context: .
    docker:
      dockerfile: src/postgres/Dockerfile
"""


def _repo(tmp_path):
    (tmp_path / "kubernetes").mkdir()
    (tmp_path / "kubernetes" / "opentelemetry-demo.yaml").write_text(_MANIFEST)
    (tmp_path / "skaffold.yaml").write_text(_SKAFFOLD)
    (tmp_path / "src" / "postgresql").mkdir(parents=True)
    (tmp_path / "src" / "postgresql" / "init.sql").write_text("CREATE USER otelu WITH PASSWORD 'otelp';\n")
    return tmp_path


def test_ensure_postgres_deployable_fixes_manifest_and_skaffold(tmp_path):
    repo = _repo(tmp_path)
    changed = k8s.ensure_postgres_deployable(repo)
    assert len(changed) == 2  # manifest + skaffold both fixed

    docs = list(yaml.safe_load_all((repo / "kubernetes/opentelemetry-demo.yaml").read_text()))
    pg = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "postgresql")
    container = pg["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == config.POSTGRES_IMAGE          # stock, not -no-otel
    mount = next(m for m in container["volumeMounts"] if m["name"] == "init-sql")
    assert mount["mountPath"] == "/docker-entrypoint-initdb.d"

    cm = next(d for d in docs if d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == config.POSTGRES_INIT_CONFIGMAP)
    assert "init.sql" in cm["data"]

    # other docs untouched
    assert any(d.get("kind") == "Deployment" and d["metadata"]["name"] == "frontend" for d in docs)

    # skaffold no longer builds postgres, but keeps the other artifact
    arts = {a["image"] for a in yaml.safe_load((repo / "skaffold.yaml").read_text())["build"]["artifacts"]}
    assert "ghcr.io/julianocosta89/postgresql-no-otel" not in arts
    assert "ghcr.io/julianocosta89/frontend-no-otel" in arts


def test_ensure_postgres_deployable_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    assert k8s.ensure_postgres_deployable(repo)        # first call changes things
    manifest_after = (repo / "kubernetes/opentelemetry-demo.yaml").read_text()
    skaffold_after = (repo / "skaffold.yaml").read_text()
    assert k8s.ensure_postgres_deployable(repo) == []  # second call is a no-op
    assert (repo / "kubernetes/opentelemetry-demo.yaml").read_text() == manifest_after
    assert (repo / "skaffold.yaml").read_text() == skaffold_after
