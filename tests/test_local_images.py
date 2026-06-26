"""
Guard: the k8s manifest must deploy locally-built de-oteled images, never the
upstream prebuilt images (which still contain OpenTelemetry).

This is the deploy-time analog of test_otel_absence: even with all OTel stripped
from source, deploying `kubernetes/opentelemetry-demo.yaml` with upstream image
refs (`ghcr.io/open-telemetry/demo:<ver>-<svc>`) would run instrumented binaries.
The sync pipeline rewrites those refs to `<NO_OTEL_IMAGE_PREFIX>/<svc>-no-otel`;
these tests enforce it and keep skaffold.yaml in sync with the manifest.
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

# Infra images legitimately pulled as-is (no OTel, not built from our source).
_THIRD_PARTY_SUBSTRINGS = ("valkey/", "open-feature/flagd", "busybox")


def _is_third_party(img: str) -> bool:
    # Stock postgres image (upstream-aligned: init.sql loaded via a ConfigMap, not
    # a custom build) — see sync/resolvers/k8s.py and config.POSTGRES_IMAGE.
    if img == config.POSTGRES_IMAGE:
        return True
    return any(tp in img for tp in _THIRD_PARTY_SUBSTRINGS)


def _manifest_images() -> list[str]:
    images = []
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec") or {}
        pod_spec = (spec.get("template") or {}).get("spec", spec)
        for key in ("containers", "initContainers"):
            for c in pod_spec.get(key, []) or []:
                if isinstance(c, dict) and c.get("image"):
                    images.append(c["image"])
    return images


def _skaffold_services() -> set[str]:
    cfg = yaml.safe_load(SKAFFOLD.read_text())
    services = set()
    for art in cfg["build"]["artifacts"]:
        name = art["image"]
        assert name.startswith(config.NO_OTEL_IMAGE_PREFIX + "/"), \
            f"skaffold artifact {name} does not use NO_OTEL_IMAGE_PREFIX {config.NO_OTEL_IMAGE_PREFIX}"
        assert name.endswith("-no-otel"), f"skaffold artifact {name} must end with -no-otel"
        services.add(name[len(config.NO_OTEL_IMAGE_PREFIX) + 1: -len("-no-otel")])
    return services


@pytest.mark.fast
def test_no_upstream_prebuilt_images():
    """No container may reference an upstream prebuilt (instrumented) demo image."""
    offenders = [img for img in _manifest_images()
                 if config.upstream_image_service(img) is not None]
    assert not offenders, (
        "k8s manifest references upstream prebuilt images (contain OTel):\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


@pytest.mark.fast
def test_app_images_use_no_otel_convention():
    """Every non-third-party image must be <prefix>/<service>-no-otel."""
    bad = []
    for img in _manifest_images():
        if _is_third_party(img):
            continue
        if not (img.startswith(config.NO_OTEL_IMAGE_PREFIX + "/") and "-no-otel" in img):
            bad.append(img)
    assert not bad, "manifest images not following the de-otel convention:\n  " + "\n  ".join(sorted(set(bad)))


@pytest.mark.fast
def test_skaffold_builds_every_deployed_app_image():
    """Every de-otel image the manifest deploys must have a skaffold artifact to build it."""
    skaffold = _skaffold_services()
    deployed = set()
    for img in _manifest_images():
        if img.startswith(config.NO_OTEL_IMAGE_PREFIX + "/") and img.endswith("-no-otel"):
            deployed.add(img[len(config.NO_OTEL_IMAGE_PREFIX) + 1: -len("-no-otel")])
    missing = deployed - skaffold
    assert not missing, f"manifest deploys images skaffold won't build: {sorted(missing)}"
