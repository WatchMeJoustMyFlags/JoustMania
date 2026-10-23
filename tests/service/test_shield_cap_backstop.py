"""
The coordinator's shield cap holds against any writer (#2044, UC-040 A34, BR-022).

Service tier (T2, ``GAME`` composition: controller manager + game coordinator from
source on a fresh flag plane). No agent runs: the test is the misbehaving writer and
sends the agent's flag-service write (``shield_seconds`` for one serial) four times.

- The cooldown is overlaid to 0 (off), so only the cap (default 3) refuses.
- The writes are spaced: each waits for its own ``agent_intervention`` event, since
  two writes pushed in one flagd configuration collapse into one (UC-040 #1323).
- Expect 3 applied (``blocked=false``) and the 4th ``blocked=true`` with
  ``shield_cap_reached``. The next game grants again (the count is per game).
"""

import asyncio
import time

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import game_coordinator_pb2
from tests.integration.flag_service import set_player_value, variant_value
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

TAG = "shield-cap-backstop"
# Weighted rate-limit headroom for the coordinator backstop (flags.md §13.2).
RATE_LIMIT_BUDGET = 50
EVENT_TIMEOUT_SECONDS = 12.0
RUNNING_TIMEOUT_SECONDS = 12.0
COLORS_TIMEOUT_SECONDS = 10.0
DEFAULT_CAP = 3


def _serve_agent_overlays(stack, flag_service) -> None:
    """Allow grant_shield, pin the backstop budget, cooldown off; wait until flagd serves them."""
    flag_service.put("agent", "policy.coordinator_backstop_per_minute", value=RATE_LIMIT_BUDGET)
    flag_service.put("agent", "policy.shield_cooldown_seconds", value=0)
    flag_service.put("agent", "interventions_allowed", variant="standard")
    allowed = variant_value("agent", "interventions_allowed", "standard")
    with flagd_probe_client(stack, "agent") as client:
        ctx = EvaluationContext()
        assert poll_until(
            lambda: (
                client.get_integer_value("policy.shield_cooldown_seconds", -1, ctx) == 0
                and client.get_integer_value("policy.coordinator_backstop_per_minute", -1, ctx) == RATE_LIMIT_BUDGET
                and client.get_string_value("interventions_allowed", "", ctx) == allowed
            )
        ), "flagd did not serve the shield-cap overlays"
        assert client.get_integer_value("policy.max_shields_per_player_per_game", -1, ctx) == DEFAULT_CAP


async def _start_running_game(game_client, mock_client, serials, headless_cleanup):
    game_id, collector = await start_game_headless(game_client, build_start_config("JoustFFA", serials), timeout=25.0)
    headless_cleanup.add_game(game_id)
    deadline = time.monotonic() + RUNNING_TIMEOUT_SECONDS
    while not any(
        g.game_id == game_id and g.state == game_coordinator_pb2.RUNNING for g in await list_games(game_client)
    ):
        if time.monotonic() > deadline:
            raise AssertionError(f"game {game_id} never reached RUNNING")
        await asyncio.sleep(0.2)
    deadline = time.monotonic() + COLORS_TIMEOUT_SECONDS
    while True:
        try:
            await verify_controllers_have_color(mock_client, serials)
            break
        except AssertionError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.2)
    return game_id, collector


def _shield_events(collector) -> list:
    return [e for e in collector.get_events("agent_intervention") if e.data.get("type") == "grant_shield"]


async def _write_and_wait(collector, game_id: str, serial: str, n: int):
    """Write one shield_seconds for ``serial`` and wait for the n-th grant_shield event."""
    set_player_value("shield_seconds", game_id, serial, 5)
    assert await async_poll_until(lambda: len(_shield_events(collector)) >= n, timeout=EVENT_TIMEOUT_SECONDS), (
        f"write {n}: no agent_intervention event; saw {[dict(e.data) for e in _shield_events(collector)]}"
    )
    return _shield_events(collector)[n - 1]


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A34", br=["BR-022", "BR-004"])
async def test_fourth_shield_is_refused_and_the_next_game_starts_fresh(stack, flag_service, headless_cleanup):
    _serve_agent_overlays(stack, flag_service)
    mock_client, mock_channel = await get_mock_client(stack)
    game_client, game_channel = await get_game_client(stack)
    headless_cleanup.add_tag(TAG)
    collectors = []
    game_ids: list[str] = []
    try:
        serials = await setup_mock_controllers(stack, count=4, reserved=True, tag=TAG)
        target = serials[0]
        game_id, collector = await _start_running_game(game_client, mock_client, serials, headless_cleanup)
        collectors.append(collector)
        game_ids.append(game_id)
        flag_service.wait_game_registered(game_id)

        # Three shields apply (cooldown off: each extends the running one).
        for n in range(1, DEFAULT_CAP + 1):
            evt = await _write_and_wait(collector, game_id, target, n)
            assert evt.data.get("blocked") == "false", f"write {n}: {dict(evt.data)}"

        # The fourth is refused by the coordinator's cap.
        evt = await _write_and_wait(collector, game_id, target, DEFAULT_CAP + 1)
        assert evt.data.get("blocked") == "true", dict(evt.data)
        assert evt.data.get("block_reason") == "shield_cap_reached", dict(evt.data)

        # The next game starts with no shields counted.
        await force_end_game_by_id(game_client, game_id, reason="shield_cap_first_game")
        game_ids.remove(game_id)
        game_id, collector = await _start_running_game(game_client, mock_client, serials, headless_cleanup)
        collectors.append(collector)
        game_ids.append(game_id)
        flag_service.wait_game_registered(game_id)
        evt = await _write_and_wait(collector, game_id, target, 1)
        assert evt.data.get("blocked") == "false", dict(evt.data)
    finally:
        for c in collectors:
            await c.stop()
        for gid in game_ids:
            await force_end_game_by_id(game_client, gid, reason="shield_cap_cleanup")
        await mock_channel.close()
        await game_channel.close()
