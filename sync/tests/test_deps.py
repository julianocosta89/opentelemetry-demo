"""
Table-driven tests for the deterministic dependency-manifest transforms.

Fixtures mirror the real conflict shapes captured from upstream: a legitimate
version bump, an OTel line to drop, and (for go) an indirect OTel entry to keep.
The invariant for every type: OTel gone, legitimate deps kept, output still valid.
"""

import json

import otel_rules
from resolvers import deps

GO_MOD = """\
module github.com/open-telemetry/opentelemetry-demo/src/checkout

go 1.22

require (
\tgithub.com/IBM/sarama v1.45.0
\tgoogle.golang.org/grpc v1.78.0
\tgo.opentelemetry.io/otel v1.38.0
\tgo.opentelemetry.io/otel/trace v1.38.0
\tgithub.com/open-feature/go-sdk v1.17.1
)

require (
\tgo.opentelemetry.io/auto/sdk v1.2.1 // indirect
\tconnectrpc.com/otelconnect v0.7.2 // indirect
)
"""

REQUIREMENTS = """\
grpcio-health-checking==1.78.0
psycopg2-binary==2.9.9
openfeature-hooks-opentelemetry==0.3.0
opentelemetry-sdk==1.38.0
openai==1.0.0
"""

PACKAGE_JSON = json.dumps({
    "name": "frontend",
    "repository": "https://github.com/opentelemetry/opentelemetry-demo",   # identity, kept
    "scripts": {
        "start": "node --require @opentelemetry/auto-instrumentations-node/register index.js",
        "build": "next build",
    },
    "dependencies": {
        "next": "15.0.0",
        "@opentelemetry/api": "^1.9.0",
        "pino-opentelemetry-transport": "^3.0.0",   # unscoped OTel dep
        "@openfeature/react-sdk": "1.2.0",
    },
    "devDependencies": {
        "@opentelemetry/instrumentation": "^0.50.0",
        "typescript": "5.4.0",
    },
    "overrides": {
        "@opentelemetry/otlp-transformer": {"protobufjs": "8.2.0"},
    },
}, indent=2)

COMPOSER_JSON = json.dumps({
    "require": {
        "slim/slim": "^4.0",
        "open-telemetry/sdk": "^1.0",
        "open-telemetry/exporter-otlp": "^1.0",
    },
}, indent=2)

CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk.Web">
  <ItemGroup>
    <PackageReference Include="Confluent.Kafka" Version="2.5.0" />
    <PackageReference Include="OpenTelemetry.Extensions.Hosting" Version="1.9.0" />
    <PackageReference Include="OpenTelemetry.Exporter.OpenTelemetryProtocol" Version="1.9.0" />
    <PackageReference Include="Npgsql" Version="8.0.0" />
  </ItemGroup>
</Project>
"""

BUILD_GRADLE = """\
plugins {
    id 'java'
}
group = "io.opentelemetry"
dependencies {
    implementation 'io.grpc:grpc-netty:1.65.0'
    implementation 'io.opentelemetry:opentelemetry-api:1.40.0'
    agent 'io.opentelemetry.javaagent:opentelemetry-javaagent:2.7.0'
}
"""

MIX_EXS = """\
defmodule FlagdUi.MixProject do
  use Mix.Project
  defp deps do
    [
      {:phoenix, "~> 1.7"},
      {:opentelemetry, "~> 1.3"},
      {:opentelemetry_exporter, "~> 1.6"},
      {:jason, "~> 1.4"}
    ]
  end
end
"""

CARGO_TOML = """\
[package]
name = "shipping"

