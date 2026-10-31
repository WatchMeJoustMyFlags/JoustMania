"""UC-010 step 6 / NFR-005: the pre-game countdown (BaseGameMode._countdown)."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import grpc
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.base import COUNTDOWN_BEEP_COUNT
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, RuntimeConfigManager
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

SERIALS = [f"mock_controller_{i}" for i in range(3)]


async def _game(events: EventCollector) -> FFAGame:
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3),
        event_publisher=events.publish,
        audio_client=None,
        game_id="game_countdown",
        initial_players=[type("P", (), {"serial": s})() for s in SERIALS],
    )
    await game._initialize_players()
    game.running = True
    game.gameplay_stream = create_autospec(grpc.aio.StreamStreamCall, instance=True)
    return game


def _config_manager(phase_duration_ms: int):
    manager = create_autospec(RuntimeConfigManager, instance=True)
    manager.get_config.return_value = GamePerformanceConfig(countdown_phase_duration_ms=phase_duration_ms)
    return manager


class TestCountdown:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[6])
    @pytest.mark.uc("UC-031", flow="main", steps=[4])
    async def test_three_phases_with_beeps_then_start_sound(self):
        """UC-010 step 6: a countdown effect per player, three beeps one phase apart, then the start sound."""
        events = EventCollector()
        game = await _game(events)
        sounds: list = []

        async def play_sound(sound, *_args, **_kwargs):
            sounds.append(sound)

        with (
            patch("services.game_coordinator.games.base.get_config_manager", return_value=_config_manager(100)),
            patch.object(game, "_play_sound", side_effect=play_sound),
            patch("services.game_coordinator.games.base.asyncio.sleep", AsyncMock()) as sleep,
        ):
            await game._countdown()

        effects = [c.args[0].game_effect for c in game.gameplay_stream.write.await_args_list]
        assert sorted(e.serial for e in effects) == sorted(SERIALS)
        assert {e.effect for e in effects} == {controller_manager_pb2.GAME_EFFECT_COUNTDOWN}
        assert {e.duration_ms for e in effects} == {100}

        assert COUNTDOWN_BEEP_COUNT == 3
        assert sounds == [Sound.SFX_BEEP_LOUD] * 3 + [Sound.SFX_START3]
        # One phase (100 ms) of 50 ms waits after each beep.
        assert sleep.await_count == 3 * (100 // 50)

        assert events.count_events_of_type("countdown_start") == 1
        assert events.get_events_of_type("countdown_start")[0] == {"phase_duration_ms": 100}
        assert events.count_events_of_type("countdown_end") == 1

    @pytest.mark.asyncio
    @pytest.mark.real_durations
    @pytest.mark.uc("UC-010", flow="main", steps=[6])
    @pytest.mark.nfr("NFR-005")
    async def test_default_phase_is_750_ms(self):
        """UC-010 step 6 / NFR-005: with the default config each phase (effect and beep gap) lasts 750 ms."""
        assert GamePerformanceConfig().countdown_phase_duration_ms == 750
        events = EventCollector()
        game = await _game(events)
        manager = create_autospec(RuntimeConfigManager, instance=True)
        manager.get_config.return_value = GamePerformanceConfig()

        with (
            patch("services.game_coordinator.games.base.get_config_manager", return_value=manager),
            patch.object(game, "_play_sound", AsyncMock()),
            patch("services.game_coordinator.games.base.asyncio.sleep", AsyncMock()) as sleep,
        ):
            await game._countdown()

        effects = [c.args[0].game_effect for c in game.gameplay_stream.write.await_args_list]
        assert {e.duration_ms for e in effects} == {750}
        assert events.get_events_of_type("countdown_start")[0] == {"phase_duration_ms": 750}
        # 3 beeps, each followed by 750 ms of 50 ms waits.
        assert sleep.await_count == 3 * 15
        assert {c.args[0] for c in sleep.await_args_list} == {0.05}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[6])
    @pytest.mark.nfr("NFR-005")
    async def test_zero_phase_duration_skips_countdown(self):
        """countdown_phase_duration_ms=0 skips effects and sounds but still brackets start/end."""
        events = EventCollector()
        game = await _game(events)

        with (
            patch("services.game_coordinator.games.base.get_config_manager", return_value=_config_manager(0)),
            patch.object(game, "_play_sound", AsyncMock()) as play_sound,
        ):
            await game._countdown()

        game.gameplay_stream.write.assert_not_awaited()
        play_sound.assert_not_awaited()
        assert events.count_events_of_type("countdown_end") == 1

    @pytest.mark.asyncio
    async def test_game_stopped_mid_countdown_plays_no_start_sound(self):
        """A game stopped during the countdown never plays the start sound."""
        events = EventCollector()
        game = await _game(events)
        sounds: list = []

        async def play_sound(sound, *_args, **_kwargs):
            sounds.append(sound)
            game.running = False

        with (
            patch("services.game_coordinator.games.base.get_config_manager", return_value=_config_manager(100)),
            patch.object(game, "_play_sound", side_effect=play_sound),
            patch("services.game_coordinator.games.base.asyncio.sleep", AsyncMock()),
        ):
            await game._countdown()

        assert sounds == [Sound.SFX_BEEP_LOUD]
        assert events.count_events_of_type("countdown_end") == 0
