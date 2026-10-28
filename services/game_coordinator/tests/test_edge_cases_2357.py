"""Game-coordinator edge cases from #2357.

- B4 (UC-030 BR-010): game deadlines run on the monotonic game clock, so a
  wall-clock step neither ends nor extends a running grace; ``until_ms`` and
  ``expires_ms`` stay wall-clock on the wire.
- B2 (UC-020 failure postcondition): a base-run mode whose ``run()`` raises or is
  cancelled still stops the game music and ends the shield feedback.
- B5: the intervention manager keeps a reference to every task it creates.
- B6: ``RuntimeConfigManager.get_config()`` returns an immutable snapshot.
"""

import asyncio
import dataclasses
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from proto import audio_pb2
from services.game_coordinator import game_clock, interventions
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode, GameEvent, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.runtime_config import RuntimeConfigManager


class _Stream:
    """Gameplay stream that accepts writes silently."""

    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)


class _Audio:
    """Audio client fake that records its calls."""

    def __init__(self):
        self.calls: list[str] = []

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("PlaySound")
        return audio_pb2.PlaySoundResponse(success=True)

    async def PlayMusic(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("PlayMusic")
        return audio_pb2.PlayMusicResponse(track_id="track-1", success=True)

    async def StopMusic(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("StopMusic")
        return audio_pb2.StopMusicResponse(success=True)

    async def SetVolume(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("SetVolume")
        return audio_pb2.SetVolumeResponse(success=True)

    async def ChangeTempo(self, request):  # noqa: N802 - gRPC stub name
        self.calls.append("ChangeTempo")
        return audio_pb2.ChangeTempoResponse(success=True)


def _ffa(events=None, audio=None) -> FFAGame:
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=(events or EventCollector()).publish,
        audio_client=audio,
        game_id="test_2357",
    )
    game.gameplay_stream = _Stream()
    return game


async def _ffa_with_players(events=None) -> FFAGame:
    game = _ffa(events)
    for serial in ("p1", "p2"):
        game.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return game


@pytest.fixture(autouse=True)
def _no_shield_limits():
    """Shield limits off (UC-040 BR-022): these tests are not about the cap or the cooldown."""
    with (
        patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)),
        patch.object(BaseGameMode, "_shield_activation_mode", return_value=base_module.SHIELD_ACTIVATION_INSTANT),
    ):
        yield


# --------------------------------------------------------------------------- #
# B4: the game clock
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-030", flow="A1", br=["BR-010"])
class TestGameClock:
    @pytest.mark.asyncio
    async def test_wall_clock_step_forward_does_not_end_a_running_grace(self):
        """An NTP step of +1 h after a 5 s shield leaves the shield running."""
        game = await _ffa_with_players()
        serial = next(iter(game.players))
        player = game.players[serial]
        assert await game.grant_shield(serial, 5.0) is True
        assert game.is_in_grace(player)

        stepped = time.time() + 3600.0
        with patch.object(time, "time", return_value=stepped):
            assert game.is_in_grace(player), "a wall-clock step ended the grace"

    @pytest.mark.asyncio
    async def test_wall_clock_step_back_does_not_extend_an_ended_grace(self):
        """A step back of 1 h does not reopen a grace that ended on the game clock."""
        game = await _ffa_with_players()
        serial = next(iter(game.players))
        player = game.players[serial]
        assert await game.grant_shield(serial, 5.0) is True

        later = game_clock.now() + 6.0
        stepped_back = time.time() - 3600.0
        with (
            patch.object(game_clock, "now", return_value=later),
            patch.object(time, "time", return_value=stepped_back),
        ):
            assert not game.is_in_grace(player), "a wall-clock step back extended the grace"

    @pytest.mark.asyncio
    async def test_until_ms_is_wall_clock_after_a_step(self):
        """UC-040 step 3: ``until_ms`` is wall-clock epoch ms even when the wall clock stepped since start."""
        events = EventCollector()
        game = await _ffa_with_players(events)
        serial = next(iter(game.players))

        # The wall clock stepped 100 s back since the clock was anchored: the
        # game clock reads 100 s ahead of time.time().
        wall = time.time()
        with (
            patch.object(game_clock, "now", return_value=wall + 100.0),
            patch.object(time, "time", return_value=wall),
        ):
            assert await game.grant_shield(serial, 5.0) is True

        shielded = events.get_events_of_type(GameEvent.PLAYER_SHIELDED)[0]
        assert int(shielded["until_ms"]) == pytest.approx((wall + 5.0) * 1000, abs=5)
        # The deadline itself is on the game clock.
        assert game.players[serial].grace_until == pytest.approx(wall + 105.0)

    def test_game_clock_ignores_the_wall_clock(self):
        before = game_clock.now()
        with patch.object(time, "time", return_value=0.0):
            during = game_clock.now()
        assert during >= before


