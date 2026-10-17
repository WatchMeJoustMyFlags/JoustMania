"""
End-to-end intervention-flow integration tests (#730, UC-053).

Proves the full agent intervention chain works against the real services:

    write to the flag service  ->  flagd gRPC sync push  ->  game coordinator
    InterventionManager evaluates + enforces  ->  effect applied to the live
    game  ->  effect observable in the OTel/EventBus signals.

The agent ActionSink (services/agent/actions, Go) is NOT run in the integration
compose (it lives in the ``agent`` profile). These tests send the SAME
flag-service writes the Go writer sends: a game-scoped value
(``PUT /interventions/<flag>`` with ``scope.game_id`` [+ ``serial``]) or a
one-shot event (``POST``, the serial in the payload). The flag service renders
them as targeting on the game id, so every write reaches its own game only.

Observable signals used (no Prometheus pull needed — game-coordinator metrics
go OTLP->collector->Prometheus, a slow/flaky push path for assertions):

- ``StreamGameEvents`` ``agent_intervention`` events carry
  ``{type, target, blocked, block_reason}`` — the EventBus mirror of the
  ``game_interventions_total{type,objective,blocked}`` counter. Every applied
  or blocked intervention emits exactly one.
- ``GetGameState`` exposes per-player ``sensitivity_factor`` and ``alive`` —
  the applied state of difficulty/lifecycle interventions.

flagd's server-side evaluation of the written per-serial targeting if-ladder is
checked against a single real flagd in the contract tier
(tests/contract/test_flagd_resolution.py, #1422), not here.

Interventions are game-scoped and die with their game (UC-053 BR-003); agent-set
overlays (permission level, budgets) are cleared by the ``flag_service`` fixture.
"""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2  # noqa: E402

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    active_flag_file,
    flagd_probe_client,
    get_game_client,
    get_mock_controller_serials,
    poll_until,
    setup_mock_controllers,
    start_game_via_menu,
)
from tests.integration.flag_service import append_event, set_player_value  # noqa: E402
from tests.integration.flag_service import default as flag_service_default  # noqa: E402

# The agent set's baseline file (read-only: expected variant values only).
AGENT_PATH = active_flag_file("agent")

# Absence window: how long the flag push + coordinator re-evaluate chain is
# given to (not) act before asserting that NOTHING happened. Positive waits are
# condition polls (#894, see helpers.poll_until); this constant remains ONLY for
# negative assertions — "the blocked/reverted/re-read write produced no effect"
# cannot be event-driven, the bounded wait IS the test.
RELOAD_SETTLE_SECONDS = 2.0

# Mutating a per-player state flag and observing the applied sensitivity_factor
# can take a couple of reload cycles under load; poll up to this long.
APPLY_TIMEOUT_SECONDS = 12.0

# =============================================================================
# Agent-set helpers: in-memory overlays on the flag service (cleared by the
# ``flag_service`` fixture at teardown; never persisted, re-review N4)
# =============================================================================


def set_interventions_allowed(variant: str) -> None:
    """Set the agent-set ``interventions_allowed`` flag to a named variant
    (``none`` / ``ambient`` / ``standard`` / ``full``)."""
    flag_service_default().put("agent", "interventions_allowed", variant=variant)


