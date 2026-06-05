"""
End-to-end intervention-flow integration tests (#730).

Proves the full agent intervention chain works against the real services:

    agent writes flag  ->  flagd file-watch reload  ->  game coordinator
    InterventionManager evaluates + enforces  ->  effect applied to the live
    game  ->  effect observable in the OTel/EventBus signals.

The agent ActionSink (services/agent/actions, Go) is NOT run in the integration
compose (it lives in the ``agent`` profile and writes the same file these tests
write). Instead these tests reproduce the EXACT mutation the Go writer produces
on the bind-mounted ``services/flagd/interventions.json`` file (state "active"
variant + defaultVariant flip, per-serial targeting if-ladder, fresh nonce per
edge dispatch). flagd reloads on the in-place write and the game coordinator —
which evaluates the ``interventions`` and ``agent`` domains via live flagd RPC —
converges on the new contents.

Observable signals used (no Prometheus pull needed — game-coordinator metrics
go OTLP->collector->Prometheus, a slow/flaky push path for assertions):

- ``StreamGameEvents`` ``agent_intervention`` events carry
  ``{type, target, blocked, block_reason}`` — the EventBus mirror of the
  ``game_interventions_total{type,objective,blocked}`` counter. Every applied
  or blocked intervention emits exactly one.
- ``GetGameState`` exposes per-player ``sensitivity_factor`` and ``alive`` —
  the applied state of difficulty/lifecycle interventions.
- A direct OpenFeature flagd RPC client (flagd port 8013, published by
  docker-compose.override.yml) evaluates the written per-serial targeting
  if-ladder, catching schema/targeting-rule rejections the Go unit tests
  (jsonlogic-direct) cannot.

interventions.json / agent.json are bind-mounted host files; the
``flag_files`` fixture snapshots and restores them so mutations never leak.
"""

import asyncio
import json
import os
import sys
import time
import uuid

import grpc
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2  # noqa: E402

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    get_game_client,
    get_mock_client,
    get_mock_controller_serials,
    setup_mock_controllers,
    start_game_via_menu,
)

# Repo-root-relative path to the bind-mounted flagd config dir. The compose
# flagd service mounts ./services/flagd into the container, so writing here on
# the host is exactly what the Go ActionSink does inside the container.
_FLAGD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../services/flagd"))
INTERVENTIONS_PATH = os.path.join(_FLAGD_DIR, "interventions.json")
AGENT_PATH = os.path.join(_FLAGD_DIR, "agent.json")

# flagd debounces file-watch reloads + the game coordinator re-evaluates on the
# PROVIDER_CONFIGURATION_CHANGED event; give the chain time to converge.
RELOAD_SETTLE_SECONDS = 2.0

# Mutating a per-player state flag and observing the applied sensitivity_factor
# can take a couple of reload cycles under load; poll up to this long.
APPLY_TIMEOUT_SECONDS = 12.0

# Variant name the Go writer overwrites for state + edge flags.
ACTIVE_VARIANT = "active"


