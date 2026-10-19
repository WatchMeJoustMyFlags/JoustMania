"""TC-018 on the service tier (T2): an agent intervention through the flag service.

Moved from tests/integration/test_flag_service_flows.py (#1524). Real flag
service (built from HEAD), flagd, controller manager (mock backend) and game
coordinator, all fresh for this module; audio is faked.

The coordinator registers the game (UC-053 main step 1), two eliminations
appended within 100 ms both apply exactly once (A4, BR-006), a later
configuration push replays nothing, the game's scope is dropped at its end
(step 7, BR-003) and a write for the ended game is refused (A6). The
coordinator's "failed read, no replay" step is a unit test
(services/game_coordinator/tests/test_event_list_reader.py).

The agent is not run: the test sends the exact requests of its writers
(services/agent/actions/writer.go).
"""

import asyncio
import time

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import game_coordinator_pb2
from tests.integration.flag_service import (
    FlagServiceError,
    append_event,
    nudge_sync,
    set_player_value,
    variant_value,
)
from tests.integration.flag_service import default as flag_service_default
from tests.integration.helpers import (
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    list_games,
    poll_until,
    setup_mock_controllers,
    start_game_headless,
    verify_controllers_have_color,
)
from tests.service.harness import GAME

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = GAME

TAG = "flag-service-flows"
# Weighted rate-limit headroom for both layers (agent budget + coordinator
# backstop, flags.md §13.2); the limiter is process-global in the coordinator.
RATE_LIMIT_BUDGET = 100
# Absence assertion window for "an unchanged event list is not applied again":
# the bounded wait IS the test (#894).
NO_REPLAY_WINDOW_SECONDS = 3.0
APPLY_TIMEOUT_SECONDS = 12.0
RUNNING_TIMEOUT_SECONDS = 12.0
COLORS_TIMEOUT_SECONDS = 10.0


# --------------------------------------------------------------------------- #
# Flag writes through the flag service (the agent's writer requests)
# --------------------------------------------------------------------------- #
def _pin_rate_limit_budget(stack) -> None:
    for flag_key in ("policy.max_interventions_per_minute", "policy.coordinator_backstop_per_minute"):
        flag_service_default().put("agent", flag_key, value=RATE_LIMIT_BUDGET)
    with flagd_probe_client(stack, "agent") as client:
        assert poll_until(
            lambda: (
                client.get_integer_value("policy.max_interventions_per_minute", -1, EvaluationContext())
                == RATE_LIMIT_BUDGET
            )
        ), "flagd did not serve the pinned rate-limit budget"


async def _serve_full_permission(stack) -> None:
    flag_service_default().put("agent", "interventions_allowed", variant="full")
    expected = variant_value("agent", "interventions_allowed", "full")
    with flagd_probe_client(stack, "agent") as client:
        assert await async_poll_until(
            lambda: client.get_string_value("interventions_allowed", "", EvaluationContext()) == expected
        ), "flagd did not serve interventions_allowed=full"


# --------------------------------------------------------------------------- #
# Game observation
# --------------------------------------------------------------------------- #
async def _await_running(game_client, game_id: str) -> None:
    async def running() -> bool:
        return any(
            g.game_id == game_id and g.state == game_coordinator_pb2.RUNNING for g in await list_games(game_client)
        )

    deadline = time.monotonic() + RUNNING_TIMEOUT_SECONDS
    while not await running():
        if time.monotonic() > deadline:
            raise AssertionError(f"game {game_id} never reached RUNNING within {RUNNING_TIMEOUT_SECONDS}s")
        await asyncio.sleep(0.2)


async def _wait_for_game_colors(mock_client, serials: list[str]) -> None:
    deadline = time.monotonic() + COLORS_TIMEOUT_SECONDS
    while True:
        try:
            await verify_controllers_have_color(mock_client, serials)
            return
        except AssertionError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.2)


async def _player(game_client, game_id: str, serial: str):
    resp = await game_client.GetGameState(game_coordinator_pb2.GetGameStateRequest(game_id=game_id))
    if not resp.success:
        return None
    return next((p for p in resp.game_info.players if p.serial == serial), None)


