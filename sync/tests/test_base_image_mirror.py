"""Tests for the Dockerfile base-image → mirror rewrite."""

import pytest

import base_image_mirror as bim

M = "mirror.gcr.io"


@pytest.mark.parametrize("ref, expected", [
    ("python:3.12", "mirror.gcr.io/library/python:3.12"),
    ("eclipse-temurin:21-jre", "mirror.gcr.io/library/eclipse-temurin:21-jre"),
    ("docker.io/library/node:22", "mirror.gcr.io/library/node:22"),
    ("index.docker.io/library/ruby:3.3", "mirror.gcr.io/library/ruby:3.3"),
    ("nginxinc/nginx-unprivileged:1.27", "mirror.gcr.io/nginxinc/nginx-unprivileged:1.27"),
    ("envoyproxy/envoy:v1.32-latest", "mirror.gcr.io/envoyproxy/envoy:v1.32-latest"),
    # left untouched (return None):
    ("mcr.microsoft.com/dotnet/sdk:10.0", None),
    ("gcr.io/distroless/static-debian12", None),
    ("ghcr.io/mlocati/php-extension-installer:latest", None),
    ("quay.io/foo/bar:1", None),
    ("scratch", None),
    ("mirror.gcr.io/library/python:3.12", None),   # already mirrored — idempotent
])
def test_mirror_image_ref(ref, expected):
    assert bim.mirror_image_ref(ref, M) == expected


def test_mirror_disabled_is_noop():
    assert bim.mirror_image_ref("python:3.12", "") is None


DOCKERFILE = """\
# Copyright
FROM --platform=$BUILDPLATFORM eclipse-temurin:21-jdk AS builder
RUN ./gradlew build
FROM builder AS intermediate
FROM eclipse-temurin:21-jre
COPY --from=builder /app /app
FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS dotnet
FROM scratch
"""


def test_rewrite_dockerfile():
    out = bim.rewrite_dockerfile(DOCKERFILE, M)
    lines = out.splitlines()
    # builder base rewritten, --platform + AS preserved
    assert "FROM --platform=$BUILDPLATFORM mirror.gcr.io/library/eclipse-temurin:21-jdk AS builder" in lines
    # runtime base rewritten
    assert "FROM mirror.gcr.io/library/eclipse-temurin:21-jre" in lines
    # stage alias NOT rewritten
    assert "FROM builder AS intermediate" in lines
    # non-docker registry untouched
    assert "FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS dotnet" in lines
    # scratch untouched
    assert "FROM scratch" in lines


def test_rewrite_is_idempotent():
    once = bim.rewrite_dockerfile(DOCKERFILE, M)
    twice = bim.rewrite_dockerfile(once, M)
    assert once == twice


def test_rewrite_tree(tmp_path):
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "a" / "Dockerfile").write_text("FROM python:3.12\n")
    (tmp_path / "src" / "b").mkdir(parents=True)
    (tmp_path / "src" / "b" / "Dockerfile").write_text("FROM gcr.io/distroless/static\n")  # unchanged
    changed = bim.rewrite_tree(tmp_path, M)
    assert changed == ["src/a/Dockerfile"]
    assert "mirror.gcr.io/library/python:3.12" in (tmp_path / "src" / "a" / "Dockerfile").read_text()
