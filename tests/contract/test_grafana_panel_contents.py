"""UC-035 step 6 and step 7: the named Grafana panels and the provisioned datasources (#1725, #2059).

Step 6 names what each folder shows; this pins every named item to a panel and
the series it reads, so removing or emptying one fails here. Step 7 finds a game's errors in
Jaeger, so the provisioned datasources are exactly Prometheus (VictoriaMetrics) and Jaeger.
"""

from __future__ import annotations

import json
import re

import infra_config as ic
import pytest

GRAFANA = ic.REPO_ROOT / "services" / "grafana"
DASHBOARDS = GRAFANA / "dashboards"
DATASOURCES = GRAFANA / "provisioning" / "datasources"


def _flatten(panels: list[dict], out: dict[str, list[str]]) -> dict[str, list[str]]:
    """Panel title -> its target expressions, rows flattened."""
    for p in panels:
        if p.get("type") == "row":
            _flatten(p.get("panels", []), out)
        else:
            out[p.get("title", "")] = [t.get("expr", "") for t in p.get("targets", [])]
    return out


def _panels(folder: str, uid: str) -> dict[str, list[str]]:
    for path in (DASHBOARDS / folder).glob("*.json"):
        dash = json.loads(path.read_text())
        if dash["uid"] == uid:
            return _flatten(dash["panels"], {})
    raise AssertionError(f"no dashboard {uid} in {folder}")


def _assert_panel_reads(
    panels: dict[str, list[str]], title: str, *patterns: str
) -> None:
    assert title in panels, f"panel {title!r} is missing"
    exprs = " ".join(panels[title])
    for pattern in patterns:
        assert re.search(pattern, exprs), (
            f"{title!r} does not read {pattern!r}: {exprs}"
        )


# (folder, dashboard uid, panel title, series the panel must read)
STEP6_PANELS = [
    # Game: per-player acceleration against the warning and death lines.
    (
        "game",
        "game-players",
        "High-Res Acceleration (100Hz)",
        r"controller_accel_magnitude",
        r"game_effective_warning_threshold",
        r"game_effective_death_threshold",
    ),
    # Game: connection quality per controller (BR-005).
    (
        "game",
        "controller-overview",
        "Connection Quality",
        r"controller_connection_quality",
    ),
    (
        "game",
        "controller-overview",
        "p95 Report Gap (10 s)",
        r"controller_bluetooth_report_gap_p95_seconds",
    ),
    (
        "game",
        "controller-overview",
        "Longest Report Gap (10 s)",
        r"controller_bluetooth_report_gap_max_seconds",
    ),
    # Game: each adapter's identity, HCI error counters and link roles (FR-122).
    (
        "game",
        "controller-overview",
        "Adapter Identity",
        r"pairing_bluetooth_adapter_info",
    ),
    (
        "game",
        "controller-overview",
        "HCI Errors (5 min)",
        r"pairing_bluetooth_adapter_hci_errors_total",
    ),
    (
        "game",
        "controller-overview",
        "Link Roles per Adapter",
        r"pairing_bluetooth_link_role_info",
    ),
    # Agent: decisions by backend and outcome.
    (
        "agent",
        "agent-operations",
        "Backend x Outcome (time range)",
        r"traces_span_metrics_calls_total",
        r"decide_backend",
        r"decide_outcome",
        r"agent\.decide",
    ),
    # Agent: remote-model latency and usage.
    (
        "agent",
        "agent-operations",
        "Model Call Latency p95 (by model)",
        r"gen_ai_client_operation_duration_seconds_bucket",
    ),
    (
        "agent",
        "agent-operations",
        "Tokens / min (by model, token type)",
        r"gen_ai_client_token_usage",
    ),
    # Agent: interventions and their measured effect.
    (
        "agent",
        "agent-operations",
        "Interventions Dispatched / min (by action)",
        r"agent_interventions_applied_total",
    ),
    (
        "agent",
        "agent-operations",
        "Effect Delta p50 (by signal)",
        r"agent_intervention_effect_delta_bucket",
    ),
    # Agent: shadow games.
    (
        "agent",
        "agent-operations",
        "Shadow Games Started / min",
        r"games_started_total",
        r"shadow",
    ),
    # Platform: infrastructure and services.
    (
        "platform",
        "joustmania-service-health",
        "CPU per service",
        r"process_cpu_seconds_total",
    ),
    (
        "platform",
        "joustmania-service-health",
        "Host CPU",
        r"system_cpu_time_seconds_total",
    ),
    # Platform: flags and the flag service.
    (
        "platform",
        "feature-flags",
        "Evaluations / s (by flag, top 15)",
        r"feature_flag_evaluation_requests_total",
    ),
    (
        "platform",
        "feature-flags",
        "Writes / min (by flag set, result and op)",
        r"flagservice_writes_total",
    ),
]


@pytest.mark.uc("UC-035", flow="main", steps=[6])
@pytest.mark.parametrize(
    ("folder", "uid", "title", "patterns"),
    [(f, u, t, p) for f, u, t, *p in STEP6_PANELS],
    ids=[row[2] for row in STEP6_PANELS],
)
def test_step6_named_panel_reads_its_series(folder, uid, title, patterns):
    _assert_panel_reads(_panels(folder, uid), title, *patterns)


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_step6_talk_dashboard_is_titled_for_the_talk():
    dash = json.loads((DASHBOARDS / "talk" / "talk.json").read_text())
    assert dash["title"] == "Talk: Two Standards"


@pytest.mark.uc("UC-035", flow="main", steps=[7])
def test_step7_grafana_provisions_only_prometheus_and_jaeger_datasources():
    sources = [
        ds
        for path in sorted(DATASOURCES.glob("*.yml"))
        for ds in (ic.load_yaml(path).get("datasources") or [])
    ]
    assert sorted(ds["type"] for ds in sources) == ["jaeger", "prometheus"], sources
    # The dashboard's Experiments link opens the experiment.progress spans in Jaeger.
    spa = (ic.REPO_ROOT / "services" / "dashboard" / "src" / "index.html").read_text()
    assert "/jaeger/search?service=agent&operation=experiment.progress" in spa


@pytest.mark.uc("UC-035", flow="main", steps=[6], br=["BR-004"])
def test_step6_every_latency_panel_reads_a_histogram():
    """Every quantile panel reads a histogram's ``_bucket`` series, so it uses the declared buckets."""
    quantile_exprs = [
        (path.name, expr)
        for path in sorted(DASHBOARDS.rglob("*.json"))
        for exprs in _flatten(json.loads(path.read_text())["panels"], {}).values()
        for expr in exprs
        if "histogram_quantile" in expr
    ]
    assert quantile_exprs, "no latency panel found"
    for name, expr in quantile_exprs:
        assert re.search(r"_bucket\b", expr), (
            f"{name}: quantile without a histogram: {expr}"
        )
