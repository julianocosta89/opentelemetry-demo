"""Tests for the shared OTel rule module."""

import otel_rules


def test_scan_text_matches_by_extension():
    hits = otel_rules.scan_text("src/x/main.go", 'import "go.opentelemetry.io/otel"\n')
    assert any(rid == "go_source" for rid, _ln, _txt in hits)


def test_go_mod_indirect_is_allowed():
    text = (
        "require (\n"
        "\tgo.opentelemetry.io/otel v1.38.0\n"               # direct → flagged
        "\tgo.opentelemetry.io/auto/sdk v1.2.1 // indirect\n"  # indirect → allowed
        ")\n"
    )
    hits = [h for h in otel_rules.scan_text("src/checkout/go.mod", text) if h[0] == "go_mod"]
    flagged_lines = {ln for _rid, ln, _txt in hits}
    assert 2 in flagged_lines       # the direct require
    assert 3 not in flagged_lines   # the indirect one is fine


def test_csharp_activitysource_rule():
    hits = otel_rules.scan_text("src/accounting/Consumer.cs",
                                'var s = new ActivitySource("Accounting");\n')
    assert any(rid == "csharp_activitysource" for rid, _ln, _txt in hits)


def test_scan_text_matches_toplevel_k8s_manifest():
    # Regression: `kubernetes/**/*.yaml` must match the top-level manifest (zero
    # intermediate dirs) — fnmatch's ** got this wrong and let dirty k8s pass validation.
    text = "env:\n  - name: OTEL_EXPORTER_OTLP_ENDPOINT\n    value: http://x:4317\n"
    hits = otel_rules.scan_text("kubernetes/opentelemetry-demo.yaml", text)
    assert any(rid == "k8s_otel_env" for rid, _ln, _txt in hits)


def test_path_matches_glob_semantics():
    m = otel_rules._path_matches
    assert m("**/*.go", "main.go")
    assert m("**/*.go", "src/checkout/main.go")
    assert m("kubernetes/**/*.yaml", "kubernetes/x.yaml")          # zero dirs
    assert m("kubernetes/**/*.yaml", "kubernetes/sub/x.yaml")      # nested
    assert m("**/Dockerfile*", "src/frontend/Dockerfile")
    assert m("docker-compose.yml", "docker-compose.yml")
    assert not m("**/*.go", "main.py")
    assert not m("kubernetes/**/*.yaml", "src/x.yaml")


def test_clean_source_has_no_hits():
    assert otel_rules.scan_text("src/x/main.go", "package main\nfunc main(){}\n") == []


def test_forbidden_paths(tmp_path):
    (tmp_path / "src" / "grafana").mkdir(parents=True)
    (tmp_path / "src" / "grafana" / "dash.json").write_text("{}")
    (tmp_path / "src" / "checkout").mkdir(parents=True)
    (tmp_path / "src" / "checkout" / "main.go").write_text("package main")
    found = otel_rules.find_forbidden_paths(tmp_path)
    rels = {str(p.relative_to(tmp_path)) for p in found}
    assert "src/grafana/dash.json" in rels
    assert "src/checkout/main.go" not in rels


def test_scan_tree_excludes_sync_and_tests(tmp_path):
    (tmp_path / "sync").mkdir()
    (tmp_path / "sync" / "rules.py").write_text('import "go.opentelemetry.io/otel"')
    (tmp_path / "app.go").write_text('import "go.opentelemetry.io/otel"')
    result = otel_rules.scan_tree(tmp_path)
    flagged = {str(p.relative_to(tmp_path)) for files in result.values() for p in files}
    assert "app.go" in flagged
    assert "sync/rules.py" not in flagged  # sync/ is excluded
