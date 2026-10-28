"""TC-007 on the full stack (T3): the closed agent loop through the real otel-collector (#1422 S8, #1404).

docs/test_cases/TC-007-agent-intervenes-in-ffa-game.md. The service tier
(``tests/service/test_tc007_rules_intervention_applied.py``) already proves the
rules decision is written and applied, with a harness sink in place of the
collector. This journey runs the SHIPPED images under docker compose and covers
the wiring that only the full stack has:

- game coordinator -> otel-collector -> agent (``otlp/agent`` fan-out, UC-041);
- agent -> flag service -> flagd -> coordinator (the decision takes effect);
- agent -> otel-collector -> Jaeger, read back through envoy the way the
  dashboard's Agent tab reads it (Jaeger query API v3, ``GET /jaeger/api/v3/traces
  ?query.serviceName=agent&query.operationName=agent.decide&query.searchDepth=5``, #2283,
  ``services/dashboard/src/agent.ts``
  ``decideQueryParams``; UC-035 step 3, decision feed only);
- the game's trace in Jaeger: the root span named after the mode, its phases,
  one span per player, and the agent's rules-path spans as remote children of
  the coordinator's ``game_cycle`` span, each ``agent.decision`` keeping its
  Link to the game root (UC-035 step 5, UC-040 BR-013 first and third clause);
  every agent span of the trace names G1 and game kind ``real`` (validation 1).

The agent and envoy are profile-gated (``docker-compose.ci.yml``), so the
module starts both into the running session project (``--no-deps``) and
removes them at teardown. No browser:
the tab's data source is its Jaeger query, and the feed fold below mirrors
``parseDecideTraces`` / ``decideRowFrom``.
"""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import menu_pb2
from tests.integration import jaeger_v3
from tests.integration.flag_service import FlagService
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    flagd_probe_client,
    force_end_game_by_id,
    get_game_client,
    get_menu_client,
    get_mock_client,
    kill_player_verified,
    mark_controllers_ready,
    poll_until,
    select_game_mode,
    setup_mock_controllers,
    wait_until_running,
)
from tests.integration.setup_guard import SetupGuard, compose_diagnostics, run_bounded

AGENT_SERVICE = "agent"
ENVOY_SERVICE = "envoy"
COORDINATOR_SERVICE_NAME = "game-coordinator-service"  # its OTEL_SERVICE_NAME

ENVOY_CONTAINER = "joustmania-envoy"  # container_name in docker-compose.yml
COMPOSE_NETWORK = "joustmania-network"
ENVOY_PORT = 80
# The dashboard reaches Jaeger through envoy on port 80 (UC-035 BR-001). The test
# uses envoy's PUBLISHED port, like the other modules (Jaeger 16686, Loki 3100).
# Fallback: when something that is not envoy answers there with a 404 (another
# listener owns host port 80, seen under WSL), or when TC007_ENVOY_VIA_CONTAINER_IP=1
# opts in, it dials envoy's address on the compose network instead (Linux docker
# only; Docker Desktop does not route to container IPs). Set once envoy is up.
ENVOY_VIA_CONTAINER_IP_ENV = "TC007_ENVOY_VIA_CONTAINER_IP"
_envoy_jaeger_api = ""
# UC-035 step 5 / telemetry.md §3: the coordinator's spans of one game.
GAME_ROOT_SPAN = "Joust Free-for-All"  # the mode's display name
GAME_PHASES = ("initialization_phase", "gameplay_phase", "game_cycle", "teardown_phase")
# Agent spans that must name the game (validation 1); game.kind where the spec puts it.
AGENT_SPANS_WITH_GAME_ID = ("agent.signal_received", "agent.decide", "agent.decision")
AGENT_SPANS_WITH_GAME_KIND = ("agent.decide", "agent.decision")
# The Agent tab's decide query (agent.ts DECIDE_OPERATION, DECIDE_TRACE_LIMIT).
DECIDE_QUERY = {"service": "agent", "operation": "agent.decide", "limit": 5}

# Interventions on, rules backend, the allow-list that lets the rules engine's
# in-game levers through on both sides (services/flagd/ci/agent.json).
AGENT_FLAGS = {
    "enabled": "on",
    "interventions_enabled": "on",
    "interventions_allowed": "standard",
    "decision.backend": "rules",
}