def set_policy_budget(per_minute: int) -> None:
    """Raise BOTH weighted rate-limit budgets for headroom (#919).

    Rate limiting is enforced in two layers that read DIFFERENT flags (the
    defense-in-depth contract): the agent's authoritative per-game budget
    (``policy.max_interventions_per_minute``) and the coordinator's generous
    process-global backstop (``policy.coordinator_backstop_per_minute``). Tests
    that drive several interventions in a tight window need headroom in BOTH,
    so both get the value (registry range 1 to 100, flags.md §13.2).
    """
    for flag_key in ("policy.max_interventions_per_minute", "policy.coordinator_backstop_per_minute"):
        flag_service_default().put("agent", flag_key, value=int(per_minute))


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
def intervention_env(docker_compose, flag_service):
    """Bind the flag-service client and pin the suite-wide rate-limit budget.

    Interventions are written through the flag service and die with their game;
    every agent-set overlay a test sets (the budget, ``interventions_allowed``)
    is DELETEd by the ``flag_service`` fixture at teardown. The budget is pinned
    up front and served by flagd before the test body runs, so the
    process-global rate limiter is headroom-independent of test order (#894:
    condition poll, not a fixed settle).
    """
    set_policy_budget(SUITE_RATE_LIMIT_BUDGET)
    from openfeature.evaluation_context import EvaluationContext

    with flagd_probe_client(docker_compose, "agent") as client:
        assert poll_until(
            lambda: (
                client.get_integer_value("policy.max_interventions_per_minute", -1, EvaluationContext())
                == SUITE_RATE_LIMIT_BUDGET
            ),
        ), "flagd did not serve the pinned suite rate-limit budget"
    yield


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
    game_client,
    serial: str,
    expected: float,
    *,
    tol: float = 0.01,
    timeout: float = APPLY_TIMEOUT_SECONDS,
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
# Per-player targeting resolution against LIVE flagd
# =============================================================================


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
@pytest.mark.asyncio
async def test_per_player_targeting_resolves_against_live_flagd(
    intervention_env, flag_service, docker_compose
):
    """Per-player values written through the flag service resolve on LIVE flagd
    RPC for their (game, serial) only: other serials and other games get the
    neutral default. Catches targeting the service renders but flagd rejects.
    """
    from openfeature.evaluation_context import EvaluationContext

    game_a, game_b = "game_itpertarget0a", "game_itpertarget0b"
    serial_a, serial_b, unknown = "SER_A", "SER_B", "SER_UNKNOWN"
    for game_id in (game_a, game_b):
        status, body = flag_service.request(
            "PUT",
            f"/games/{game_id}",
            {
                "coordinator_instance": "integration/targeting",
                "game_kind": "shadow",
                "serials": [serial_a, serial_b],
            },
        )
        assert status == 200, body
    try:
        set_player_value("player_sensitivity_factor", game_a, serial_a, 1.5)
        set_player_value("player_sensitivity_factor", game_a, serial_b, 0.7)

        # resolver="rpc" deliberately: flagd's server-side evaluation of the
        # rendered targeting (in-process would evaluate it client-side).
        with flagd_probe_client(
            docker_compose, "interventions", resolver="rpc"
        ) as client:

            def _eval(game_id: str, serial: str) -> float:
                return client.get_float_value(
                    "player_sensitivity_factor",
                    1.0,
                    EvaluationContext(
                        targeting_key=serial, attributes={"gameId": game_id}
                    ),
                )

            assert poll_until(
                lambda: (
                    abs(_eval(game_a, serial_a) - 1.5) <= 0.01
                    and abs(_eval(game_a, serial_b) - 0.7) <= 0.01
                ),
                APPLY_TIMEOUT_SECONDS,
            ), (
                f"game_a: serial_a={_eval(game_a, serial_a)} (want 1.5), serial_b={_eval(game_a, serial_b)} (want 0.7)"
            )
            assert abs(_eval(game_a, unknown) - 1.0) <= 0.01, (
                "an unknown serial must resolve the default"
            )
            assert abs(_eval(game_b, serial_a) - 1.0) <= 0.01, (
                "another game must resolve the default"
            )
    finally:
        for game_id in (game_a, game_b):
            flag_service.request("DELETE", f"/games/{game_id}")


# =============================================================================
# Scenario 4: permission-block negative
# =============================================================================


@pytest.mark.asyncio
async def test_permission_layer_blocks_disallowed_intervention(
    intervention_env, docker_compose, game_client
):
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

            game_id = await collector.wait_for_game_id()
            append_event("eliminate_player", game_id, {"serial": victim})

            evt = await _wait_for_intervention_event(
                collector, "eliminate_player", blocked="true"
            )
            assert evt.data.get("blocked") == "true"
            assert evt.data.get("block_reason") == "not_allowed", (
                f"unexpected block_reason: {evt.data.get('block_reason')}"
            )

            # Game state unchanged: the blocked intervention never killed anyone.
            # Absence assertion — kept as a bounded wait by design (#894): there
            # is no event to wait for when the correct behavior is "nothing".
            await asyncio.sleep(RELOAD_SETTLE_SECONDS)
            assert (await _player(game_client, victim)).alive, (
                "blocked intervention still killed the player"
            )
    finally:
        await channel.close()
