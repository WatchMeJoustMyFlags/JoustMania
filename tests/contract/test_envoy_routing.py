"""Envoy is the single host entry and routes the documented UI paths (#1365, UC-035 step 1, BR-001).

Static checks over services/envoy/envoy.yaml and docker-compose.yml, plus
``envoy --mode validate`` with the compose image when docker is available.
"""

from __future__ import annotations

import subprocess

import infra_config as ic
import pytest

# UC-035 / telemetry.md: path the operator opens -> (envoy cluster, path the upstream receives).
DOCUMENTED_ROUTES = {
    "/": ("dashboard", "/"),
    "/index.html": ("dashboard", "/index.html"),
    "/grafana/d/abc": ("grafana", "/grafana/d/abc"),  # Grafana serve_from_sub_path
    "/jaeger/search": ("jaeger", "/jaeger/search"),  # Jaeger base_path /jaeger
    "/loki/loki/api/v1/query_range": ("loki", "/loki/api/v1/query_range"),
    "/victoria-metrics/api/v1/query": ("victoria_metrics", "/api/v1/query"),
    "/experiments": ("agent_dossier", "/experiments"),  # UC-035 A1
    "/experiments/exp-1": ("agent_dossier", "/experiments/exp-1"),
    "/pairing/unpair": ("connect_proxy", "/pairing/unpair"),  # operator unpair (UC-005)
    "/joustmania.menu.MenuService/StreamMenuEvents": (
        "connect_proxy",
        "/joustmania.menu.MenuService/StreamMenuEvents",
    ),
}

# Bare UI paths redirect to their trailing-slash form.
DOCUMENTED_REDIRECTS = {
    "/grafana": "/grafana/",
    "/jaeger": "/jaeger/",
    "/victoria-metrics": "/victoria-metrics/",
}

# UC-035 BR-001: host ports other than envoy :80, for the host-networked pairing daemon.
BR001_HOST_PORT_EXCEPTIONS = {
    "flagd": {"8015:8015"},
    "rust-hid": {"50058:50058"},
    # OTLP for the host-networked pairing daemon; not a UI. The collector's
    # Prometheus exporter (8889) went with the Prometheus container (#1449).
    "otel-collector": {"4317:4317"},
}


@pytest.mark.uc("UC-035", flow="main", steps=[1], br=["BR-001"])
def test_envoy_listens_on_port_80_and_is_published_on_host_80():
    """UC-035 step 1 / BR-001: the operator's one URL is envoy on port 80."""
    assert ic.envoy_listener_port() == 80
    assert ic.compose_services()["envoy"]["ports"] == ["80:80"]


@pytest.mark.uc("UC-035", flow="main", br=["BR-001"])
def test_no_ui_service_publishes_a_host_port():
    """UC-035 BR-001: UIs and HTTP APIs are reachable only through envoy."""
    published = {
        name: set(svc.get("ports") or [])
        for name, svc in ic.compose_services().items()
        if svc.get("ports") and name != "envoy"
    }
    unexpected = {
        name: ports
        for name, ports in published.items()
        if ports != BR001_HOST_PORT_EXCEPTIONS.get(name)
    }
    assert not unexpected, f"host ports outside UC-035 BR-001: {unexpected}"


@pytest.mark.uc("UC-035", flow="main", steps=[1, 3, 5, 6, 7])
@pytest.mark.parametrize(("path", "expected"), sorted(DOCUMENTED_ROUTES.items()))
def test_documented_path_routes_to_its_backend(path, expected):
    """UC-035 steps 1-7: dashboard, Jaeger, Grafana, Loki and VictoriaMetrics paths."""
    cluster, upstream_path = expected
    assert ic.resolve_route(path) == {
        "cluster": cluster,
        "upstream_path": upstream_path,
    }


