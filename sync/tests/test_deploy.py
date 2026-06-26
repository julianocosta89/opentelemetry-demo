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


# ── ensure_skaffold_artifacts: auto-derive coverage from the manifest ────────────

def _img(svc):
    return f"{config.NO_OTEL_IMAGE_PREFIX}/{svc}-no-otel"


def _artifact_block(svc, dockerfile):
    return f"  - image: {_img(svc)}\n    context: .\n    docker:\n      dockerfile: {dockerfile}\n"


def _write_manifest_sk(repo, services):
    docs = ["apiVersion: v1\nkind: Namespace\nmetadata:\n  name: otel-demo"]
    for s in services:
        docs.append(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
            f"  name: {s}\nspec:\n  template:\n    spec:\n      containers:\n"
            f"      - name: {s}\n        image: {_img(s)}"
        )
    (repo / "kubernetes").mkdir(exist_ok=True)
    (repo / "kubernetes" / "opentelemetry-demo.yaml").write_text("\n---\n".join(docs) + "\n")


def _write_skaffold_sk(repo, artifacts):
    body = "".join(_artifact_block(s, df) for s, df in artifacts)
    (repo / "skaffold.yaml").write_text(
        "apiVersion: skaffold/v3\nkind: Config\nbuild:\n  local:\n"
        "    push: false  # keep this comment\n"
        f"  artifacts:\n{body}manifests:\n  rawYaml:\n    - kubernetes/*\n"
    )


def _write_compose_env_sk(repo, services):
    """services: {svc: (ENV_VAR, dockerfile_path)} → compose.yaml + .env."""
    svc_blocks = "".join(
        f"  {svc}:\n    build:\n      context: ./\n      dockerfile: ${{{var}}}\n"
        for svc, (var, _df) in services.items()
    )
    env_lines = "".join(f"{var}={df}\n" for _svc, (var, df) in services.items())
    (repo / "compose.yaml").write_text("services:\n" + svc_blocks)
    (repo / ".env").write_text(env_lines)


def test_skaffold_adds_missing_service_with_mapped_path_and_is_idempotent(tmp_path):
    _write_manifest_sk(tmp_path, ["keep", "foo"])
    _write_skaffold_sk(tmp_path, [("keep", "src/keep/Dockerfile")])
    # foo builds from a NON-standard nested path — proves we use the mapping, not a guess
    _write_compose_env_sk(tmp_path, {
        "keep": ("KEEP_DOCKERFILE", "./src/keep/Dockerfile"),
        "foo": ("FOO_DOCKERFILE", "./src/foo/src/Dockerfile"),
    })
    (tmp_path / "src/foo/src").mkdir(parents=True)
    (tmp_path / "src/foo/src/Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "src/keep").mkdir(parents=True)
    (tmp_path / "src/keep/Dockerfile").write_text("FROM scratch\n")

    changes = k8s.ensure_skaffold_artifacts(tmp_path)
    assert any("foo" in c and "added" in c for c in changes)

    arts = {a["image"]: a for a in yaml.safe_load((tmp_path / "skaffold.yaml").read_text())["build"]["artifacts"]}
    assert arts[_img("foo")]["docker"]["dockerfile"] == "src/foo/src/Dockerfile"  # mapped, non-standard
    assert _img("keep") in arts

    text = (tmp_path / "skaffold.yaml").read_text()
    assert "push: false  # keep this comment" in text          # file structure/comments preserved
    assert _artifact_block("keep", "src/keep/Dockerfile") in text  # existing artifact verbatim

    assert k8s.ensure_skaffold_artifacts(tmp_path) == []        # idempotent


def test_skaffold_removes_orphan_artifact(tmp_path):
    _write_manifest_sk(tmp_path, ["keep"])
    _write_skaffold_sk(tmp_path, [("keep", "src/keep/Dockerfile"), ("bar", "src/bar/Dockerfile")])
    _write_compose_env_sk(tmp_path, {"keep": ("KEEP_DOCKERFILE", "./src/keep/Dockerfile")})
    changes = k8s.ensure_skaffold_artifacts(tmp_path)
    assert any("bar" in c and "orphan" in c for c in changes)
    arts = {a["image"] for a in yaml.safe_load((tmp_path / "skaffold.yaml").read_text())["build"]["artifacts"]}
    assert arts == {_img("keep")}


def test_skaffold_does_not_fabricate_when_dockerfile_unmapped(tmp_path):
    _write_manifest_sk(tmp_path, ["nodf"])
    _write_skaffold_sk(tmp_path, [])
    _write_compose_env_sk(tmp_path, {})        # nodf has no mapping at all
    changes = k8s.ensure_skaffold_artifacts(tmp_path)
    assert any("nodf" in c and "needs review" in c for c in changes)
    arts = yaml.safe_load((tmp_path / "skaffold.yaml").read_text())["build"]["artifacts"] or []
    assert not any(a["image"] == _img("nodf") for a in arts)   # not fabricated