# =============================================================================
# Flag-file mutation helpers (mirror services/agent/actions/writer.go)
# =============================================================================


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write (matches the Go writer / admin-mode writer).

    No temp+rename: rename over a bind mount that flagd watches triggers EBUSY.
    """
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)


def write_state(flag_key: str, value) -> None:
    """State-shaped write: set the ``active`` variant + flip defaultVariant.

    Mirrors Writer.setState (services/agent/actions/writer.go).
    """
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][ACTIVE_VARIANT] = value
    flag["defaultVariant"] = ACTIVE_VARIANT
    _dump_in_place(INTERVENTIONS_PATH, doc)


def write_targeted(flag_key: str, neutral: str, serial: str, value) -> None:
    """Per-player state write with a flagd targeting if-ladder.

    Mirrors Writer.setTargeted: a per-serial ``agent_<serial>`` variant plus a
    nested ``if(=== targetingKey serial, agent_<serial>, ... neutral)`` ladder,
    defaultVariant flipped to the neutral variant. Merges with any existing
    agent-driven serials (does not clobber other players).
    """
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][f"agent_{serial}"] = value

    agent_serials = sorted(
        name[len("agent_"):] for name in flag["variants"] if name.startswith("agent_")
    )
    expr: object = neutral
    for s in reversed(agent_serials):
        expr = {
            "if": [
                {"===": [{"var": "targetingKey"}, s]},
                f"agent_{s}",
                expr,
            ]
        }
    flag["targeting"] = expr
    flag["defaultVariant"] = neutral
    _dump_in_place(INTERVENTIONS_PATH, doc)


def write_edge(flag_key: str, payload: str = "") -> str:
    """Edge-triggered write: ``active`` variant holds ``<nonce>:<payload>``.

    Mirrors Writer.setEdge. Returns the fresh nonce so the caller can later
    re-assert exactly-once (same nonce = no re-apply). ``payload=""`` writes a
    nonce-only value (the end_game shape).
    """
    nonce = uuid.uuid4().hex
    value = nonce if payload == "" else f"{nonce}:{payload}"
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][ACTIVE_VARIANT] = value
    flag["defaultVariant"] = ACTIVE_VARIANT
    _dump_in_place(INTERVENTIONS_PATH, doc)
    return nonce


def rewrite_edge_value(flag_key: str, raw_value: str) -> None:
    """Force flagd to re-serve a specific ``active`` value (re-read with the SAME
    nonce, to assert the exactly-once guard). Touches another field so the file
    changes byte-wise and flagd reloads, but the edge value itself is unchanged.
    """
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][ACTIVE_VARIANT] = raw_value
    flag["defaultVariant"] = ACTIVE_VARIANT
    # Toggle a harmless metadata marker to guarantee a byte-level change.
    flag.setdefault("_reload_marker", 0)
    flag["_reload_marker"] = int(flag["_reload_marker"]) + 1
    _dump_in_place(INTERVENTIONS_PATH, doc)


def set_interventions_allowed(variant: str) -> None:
    """Flip the agent-domain ``interventions_allowed`` flag to a named variant
    (``none`` / ``ambient`` / ``standard`` / ``full``)."""
    doc = _load(AGENT_PATH)
    doc["flags"]["interventions_allowed"]["defaultVariant"] = variant
    _dump_in_place(AGENT_PATH, doc)


def set_policy_budget(per_minute: int) -> None:
    """Raise the weighted rate-limit budget (policy.max_interventions_per_minute).

    The shipped variants cap at ``aggressive`` (4); tests that drive several
    interventions in a tight window need more headroom, so we inject a custom
    ``active`` variant and flip defaultVariant to it. The manager reads this via
    ``get_integer_value`` and feeds it to the weighted sliding-window limiter.
    """
    doc = _load(AGENT_PATH)
    flag = doc["flags"]["policy.max_interventions_per_minute"]
    flag["variants"]["active"] = int(per_minute)
    flag["defaultVariant"] = "active"
    _dump_in_place(AGENT_PATH, doc)


# =============================================================================
# Fixtures
# =============================================================================


# Generous rate-limit budget the intervention-flow suite runs under. The
# weighted sliding-window limiter is process-GLOBAL and lives on the
# game-coordinator's InterventionManager for the whole compose session — it is
# NOT reset between tests. A test that applies several interventions can leave
# residual weight in the shared 60s window, and budget-flag propagation to flagd
# lags the first write by a reload cycle; either can rate-limit a later test's
# final write regardless of suite size/order (the #824 flake: the last test's
# final write of [1.5,0.7,1.5,0.5,2.0] never reached 2.0). Pinning a high budget
# for the WHOLE suite, applied by the snapshot/restore fixture before any test
# body runs, removes that cross-test bleed systemically. Rate-limit SEMANTICS are
# unit-tested in services/game_coordinator/tests; the integration suite's job is
# propagation/effect, so a high budget here is the correct posture — a test that
# explicitly exercises rate limiting would pin its own (lower) budget after this.
SUITE_RATE_LIMIT_BUDGET = 50


@pytest.fixture
def flag_files(docker_compose):
    """Snapshot interventions.json + agent.json; restore byte-for-byte on exit.

    Also raises ``policy.max_interventions_per_minute`` to a generous suite-wide
    budget for the duration of the test (see ``SUITE_RATE_LIMIT_BUDGET``) and
    waits a reload cycle so flagd serves it before the test body issues any
    intervention write — this makes the process-global rate limiter
    headroom-independent of test ordering / suite size. Restored at teardown via
    the same in-place write so flagd reloads the committed baseline and no
    mutation (budget included) leaks into the repo or later tests.
    """
    with open(INTERVENTIONS_PATH) as fh:
        interventions_backup = fh.read()
    with open(AGENT_PATH) as fh:
        agent_backup = fh.read()

    # Pin the generous budget up front and let flagd reload it before the test
    # body runs, so the very first intervention write already sees the headroom.
    set_policy_budget(SUITE_RATE_LIMIT_BUDGET)
    time.sleep(RELOAD_SETTLE_SECONDS)

    yield

    with open(INTERVENTIONS_PATH, "w") as fh:
        fh.write(interventions_backup)
    with open(AGENT_PATH, "w") as fh:
        fh.write(agent_backup)
    # Let flagd reload the restored baseline before the next test mutates it.
    time.sleep(RELOAD_SETTLE_SECONDS)


@pytest.fixture
async def game_client(docker_compose):
    client, channel = await get_game_client(docker_compose)
    yield client
    await channel.close()


# =============================================================================
# Observable-signal helpers
# =============================================================================


def _intervention_events(collector: GameEventCollector, type_id: str) -> list:
    """All agent_intervention events for a given intervention type."""
    return [
        e
        for e in collector.get_events("agent_intervention")
        if e.data.get("type") == type_id
    ]


async def _wait_for_intervention_event(
    collector: GameEventCollector,
    type_id: str,
    *,
    blocked: str | None = None,
    timeout: float = APPLY_TIMEOUT_SECONDS,
):
    """Wait until an agent_intervention event of ``type_id`` (optionally with a
    given ``blocked`` value) has been observed. Returns that event."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in _intervention_events(collector, type_id):
            if blocked is None or e.data.get("blocked") == blocked:
                return e
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"no agent_intervention(type={type_id}, blocked={blocked}) within {timeout}s; "
        f"saw: {[dict(e.data) for e in collector.get_events('agent_intervention')]}"
    )


