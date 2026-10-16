"""The OTel collector fan-out is best effort (#1365, UC-035 A3, UC-041 step 2).

Static checks over services/otel-collector/config.yaml: a slow or down backend
(Jaeger, VictoriaMetrics, the agent) must not block delivery to the others.
The service side (SDK exporters fail open when the collector itself is down)
is covered by lib/tests/test_telemetry_fail_open.py.
"""

from __future__ import annotations

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


def test_every_pipeline_starts_with_the_memory_limiter(collector):
    """A backend outage that grows queues must hit the memory limiter, not OOM the collector (#1155)."""
    for name, pipeline in collector["service"]["pipelines"].items():
        assert pipeline["processors"][0] == "memory_limiter", name
