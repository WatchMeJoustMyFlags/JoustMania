"""A death that keeps the player in the game is no elimination (UC-041 BR-008; #2524).

NonStop (UC-026 BR-004), Zombies (UC-023 BR-007) and Swapper (UC-024 BR-005) apply
the Fight Club rule (#2517): such a death sets no ``game_player_elimination_order`` and
its ``player_death`` span event carries ``death.eliminated`` false, so the agent's
elimination sequence stays empty and its elimination fraction 0. A player who leaves
the game for good (a NonStop or Swapper disconnect) is the elimination: its order is
recorded once and a disconnect death carries ``death.eliminated`` true.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop  # noqa: E402

from services.game_coordinator import game_clock  # noqa: E402
from services.game_coordinator.games.base import GameState  # noqa: E402
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame  # noqa: E402
from services.game_coordinator.games.swapper import SwapperGame  # noqa: E402
from services.game_coordinator.games.zombie import ZombieGame  # noqa: E402

BASE_METRICS = "services.game_coordinator.games.base.metrics"


class RecordingStream:
    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


def _with_spans(game) -> None:
    for player in game.players.values():
        player.span = MagicMock()


def _death_flags(span) -> list:
    """``death.eliminated`` of every ``player_death`` event on a player span (None when absent)."""
    return [
        c.kwargs["attributes"].get("death.eliminated")
        for c in span.add_event.call_args_list
        if c.args and c.args[0] == "player_death"
    ]


def _orders(base_metrics) -> list[tuple[str, int]]:
    gauge = base_metrics.player_elimination_order
    sets = gauge.labels.return_value.set.call_args_list
    return [
        (labels.kwargs["serial"], set_call.args[0])
        for labels, set_call in zip(gauge.labels.call_args_list, sets, strict=True)
    ]


# ---------------------------------------------------------------------------
# NonStop (UC-026 BR-004)
# ---------------------------------------------------------------------------


async def _nonstop(num_players: int = 4) -> NonstopJoustGame:
    client = MockControllerManagerService(num_controllers=num_players, death_schedule={})
    game = NonstopJoustGame(
        controller_manager_client=client,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_nonstop_no_elim",
    )
    await game._initialize_players_impl(client.controllers)
    game.gameplay_stream = RecordingStream()
    game.state = GameState.RUNNING
    game.running = True
    game._play_sound = async_noop
    _with_spans(game)
    return game


class TestNonstopRespawnDeathIsNoElimination:
    @pytest.mark.uc("UC-026", flow="main", steps=[2], br=["BR-004"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_respawn_deaths_record_no_elimination_order(self):
        """4 players die and respawn 3 times each: 12 deaths, no elimination order, ``death.eliminated`` false."""
        game = await _nonstop(4)
        rounds = 3

        with patch(BASE_METRICS) as base_metrics:
            for _ in range(rounds):
                for serial in game.players:
                    assert await game._kill_player(serial, 5.0) is True
                    assert await game._respawn_player(serial) is True

            base_metrics.player_elimination_order.labels.assert_not_called()

        assert game._elimination_count == 0
        assert game.dead_count == rounds * len(game.players)
        for player in game.players.values():
            assert player.deaths == rounds
            assert _death_flags(player.span) == [False] * rounds

    @pytest.mark.uc("UC-026", flow="main", steps=[2], br=["BR-004"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_an_agent_eliminate_respawns_and_is_no_elimination(self):
        game = await _nonstop(3)
        serial = next(iter(game.players))

        with patch(BASE_METRICS) as base_metrics:
            assert await game._kill_player(serial, 0.0, reason="agent_intervention") is True
            base_metrics.player_elimination_order.labels.assert_not_called()

        assert _death_flags(game.players[serial].span) == [False]

    @pytest.mark.uc("UC-026", flow="A2", br=["BR-004"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_a_disconnect_death_is_the_elimination(self):
        """After two respawn deaths a disconnect takes the player out: order 1, that death says eliminated."""
        game = await _nonstop(3)
        serial = next(iter(game.players))

        with patch(BASE_METRICS) as base_metrics:
            for _ in range(2):
                assert await game._kill_player(serial, 5.0) is True
                assert await game._respawn_player(serial) is True
            await game._handle_disconnect(serial)

            assert _orders(base_metrics) == [(serial, 1)]

        assert game._elimination_count == 1
        assert _death_flags(game.players[serial].span) == [False, False, True]

    @pytest.mark.uc("UC-026", flow="A2", br=["BR-004"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_a_disconnect_while_waiting_to_respawn_records_the_order_once_without_a_death(self):
        game = await _nonstop(3)
        serial = next(iter(game.players))

        with patch(BASE_METRICS) as base_metrics:
            assert await game._kill_player(serial, 5.0) is True
            await game._handle_disconnect(serial)
            await game._handle_disconnect(serial)  # a repeated disconnect changes nothing

            assert _orders(base_metrics) == [(serial, 1)]

        assert game.players[serial].deaths == 1
        assert _death_flags(game.players[serial].span) == [False]

    @pytest.mark.uc("UC-026", flow="A2", br=["BR-003", "BR-004"])
    @pytest.mark.asyncio
    async def test_a_disconnect_while_waiting_after_the_latch_records_no_order(self):
        game = await _nonstop(3)
        serial = next(iter(game.players))

        with patch(BASE_METRICS) as base_metrics:
            assert await game._kill_player(serial, 5.0) is True
            game._latch_game_outcome()
            await game._handle_disconnect(serial)

            base_metrics.player_elimination_order.labels.assert_not_called()


# ---------------------------------------------------------------------------
# Zombies (UC-023 BR-007)
# ---------------------------------------------------------------------------


async def _zombies(num_players: int = 6) -> ZombieGame:
    client = MockControllerManagerService(num_controllers=num_players, death_schedule={}, max_duration=10.0)
    game = ZombieGame(
        controller_manager_client=client,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_zombie_no_elim",
    )
    await game._initialize_players_impl(client.controllers)
    game.gameplay_stream = RecordingStream()
    game.running = True
    game._play_sound = async_noop
    _with_spans(game)
    return game


class TestZombiesDeathIsNoElimination:
    @pytest.mark.uc("UC-023", flow="main", steps=[3], br=["BR-007"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_a_human_turned_zombie_is_no_elimination(self):
        """Every human but one is converted: no elimination order, every ``player_death`` says not eliminated."""
        game = await _zombies(6)
        humans = list(game.human_serials)
        converted = humans[:-1]

        with patch(BASE_METRICS) as base_metrics:
            for serial in converted:
                assert await game._kill_player(serial, 3.0) is True
                game.players[serial].grace_until = 0.0

            base_metrics.player_elimination_order.labels.assert_not_called()

        assert game._elimination_count == 0
        for serial in converted:
            assert game.players[serial].alive is True
            assert game.players[serial].is_zombie is True
            assert _death_flags(game.players[serial].span) == [False]
        await game.cleanup()

    @pytest.mark.uc("UC-023", flow="main", steps=[3, 4], br=["BR-007"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_zombie_deaths_and_disconnects_are_no_elimination(self):
        """A zombie dies, respawns and dies again; a human and a zombie disconnect: still no elimination order."""
        game = await _zombies(6)
        zombie = game.zombie_serials[0]
        other_zombie = game.zombie_serials[1]
        human = game.human_serials[0]

        with patch(BASE_METRICS) as base_metrics:
            assert await game._kill_player(zombie, 5.0) is True
            assert await game._respawn_player(zombie, cause="revive") is True
            game.players[zombie].grace_until = 0.0
            assert await game._kill_player(zombie, 5.0) is True
            await game._handle_disconnect(human)
            await game._handle_disconnect(other_zombie)

            base_metrics.player_elimination_order.labels.assert_not_called()

        assert game._elimination_count == 0
        assert _death_flags(game.players[zombie].span) == [False, False]
        assert _death_flags(game.players[human].span) == [False]
        assert _death_flags(game.players[other_zombie].span) == [False]
        await game.cleanup()


# ---------------------------------------------------------------------------
# Swapper (UC-024 BR-005)
# ---------------------------------------------------------------------------


async def _swapper(num_players: int = 4) -> SwapperGame:
    client = MockControllerManagerService(num_controllers=num_players, death_schedule={}, max_duration=10.0)
    game = SwapperGame(
        controller_manager_client=client,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_swapper_no_elim",
    )
    await game._initialize_players_impl(client.controllers)
    await game._assign_team_colors()
    game.gameplay_stream = RecordingStream()
    game.running = True
    game.state = GameState.RUNNING
    game.start_time = game_clock.now() - 5.0
    game._play_sound = async_noop
    game._swap_pause = async_noop
    _with_spans(game)
    return game


def _swap_flags(serial: str, spans: dict) -> list:
    """``death.eliminated`` across every span the player had (a swap ends the old span)."""
    return [flag for span in spans[serial] for flag in _death_flags(span)]


class TestSwapperSwapIsNoElimination:
    @pytest.mark.uc("UC-024", flow="main", steps=[2], br=["BR-005"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_swaps_record_no_elimination_order(self):
        game = await _swapper(4)
        first, second = "mock_controller_0", "mock_controller_1"
        spans = {first: [game.players[first].span], second: [game.players[second].span]}

        with patch(BASE_METRICS) as base_metrics:
            for serial in (first, second, first):
                assert await game._kill_player(serial, 3.0) is True
                game.players[serial].grace_until = 0.0
                # The swap ended the span and opened a new one: track a mock again.
                game.players[serial].span = MagicMock()
                spans[serial].append(game.players[serial].span)

            base_metrics.player_elimination_order.labels.assert_not_called()

        assert game._elimination_count == 0
        assert _swap_flags(first, spans) == [False, False]
        assert _swap_flags(second, spans) == [False]
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-005"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_a_disconnected_player_is_the_elimination(self):
        game = await _swapper(4)
        swapped, gone = "mock_controller_0", "mock_controller_2"
        gone_span = game.players[gone].span

        with patch(BASE_METRICS) as base_metrics:
            assert await game._kill_player(swapped, 3.0) is True
            await game._handle_disconnect(gone)

            assert _orders(base_metrics) == [(gone, 1)]

        assert _death_flags(gone_span) == [True]
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", steps=[3], br=["BR-005"])
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.asyncio
    async def test_a_disconnect_during_its_own_swap_records_the_order_once(self):
        game = await _swapper(4)
        target = "mock_controller_0"
        span = game.players[target].span
        paused, release = asyncio.Event(), asyncio.Event()

        async def gated_pause():
            paused.set()
            await release.wait()

        game._swap_pause = gated_pause

        with patch(BASE_METRICS) as base_metrics:
            kill = asyncio.create_task(game._kill_player(target, 3.0))
            await asyncio.wait_for(paused.wait(), 2.0)
            await game._handle_disconnect(target)
            assert _orders(base_metrics) == [(target, 1)]
            release.set()
            assert await asyncio.wait_for(kill, 2.0) is True

            assert _orders(base_metrics) == [(target, 1)]

        assert game.players[target].alive is False
        # The swap is the only death: it started before the disconnect, so it says not eliminated.
        assert _death_flags(span) == [False]
        await game.cleanup()