# --------------------------------------------------------------------------- #
# B2: a raising or cancelled base run() stops the music and the shield feedback
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-020", cond=["Post-F-1"])
class TestRunFailureTeardown:
    @pytest.mark.asyncio
    async def test_raising_run_stops_music_and_ends_shield_feedback(self):
        audio = _Audio()
        game = _ffa(audio=audio)
        end_feedback = AsyncMock()
        with (
            patch.object(game, "_initialize_players", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(game, "_end_shield_feedback", end_feedback),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await game.run()

        assert "StopMusic" in audio.calls
        end_feedback.assert_awaited_once()
        assert game.game_error_published

    @pytest.mark.asyncio
    async def test_cancelled_run_stops_music(self):
        audio = _Audio()
        game = _ffa(audio=audio)
        started = asyncio.Event()

        async def _hang():
            started.set()
            await asyncio.Event().wait()

        with patch.object(game, "_initialize_players", _hang):
            task = asyncio.create_task(game.run())
            await asyncio.wait_for(started.wait(), 5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert "StopMusic" in audio.calls

    @pytest.mark.asyncio
    async def test_normal_teardown_stops_music_once(self):
        """The finally does not repeat what the teardown step already did."""
        audio = _Audio()
        game = _ffa(audio=audio)
        game.running = True
        with (
            patch.object(game, "_initialize_players", AsyncMock()),
            patch.object(game, "_run_gameplay_phase", AsyncMock()),
            patch.object(game, "_start_gameplay_stream", AsyncMock()),
            patch.object(game, "_start_game_music", AsyncMock()),
            patch.object(game, "_end_game_impl", AsyncMock()),
            patch.object(game, "_end_shield_feedback", AsyncMock()) as end_feedback,
        ):
            game.players = {s: Player(serial=s, alive=True, color=(0, 255, 0)) for s in ("a", "b")}
            await game.run()

        assert audio.calls.count("StopMusic") == 1
        end_feedback.assert_awaited_once()


# --------------------------------------------------------------------------- #
# B5: the intervention manager keeps its tasks
# --------------------------------------------------------------------------- #
class TestInterventionTaskReferences:
    def _manager(self):
        return interventions.InterventionManager(event_publisher=AsyncMock(), get_game=lambda: None)

    @pytest.mark.asyncio
    async def test_flag_change_task_is_referenced_until_done(self):
        mgr = self._manager()
        gate = asyncio.Event()

        async def _evaluate():
            await gate.wait()

        with patch.object(mgr, "evaluate_all", _evaluate):
            mgr._on_flags_changed(None)
            assert len(mgr._background_tasks) == 1
            gate.set()
            await asyncio.gather(*mgr._background_tasks)
            await asyncio.sleep(0)
        assert not mgr._background_tasks

    @pytest.mark.asyncio
    async def test_deferral_recheck_task_is_referenced(self):
        mgr = self._manager()
        gate = asyncio.Event()

        async def _evaluate():
            await gate.wait()

        with (
            patch.object(mgr, "evaluate_all", _evaluate),
            patch.object(interventions, "DEFERRAL_RECHECK_S", 0),
        ):
            mgr._schedule_deferral_recheck()
            await asyncio.sleep(0.01)
            assert len(mgr._background_tasks) == 1
            gate.set()
            await asyncio.gather(*mgr._background_tasks)
            await asyncio.sleep(0)
        assert not mgr._background_tasks


# --------------------------------------------------------------------------- #
# B6: get_config() hands out an immutable snapshot
# --------------------------------------------------------------------------- #
class TestRuntimeConfigSnapshot:
    @patch("openfeature.api.add_handler")
    @patch("lib.feature_flags.get_flag_client")
    def test_snapshot_is_not_changed_by_a_refresh(self, mock_get_client, _mock_add_handler):
        client = MagicMock()
        mock_get_client.return_value = client
        client.get_integer_value.side_effect = [60, 3, 750, 3000]
        manager = RuntimeConfigManager()
        snapshot = manager.get_config()

        client.get_integer_value.side_effect = [30, 3, 500, 1000]
        manager._refresh_from_flags()

        assert snapshot.update_frequency_hz == 60
        assert snapshot.countdown_phase_duration_ms == 750
        fresh = manager.get_config()
        assert fresh.update_frequency_hz == 30
        assert fresh.countdown_phase_duration_ms == 500

    def test_snapshot_is_frozen(self):
        config = RuntimeConfigManager().get_config()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.update_frequency_hz = 15
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.analytics.zone_still_max = 0.5
