"""The OTel collector fan-out is best effort (#1365, UC-035 A3, UC-041 step 2).

Static checks over services/otel-collector/config.yaml: a slow or down backend
(Jaeger, VictoriaMetrics, the agent) must not block delivery to the others.
The service side (SDK exporters fail open when the collector itself is down)
is covered by lib/tests/test_telemetry_fail_open.py.
"""

from __future__ import annotations

import re

import infra_config as ic
import pytest


@pytest.fixture(scope="module")
def collector():
    return ic.load_yaml(ic.COLLECTOR_FILE)


def _parse_seconds(value: str) -> float:
    units = {"ms": 0.001, "s": 1, "m": 60}
    for unit in ("ms", "s", "m"):
        if value.endswith(unit):
            return float(value[: -len(unit)]) * units[unit]
    raise AssertionError(f"unparseable duration {value!r}")


@pytest.mark.uc("UC-041", flow="main", steps=[2])
@pytest.mark.uc("UC-035", flow="A3")
def test_agent_exporter_is_best_effort(collector):
    """UC-041 step 2: forwarding to the agent uses a 2 s timeout, no retries and a bounded queue."""
    agent = collector["exporters"]["otlp/agent"]
    assert agent["endpoint"] == "agent:4317"
    assert _parse_seconds(agent["timeout"]) == 2
    assert agent["retry_on_failure"] == {"enabled": False}
    queue = agent["sending_queue"]
    assert queue["enabled"] is True, (
        "without a queue a slow agent back-pressures the pipeline"
    )
    assert 0 < queue["queue_size"] <= 10_000, (
        "queue must be bounded so overflow is dropped"
    )


@pytest.mark.nfr("NFR-024")
@pytest.mark.uc("UC-035", flow="A3")
def test_agent_fan_out_is_two_seconds_no_retries_and_a_1000_item_queue(collector):
    """NFR-024: 2 s timeout, no retries and a 1000-item queue (the figures docs/requirements.md states)."""
    agent = collector["exporters"]["otlp/agent"]
    assert _parse_seconds(agent["timeout"]) == 2
    assert agent["retry_on_failure"]["enabled"] is False
    assert agent["sending_queue"]["queue_size"] == 1000


@pytest.mark.uc("UC-035", flow="A3")
@pytest.mark.parametrize("pipeline", ["traces", "metrics"])
def test_agent_is_never_the_only_sink_of_a_pipeline(collector, pipeline):
    """UC-035 A3: traces still reach Jaeger and metrics VictoriaMetrics when the agent is down."""
    exporters = collector["service"]["pipelines"][pipeline]["exporters"]
    assert "otlp/agent" in exporters
    storage = {"traces": "otlp/jaeger", "metrics": "prometheusremotewrite"}[pipeline]
    assert storage in exporters


@pytest.mark.uc("UC-035", flow="A3")
def test_no_exporter_disables_its_sending_queue(collector):
    """UC-035 A3: each exporter keeps its own (default-on) queue, so one stuck backend never blocks the fan-out."""
    disabled = [
        name
        for name, cfg in collector["exporters"].items()
        if isinstance(cfg, dict)
        and (cfg.get("sending_queue") or {}).get("enabled") is False
    ]
    assert not disabled, (
        f"exporters with sending_queue disabled block their pipeline: {disabled}"
    )


@pytest.mark.uc("UC-035", flow="A3")
def test_every_pipeline_starts_with_the_memory_limiter(collector):
    """A backend outage that grows queues must hit the memory limiter, not OOM the collector (#1155)."""
    for name, pipeline in collector["service"]["pipelines"].items():
        assert pipeline["processors"][0] == "memory_limiter", name


def _limiter_default(value: str) -> int:
    """The default of an ``${env:VAR:-default}`` setting (the value base, Pi, prod and CI run with)."""
    match = re.fullmatch(r"\$\{env:[A-Z_]+:-(\d+)\}", str(value))
    assert match, f"not an env-substituted default: {value!r}"
    return int(match.group(1))


@pytest.mark.uc("UC-035", flow="A3")
@pytest.mark.nfr("NFR-021")
def test_memory_limiter_backpressures_at_600_mib_under_the_768_mib_container(collector):
    """NFR-021: limit 600 MiB, spike 150 MiB, both below the container's 768 MiB cgroup limit (#1155)."""
    limiter = collector["processors"]["memory_limiter"]
    limit = _limiter_default(limiter["limit_mib"])
    spike = _limiter_default(limiter["spike_limit_mib"])
    assert (limit, spike) == (600, 150)
    container = ic.compose_services()["otel-collector"]["deploy"]["resources"][
        "limits"
    ]["memory"]
    assert container == "768m"
    assert limit < 768, (
        "a limiter above the cgroup limit never engages: the kernel OOM-kills first"
    )


@pytest.mark.uc("UC-035", flow="A3")
def test_no_pipeline_exports_to_loki_and_logs_ends_in_a_sink(collector):
    """UC-035 step 7, A3 (FR-082 Obsolete, #2059): Loki is gone. No exporter or
    receiver for container logs remains; the OTLP logs pipeline stays (so the
    Python log exporter is not answered UNIMPLEMENTED) and ends in the debug sink."""
    pipelines = collector["service"]["pipelines"]
    assert not [name for name in collector["exporters"] if "loki" in name]
    assert not [name for name in collector["receivers"] if name.startswith("filelog")]
    assert "logs/containers" not in pipelines
    for name, pipeline in pipelines.items():
        assert not [e for e in pipeline["exporters"] if "loki" in e], name
        assert not [r for r in pipeline["receivers"] if r.startswith("filelog")], name
    assert pipelines["logs"]["receivers"] == ["otlp"]
    assert pipelines["logs"]["exporters"] == ["debug"]


@pytest.mark.uc("UC-035", flow="A3")
def test_no_compose_service_envoy_cluster_or_datasource_named_loki():
    """UC-035 A3 (FR-082 Obsolete, #2059): nothing in compose, envoy or Grafana names Loki."""
    compose = ic.load_yaml(ic.COMPOSE_FILE)
    assert "loki" not in compose["services"]
    assert "loki-data" not in (compose.get("volumes") or {})
    assert "loki" not in ic.envoy_clusters()
    assert not (ic.REPO_ROOT / "services" / "loki").exists()
    datasources = ic.REPO_ROOT / "services" / "grafana" / "provisioning" / "datasources"
    assert not list(datasources.glob("*loki*"))
    mounts = ic.compose_services()["otel-collector"]["volumes"]
    assert not [m for m in mounts if m.startswith("/var/lib/docker/containers")]


@pytest.mark.uc("UC-035", cond=["Pre-1"])
def test_full_stack_has_every_observability_service_the_lite_stack_lacks():
    """UC-035 Pre-1: the observability UIs and stores exist in the full stack only, never in the lite stack."""
    observability = {"jaeger", "victoria-metrics", "grafana", "otel-collector"}
    full = set(ic.compose_services())
    lite = set(ic.load_yaml(ic.REPO_ROOT / "docker-compose.lite.yml")["services"])
    assert observability <= full, sorted(observability - full)
    assert "envoy" in full
    assert not observability & lite, (
        f"lite must not carry {sorted(observability & lite)}"
    )