async def _wait_until_dead(game_client, game_id: str, serial: str) -> bool:
    deadline = time.monotonic() + APPLY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        p = await _player(game_client, game_id, serial)
        if p is not None and not p.alive:
            return True
        await asyncio.sleep(0.2)
    return False


def _intervention_events(collector, type_id: str, *, blocked=None) -> list:
    return [
        e
        for e in collector.get_events("agent_intervention")
        if e.data.get("type") == type_id and (blocked is None or e.data.get("blocked") == blocked)
    ]


def _registered(flag_service, game_id: str) -> bool:
    return any(g["game_id"] == game_id for g in flag_service.games())


@pytest.mark.asyncio
@pytest.mark.uc("UC-053", flow="main", br=["BR-003"], tc="TC-018")
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"], tc="TC-018")
@pytest.mark.uc("UC-053", flow="A6", tc="TC-018")
async def test_two_eliminations_through_the_flag_service(service_stack, flag_service, headless_cleanup):
    stack = service_stack
    _pin_rate_limit_budget(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    game_client, game_channel = await get_game_client(stack)
    headless_cleanup.add_tag(TAG)
    collector = None
    game_id = ""
    try:
        # Step 1: the host allows every intervention; a game starts with 4 players.
        await _serve_full_permission(stack)
        serials = await setup_mock_controllers(stack, count=4, reserved=True, tag=TAG)
        game_id, collector = await start_game_headless(
            game_client, build_start_config("JoustFFA", serials), timeout=25.0
        )
        headless_cleanup.add_game(game_id)
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # Step 2: the coordinator registered the game with its serials (FR-135).
        game = flag_service.wait_game_registered(game_id)
        assert sorted(game["serials"]) == sorted(serials), game

        # Step 3: two eliminations within 100 ms (one event list, two seqs).
        first, second = serials[0], serials[1]
        started = time.monotonic()
        seq_a = append_event("eliminate_player", game_id, {"serial": first})
        seq_b = append_event("eliminate_player", game_id, {"serial": second})
        assert time.monotonic() - started < 0.1, "the two writes took 100 ms or more"
        assert seq_b == seq_a + 1, (seq_a, seq_b)

        # Step 4: both apply, each exactly once.
        assert await _wait_until_dead(game_client, game_id, first), f"{first} alive"
        assert await _wait_until_dead(game_client, game_id, second), f"{second} alive"
        applied = len(_intervention_events(collector, "eliminate_player", blocked="false"))
        assert applied == 2, f"{applied} eliminations applied, want 2"

        # Step 5: a new configuration push with the list unchanged replays nothing.
        # Absence assertion: the bounded wait is the test (#894).
        nudge_sync()
        await asyncio.sleep(NO_REPLAY_WINDOW_SECONDS)
        assert len(_intervention_events(collector, "eliminate_player", blocked="false")) == applied, (
            "an unchanged event list was applied again"
        )
        assert (await _player(game_client, game_id, serials[2])).alive

        # Step 6: a serial outside the game is refused (A5/A6 checks of step 3).
        with pytest.raises(FlagServiceError) as refused:
            set_player_value("shield_seconds", game_id, "NOTINGAME", 5)
        assert (refused.value.status, refused.value.reason) == (422, "serial_not_in_game")

        # Step 7: the game ends; the flag service drops its scope (BR-003).
        await force_end_game_by_id(game_client, game_id, reason="tc018_end")
        assert await async_poll_until(lambda: not _registered(flag_service, game_id), timeout=10.0), (
            "the ended game is still registered"
        )
        view = flag_service.get("interventions", "eliminate_player")
        assert game_id not in (view.get("events") or {}), "the event list outlived its game"

        # Step 8: a late write for the ended game is refused (A6).
        with pytest.raises(FlagServiceError) as late:
            append_event("eliminate_player", game_id, {"serial": serials[2]})
        assert (late.value.status, late.value.reason) == (409, "game_not_live")
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="tc018_cleanup")
        await mock_channel.close()
        await game_channel.close()