[dependencies]
tonic = "0.12"
opentelemetry = "0.24"
opentelemetry-otlp = "0.17"
tokio = "1"
"""


def _no_otel(rel_path: str, text: str):
    return otel_rules.scan_text(rel_path, text)


def test_go_mod_drops_direct_otel_keeps_indirect_and_bumps():
    out = deps.transform_go_mod(GO_MOD)
    assert "go.opentelemetry.io/otel v1.38.0" not in out
    assert "go.opentelemetry.io/otel/trace" not in out
    # indirect OTel stays (go mod tidy settles it); the absence rule allows it
    assert "go.opentelemetry.io/auto/sdk v1.2.1 // indirect" in out
    # legitimate deps + bumps preserved
    assert "google.golang.org/grpc v1.78.0" in out
    assert "github.com/open-feature/go-sdk v1.17.1" in out
    assert not _no_otel("src/checkout/go.mod", out)  # go_mod rule allows indirect


def test_requirements_drops_otel_keeps_bumps():
    out = deps.transform_requirements(REQUIREMENTS)
    assert "opentelemetry" not in out.lower()
    assert "grpcio-health-checking==1.78.0" in out
    assert "openai==1.0.0" in out
    assert not _no_otel("src/recommendation/requirements.txt", out)


def test_package_json_valid_and_otel_free():
    out = deps.transform_package_json(PACKAGE_JSON)
    obj = json.loads(out)  # must stay valid JSON
    # all OTel deps gone, scoped and unscoped, across every section
    assert "opentelemetry" not in out.lower().replace(
        "github.com/opentelemetry/opentelemetry-demo", "")  # ...except the repo URL
    assert "pino-opentelemetry-transport" not in obj["dependencies"]
    assert "overrides" not in obj                       # emptied → dropped
    # npm start script keeps the command but drops the --require instrumentation flag
    assert obj["scripts"]["start"] == "node index.js"
    assert obj["scripts"]["build"] == "next build"
    # business deps + identity preserved
    assert obj["dependencies"]["next"] == "15.0.0"
    assert obj["dependencies"]["@openfeature/react-sdk"] == "1.2.0"
    assert obj["repository"] == "https://github.com/opentelemetry/opentelemetry-demo"
    assert not _no_otel("src/frontend/package.json", out)


def test_composer_json_valid_and_otel_free():
    out = deps.transform_composer_json(COMPOSER_JSON)
    obj = json.loads(out)
    assert obj["require"] == {"slim/slim": "^4.0"}


def test_csproj_drops_otel_packagerefs():
    out = deps.transform_csproj(CSPROJ)
    assert "OpenTelemetry" not in out
    assert "Confluent.Kafka" in out and "Npgsql" in out
    assert not _no_otel("src/accounting/Accounting.csproj", out)


def test_gradle_keeps_identity_and_drops_otel_deps():
    out = deps.transform_gradle(BUILD_GRADLE)
    assert 'group = "io.opentelemetry"' in out  # identity KEPT verbatim (not renamed)
    assert "opentelemetry-api" not in out       # OTel dependency dropped
    assert "opentelemetry-javaagent" not in out  # OTel agent dropped
    assert "io.grpc:grpc-netty" in out          # non-OTel dep kept
    assert not _no_otel("src/ad/build.gradle", out)   # gate sees no OTel artifacts


def test_mix_exs_drops_otel_deps():
    out = deps.transform_mix_exs(MIX_EXS)
    assert "opentelemetry" not in out.lower()
    assert ":phoenix" in out and ":jason" in out
    assert not _no_otel("src/flagd-ui/mix.exs", out)


def test_cargo_toml_drops_otel():
    out = deps.transform_cargo_toml(CARGO_TOML)
    assert "opentelemetry" not in out.lower()
    assert 'tonic = "0.12"' in out and 'tokio = "1"' in out
    assert not _no_otel("src/shipping/Cargo.toml", out)


def test_transforms_strip_stray_conflict_markers():
    marked = "<<<<<<< HEAD\nfoo==1.0\n=======\nfoo==2.0\nopentelemetry-sdk==1.0\n>>>>>>> upstream\n"
    out = deps.transform_requirements(marked)
    assert "<<<<<<<" not in out and ">>>>>>>" not in out and "=======" not in out
    assert "opentelemetry" not in out.lower()
