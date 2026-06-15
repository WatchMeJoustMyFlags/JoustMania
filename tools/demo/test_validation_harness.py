#!/usr/bin/env python3
"""Unit tests for the #1070 validation harness — matrix + report logic only.

The LIVE collection path (docker logs / Prometheus / Jaeger) is NOT exercised
here; those are stubbed with hand-built Observations. This proves the
matrix is well-formed and the PASS/ANOMALY judgement matches the real log /
metric / span shapes the harness collects against.

Run from the game-coordinator uv env (it vendors grpcio + protobuf, which the
imported demo_driver needs)::

    cd services/game_coordinator
    uv run pytest ../../tools/demo/test_validation_harness.py -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tools.demo import validation_harness as vh  # noqa: E402


# --------------------------------------------------------------------------- #
# Matrix sanity
# --------------------------------------------------------------------------- #
def test_matrix_is_non_empty_and_unique():
    assert vh.SCENARIOS, "scenario matrix is empty"
    # data-driven: every scenario keyed by its own name
    for name, sc in vh.SCENARIOS.items():
        assert sc.name == name


def test_matrix_covers_rules_levels_and_edges():
    names = set(vh.SCENARIOS)
    # each decision rule the driver can trip is represented
    assert {"skill_gap", "statue", "death_cascade", "idle_dominator", "low_variance"} <= names
    # interventions allowed vs blocked at multiple permission levels
    assert {"blocked_ambient", "blocked_none", "allowed_full"} <= names
    # edge cases
    assert {"edge_underpopulated", "edge_rapid_cascade"} <= names


def test_every_scenario_has_a_runner_and_expectation():
    for sc in vh.SCENARIOS.values():
        assert callable(sc.runner)
        assert sc.expect.rule
        # allowed scenarios that expect a decision must name interventions
        if sc.expect.allowed and sc.expect.expect_decision:
            assert sc.expect.interventions
        # blocked scenarios carry a reason to check
        if not sc.expect.allowed:
            assert sc.expect.block_reason


def test_metric_query_helper_shapes():
    applied = vh._intervention_metric("send_controller_effect", blocked=False)
    blocked = vh._intervention_metric("send_controller_effect", blocked=True)
    assert 'type="send_controller_effect"' in applied
    assert 'blocked="false"' in applied
    assert 'blocked="true"' in blocked


# --------------------------------------------------------------------------- #
# Report logic — allowed scenario
# --------------------------------------------------------------------------- #
def _sc(name):
    return vh.SCENARIOS[name]


def test_allowed_pass_when_applied_counter_moved():
    sc = _sc("statue")  # ambient, send_controller_effect ALLOWED
    obs = vh.Observations(
        agent_log="ts=… msg=agent.evaluate mode=rules model=phi4-mini\n",
        metrics={
            "sum(agent_evaluations_total)": 12.0,
            vh._intervention_metric("send_controller_effect", False): 3.0,
            vh._intervention_metric("send_controller_effect", True): 0.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.PASS, res.reasons


def test_allowed_anomaly_when_rule_never_fired():
    sc = _sc("statue")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 8.0,
            vh._intervention_metric("send_controller_effect", False): 0.0,
            vh._intervention_metric("send_controller_effect", True): 0.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("never" in r or "not have fired" in r for r in res.reasons)


def test_allowed_anomaly_when_unexpectedly_blocked():
    sc = _sc("statue")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n"
        "msg=agent.decision_blocked intervention=send_controller_effect reason=not_allowed\n",
        metrics={
            "sum(agent_evaluations_total)": 8.0,
            vh._intervention_metric("send_controller_effect", False): 0.0,
            vh._intervention_metric("send_controller_effect", True): 2.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("BLOCKED" in r for r in res.reasons)


# --------------------------------------------------------------------------- #
# Report logic — blocked scenario
# --------------------------------------------------------------------------- #
def test_blocked_pass_when_block_observed_in_logs():
    sc = _sc("blocked_ambient")  # adjust_player_sensitivity BLOCKED, not_allowed
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n"
        "msg=agent.decision_blocked intervention=adjust_player_sensitivity reason=not_allowed\n",
        metrics={
            "sum(agent_evaluations_total)": 10.0,
            vh._intervention_metric("adjust_player_sensitivity", True): 4.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.PASS, res.reasons


def test_blocked_anomaly_when_no_block_seen():
    sc = _sc("blocked_ambient")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 10.0,
            vh._intervention_metric("adjust_player_sensitivity", True): 0.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("BLOCKED" in r for r in res.reasons)


def test_blocked_pass_via_metric_only_without_log():
    sc = _sc("blocked_none")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 5.0,
            vh._intervention_metric("send_controller_effect", True): 1.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.PASS, res.reasons


# --------------------------------------------------------------------------- #
# Report logic — mode, heartbeat, edge, span chain, collection errors
# --------------------------------------------------------------------------- #
def test_anomaly_when_no_evaluate_line():
    sc = _sc("statue")
    obs = vh.Observations(agent_log="", metrics={"sum(agent_evaluations_total)": 1.0})
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("did not run" in r for r in res.reasons)


def test_anomaly_when_zero_evaluations_metric():
    sc = _sc("statue")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 0.0,
            vh._intervention_metric("send_controller_effect", False): 0.0,
            vh._intervention_metric("send_controller_effect", True): 0.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("evaluated nothing" in r for r in res.reasons)


def test_edge_underpopulated_passes_when_quiet():
    sc = _sc("edge_underpopulated")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={"sum(agent_evaluations_total)": 4.0},
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.PASS, res.reasons


def test_edge_underpopulated_anomaly_when_rule_fires():
    sc = _sc("edge_underpopulated")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 4.0,
            vh._intervention_metric("adjust_player_sensitivity", False): 2.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("spuriously" in r for r in res.reasons)


def test_collection_error_yields_error_verdict():
    sc = _sc("statue")
    obs = vh.Observations(agent_log="", metrics={}, errors=["docker logs failed: boom"])
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ERROR


def test_broken_span_chain_flagged():
    sc = _sc("statue")
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 6.0,
            vh._intervention_metric("send_controller_effect", False): 2.0,
            vh._intervention_metric("send_controller_effect", True): 0.0,
        },
        span_chain=["agent.decision", "agent.action"],  # missing the signal_received root
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("signal_received" in r for r in res.reasons)


def test_mode_mismatch_flagged():
    sc = _sc("statue")
    # craft an expectation of llm; the matrix default is rules, so build a copy
    import dataclasses

    sc = dataclasses.replace(sc, expect=dataclasses.replace(sc.expect, mode="llm"))
    obs = vh.Observations(
        agent_log="msg=agent.evaluate mode=rules\n",
        metrics={
            "sum(agent_evaluations_total)": 6.0,
            vh._intervention_metric("send_controller_effect", False): 2.0,
            vh._intervention_metric("send_controller_effect", True): 0.0,
        },
    )
    res = vh.evaluate(sc, obs)
    assert res.verdict == vh.ANOMALY
    assert any("mode=llm" in r for r in res.reasons)


# --------------------------------------------------------------------------- #
# CLI selection
# --------------------------------------------------------------------------- #
def test_select_all():
    args = vh.build_parser().parse_args(["--all"])
    assert len(vh.select_scenarios(args)) == len(vh.SCENARIOS)


def test_select_subset_csv():
    args = vh.build_parser().parse_args(["--scenario", "statue,skill_gap"])
    chosen = vh.select_scenarios(args)
    assert [s.name for s in chosen] == ["statue", "skill_gap"]


def test_select_unknown_exits():
    args = vh.build_parser().parse_args(["--scenario", "does_not_exist"])
    with pytest.raises(SystemExit):
        vh.select_scenarios(args)


def test_set_flag_default_variant_roundtrip(tmp_path, monkeypatch):
    import json

    flag_doc = {"flags": {"interventions_allowed": {"defaultVariant": "ambient"}}}
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(flag_doc))
    monkeypatch.setenv("FLAGD_FLAG_DIR", str(tmp_path))

    snap = vh.snapshot_flags({"agent"})
    vh.set_flag_default_variant("agent", "interventions_allowed", "standard")
    reloaded = json.loads(path.read_text())
    assert reloaded["flags"]["interventions_allowed"]["defaultVariant"] == "standard"

    vh.restore_flags(snap)
    restored = json.loads(path.read_text())
    assert restored["flags"]["interventions_allowed"]["defaultVariant"] == "ambient"
