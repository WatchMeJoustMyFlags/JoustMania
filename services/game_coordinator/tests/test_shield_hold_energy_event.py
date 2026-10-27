"""
The ``player_shield_energy`` game event of the shield hold (#2096, UC-035 BR-008).

The outbound hold task publishes it at every transition it sends the controller,
so the dashboard bar follows the pool live; the frame path never publishes it.
No periodic tick: between two transitions the energy is linear in time, and the
event carries the rate.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_shield_hold_feedback import (  # noqa: E402 - path set above
    Clock,
    Flags,
    _make_ffa,
    drain,
    fill,
    start_holding,
    step,
)

from lib.types import GameEvent
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode

EVENT = GameEvent.PLAYER_SHIELD_ENERGY


@pytest.fixture(autouse=True)
def _no_shield_limits():
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


@pytest.fixture
def clock():
    c = Clock()
    with patch.object(base_module.time, "time", side_effect=lambda: c.t):
        yield c


@pytest.fixture
def flags():
    f = Flags()
    with (
        patch.object(base_module, "read_string_flag", side_effect=f.read),
        patch.object(base_module, "read_float_flag", side_effect=f.read),
        patch.object(base_module, "read_int_flag", side_effect=f.read),
    ):
        yield f


def _start(g, clock):
    """What the start of the game loop does (UC-040 BR-026, BR-027)."""
    g._latch_shield_hold()
    g._start_shield_hold_clock(clock.t)
    g._queue_hold_feedback_all()


@pytest_asyncio.fixture
async def game(clock, flags):  # noqa: ARG001 - flags patch base for the test
    g = _make_ffa()
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        _start(g, clock)
        yield g
        g._cancel_hold_feedback()
        await drain()


def events(game, serial=None):
    collector = game.event_publisher.__self__
    return [d for d in collector.get_events_of_type(EVENT) if serial is None or d["serial"] == serial]


def clear_events(game):
    game.event_publisher.__self__.clear()


class TestUC035HoldEnergyEvent:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    async def test_game_start_publishes_every_players_pool(self, game):
        """UC-035 BR-008: the start of the game loop publishes each player's empty pool with the latched values."""
        await drain()
        for serial in ("p1", "p2"):
            (event,) = events(game, serial)
            assert event == {
                "serial": serial,
                "state": "empty",
                "source": "none",
                "energy_ms": "0",
                "full_ms": "5000",
                "cap_ms": "5000",
                "min_burst_ms": "300",
                "budget_ms": "15000",
                "budget_remaining_ms": "15000",
                "rate_ms_per_s": "0",
                "remaining_ms": "0",
            }

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_activation_publishes_active_draining_with_the_time_left(self, game, clock):
        """UC-035 BR-008: an activation is `active`, draining at 1 s/s, ending after the usable energy."""
        await drain()
        clear_events(game)
        await start_holding(game, clock, energy=3.0)
        await drain()

        (event,) = events(game, "p1")
        assert event["state"] == "active"
        assert event["energy_ms"] == "3000"
        assert event["rate_ms_per_s"] == "-1000"
        assert event["remaining_ms"] == "3000"
        assert event["source"] == "none"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_release_publishes_the_energy_left_idle(self, game, clock):
        """UC-035 BR-008: a release publishes the charged state with what is left and the budget used."""
        await start_holding(game, clock, energy=3.0)
        await drain()
        clear_events(game)
        for _ in range(60):  # hold for 1 s
            await step(game, clock, "p1", True)
        await step(game, clock, "p1", False)
        await drain()

        (event,) = events(game, "p1")
        assert event["state"] == "charged"
        assert 1950 <= int(event["energy_ms"]) <= 2000
        assert 13950 <= int(event["budget_remaining_ms"]) <= 14000
        assert event["rate_ms_per_s"] == "0"
        assert event["remaining_ms"] == "0"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-040", flow="A39")
    async def test_running_out_publishes_empty(self, game, clock):
        """UC-035 BR-008: held until the energy ran out: `empty` at 0."""
        await start_holding(game, clock, energy=0.5)
        await drain()
        clear_events(game)
        for _ in range(40):
            await step(game, clock, "p1", True)
        await drain()

        (event,) = events(game, "p1")
        assert (event["state"], event["energy_ms"]) == ("empty", "0")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-024"])
    async def test_a_used_up_budget_publishes_exhausted(self, game, clock):
        """UC-035 BR-008: less than a minimum burst of budget left is `exhausted` (budget used up)."""
        game.players["p1"].hold_budget_used = 14.5
        await start_holding(game, clock, energy=5.0)
        await drain()
        clear_events(game)
        for _ in range(40):
            await step(game, clock, "p1", True)
        await drain()

        (event,) = events(game, "p1")
        assert event["state"] == "exhausted"
        assert event["budget_remaining_ms"] == "0"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-040", flow="A36")
    async def test_a_grant_publishes_source_agent(self, game):
        """UC-035 BR-008: a grant marks the source `agent`; the next event without a grant has none."""
        await drain()
        clear_events(game)
        assert await game.grant_shield("p1", 2.0)
        await drain()

        (event,) = events(game, "p1")
        assert (event["state"], event["source"], event["energy_ms"]) == ("charged", "agent", "2000")

        clear_events(game)
        game._queue_hold_feedback(game.players["p1"])
        await drain()
        assert events(game, "p1")[0]["source"] == "none"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-040", flow="A38")
    async def test_recharging_publishes_the_rate_and_the_full_level(self, clock, flags):
        """UC-035 BR-008: a recharging pool is `recharging`, source `recharge`, with the rate; no tick follows."""
        flags.values["shield.recharge_per_second"] = 0.25
        g = _make_ffa()
        _start(g, clock)
        await drain()
        (event,) = events(g, "p1")
        assert (event["state"], event["source"], event["rate_ms_per_s"]) == ("recharging", "recharge", "250")
        assert event["full_ms"] == "5000"

        clock.t += 2.0  # energy changes, but no transition: nothing is published
        await asyncio.sleep(0.01)
        await drain()
        assert len(events(g, "p1")) == 1
        g._cancel_hold_feedback()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    async def test_a_dead_player_and_an_ended_game_publish_nothing(self, game, clock):
        """UC-035 BR-008: the dashboard clears on player_death and game end; no energy event follows them."""
        await drain()
        clear_events(game)
        game.players["p1"].alive = False
        game._queue_hold_feedback(game.players["p1"])
        await drain()
        assert events(game, "p1") == []

        await game._end_shield_feedback()
        game._queue_hold_feedback(game.players["p2"])
        await drain()
        assert events(game) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-019"])
    async def test_a_trigger_game_publishes_no_energy_event(self, clock, flags):
        """UC-035 BR-008: no hold game, no energy event, so the dashboard shows no bar."""
        flags.values["shield.activation"] = "trigger"
        g = _make_ffa()
        _start(g, clock)
        await drain()
        fill(g, "p1", 3.0)
        await step(g, clock, "p1", False)
        await step(g, clock, "p1", True)
        await drain()
        assert events(g) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_the_frame_path_never_publishes(self, game, clock):
        """UC-030 BR-008: a frame only marks the player; the event goes out from the outbound task."""
        await drain()
        clear_events(game)
        await start_holding(game, clock, energy=3.0)
        assert events(game) == []  # nothing yet: the frame returned without publishing
        await drain()
        assert len(events(game, "p1")) == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", br=["BR-008"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_a_failing_publisher_keeps_the_controller_feedback(self, game, clock):
        """UC-035 BR-008: the dashboard event is best effort; the controller still gets its hold command."""
        await drain()
        game.gameplay_stream.writes.clear()
        game.event_publisher = AsyncMock(side_effect=RuntimeError("bus down"))
        await start_holding(game, clock, energy=3.0)
        await drain()
        assert [n for n, _, _ in game.gameplay_stream.effects("p1")] == ["GAME_EFFECT_SHIELD_HOLD"]