async def _get_state(game_client) -> game_coordinator_pb2.GameInfo:
    resp = await game_client.GetGameState(game_coordinator_pb2.GetGameStateRequest())
    assert resp.success, f"GetGameState failed: {resp.error}"
    return resp.game_info


async def _player(game_client, serial: str):
    info = await _get_state(game_client)
    for p in info.players:
        if p.serial == serial:
            return p
    return None


async def _wait_for_sensitivity_factor(
    game_client, serial: str, expected: float, *, tol: float = 0.01, timeout: float = APPLY_TIMEOUT_SECONDS
) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        p = await _player(game_client, serial)
        if p is not None:
            last = p.sensitivity_factor
            if abs(p.sensitivity_factor - expected) <= tol:
                return
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"{serial} sensitivity_factor did not reach {expected} (last={last}) within {timeout}s"
    )


# =============================================================================
# Scenario 1 + 2: per-player sensitivity factor (state e2e + live flagd RPC)
# =============================================================================


@pytest.mark.asyncio
async def test_player_sensitivity_factor_targets_one_player(flag_files, docker_compose, game_client):
    """State-shaped e2e: a per-serial player_sensitivity_factor targeting rule
    applies the factor to the targeted player ONLY, observable via
    GetGameState.sensitivity_factor and an unblocked agent_intervention event.
    """
    set_interventions_allowed("full")  # adjust_player_sensitivity must be allowed
    # Rate-limit budget headroom is provided suite-wide by the flag_files fixture
    # (SUITE_RATE_LIMIT_BUDGET), so this test needs no per-test budget bump.
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    target, other = serials[0], serials[1]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )

            # Baseline: every player at the neutral 1.0 factor.
            info = await _get_state(game_client)
            for p in info.players:
                assert abs(p.sensitivity_factor - 1.0) <= 0.01, (
                    f"{p.serial} not at neutral baseline: {p.sensitivity_factor}"
                )

            # Agent writes a targeting rule for the target serial only.
            write_targeted("player_sensitivity_factor", "default", target, 1.5)

            # Observable signal A: the unblocked intervention event fires.
            evt = await _wait_for_intervention_event(
                collector, "adjust_player_sensitivity", blocked="false"
            )
            assert evt.data.get("blocked") == "false"

            # Observable signal B: only the targeted player's factor changed.
            await _wait_for_sensitivity_factor(game_client, target, 1.5)
            other_player = await _player(game_client, other)
            assert other_player is not None
            assert abs(other_player.sensitivity_factor - 1.0) <= 0.01, (
                f"non-targeted {other} changed: {other_player.sensitivity_factor}"
            )
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_per_player_targeting_resolves_against_live_flagd(flag_files, docker_compose):
    """Per-player targeting against LIVE flagd RPC: the written if-ladder must
    resolve correctly via OpenFeature for distinct serials + unknown->default.
    Catches schema/targeting-rule rejections the Go unit tests cannot.
    """
    from openfeature.contrib.provider.flagd import FlagdProvider
    from openfeature.contrib.provider.flagd.config import ResolverType
    from openfeature.evaluation_context import EvaluationContext

    serial_a, serial_b, unknown = "SER_A", "SER_B", "SER_UNKNOWN"

    # Write two distinct per-serial branches (merged into one if-ladder).
    write_targeted("player_sensitivity_factor", "default", serial_a, 1.5)
    write_targeted("player_sensitivity_factor", "default", serial_b, 0.7)
    time.sleep(RELOAD_SETTLE_SECONDS)

    host = docker_compose.get_service_host("flagd", 8013)
    port = int(docker_compose.get_service_port("flagd", 8013))

    provider = FlagdProvider(
        host=host,
        port=port,
        resolver_type=ResolverType.RPC,
        selector="flagSetId=interventions",
    )
    from openfeature import api

    api.set_provider(provider, domain="it_interventions")
    client = api.get_client("it_interventions")

    # Poll: provider connect + flagd reload may lag the file write.
    def _eval(serial: str) -> float:
        return client.get_float_value(
            "player_sensitivity_factor", 1.0, EvaluationContext(targeting_key=serial)
        )

    deadline = time.time() + APPLY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if abs(_eval(serial_a) - 1.5) <= 0.01 and abs(_eval(serial_b) - 0.7) <= 0.01:
            break
        time.sleep(0.3)

    assert abs(_eval(serial_a) - 1.5) <= 0.01, f"serial_a resolved to {_eval(serial_a)}"
    assert abs(_eval(serial_b) - 0.7) <= 0.01, f"serial_b resolved to {_eval(serial_b)}"
    # Unknown serial falls through the if-ladder to the neutral default (1.0).
    assert abs(_eval(unknown) - 1.0) <= 0.01, f"unknown resolved to {_eval(unknown)}"

    provider.shutdown()


