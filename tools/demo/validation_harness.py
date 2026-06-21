#!/usr/bin/env python3
"""
Real-stack validation harness — exercise the live stack broadly, observe, find gaps (#1070).

This is the maintainer's "run the REAL stack, check as many things as possible"
capability: exploratory, observability-driven gap-finding. It COMPLEMENTS the unit
suite and the integration tests (#1069) — those catch *known* regressions; this
surfaces the *unknown* gaps by driving the live system across a broad scenario
matrix and reporting what the agent ACTUALLY did versus what was expected.

It builds directly on the #794 demo driver (``tools/demo/demo_driver.py``): the
same movement-zone primitives (STILL/ACTIVE/WARNING/DANGER), the same
real-game-via-menu start path, and the same scenario *patterns* that trip a named
agent rule. On top of that it adds three things the bare driver does not have:

  1. A data-driven SCENARIO MATRIX — each scenario carries its setup (players,
     pattern, flags to flip, interventions_allowed level) and its EXPECTED agent
     behavior (which rule fires, which intervention, allowed-vs-blocked, mode).
  2. OBSERVATION COLLECTION from the live stack — after driving each scenario it
     queries ``docker logs joustmania-agent`` for the decision/intervention/mode
     lines and Prometheus (via envoy ``/prometheus/``) for the relevant counters,
     optionally Jaeger for the decision span chain.
  3. A PASS/ANOMALY REPORT — per-scenario verdict (did the expected behavior
     happen?) plus a summary that FLAGS gaps (expected-but-didn't-fire, a missing
     or zero metric, a broken span chain, mode=rules when llm was expected, …),
     and prints WHERE to look for each anomaly. Exits non-zero on a hard anomaly
     when ``--fail-on-anomaly`` is given.

It is idempotent: the flags a scenario flips are snapshotted and restored after
the run (the integration-test pattern), so a run does not pollute stack state.

Design note (#1070): the LIVE collection path is designed against the REAL log /
metric / span shapes (see ``services/agent/decision/telemetry.go`` span names,
``metrics.py`` counters, the slog ``agent.evaluate`` / ``agent.decision_blocked``
lines). It is NOT faked. The full live run (drive the real stack, produce a real
gap report) is done during a stack smoke-test; the matrix + report logic are
unit-testable with the collection path mocked (see tests/test_validation_harness.py).

Run it from the game-coordinator uv env (it vendors grpcio + protobuf), exactly
like the #794 driver::

    make dry-run
    ./scripts/agent-killswitch.sh on
    cd services/game_coordinator
    uv run python ../../tools/demo/validation_harness.py --all
    uv run python ../../tools/demo/validation_harness.py --list
    uv run python ../../tools/demo/validation_harness.py --scenario statue
    uv run python ../../tools/demo/validation_harness.py --scenario statue,skill_gap --fail-on-anomaly
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# Reuse the #794 driver's primitives wholesale — same proto stubs, same movement
# vectors, same real-game-via-menu start path, same scenario patterns.
from tools.demo import demo_driver as dd  # noqa: E402

# ---------------------------------------------------------------------------
# Observation collection — against the LIVE stack.
#
# Two sources, both already exposed by `make dry-run`:
#   * agent logs:  `docker logs joustmania-agent` (slog text handler). The agent
#                  emits `agent.evaluate mode=…`, `agent.decision_blocked
#                  intervention=… reason=…`, experiment lifecycle lines, etc.
#   * Prometheus:  the envoy ingress at http://localhost:8080/prometheus/ proxies
#                  to Prometheus (which scrapes VM). The instant-query API is
#                  `/prometheus/api/v1/query?query=<promql>`.
# Jaeger (span chain) is an OPTIONAL third source via /jaeger/api/traces.
# ---------------------------------------------------------------------------
AGENT_CONTAINER = "joustmania-agent"


def log(msg: str) -> None:
    print(f"[validation] {msg}", flush=True)


@dataclass
class Observations:
    """What the live stack reported for one scenario window."""

    agent_log: str = ""              # raw `docker logs` tail captured for the window
    metrics: dict[str, float] = field(default_factory=dict)  # promql -> scalar value
    span_chain: list[str] = field(default_factory=list)      # ordered span names seen
    errors: list[str] = field(default_factory=list)          # collection failures (not anomalies)


class LiveCollector:
    """Collects observations from a running stack. All methods degrade gracefully:
    a collection failure becomes a recorded error, never a crash — so a missing
    Jaeger or a renamed container surfaces as an anomaly hint, not a traceback."""

    def __init__(self, host: str, prom_base: str, jaeger_base: str, container: str):
        self.host = host
        self.prom_base = prom_base.rstrip("/")
        self.jaeger_base = jaeger_base.rstrip("/")
        self.container = container

    # -- agent logs -------------------------------------------------------
    def agent_logs_since(self, since_unix: float) -> tuple[str, str | None]:
        """Return (logs, error). `since_unix` lower-bounds the window so each
        scenario only sees its own lines (docker --since accepts a timestamp)."""
        if shutil.which("docker") is None:
            return "", "docker CLI not found on PATH"
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_unix))
        try:
            proc = subprocess.run(
                ["docker", "logs", "--since", since, self.container],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return "", f"docker logs failed: {exc}"
        if proc.returncode != 0:
            return "", f"docker logs rc={proc.returncode}: {proc.stderr.strip()[:200]}"
        return proc.stdout + proc.stderr, None

    # -- Prometheus -------------------------------------------------------
    def metric(self, promql: str) -> tuple[float | None, str | None]:
        """Instant-query Prometheus; return (scalar, error). Sums the result
        vector so label-split series collapse to one number (what the report
        cares about: did the counter move at all)."""
        url = f"{self.prom_base}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, f"prometheus query failed: {exc}"
        if payload.get("status") != "success":
            return None, f"prometheus status={payload.get('status')}"
        result = payload.get("data", {}).get("result", [])
        if not result:
            return 0.0, None  # query valid, series absent -> 0 (a real, reportable signal)
        total = 0.0
        for series in result:
            try:
                total += float(series["value"][1])
            except (KeyError, IndexError, ValueError, TypeError):
                continue
        return total, None

    # -- Jaeger (optional span chain) ------------------------------------
    def span_names(self, service: str, since_unix: float, lookback_s: int) -> tuple[list[str], str | None]:
        """Return the distinct span operation names seen for `service` in the
        window. Used to confirm the decision audit chain
        (agent.signal_received -> agent.decision -> agent.action) is intact."""
        end_us = int(time.time() * 1_000_000)
        start_us = int((since_unix - 1) * 1_000_000)
        params = urllib.parse.urlencode(
            {"service": service, "start": start_us, "end": end_us, "limit": 50}
        )
        url = f"{self.jaeger_base}/api/traces?{params}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return [], f"jaeger query failed: {exc}"
        names: list[str] = []
        for trace in payload.get("data", []):
            for span in trace.get("spans", []):
                name = span.get("operationName")
                if name and name not in names:
                    names.append(name)
        return names, None


# ---------------------------------------------------------------------------
# Flag snapshot / restore — keep a run idempotent (integration-test pattern).
#
# A scenario may need a non-default `interventions_allowed` (e.g. `standard` to
# let a difficulty intervention DISPATCH). We edit the host flag file flagd is
# mounted on (FLAGD_FLAG_DIR/<domain>.json), snapshot it first, and restore the
# exact bytes after — so the stack is left as we found it.
# ---------------------------------------------------------------------------
def _flag_dir() -> str:
    return os.environ.get(
        "FLAGD_FLAG_DIR",
        os.path.join(os.path.dirname(__file__), "../..", "services", "flagd"),
    )


def flag_file(domain: str) -> str:
    return os.path.join(_flag_dir(), f"{domain}.json")


def snapshot_flags(domains: set[str]) -> dict[str, str]:
    """Return {path: raw_bytes_text} for each domain file that exists."""
    snap: dict[str, str] = {}
    for domain in domains:
        path = flag_file(domain)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                snap[path] = fh.read()
    return snap


def restore_flags(snapshot: dict[str, str]) -> None:
    for path, text in snapshot.items():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    if snapshot:
        log(f"restored {len(snapshot)} flag file(s) to their pre-run state")


def set_flag_default_variant(domain: str, flag_key: str, variant: str) -> None:
    """Set a flag's defaultVariant in the host flagd file (hot-reloaded). A no-op
    if the file or flag is absent — the scenario's expectation will then surface
    the gap rather than the harness crashing."""
    path = flag_file(domain)
    if not os.path.exists(path):
        log(f"WARN: flag file {path} missing; cannot set {flag_key}={variant}")
        return
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    flag = doc.get("flags", {}).get(flag_key)
    if flag is None:
        log(f"WARN: flag {flag_key} absent in {domain}.json; cannot set {variant}")
        return
    flag["defaultVariant"] = variant
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    log(f"set {domain}.{flag_key} defaultVariant={variant}")


# ---------------------------------------------------------------------------
# Scenario matrix — data-driven + extensible.
#
# Each Scenario reuses a #794 driver pattern (the `runner`) and adds the live
# EXPECTATION the report checks: the rule it should trip, the intervention(s) the
# agent should DECIDE, whether they should be ALLOWED or BLOCKED at the configured
# permission level, and the metric/log/span signatures to look for.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Expectation:
    rule: str                          # human label, e.g. "R3 skill gap"
    interventions: tuple[str, ...]     # interventions the agent should DECIDE
    allowed: bool                      # at the scenario's permission level, allowed or blocked?
    mode: str = "rules"                # expected inference mode (rules unless llm wired)
    expect_decision: bool = True       # some edge cases expect the agent to do NOTHING
    block_reason: str = ""             # when blocked, the expected reason (not_allowed, low_battery, …)


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    players: int
    duration: float
    # flags to flip for this scenario, as (domain, flag_key, variant). Snapshotted+restored.
    flags: tuple[tuple[str, str, str], ...]
    expect: Expectation
    runner: Callable[[dd.Stack, list[str], float], None]
    game_mode: str = "JoustFFA"


# `interventions_allowed` variants from services/flagd/agent.json (see file).
AMBIENT = ("agent", "interventions_allowed", "ambient")
STANDARD = ("agent", "interventions_allowed", "standard")
FULL = ("agent", "interventions_allowed", "full")
NONE = ("agent", "interventions_allowed", "none")
# Shorten the accelerate target so R5/R6 fire within a demo-length window.
FAST_ACCEL = ("agent", "fitness.accelerate.target_session_seconds", "fast")


def _scenarios() -> dict[str, Scenario]:
    s: list[Scenario] = [
        # -- each decision rule the driver can trip -----------------------
        Scenario(
            name="skill_gap",
            summary="High/low skill cohorts -> skill gap > 0.4 (R3). Under standard, the "
            "adjust_player_sensitivity difficulty intervention is ALLOWED -> should dispatch.",
            players=4, duration=40.0, flags=(STANDARD,),
            runner=dd.scenario_skill_gap,
            expect=Expectation(rule="R3 skill gap", interventions=("adjust_player_sensitivity",),
                               allowed=True),
        ),
        Scenario(
            name="statue",
            summary="One controller held perfectly still -> variance->0 (R8). send_controller_effect "
            "is in the stock ambient allow-list -> should dispatch.",
            players=4, duration=40.0, flags=(AMBIENT,),
            runner=dd.scenario_statue,
            expect=Expectation(rule="R8 statue", interventions=("send_controller_effect",),
                               allowed=True),
        ),
        Scenario(
            name="death_cascade",
            summary="Eliminate players fast while the session is young (R1/R2 endurance). "
            "adjust_music_tempo/play_audio_cue ALLOWED under standard.",
            players=5, duration=45.0, flags=(STANDARD,),
            runner=dd.scenario_death_cascade,
            expect=Expectation(rule="R1/R2 endurance",
                               interventions=("adjust_music_tempo", "play_audio_cue"),
                               allowed=True),
        ),
        Scenario(
            name="idle_dominator",
            summary="One player pegged high, the rest idle, past the accelerate target (R5/R6). "
            "fitness.accelerate.target shortened to 'fast' so it fires in-window.",
            players=4, duration=70.0, flags=(STANDARD, FAST_ACCEL),
            runner=dd.scenario_idle_dominator,
            expect=Expectation(rule="R5/R6 accelerate",
                               interventions=("adjust_music_tempo", "eliminate_player"),
                               allowed=True),
        ),
        Scenario(
            name="low_variance",
            summary="Whole field near-still -> low activity (R1/R2/R8 pressure). Ambient-friendly: "
            "calming cues / statue nudges dispatch.",
            players=4, duration=40.0, flags=(AMBIENT,),
            runner=dd.scenario_low_variance,
            expect=Expectation(rule="R1/R2/R8 low activity",
                               interventions=("play_audio_cue", "send_controller_effect"),
                               allowed=True),
        ),
        # -- interventions ALLOWED vs BLOCKED, each level -----------------
        Scenario(
            name="blocked_ambient",
            summary="Skill-gap pattern under the stock ambient allow-list -> the difficulty "
            "intervention adjust_player_sensitivity is BLOCKED (block_reason=not_allowed). "
            "The permission chain is visible end to end.",
            players=4, duration=40.0, flags=(AMBIENT,),
            runner=dd.scenario_skill_gap,
            expect=Expectation(rule="R3 blocked@ambient",
                               interventions=("adjust_player_sensitivity",),
                               allowed=False, block_reason="not_allowed"),
        ),
        Scenario(
            name="blocked_none",
            summary="Statue pattern under interventions_allowed=none -> even the ambient "
            "send_controller_effect is BLOCKED. Confirms the strictest level blocks everything.",
            players=4, duration=40.0, flags=(NONE,),
            runner=dd.scenario_statue,
            expect=Expectation(rule="R8 blocked@none",
                               interventions=("send_controller_effect",),
                               allowed=False, block_reason="not_allowed"),
        ),
        Scenario(
            name="allowed_full",
            summary="Idle-dominator past 1.5x target under interventions_allowed=full -> the "
            "terminal-ish eliminate_player is ALLOWED (full is the only level that permits it).",
            players=4, duration=70.0, flags=(FULL, FAST_ACCEL),
            runner=dd.scenario_idle_dominator,
            expect=Expectation(rule="R6 allowed@full",
                               interventions=("eliminate_player",), allowed=True),
        ),
        # -- edge cases ---------------------------------------------------
        Scenario(
            name="edge_underpopulated",
            summary="A 1-player game: most rules need >=2 active players (skill gap, R6). The agent "
            "should DECIDE little/nothing -> confirms it does not fire spuriously on a thin field.",
            players=1, duration=30.0, flags=(STANDARD,),
            runner=dd.scenario_low_variance,
            expect=Expectation(rule="under-populated", interventions=(), allowed=True,
                               expect_decision=False),
        ),
        Scenario(
            name="edge_rapid_cascade",
            summary="A fast, dense death cascade (5 players, tight cadence) early in the session "
            "-> endurance urgency should spike; confirms R1/R2 fire under rapid elimination.",
            players=5, duration=30.0, flags=(STANDARD,),
            runner=dd.scenario_death_cascade,
            expect=Expectation(rule="R1/R2 rapid cascade",
                               interventions=("adjust_music_tempo", "play_audio_cue"),
                               allowed=True),
        ),
    ]
    return {sc.name: sc for sc in s}


SCENARIOS = _scenarios()


# ---------------------------------------------------------------------------
# Report — per-scenario PASS/ANOMALY + summary + where-to-look.
# ---------------------------------------------------------------------------
PASS = "PASS"
ANOMALY = "ANOMALY"
ERROR = "ERROR"  # collection itself failed; can't judge

# slog line signatures emitted by the agent (services/agent/decision/decision.go).
_RE_EVALUATE = re.compile(r"agent\.evaluate.*?mode=(\w+)")
_RE_BLOCKED = re.compile(r"agent\.decision_blocked.*?intervention=(\S+).*?reason=(\S+)", re.DOTALL)


@dataclass
class ScenarioResult:
    name: str
    verdict: str
    reasons: list[str] = field(default_factory=list)   # why ANOMALY/ERROR
    where: list[str] = field(default_factory=list)      # where to look
    obs: Observations | None = None


def _intervention_metric(intervention: str, blocked: bool) -> str:
    blocked_label = "true" if blocked else "false"
    return (
        f'sum(game_interventions_total{{type="{intervention}",blocked="{blocked_label}"}})'
    )


def evaluate(scenario: Scenario, obs: Observations) -> ScenarioResult:
    """Compare expected agent behavior against the collected observations. Pure —
    no I/O — so it is unit-testable with hand-built Observations."""
    res = ScenarioResult(name=scenario.name, verdict=PASS, obs=obs)
    exp = scenario.expect

    # If collection itself failed wholesale (no logs AND metric errors), we can't judge.
    if obs.errors and not obs.agent_log and not obs.metrics:
        res.verdict = ERROR
        res.reasons.extend(obs.errors)
        res.where.append(f"docker logs {AGENT_CONTAINER}; check the stack is up (make dry-run)")
        return res

    # 1. Mode check: the decision loop logged its mode at least once.
    modes = set(_RE_EVALUATE.findall(obs.agent_log))
    if not modes:
        res.verdict = ANOMALY
        res.reasons.append(
            "no `agent.evaluate` line found — the decision loop did not run "
            "(killswitch off? agent down? no telemetry reaching it?)"
        )
        res.where.append(f"docker logs {AGENT_CONTAINER} | grep agent.evaluate; ./scripts/agent-killswitch.sh on")
    elif exp.mode not in modes:
        res.verdict = ANOMALY
        res.reasons.append(
            f"expected mode={exp.mode} but agent ran mode(s)={sorted(modes)} "
            f"(LLM backend unreachable falls back to rules — expected unless wired)"
        )
        res.where.append("agent.json `mode` flag; AGENT_INFERENCE_BACKEND on the agent container")

    # 2. Did the decision loop produce evaluations at all (metric heartbeat)?
    evals = obs.metrics.get("sum(agent_evaluations_total)")
    if evals is not None and evals == 0:
        res.verdict = ANOMALY
        res.reasons.append("agent_evaluations_total is 0 — the agent evaluated nothing this window")
        res.where.append("Prometheus: agent_evaluations_total ; Jaeger service=agent")

    # 3. Edge case: the agent should do (almost) nothing.
    if not exp.expect_decision:
        fired = []
        for intervention in ("adjust_player_sensitivity", "eliminate_player", "grant_shield"):
            applied = obs.metrics.get(_intervention_metric(intervention, blocked=False), 0)
            if applied and applied > 0:
                fired.append(intervention)
        if fired:
            res.verdict = ANOMALY
            res.reasons.append(
                f"under-populated game still fired {fired} — a rule needing >=2 players "
                f"fired spuriously"
            )
            res.where.append("Jaeger service=agent: inspect agent.decision spans for this game_id")
        return res

    # 4. Allowed-vs-blocked: check the right counter moved for each expected intervention.
    blocked_hits = {(i, r) for i, r in _RE_BLOCKED.findall(obs.agent_log)}
    for intervention in exp.interventions:
        applied = obs.metrics.get(_intervention_metric(intervention, blocked=False))
        blocked = obs.metrics.get(_intervention_metric(intervention, blocked=True))
        log_blocked = any(i == intervention for i, _ in blocked_hits)

        if exp.allowed:
            # Expect the APPLIED counter to move (blocked counter should not be the only signal).
            if applied is not None and applied == 0 and (blocked or 0) == 0 and not log_blocked:
                res.verdict = ANOMALY
                res.reasons.append(
                    f"expected ALLOWED `{intervention}` ({exp.rule}) but neither applied nor "
                    f"blocked counter moved — the rule may not have fired at all"
                )
                res.where.append(
                    f"Prometheus: {_intervention_metric(intervention, False)} ; "
                    f"Jaeger service=agent: agent.decision -> agent.action for this game"
                )
            elif applied is not None and applied == 0 and (blocked or log_blocked):
                res.verdict = ANOMALY
                res.reasons.append(
                    f"expected `{intervention}` to be ALLOWED but it was BLOCKED — the permission "
                    f"level may be narrower than the scenario configured ({scenario.flags})"
                )
                res.where.append("agent.json interventions_allowed; docker logs | grep agent.decision_blocked")
        else:
            # Expect it BLOCKED with the right reason.
            reason_ok = any(
                i == intervention and (not exp.block_reason or r.startswith(exp.block_reason))
                for i, r in blocked_hits
            )
            if not reason_ok and (blocked or 0) == 0:
                res.verdict = ANOMALY
                res.reasons.append(
                    f"expected `{intervention}` BLOCKED (reason={exp.block_reason or 'any'}) but no "
                    f"block was observed in logs or metrics — the rule may not have fired, or the "
                    f"permission chain did not record the block"
                )
                res.where.append(
                    f"Prometheus: {_intervention_metric(intervention, True)} ; "
                    f"docker logs {AGENT_CONTAINER} | grep agent.decision_blocked"
                )

    # 5. Span chain: if Jaeger was queried, the audit chain should be intact.
    if obs.span_chain:
        if "agent.signal_received" not in obs.span_chain:
            res.verdict = ANOMALY if res.verdict == PASS else res.verdict
            res.reasons.append(
                "Jaeger has agent spans but no `agent.signal_received` — the decision audit "
                "chain root is missing (broken span chain)"
            )
            res.where.append("Jaeger service=agent: expect agent.signal_received -> agent.decision -> agent.action")
        elif exp.allowed and "agent.action" not in obs.span_chain and "agent.decision" in obs.span_chain:
            # decided but never acted, while we expected an allowed dispatch
            res.reasons.append(
                "NOTE: agent.decision spans present but no agent.action — decisions were made but "
                "not dispatched (act gates closed: agent.json interventions_enabled / enabled flags)"
            )
            res.where.append("docs/agent-act-runbook.md: open the act gates to see agent.action")

    return res


# ---------------------------------------------------------------------------
# Drive one scenario against the live stack, then collect + evaluate.
# ---------------------------------------------------------------------------
def run_scenario(stack: dd.Stack, scenario: Scenario, collector: LiveCollector,
                 args: argparse.Namespace) -> ScenarioResult:
    log("=" * 72)
    log(f"SCENARIO {scenario.name}: {scenario.summary}")
    log(f"  players={scenario.players} duration={scenario.duration}s "
        f"expect={scenario.expect.rule} allowed={scenario.expect.allowed}")

    domains = {domain for domain, _, _ in scenario.flags}
    snapshot = snapshot_flags(domains) if not args.no_flag_changes else {}
    started = time.time()
    serials: list[str] = []
    try:
        if not args.no_flag_changes:
            for domain, key, variant in scenario.flags:
                set_flag_default_variant(domain, key, variant)
            # give flagd's file-watch a moment to reload before we start driving
            time.sleep(args.flag_settle)

        serials = dd.add_controllers(stack, scenario.players)
        game_id = dd.start_real_game(stack, serials, scenario.game_mode, args.start_timeout)
        log(f"  game_id={game_id}; driving the pattern…")
        scenario.runner(stack, serials, scenario.duration)
        log("  pattern complete; letting the agent settle before collecting")
        time.sleep(args.collect_settle)

        obs = Observations()
        logs, log_err = collector.agent_logs_since(started)
        obs.agent_log = logs
        if log_err:
            obs.errors.append(log_err)
        for promql in _metric_queries(scenario):
            val, err = collector.metric(promql)
            if err:
                obs.errors.append(err)
            elif val is not None:
                obs.metrics[promql] = val
        if args.jaeger:
            spans, span_err = collector.span_names("agent", started, int(scenario.duration) + 60)
            obs.span_chain = spans
            if span_err:
                obs.errors.append(span_err)

        result = evaluate(scenario, obs)
    finally:
        try:
            stack.coord.ForceEndGame(
                dd.gcpb.ForceEndGameRequest(reason="validation_harness_done")
            )
        except dd.grpc.RpcError:
            pass
        if serials and not args.keep_controllers:
            dd.remove_demo_controllers(stack)
        restore_flags(snapshot)
    return result


def _metric_queries(scenario: Scenario) -> list[str]:
    """The PromQL the report needs for this scenario."""
    queries = ["sum(agent_evaluations_total)"]
    for intervention in scenario.expect.interventions or (
        "adjust_player_sensitivity", "eliminate_player",
    ):
        queries.append(_intervention_metric(intervention, blocked=False))
        queries.append(_intervention_metric(intervention, blocked=True))
    return queries


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def print_matrix() -> None:
    print("Validation scenario matrix (#1070):\n")
    for name, sc in SCENARIOS.items():
        exp = sc.expect
        verb = "BLOCKED" if not exp.allowed else ("nothing" if not exp.expect_decision else "ALLOWED")
        print(f"  {name}")
        print(f"      what:        {sc.summary}")
        print(f"      players:     {sc.players}   duration: {sc.duration}s   flags: "
              f"{[f'{d}.{k}={v}' for d, k, v in sc.flags]}")
        print(f"      expect:      {exp.rule} -> {verb} "
              f"interventions={list(exp.interventions) or '(none)'} mode={exp.mode}\n")


def print_report(results: list[ScenarioResult]) -> int:
    print("\n" + "=" * 72)
    print("VALIDATION REPORT (#1070) — what the agent ACTUALLY did vs expected")
    print("=" * 72)
    hard_anomalies = 0
    for r in results:
        marker = {PASS: "PASS    ", ANOMALY: "ANOMALY ", ERROR: "ERROR   "}[r.verdict]
        print(f"  [{marker}] {r.name}")
        for reason in r.reasons:
            print(f"             - {reason}")
        if r.verdict == ANOMALY:
            hard_anomalies += 1
    n = len(results)
    n_pass = sum(1 for r in results if r.verdict == PASS)
    n_anom = sum(1 for r in results if r.verdict == ANOMALY)
    n_err = sum(1 for r in results if r.verdict == ERROR)
    print("-" * 72)
    print(f"  {n_pass}/{n} PASS   {n_anom} ANOMALY   {n_err} ERROR")

    if n_anom or n_err:
        print("\nWhere to look for the flagged gaps:")
        seen: set[str] = set()
        for r in results:
            if r.verdict in (ANOMALY, ERROR):
                for where in r.where:
                    if where not in seen:
                        seen.add(where)
                        print(f"  * {where}")
    print("\nDashboards:  http://localhost:8080/grafana/ (agent + experiments tabs)")
    print("Jaeger:      http://localhost:8080/jaeger/  service=agent")
    print("Prometheus:  http://localhost:8080/prometheus/")
    return hard_anomalies


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def select_scenarios(args: argparse.Namespace) -> list[Scenario]:
    if args.all:
        return list(SCENARIOS.values())
    chosen: list[Scenario] = []
    for name in args.scenario.split(","):
        name = name.strip()
        if not name:
            continue
        if name not in SCENARIOS:
            print(f"unknown scenario {name!r}\n", file=sys.stderr)
            print_matrix()
            raise SystemExit(2)
        chosen.append(SCENARIOS[name])
    return chosen


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--all", action="store_true", help="run the whole scenario matrix")
    p.add_argument("--scenario", default="statue",
                   help="comma-separated scenario name(s) to run (see --list)")
    p.add_argument("--list", action="store_true", help="print the scenario matrix and exit")
    p.add_argument("--fail-on-anomaly", action="store_true",
                   help="exit non-zero if any scenario reports a hard ANOMALY")
    p.add_argument("--jaeger", action="store_true",
                   help="also query Jaeger for the decision span chain (slower)")
    p.add_argument("--no-flag-changes", action="store_true",
                   help="do not flip/restore flags (run against whatever is currently set)")
    # stack endpoints (defaults match the #794 driver)
    p.add_argument("--host", default="localhost")
    p.add_argument("--mock-port", type=int, default=50062)
    p.add_argument("--menu-port", type=int, default=50054)
    p.add_argument("--coord-port", type=int, default=50053)
    p.add_argument("--prom-base", default="http://localhost:8080/prometheus",
                   help="Prometheus base URL (via envoy ingress)")
    p.add_argument("--jaeger-base", default="http://localhost:8080/jaeger",
                   help="Jaeger base URL (via envoy ingress)")
    p.add_argument("--agent-container", default=AGENT_CONTAINER,
                   help="agent container name for `docker logs`")
    # timings
    p.add_argument("--start-timeout", type=float, default=30.0)
    p.add_argument("--flag-settle", type=float, default=3.0,
                   help="seconds to wait after flipping flags (flagd reload)")
    p.add_argument("--collect-settle", type=float, default=5.0,
                   help="seconds to wait after the pattern before collecting")
    p.add_argument("--keep-controllers", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        print_matrix()
        return 0

    scenarios = select_scenarios(args)
    collector = LiveCollector(args.host, args.prom_base, args.jaeger_base, args.agent_container)
    stack = dd.Stack(args.host, args.mock_port, args.menu_port, args.coord_port)
    results: list[ScenarioResult] = []
    try:
        for scenario in scenarios:
            results.append(run_scenario(stack, scenario, collector, args))
    except KeyboardInterrupt:
        log("interrupted — reporting what we have so far")
    finally:
        stack.close()

    hard = print_report(results)
    if args.fail_on_anomaly and hard:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