AGENT_UP_TIMEOUT = 600.0  # build (build mode) + health wait
AGENT_WAIT_TIMEOUT = 180
ENVOY_UP_TIMEOUT = 120.0
START_TIMEOUT = 25.0
# Early elimination -> a rule fires -> write -> flagd -> coordinator applies.
DECIDE_APPLY_TIMEOUT = 60.0
# Span end -> agent / coordinator batch export -> collector -> Jaeger.
JAEGER_TIMEOUT = 30.0

_SETUP = SetupGuard("TC-007: agent + envoy bring-up")
_FLAGS: FlagService | None = None


# --------------------------------------------------------------------------- #
# Stack: agent + envoy on top of the session stack
# --------------------------------------------------------------------------- #
def _flags_served(client) -> bool:
    ctx = EvaluationContext()
    return (
        client.get_boolean_value("enabled", False, ctx) is True
        and client.get_boolean_value("interventions_enabled", False, ctx) is True
        and client.get_string_value("decision.backend", "", ctx) == "rules"
    )


def _set_agent_flags() -> None:
    for flag, variant in AGENT_FLAGS.items():
        _FLAGS.put("agent", flag, variant=variant)


@pytest.fixture(scope="module")
def closed_loop_stack(docker_compose, request):
    """The session stack plus the agent (interventions on, rules) and envoy."""
    global _FLAGS
    cwd = str(docker_compose.context)
    prefix = list(docker_compose.docker_compose_command())
    env = dict(os.environ)
    use_prebuilt = os.getenv("USE_PREBUILT_IMAGES", "false").lower() == "true"
    use_dev_mounts = os.getenv("USE_DEV_MOUNTS", "false").lower() == "true"

    def _remove_agent_and_envoy() -> None:
        subprocess.run(
            [*prefix, "--profile", AGENT_SERVICE, "--profile", "dashboard"]
            + ["rm", "-sf", AGENT_SERVICE, ENVOY_SERVICE],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    _FLAGS = FlagService.from_compose(docker_compose)
    try:  # cleanup also when the bring-up fails half way
        with _SETUP.attempt(
            request.config,
            diagnose=lambda: compose_diagnostics(prefix, cwd=cwd, env=env),
        ):
            _bring_up(docker_compose, prefix, cwd, env, use_prebuilt or use_dev_mounts)
        yield docker_compose
    finally:
        _remove_agent_and_envoy()
        _FLAGS.cleanup()
        with flagd_probe_client(docker_compose, "agent") as client:
            poll_until(
                lambda: (
                    client.get_boolean_value("enabled", True, EvaluationContext())
                    is False
                )
            )


def _bring_up(docker_compose, prefix, cwd, env, no_build: bool) -> None:
    """Serve the agent flags, start the agent, then envoy; wait until envoy answers."""
    global _envoy_jaeger_api
    _set_agent_flags()
    with flagd_probe_client(docker_compose, "agent") as client:
        assert poll_until(lambda: _flags_served(client)), (
            f"flagd did not serve {AGENT_FLAGS}"
        )
    # --no-deps: never recreate the running flag service / flagd (it would drop the overlays).
    up = [*prefix, "--profile", AGENT_SERVICE, "up", "-d", "--wait", "--no-deps"]
    up += ["--wait-timeout", str(AGENT_WAIT_TIMEOUT)]
    if not no_build:
        up.append("--build")
    run_bounded(
        [*up, AGENT_SERVICE],
        what="start agent",
        timeout=AGENT_UP_TIMEOUT,
        cwd=cwd,
        env=env,
    )
    # Envoy has no healthcheck (distroless); its readiness is polled below.
    run_bounded(
        [*prefix, "--profile", "dashboard", "up", "-d", "--no-deps", ENVOY_SERVICE],
        what="start envoy",
        timeout=ENVOY_UP_TIMEOUT,
        cwd=cwd,
        env=env,
    )
    _envoy_jaeger_api = _envoy_base(prefix, cwd, env) + "/jaeger/api"
    assert poll_until(_jaeger_reachable, timeout=30.0), (
        f"envoy never answered {_envoy_jaeger_api}/v3/services"
    )


def _probe(url: str) -> tuple[int, str]:
    """(HTTP status, Server header) of ``url``; (0, "") when nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - loopback test URL
            return response.status, response.headers.get("server", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("server", "")
    except OSError:
        return 0, ""


def _container_base() -> str:
    address = subprocess.run(
        ["docker", "inspect", "-f"]
        + [f'{{{{(index .NetworkSettings.Networks "{COMPOSE_NETWORK}").IPAddress}}}}']
        + [ENVOY_CONTAINER],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    assert address, f"{ENVOY_CONTAINER} has no address on {COMPOSE_NETWORK}"
    return f"http://{address}:{ENVOY_PORT}"


def _envoy_base(prefix, cwd, env) -> str:
    """Envoy's published host:port; the container address only on opt-in or a foreign 404."""
    if os.getenv(ENVOY_VIA_CONTAINER_IP_ENV) == "1":
        return _container_base()
    published = subprocess.run(
        [*prefix, "--profile", "dashboard", "port", ENVOY_SERVICE, str(ENVOY_PORT)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    host, _, port = published.rpartition(":")
    host = "127.0.0.1" if host in ("0.0.0.0", "[::]", "") else host
    base = f"http://{host}:{port}"
    last = [(0, "")]

    def _answered() -> bool:
        last[0] = _probe(f"{base}/jaeger/api/v3/services")
        return last[0][0] != 0

    poll_until(_answered, timeout=15.0)
    code, server = last[0]
    if code == 404 and server != "envoy":
        return _container_base()  # host port 80 belongs to another listener
    return base


# --------------------------------------------------------------------------- #
# Jaeger through envoy (the Agent tab's data source)
# --------------------------------------------------------------------------- #
def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - loopback test URL
        return json.load(response)


def _jaeger_reachable() -> bool:
    try:
        return "services" in _get_json(f"{_envoy_jaeger_api}/v3/services")
    except (OSError, ValueError):
        return False


def _decide_traces() -> list[dict]:
    """The Agent tab's poll: the v3 search with the tab's service, operation and depth."""
    try:
        return jaeger_v3.find_traces(_envoy_jaeger_api, **DECIDE_QUERY)
    except (OSError, ValueError):
        return []


def _trace(trace_id: str) -> dict:
    try:
        return jaeger_v3.get_trace(_envoy_jaeger_api, trace_id)
    except (OSError, ValueError):
        return {}


def _tags(span: dict) -> dict:
    return {t["key"]: t["value"] for t in span.get("tags", [])}


def _parent_id(span: dict) -> str:
    """CHILD_OF reference, as agent.ts parentSpanId reads it."""
    ref = next(
        (r for r in span.get("references", []) if r.get("refType") == "CHILD_OF"), None
    )
    return ref["spanID"] if ref else ""


def _service(trace: dict, span: dict) -> str:
    return (
        trace.get("processes", {}).get(span.get("processID"), {}).get("serviceName", "")
    )


def _decide_rows(traces: list[dict], game_id: str) -> list[dict]:
    """The decide feed rows of ``game_id``, folded as parseDecideTraces/decideRowFrom do (rules path)."""
    rows = []
    for trace in traces:
        spans = trace.get("spans", [])
        by_id = {s["spanID"]: s for s in spans}
        for span in spans:
            tags = _tags(span)
            if (
                span["operationName"] != "agent.decide"
                or tags.get("game.id") != game_id
            ):
                continue
            parent = by_id.get(_parent_id(span))
            siblings = []
            if (
                parent is not None
                and parent["operationName"] == "agent.signal_received"
            ):
                siblings = [
                    s
                    for s in spans
                    if s["operationName"] == "agent.decision"
                    and _parent_id(s) == parent["spanID"]
                ]
            rows.append(
                {
                    "trace_id": span["traceID"],
                    "span_id": span["spanID"],
                    "backend": tags.get("decide.backend", ""),
                    "requested": tags.get("decide.backend.requested", ""),
                    "fallback_reason": tags.get("decide.fallback_reason", ""),
                    "outcome": tags.get("decide.outcome", ""),
                    "game_kind": tags.get("game.kind", ""),
                    "decisions": [
                        (
                            _tags(d).get("decision.action", ""),
                            _tags(d).get("decision.blocked"),
                        )
                        for d in siblings
                    ],
                    "decision_ids": [d["spanID"] for d in siblings],
                }
            )
    return rows


def _game_trace_problems(
    trace: dict, row: dict, game_id: str, serials: list[str]
) -> list[str]:
    """What G1's trace in Jaeger lacks (UC-035 step 5, UC-040 BR-013, validation 1); [] when complete."""
    spans = trace.get("spans", [])
    by_id = {s["spanID"]: s for s in spans}
    coordinator = [s for s in spans if _service(trace, s) == COORDINATOR_SERVICE_NAME]
    agent = [s for s in spans if _service(trace, s) == AGENT_SERVICE]
    problems: list[str] = []

    # UC-035 step 5: the root span named after the mode, its phases, one span per player.
    roots = [
        s
        for s in coordinator
        if s["operationName"] == GAME_ROOT_SPAN and _tags(s).get("game.id") == game_id
    ]
    if len(roots) != 1:
        problems.append(f"{len(roots)} root spans {GAME_ROOT_SPAN!r} for {game_id}")
    root_id = roots[0]["spanID"] if roots else ""
    names = {s["operationName"] for s in coordinator}
    missing = [phase for phase in GAME_PHASES if phase not in names]
    if missing:
        problems.append(
            f"missing phases {missing} (coordinator spans: {sorted(names)})"
        )
    player_serials = sorted(
        _tags(s).get("player.serial", "")
        for s in coordinator
        if s["operationName"] == "player_lifecycle"
        or s["operationName"].startswith("player: ")
    )
    if player_serials != sorted(serials):
        problems.append(
            f"player spans for {player_serials}, expected one each for {sorted(serials)}"
        )

    # UC-040 BR-013: the rules-path agent.decide sits under agent.signal_received,
    # a remote child of the coordinator's game_cycle ...
    decide = by_id.get(row["span_id"])
    signal = by_id.get(_parent_id(decide)) if decide else None
    cycle = by_id.get(_parent_id(signal)) if signal else None
    chain = [
        f"{s['operationName']}@{_service(trace, s)}" if s else "missing"
        for s in (decide, signal, cycle)
    ]
    if chain[1:] != [
        "agent.signal_received@agent",
        f"game_cycle@{COORDINATOR_SERVICE_NAME}",
    ]:
        problems.append(f"decide chain {chain}")
    # ... and each of its agent.decision spans keeps its Link to the game root.
    for decision_id in row["decision_ids"]:
        refs = by_id.get(decision_id, {}).get("references", [])
        if not any(
            r.get("refType") == "FOLLOWS_FROM" and r.get("spanID") == root_id
            for r in refs
        ):
            problems.append(
                f"agent.decision {decision_id} has no Link to the game root {root_id}: {refs}"
            )

    # Validation 1: one game, one context; every agent span names G1 and kind real.
    for s in agent:
        tags = _tags(s)
        name = s["operationName"]
        if ("game.id" in tags or name in AGENT_SPANS_WITH_GAME_ID) and tags.get(
            "game.id"
        ) != game_id:
            problems.append(f"{name} {s['spanID']} game.id={tags.get('game.id')!r}")
        if ("game.kind" in tags or name in AGENT_SPANS_WITH_GAME_KIND) and tags.get(
            "game.kind"
        ) != "real":
            problems.append(f"{name} {s['spanID']} game.kind={tags.get('game.kind')!r}")
    if not agent:
        problems.append("no agent spans in the game trace")
    return problems


# --------------------------------------------------------------------------- #
# Game events
# --------------------------------------------------------------------------- #
def _applied_types(collector: GameEventCollector, game_id: str) -> list[str]:
    """Types of the unblocked agent_intervention events the coordinator published for ``game_id``."""
    return [
        e.data.get("type", "")
        for e in collector.get_events("agent_intervention")
        if e.game_id == game_id and e.data.get("blocked") == "false"
    ]


@pytest.mark.asyncio
@pytest.mark.uc("UC-041", flow="main", steps=[1, 2, 5, 6], tc="TC-007")
@pytest.mark.uc("UC-040", flow="main", steps=[13], tc="TC-007")
# BR-013: the rules-path clause (signal_received under game_cycle) and the
# decision's Link to the game root; the remote path and the effect span are not run.
@pytest.mark.uc("UC-040", br=["BR-013"], tc="TC-007")
@pytest.mark.uc("UC-035", flow="main", steps=[3, 5], tc="TC-007")
async def test_tc007_closed_loop_through_collector_and_jaeger(
    closed_loop_stack, ensure_game_stopped
):
    """TC-007 steps 1-6 and 8 (data only) on the shipped images: the rules decision reaches the
    game through the real collector, and Jaeger (via envoy, the Agent tab's query) holds it
    under the game's game_cycle span."""
    docker_compose = closed_loop_stack
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    menu_client, menu_channel = await get_menu_client(docker_compose)
    collector = GameEventCollector(game_client)
    game_id = ""
    try:
        await collector.start()

        # Step 1: start FFA from the menu on four mock controllers (G1).
        serials = await setup_mock_controllers(docker_compose, count=4)
        assert len(serials) == 4, f"Step 1: expected 4 mock controllers, got {serials}"
        await menu_client.StopMenu(menu_pb2.StopMenuRequest())
        started = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
        assert started.success, f"Step 1: StartMenu failed: {started.error}"
        assert await select_game_mode(menu_client, "JoustFFA"), (
            "Step 1: select_game JoustFFA rejected"
        )
        known = {e.game_id for e in collector.get_events("game_started")}

        def _new_game() -> str:
            return next(
                (
                    e.game_id
                    for e in collector.get_events("game_started")
                    if e.game_id not in known
                ),
                "",
            )

        for _attempt in range(2):  # one dropped ready press must not fail the step
            await mark_controllers_ready(mock_client, serials)
            if await async_poll_until(lambda: bool(_new_game()), timeout=START_TIMEOUT):
                break
        game_id = _new_game()
        assert game_id, (
            f"Step 1: no game_started after ready-up: {[e.event_type for e in collector.get_events()]}"
        )
        running = await wait_until_running(game_client, {game_id}, timeout=15.0)
        assert game_id in running, f"Step 1: {game_id} never RUNNING"

        # Step 3: early elimination of one player.
        assert await kill_player_verified(
            mock_client, game_client, serials[0], lambda p: not p.alive
        ), f"Step 3: {serials[0]} was not eliminated in {game_id}"

        # Steps 2, 4 and 6: the agent saw G1's telemetry through the collector, the
        # rules engine decided, and the coordinator applied an unblocked intervention.
        assert await async_poll_until(
            lambda: bool(_applied_types(collector, game_id)), DECIDE_APPLY_TIMEOUT
        ), (
            f"Step 6: no unblocked agent_intervention for {game_id} within {DECIDE_APPLY_TIMEOUT}s: "
            f"{[dict(e.data) for e in collector.get_events('agent_intervention')]}"
        )
        applied = set(_applied_types(collector, game_id))

        # End the game so the coordinator exports its game_cycle span (it ends with the game).
        await force_end_game_by_id(game_client, game_id, reason="tc007_done")
        game_id_done = game_id
        game_id = ""

        # Step 8 (data): the Agent tab's Jaeger query, through envoy, holds a rules
        # agent.decide row for G1 whose sibling decision is the applied intervention.
        rows: list[dict] = []
        found: list[dict] = []

        def _row_for_applied() -> bool:
            rows[:] = _decide_rows(_decide_traces(), game_id_done)
            found[:] = [
                r
                for r in rows
                if any(a in applied and b is False for a, b in r["decisions"])
            ]
            return bool(found)

        assert await async_poll_until(_row_for_applied, JAEGER_TIMEOUT), (
            f"Step 8: the Agent tab query {DECIDE_QUERY} returned no agent.decide row for {game_id_done} "
            f"with an unblocked decision in {sorted(applied)}: rows {rows}"
        )
        row = found[0]
        # Step 5: rules answered, rules was requested, no fallback; validation 1: game kind real.
        assert (row["backend"], row["requested"], row["fallback_reason"]) == (
            "rules",
            "rules",
            "",
        ), f"Step 5: decide row {row}"
        assert row["outcome"] == "act" and row["game_kind"] == "real", (
            f"Step 5 / validation 1: decide row {row}"
        )

        # Step 8 (Jaeger): G1's whole trace: root span, phases and player spans
        # (UC-035 step 5); agent.decide -> agent.signal_received -> remote game_cycle,
        # and the decisions' Link to the game root (UC-040 BR-013); every agent span
        # names G1 and kind real (validation 1).
        problems: list[str] = []

        def _trace_complete() -> bool:
            problems[:] = _game_trace_problems(
                _trace(row["trace_id"]), row, game_id_done, serials
            )
            return not problems

        assert await async_poll_until(_trace_complete, JAEGER_TIMEOUT), (
            f"Step 8 / validation 1: trace {row['trace_id']} of {game_id_done}: {problems}"
        )
    finally:
        await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="tc007_cleanup")
        await menu_channel.close()
        await mock_channel.close()
        await game_channel.close()
