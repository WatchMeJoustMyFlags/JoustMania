"""Envoy is the single host entry and routes the documented UI paths (#1365, UC-035 step 1, BR-001).

Static checks over services/envoy/envoy.yaml and docker-compose.yml, plus
``envoy --mode validate`` with the compose image when docker is available.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
import uuid

import infra_config as ic
import pytest

# UC-035 / telemetry.md: path the operator opens -> (envoy cluster, path the upstream receives).
DOCUMENTED_ROUTES = {
    "/": ("dashboard", "/"),
    "/index.html": ("dashboard", "/index.html"),
    "/grafana/d/abc": ("grafana", "/grafana/d/abc"),  # Grafana serve_from_sub_path
    "/jaeger/search": ("jaeger", "/jaeger/search"),  # Jaeger base_path /jaeger
    "/victoria-metrics/api/v1/query": ("victoria_metrics", "/api/v1/query"),
    "/experiments": ("agent_dossier", "/experiments"),  # UC-035 A1
    "/experiments/exp-1": ("agent_dossier", "/experiments/exp-1"),
    "/pairing/unpair": ("connect_proxy", "/pairing/unpair"),  # operator unpair (UC-005)
    "/joustmania.menu.MenuService/StreamMenuEvents": (
        "connect_proxy",
        "/joustmania.menu.MenuService/StreamMenuEvents",
    ),
}

# Non-UI Connect routes the host-networked pairing daemon relies on.
PAIRING_DAEMON_ROUTES = {
    # UC-001 BR-005: pairing.bluez_restart_mode=when_idle reads the primary game's state here
    # (bluez_reload.DEFAULT_GAME_STATE_URL); a missing route reads as "game running" forever.
    "/joustmania.game_coordinator.GameCoordinatorService/GetGameState": (
        "connect_proxy",
        "/joustmania.game_coordinator.GameCoordinatorService/GetGameState",
    ),
}

# Bare UI paths redirect to their trailing-slash form.
DOCUMENTED_REDIRECTS = {
    "/grafana": "/grafana/",
    "/jaeger": "/jaeger/",
    "/victoria-metrics": "/victoria-metrics/",
}

# UC-035 BR-001: host ports other than envoy :80, for the host-networked pairing daemon.
# Bound to loopback only, so none is reachable from the venue network (#1319).
BR001_HOST_PORT_EXCEPTIONS = {
    "flagd": {"127.0.0.1:8015:8015"},
    "rust-hid": {"127.0.0.1:50058:50058"},
    # OTLP for the host-networked pairing daemon; not a UI. The collector's
    # Prometheus exporter (8889) went with the Prometheus container (#1449).
    "otel-collector": {"127.0.0.1:4317:4317"},
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
    """UC-035 steps 1-7: dashboard, Jaeger, Grafana and VictoriaMetrics paths."""
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


@pytest.mark.uc("UC-035", flow="main", steps=[1])
def test_loki_route_is_gone_and_falls_through_to_the_dashboard():
    """#2059: no Loki cluster; /loki/ falls through to the dashboard SPA like /prometheus/."""
    assert "loki" not in ic.envoy_clusters()
    assert ic.resolve_route("/loki/loki/api/v1/query_range")["cluster"] == "dashboard"


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


@pytest.mark.uc("UC-035", flow="A1")
@pytest.mark.uc("UC-035", flow="A3")
def test_agent_down_envoy_answers_experiments_with_its_own_error():
    """UC-035 A1 / A3: with no agent to route to, envoy itself answers /experiments with 503."""
    if not ic.docker_available():
        pytest.skip("docker not available")
    name = f"uc035-envoy-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::80",
            # No resolver: the STRICT_DNS `agent` cluster gets no hosts, so envoy answers
            # "no healthy upstream" at once. Host DNS (WSL2) may otherwise resolve `agent`
            # to an unroutable address and answer "connection timeout" (#1951).
            "--dns", "127.0.0.1",
            "-v", f"{ic.ENVOY_FILE}:/etc/envoy/envoy.yaml:ro",
            ic.compose_image("envoy"), "-c", "/etc/envoy/envoy.yaml",
        ],
        capture_output=True, text=True, check=True, timeout=300,
    )  # fmt: skip
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "80/tcp"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            .stdout.split(":")[-1]
            .strip()
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:  # envoy's own /health answers once the listener is up
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            assert time.monotonic() < deadline, "envoy did not start"
            time.sleep(0.5)

        with pytest.raises(urllib.error.HTTPError) as answer:
            urllib.request.urlopen(f"{base}/experiments", timeout=10)
        assert answer.value.code == 503
        assert answer.value.read().decode() == "no healthy upstream"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, check=False, timeout=60
        )


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


@pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
@pytest.mark.parametrize(("path", "expected"), sorted(PAIRING_DAEMON_ROUTES.items()))
def test_pairing_daemon_game_state_route(path, expected):
    """UC-001 BR-005: envoy forwards the GetGameState Connect path unchanged to the connect-proxy."""
    cluster, upstream_path = expected
    assert ic.resolve_route(path) == {
        "cluster": cluster,
        "upstream_path": upstream_path,
    }


@pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
def test_pairing_daemon_default_url_uses_the_routed_path():
    """The daemon's default GAME_STATE_URL is exactly the routed path on envoy's host port 80."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "services/pairing_daemon/psmove_pairing/bluez_reload.py"
    ).read_text()
    urls = [
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_GAME_STATE_URL"
            for t in node.targets
        )
    ]
    assert urls == ["http://localhost" + next(iter(PAIRING_DAEMON_ROUTES))]
    assert ic.envoy_listener_port() == 80