@pytest.mark.uc("UC-035", flow="main", steps=[1], br=["BR-001"])
def test_prometheus_container_and_route_are_gone():
    """#1449: VictoriaMetrics is the only metrics store. No prometheus service, volume
    or envoy cluster remains, and /prometheus/ falls through to the dashboard SPA."""
    compose = ic.load_yaml(ic.COMPOSE_FILE)
    assert "prometheus" not in compose["services"]
    assert "prometheus-data" not in (compose.get("volumes") or {})
    assert "prometheus" not in ic.envoy_clusters()
    assert ic.resolve_route("/prometheus/graph")["cluster"] == "dashboard"


@pytest.mark.uc("UC-035", flow="A1")
def test_experiment_dossier_routes_to_the_agent():
    """UC-035 A1: /experiments and /experiments/<id> reach the agent's dossier server."""
    assert ic.resolve_route("/experiments")["cluster"] == "agent_dossier"
    assert ic.resolve_route("/experiments/42")["upstream_path"] == "/experiments/42"
    assert ic.envoy_clusters()["agent_dossier"] == ("agent", 13134)
    assert (
        ic.compose_services()["agent"]["environment"].count("AGENT_HEALTH_ADDR=:13134")
        == 1
    )


@pytest.mark.parametrize(("path", "target"), sorted(DOCUMENTED_REDIRECTS.items()))
def test_bare_ui_paths_redirect_to_trailing_slash(path, target):
    assert ic.resolve_route(path) == {"redirect": target}


def test_health_is_answered_by_envoy():
    assert ic.resolve_route("/health") == {"direct_response": 200}


def test_catch_all_dashboard_route_is_last():
    """A catch-all earlier in the list would shadow every UI path."""
    routes = ic.envoy_routes()
    assert routes[-1]["match"] == {"prefix": "/"}
    assert routes[-1]["route"]["cluster"] == "dashboard"
    assert all(r["match"] != {"prefix": "/"} for r in routes[:-1])


def test_every_cluster_points_at_a_compose_service_port():
    """Every envoy cluster (UI routes, /pairing/, the tracing exporter) targets a compose
    service on a port that service actually exposes, listens on or is probed on."""
    clusters = ic.envoy_clusters()
    services = ic.compose_services()
    assert len(clusters) >= 8  # 9 before the prometheus cluster was removed (#1449)
    for name, (host, port) in clusters.items():
        assert host in services, (
            f"cluster {name} targets unknown compose service {host}"
        )
        assert port in ic.compose_container_ports(host), (
            f"cluster {name} -> {host}:{port}, but compose shows {sorted(ic.compose_container_ports(host))}"
        )


def test_ui_sub_path_settings_match_the_routes():
    """The prefix-preserving routes rely on each UI being configured for its sub-path."""
    grafana_ini = (ic.REPO_ROOT / "services" / "grafana" / "grafana.ini").read_text()
    assert "serve_from_sub_path = true" in grafana_ini
    assert "root_url = %(protocol)s://%(domain)s/grafana/" in grafana_ini
    assert (
        "base_path: /jaeger"
        in (ic.REPO_ROOT / "services" / "jaeger" / "config.yaml").read_text()
    )


@pytest.mark.uc("UC-035", flow="main", steps=[1])
def test_envoy_config_validates_with_the_compose_image():
    """envoy --mode validate with the image docker-compose.yml runs."""
    if not ic.docker_available():
        pytest.skip("docker not available")
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-v", f"{ic.ENVOY_FILE}:/cfg/envoy.yaml:ro",
            ic.compose_image("envoy"), "--mode", "validate", "-c", "/cfg/envoy.yaml",
        ],
        capture_output=True, text=True, check=False, timeout=300,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]


@pytest.mark.parametrize(
    ("text", "ports"),
    [
        (":13134", {13134}),
        ("http://localhost:13134/healthz", {13134}),
        ("wget -qO- http://127.0.0.1:8428/-/healthy", {8428}),
        ("0.0.0.0:8889", {8889}),
        ("http://otel-collector:4317", set()),  # another service's port
        ("flagd:8013", set()),
    ],
)
def test_compose_port_scan_ignores_other_services_addresses(text, ports):
    assert {int(p) for p in ic._PORT_IN_TEXT_RE.findall(text)} == ports
