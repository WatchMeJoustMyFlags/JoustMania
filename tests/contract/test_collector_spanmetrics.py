"""Decide backend x outcome metrics come from the collector's spanmetrics connector (#1427).

Maintainer decision 2 on #1427: the agent has no decide counter; the OTel
collector derives `traces_span_metrics_*` from the agent's `agent.decide`
spans. These static checks keep the pieces in step: the custom collector build
includes the connector, the config feeds it only agent.decide spans, its
dimensions are the span attributes the agent sets, and its metrics reach
VictoriaMetrics, where the Agent Decisions & Interventions dashboard reads them.
"""

from __future__ import annotations

import re

import infra_config as ic
import pytest

BUILDER_FILE = ic.REPO_ROOT / "images" / "otel-collector" / "builder-config.yaml"
DECIDE_HOOK = ic.REPO_ROOT / "services" / "agent" / "decision" / "decide_hook.go"


@pytest.fixture(scope="module")
def collector():
    return ic.load_yaml(ic.COLLECTOR_FILE)


def _go_const(name: str) -> str:
    match = re.search(rf'\b{name}\s*=\s*"([^"]+)"', DECIDE_HOOK.read_text())
    assert match, f"{name} not found in {DECIDE_HOOK}"
    return match.group(1)


def test_collector_build_includes_the_spanmetrics_connector():
    gomods = [c["gomod"] for c in ic.load_yaml(BUILDER_FILE).get("connectors", [])]
    assert any("/connector/spanmetricsconnector " in g for g in gomods), gomods


@pytest.mark.uc("UC-035", flow="main", steps=[3])
def test_spanmetrics_dimensions_are_the_decide_span_attributes(collector):
    """UC-035 step 3: the connector groups by the attributes the agent sets on agent.decide."""
    connector = collector["connectors"]["span_metrics"]
    dims = [d["name"] for d in connector["dimensions"]]
    assert dims == [_go_const("AttrDecideBackend"), _go_const("AttrDecideOutcome")]
    # Explicit buckets, so the latency p95 panel never relies on default buckets.
    assert connector["histogram"]["unit"] == "s"
    assert connector["histogram"]["explicit"]["buckets"]
    # A per-restart collector.instance.id label would start new series on every restart.
    assert "collector.instance.id" in connector["exclude_dimensions"]
    # The agent's instance label changes on every recreate: dead series must expire.
    assert connector["metrics_expiration"]


@pytest.mark.uc("UC-035", flow="main", steps=[3], br=["BR-004"])
def test_spanmetrics_decide_histogram_buckets_span_5ms_to_32s(collector):
    """UC-035 BR-004: the agent.decide duration histogram uses explicit 5 ms to 32 s buckets."""
    buckets = collector["connectors"]["span_metrics"]["histogram"]["explicit"][
        "buckets"
    ]
    assert buckets[0] == "5ms"
    assert buckets[-1] == "32s"
    assert buckets == [
        "5ms",
        "10ms",
        "25ms",
        "50ms",
        "100ms",
        "250ms",
        "500ms",
        "1s",
        "2s",
        "4s",
        "8s",
        "16s",
        "32s",
    ]


def test_spanmetrics_only_sees_agent_decide_spans(collector):
    """Bounded cardinality: a dedicated traces pipeline filters to agent.decide of service agent."""
    pipelines = collector["service"]["pipelines"]
    feeding = [
        name
        for name, p in pipelines.items()
        if "span_metrics" in p.get("exporters", [])
    ]
    assert feeding == ["traces/span_metrics"]
    processors = pipelines["traces/span_metrics"]["processors"]
    assert "filter/agent_decide" in processors
    (condition,) = collector["processors"]["filter/agent_decide"]["trace_conditions"]
    assert f'span.name != "{_go_const("SpanDecide")}"' in condition
    assert 'resource.attributes["service.name"] != "agent"' in condition
    # The filter must not sit in the main traces pipeline (it would drop spans from Jaeger).
    assert "filter/agent_decide" not in pipelines["traces"]["processors"]


def test_spanmetrics_reach_victoriametrics(collector):
    pipelines = collector["service"]["pipelines"]
    consuming = [
        name
        for name, p in pipelines.items()
        if "span_metrics" in p.get("receivers", [])
    ]
    assert consuming == ["metrics/span_metrics"]
    assert "prometheus_remote_write" in pipelines["metrics/span_metrics"]["exporters"]