# =============================================================================
# Scenario 3: edge-triggered elimination + exactly-once nonce
# =============================================================================


@pytest.mark.asyncio
async def test_eliminate_player_edge_trigger_and_exactly_once(flag_files, docker_compose, game_client):
    """Edge-triggered e2e: an eliminate_player nonce write kills the targeted
    player through the real _kill_player path (observable as alive=False +
    unblocked intervention event). Re-reading the SAME nonce does NOT re-apply;
    a FRESH nonce applies again to a different player.
    """
    set_interventions_allowed("full")  # eliminate_player must be allowed
    # Two HARD eliminations (2.0 each) fit easily under the suite-wide budget the
    # flag_files fixture pins (SUITE_RATE_LIMIT_BUDGET); no per-test bump needed.
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    victim, second_victim = serials[0], serials[1]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )

            assert (await _player(game_client, victim)).alive, "victim should start alive"

            # Edge trigger 1: eliminate the victim.
            nonce1 = write_edge("eliminate_player", victim)
            await _wait_for_intervention_event(
                collector, "eliminate_player", blocked="false"
            )

            # Observable: the player died through the natural kill path.
            deadline = time.time() + APPLY_TIMEOUT_SECONDS
            while time.time() < deadline and (await _player(game_client, victim)).alive:
                await asyncio.sleep(0.3)
            assert not (await _player(game_client, victim)).alive, "victim was not eliminated"

            applied_after_first = len(
                [e for e in _intervention_events(collector, "eliminate_player")
                 if e.data.get("blocked") == "false"]
            )

            # Re-read the SAME nonce (force flagd reload, value unchanged): the
            # exactly-once guard must NOT re-apply.
            rewrite_edge_value("eliminate_player", f"{nonce1}:{victim}")
            time.sleep(RELOAD_SETTLE_SECONDS + 1.0)
            applied_after_reread = len(
                [e for e in _intervention_events(collector, "eliminate_player")
                 if e.data.get("blocked") == "false"]
            )
            assert applied_after_reread == applied_after_first, (
                "same nonce re-applied the elimination (exactly-once broken)"
            )

            # FRESH nonce -> applies again, to a different player.
            assert (await _player(game_client, second_victim)).alive
            write_edge("eliminate_player", second_victim)
            deadline = time.time() + APPLY_TIMEOUT_SECONDS
            while time.time() < deadline and (await _player(game_client, second_victim)).alive:
                await asyncio.sleep(0.3)
            assert not (await _player(game_client, second_victim)).alive, (
                "fresh nonce did not re-apply elimination"
            )
    finally:
        await channel.close()


