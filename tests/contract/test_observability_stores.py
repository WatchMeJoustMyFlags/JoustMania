"""Observability stores: loopback-only dev ports, retention and metric export interval.

UC-035 BR-001 / C-015 (the dev override publishes Jaeger and VictoriaMetrics on 127.0.0.1 only),
NFR-023 (retention) and NFR-022 (metric export interval). Static checks over compose, the Jaeger
config and the observability flag file.
"""

from __future__ import annotations

import json

import infra_config as ic
import pytest

OVERRIDE_FILE = ic.REPO_ROOT / "docker-compose.override.yml"
JAEGER_CONFIG = ic.REPO_ROOT / "services" / "jaeger" / "config.yaml"
OBSERVABILITY_FLAGS = ic.REPO_ROOT / "services" / "flagd" / "observability.json"


@pytest.mark.uc("UC-035", br=["BR-001"])
@pytest.mark.parametrize(
    ("service", "port"), [("jaeger", 16686), ("victoria-metrics", 8428)]
)
def test_override_publishes_query_ports_on_loopback_only(service, port):
    """C-015: the dev override binds Jaeger and VictoriaMetrics to 127.0.0.1, never all interfaces."""
    ports = ic.load_yaml(OVERRIDE_FILE)["services"][service]["ports"]
    assert [str(p) for p in ports] == [f"127.0.0.1:{port}:{port}"]


@pytest.mark.uc("UC-035", br=["BR-001"])
@pytest.mark.parametrize("service", ["jaeger", "victoria-metrics"])
def test_base_compose_publishes_no_query_port(service):
    """C-015: the base stack (the Pi path) exposes them to the network only, never to the host."""
    assert "ports" not in ic.compose_services()[service]


LITE_FILE = ic.REPO_ROOT / "docker-compose.lite.yml"


@pytest.mark.uc("UC-035", br=["BR-001"])
def test_lite_stack_publishes_only_envoy_to_the_network():
    """C-015: in the lite stack only envoy's port 80 reaches the network; flagd 8015 and rust-hid 50058 are loopback."""
    published = {
        name: [str(p) for p in svc["ports"]]
        for name, svc in ic.load_yaml(LITE_FILE)["services"].items()
        if svc.get("ports")
    }
    assert published.pop("envoy") == ["80:80"]
    for name, ports in published.items():
        for port in ports:
            assert port.startswith("127.0.0.1:"), (
                f"lite {name} publishes {port} beyond loopback"
            )
    assert sorted(p for ports in published.values() for p in ports) == [
        "127.0.0.1:50058:50058",
        "127.0.0.1:8015:8015",
    ]


@pytest.mark.nfr("NFR-023")
def test_victoria_metrics_keeps_seven_days():
    """NFR-023: VictoriaMetrics retains 7 days."""
    command = ic.compose_services()["victoria-metrics"]["command"]
    assert "-retentionPeriod=7d" in command


@pytest.mark.nfr("NFR-023")
def test_jaeger_span_ttl_is_bounded():
    """NFR-023: the badger span TTL is set (24h) so spans are not kept forever (#1191)."""
    badger = ic.load_yaml(JAEGER_CONFIG)["extensions"]["jaeger_storage"]["backends"][
        "badger"
    ]["badger"]
    assert badger["ttl"]["spans"] == "24h"


@pytest.mark.nfr("NFR-022")
def test_metric_export_interval_defaults_to_1s_and_100ms_for_the_game_services():
    """NFR-022: 1000 ms by default; 100 ms by targeting for controller-manager and game-coordinator only."""
    flag = json.loads(OBSERVABILITY_FLAGS.read_text())["flags"][
        "metrics_export_interval_ms"
    ]
    assert flag["variants"][flag["defaultVariant"]] == 1000
    condition = flag["targeting"]["if"]
    services, then, otherwise = condition[0]["in"][1], condition[1], condition[2]
    assert services == ["controller-manager-service", "game-coordinator-service"]
    assert flag["variants"][then] == 100
    assert otherwise is None


def _mib(value: str) -> int:
    """Compose / Go memory size ("1024m", "850MiB") in MiB."""
    text = str(value).strip().lower().removesuffix("ib").removesuffix("b")
    units = {"m": 1, "g": 1024}
    return int(text[:-1]) * units[text[-1]]


@pytest.mark.nfr("NFR-021")
def test_jaeger_go_heap_is_soft_capped_below_its_container_limit():
    """NFR-021 (#2223): GOMEMLIMIT keeps Jaeger's Go heap at most ~85% of the 1 GiB limit.

    Badger in Jaeger 2.19 has no cache knobs (memtables 5 x 64 MiB + 256 MiB block cache by
    default), so the Go soft limit is the only lever that makes the GC reclaim query garbage
    before the cgroup OOM-kills the trace store.
    """
    jaeger = ic.compose_services()["jaeger"]
    limit_mib = _mib(jaeger["deploy"]["resources"]["limits"]["memory"])
    env = dict(item.split("=", 1) for item in jaeger.get("environment", []))
    assert "GOMEMLIMIT" in env, "jaeger has no GOMEMLIMIT soft limit"
    soft_mib = _mib(env["GOMEMLIMIT"])
    assert limit_mib == 1024
    assert soft_mib <= limit_mib * 0.85
    assert soft_mib >= limit_mib * 0.75, (
        "a soft limit far below the live badger set thrashes the GC"
    )
