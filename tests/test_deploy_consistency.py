"""
Deploy-consistency gate: skaffold.yaml and the k8s manifest must agree, and every
build artifact must be buildable. These are the deploy-time guards that catch the
class of bug where `skaffold dev` fails or deploys the wrong thing — e.g. skaffold
building a postgres image from a Dockerfile that no longer exists, or the manifest
deploying an image skaffold never builds.

All @pytest.mark.fast so they run inside the pipeline's verification gate, making a
sync land in NEEDS-REVIEW whenever the deploy would be inconsistent.
"""

import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "sync"))

import config  # noqa: E402

MANIFEST = PROJECT_ROOT / "kubernetes" / "opentelemetry-demo.yaml"
SKAFFOLD = PROJECT_ROOT / "skaffold.yaml"


def _skaffold_artifacts() -> list[dict]:
    return yaml.safe_load(SKAFFOLD.read_text())["build"]["artifacts"]


def _manifest_no_otel_images() -> set[str]:
    """The `<prefix>/<svc>-no-otel` images the manifest actually deploys."""
    images = set()
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec") or {}
        pod_spec = (spec.get("template") or {}).get("spec", spec)
        for key in ("containers", "initContainers"):
            for c in pod_spec.get(key, []) or []:
                img = isinstance(c, dict) and c.get("image") or ""
                if img.startswith(config.NO_OTEL_IMAGE_PREFIX + "/") and img.endswith("-no-otel"):
                    images.add(img)
    return images


@pytest.mark.fast
def test_skaffold_dockerfiles_exist():
    """Every skaffold build artifact's dockerfile must exist — a dangling path (e.g.
    a postgres artifact pointing at a removed src/postgres/Dockerfile) breaks the build."""
    missing = []
    for art in _skaffold_artifacts():
        dockerfile = (art.get("docker") or {}).get("dockerfile")
        if dockerfile and not (PROJECT_ROOT / dockerfile).is_file():
            missing.append(f"{art['image']} -> {dockerfile}")
    assert not missing, "skaffold artifacts reference missing Dockerfiles:\n  " + "\n  ".join(missing)


@pytest.mark.fast
def test_skaffold_artifacts_match_manifest_images():
    """skaffold must build exactly the de-oteled images the manifest deploys — no
    orphan artifact (builds something never deployed) and no unbuilt deployed image."""
    artifacts = {art["image"] for art in _skaffold_artifacts()}
    deployed = _manifest_no_otel_images()
    orphan = artifacts - deployed       # skaffold builds it, manifest doesn't deploy it
    unbuilt = deployed - artifacts      # manifest deploys it, skaffold doesn't build it
    assert not orphan and not unbuilt, (
        f"skaffold/manifest image mismatch\n  orphan artifacts: {sorted(orphan)}\n"
        f"  unbuilt deployed images: {sorted(unbuilt)}"
    )


@pytest.mark.fast
def test_postgres_is_stock_not_built():
    """Postgres must run the stock image + init.sql ConfigMap, not a built image."""
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if isinstance(d, dict)]
    pg = next((d for d in docs
               if d.get("kind") == "Deployment" and (d.get("metadata") or {}).get("name") == "postgresql"), None)
    assert pg is not None, "no postgresql Deployment in the manifest"
    image = pg["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == config.POSTGRES_IMAGE, f"postgres should use stock {config.POSTGRES_IMAGE}, got {image!r}"
    assert "no-otel" not in image, "postgres must not be a *-no-otel built image"

    cms = [d for d in docs
           if d.get("kind") == "ConfigMap" and (d.get("metadata") or {}).get("name") == config.POSTGRES_INIT_CONFIGMAP]
    assert len(cms) == 1, f"expected one {config.POSTGRES_INIT_CONFIGMAP} ConfigMap, found {len(cms)}"
    assert "init.sql" in (cms[0].get("data") or {}), "init.sql ConfigMap missing its init.sql key"

    pg_artifacts = [a["image"] for a in _skaffold_artifacts() if "postgres" in a["image"]]
    assert not pg_artifacts, f"skaffold should not build a postgres image: {pg_artifacts}"