# =============================================================================
# Scenario 4: permission-block negative
# =============================================================================


@pytest.mark.asyncio
async def test_permission_layer_blocks_disallowed_intervention(flag_files, docker_compose, game_client):
    """Permission-block negative: with interventions_allowed=ambient (the
    committed default), eliminate_player is NOT in the allowed set, so the
    intervention is blocked (block_reason=not_allowed) and the player stays
    alive — the effect never reaches the kill path.
    """
    set_interventions_allowed("ambient")  # eliminate_player NOT allowed
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    victim = serials[0]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )
            assert (await _player(game_client, victim)).alive

            write_edge("eliminate_player", victim)

            evt = await _wait_for_intervention_event(
                collector, "eliminate_player", blocked="true"
            )
            assert evt.data.get("blocked") == "true"
            assert evt.data.get("block_reason") == "not_allowed", (
                f"unexpected block_reason: {evt.data.get('block_reason')}"
            )

            # Game state unchanged: the blocked intervention never killed anyone.
            await asyncio.sleep(RELOAD_SETTLE_SECONDS)
            assert (await _player(game_client, victim)).alive, (
                "blocked intervention still killed the player"
            )
    finally:
        await channel.close()


# =============================================================================
# Scenario 5: mode-capability matrix (revive blocked in FFA, allowed in NonStop)
# =============================================================================


