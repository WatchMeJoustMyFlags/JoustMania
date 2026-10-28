"""The agent observe path: collector fan-out to the agent (UC-041 Pre-1, Post-F-1).

Static checks over services/otel-collector/config.yaml and docker-compose.yml.
The best-effort exporter settings (timeout, retries, queue) are covered by
test_collector_best_effort.py.
"""

from __future__ import annotations

import infra_config as ic
import pytest


@pytest.fixture(scope="module")
def collector():
    return ic.load_yaml(ic.COLLECTOR_FILE)


@pytest.mark.uc("UC-041", flow="main", cond=["Pre-1"])
@pytest.mark.parametrize("pipeline", ["traces", "metrics"])
def test_collector_fans_out_traces_and_metrics_to_the_agent_on_4317(
    collector, pipeline
):
    """UC-041 Pre-1: both pipelines export to otlp/agent, which targets agent:4317."""
    assert collector["exporters"]["otlp/agent"]["endpoint"] == "agent:4317"
    pipe = collector["service"]["pipelines"][pipeline]
    assert "otlp/agent" in pipe["exporters"]
    assert "otlp" in pipe["receivers"]


@pytest.mark.uc("UC-041", flow="main", cond=["Pre-1"])
def test_agent_service_listens_on_the_port_the_collector_targets():
    """UC-041 Pre-1: the compose agent service listens on :4317."""
    agent = ic.compose_services()["agent"]
    assert "AGENT_LISTEN_ADDR=:4317" in agent["environment"]
    assert "4317" in [str(p) for p in agent["expose"]]


@pytest.mark.uc("UC-041", flow="main", cond=["Post-F-1"])
@pytest.mark.parametrize(
    ("pipeline", "storage"),
    [("traces", "otlp/jaeger"), ("metrics", "prometheus_remote_write")],
)
def test_storage_exporters_are_independent_of_the_agent_exporter(
    collector, pipeline, storage
):
    """UC-041 Post-F-1 (static proxy): the storage exporters do not depend on the agent.

    A static config check, not a runtime test with the agent down. The storage
    exporter is its own exporter in the same fan-out pipeline, targets its own
    backend, and is the only one of the two that keeps the collector's default
    retries; the agent exporter is the only one that is best-effort (short
    timeout, no retries, bounded queue), so a dead agent costs at most its own
    timeout and drops (see also test_collector_best_effort.py).
    """
    exporters = collector["exporters"]
    pipe_exporters = collector["service"]["pipelines"][pipeline]["exporters"]
    assert storage in pipe_exporters and "otlp/agent" in pipe_exporters
    assert storage != "otlp/agent"

    agent = exporters["otlp/agent"]
    store = exporters[storage]
    assert "agent" not in store["endpoint"]
    assert agent["timeout"] == "2s"
    assert agent["retry_on_failure"] == {"enabled": False}
    assert agent["sending_queue"]["enabled"] is True
    # The best-effort settings belong to the agent exporter only: the storage
    # exporter carries none of them, so it keeps the collector defaults.
    for key in ("timeout", "retry_on_failure", "sending_queue"):
        assert key not in store, f"{storage} must not copy the agent's {key}"