@pytest.mark.asyncio
async def test_revive_blocked_in_permanent_elimination_mode(flag_files, docker_compose, game_client):
    """Mode-matrix negative: revive_player is blocked in FFA (permanent
    elimination); block_reason=mode_unsupported."""
    set_interventions_allowed("full")  # revive_player allowed at policy level
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    victim = serials[0]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )
            write_edge("revive_player", victim)
            evt = await _wait_for_intervention_event(
                collector, "revive_player", blocked="true"
            )
            assert evt.data.get("block_reason") == "mode_unsupported", (
                f"expected mode_unsupported, got {evt.data.get('block_reason')}"
            )
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_revive_allowed_in_respawn_mode(flag_files, docker_compose, game_client):
    """Mode-matrix positive: revive_player is allowed in NonStop (respawn mode);
    the intervention is NOT blocked by the capability matrix."""
    set_interventions_allowed("full")
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    victim = serials[0]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="NonStop", event_collector=collector
            )
            write_edge("revive_player", victim)
            evt = await _wait_for_intervention_event(collector, "revive_player")
            # In a respawn mode the capability matrix must NOT block it.
            assert evt.data.get("block_reason") != "mode_unsupported", (
                "revive wrongly blocked as mode_unsupported in a respawn mode"
            )
    finally:
        await channel.close()


# =============================================================================
# Scenario 6: writer gate (agent never writes when disabled by default)
# =============================================================================


def test_agent_interventions_disabled_by_default():
    """Writer gate: the agent ActionSink is gated by AGENT_INTERVENTIONS_ENABLED
    and defaults OFF, so it never mutates interventions.json in the integration
    compose.

    The agent's decision loop cannot be driven deterministically from this
    harness (it lives in the ``agent`` compose profile and is not started for
    integration tests), so per the PR plan this scenario is covered by the Go
    unit tests (services/agent/actions/writer_test.go) for the actual no-write
    behavior. Here we assert the compose default keeps the gate OFF, which is
    the property that makes those unit tests load-bearing for the e2e contract.
    """
    compose_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../docker-compose.yml")
    )
    with open(compose_path) as fh:
        compose_text = fh.read()

    assert "AGENT_INTERVENTIONS_ENABLED" in compose_text, (
        "agent service should reference the AGENT_INTERVENTIONS_ENABLED gate"
    )
    # The gate must default to false (env default ``:-false`` or explicit false).
    assert (
        "AGENT_INTERVENTIONS_ENABLED:-false" in compose_text
        or "AGENT_INTERVENTIONS_ENABLED=false" in compose_text
    ), "AGENT_INTERVENTIONS_ENABLED must default to false in docker-compose.yml"


# =============================================================================
# Scenario 7: repeated in-place writes keep flagd's file watch healthy
# =============================================================================


@pytest.mark.asyncio
async def test_repeated_in_place_writes_all_propagate(flag_files, docker_compose, game_client):
    """Several consecutive in-place mutations must all propagate through flagd's
    file watch — no missed reload / stuck inotify after repeated writes.

    Drives player_sensitivity_factor through a sequence of distinct values and
    asserts the game converges on the FINAL value (and that an intermediate
    value was observed at least once, proving multiple reloads landed).
    """
    set_interventions_allowed("full")
    # Five MEDIUM writes (1.0 each) fit under the suite-wide budget pinned by the
    # flag_files fixture (SUITE_RATE_LIMIT_BUDGET); no per-test bump needed.
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    target = serials[0]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )

            sequence = [1.5, 0.7, 1.5, 0.5, 2.0]
            observed_intermediate = False
            for value in sequence:
                write_targeted("player_sensitivity_factor", "default", target, value)
                time.sleep(RELOAD_SETTLE_SECONDS)
                p = await _player(game_client, target)
                if p is not None and abs(p.sensitivity_factor - value) <= 0.01:
                    observed_intermediate = True

            # Final value must converge (clamped range is 0.5..2.0, all in-range).
            await _wait_for_sensitivity_factor(game_client, target, sequence[-1])
            assert observed_intermediate, (
                "no intermediate write was ever observed — file watch may be stuck"
            )

            # And the intervention event stream kept firing across all writes.
            applied = [
                e for e in _intervention_events(collector, "adjust_player_sensitivity")
                if e.data.get("blocked") == "false"
            ]
            assert len(applied) >= 2, (
                f"expected multiple applied events across writes, got {len(applied)}"
            )
    finally:
        await channel.close()
