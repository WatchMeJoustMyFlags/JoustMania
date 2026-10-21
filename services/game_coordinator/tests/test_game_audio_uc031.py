"""UC-031 Hear Game Audio, coordinator side (#1362, #1507).

- step 2: at game start the music plays at ``audio_volume.countdown_music``
  during the countdown, then at ``audio_volume.game``. Drives the real
  ``FFAGame.run()`` with the countdown, EMA warm-up and game loop stubbed, so only
  the audio calls ``run()`` makes are observed (in order, interleaved with the
  countdown).
- step 4: a full FFA game plays an explosion on each death and "congratulations"
  for the winner; the agent's ``play_audio_cue`` reaches the audio service as a
  ``PlaySound`` of the sound id (``agent_cue``).
- Post-F-1: audio failures never stop a game: a full FFA ``run()`` against an
  audio service whose every RPC fails still ends with a winner. A failing
  ``ChangeTempo`` (the intensity continues, the glide is resent) is covered by
  test_intensity_ramp_uc031.py (#1833).

Step 3 (1.0x / 1.3x alternation, thresholds follow) is asserted by
test_ffa_uc020.py::test_music_alternates_windows_and_death_threshold_follows_the_window.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from lib.types import GameEvent, Sound
from proto import audio_pb2, game_coordinator_pb2
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import FAST_MUSIC_SPEED, SLOW_MUSIC_SPEED
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext, InterventionManager
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

# Edge values (not the defaults 0.15 / 0.7), so the test proves the flags are used.
FLAG_VOLUMES = {"audio_volume.countdown_music": 0.25, "audio_volume.game": 0.9}


class RecordingAudio:
    """Stands in for ``audio_pb2_grpc.AudioServiceStub``; ``log`` is shared with the countdown stub."""

    def __init__(self, log: list):
        self.log = log
        self.durations: list[float] = []

    async def SetVolume(self, request):  # noqa: N802 - gRPC stub name
        self.log.append(("SetVolume", round(request.volume, 3)))
        return audio_pb2.SetVolumeResponse(success=True)

    async def PlayMusic(self, request):  # noqa: N802 - gRPC stub name
        self.log.append(("PlayMusic", request.file_pattern))
        return audio_pb2.PlayMusicResponse(track_id="track-1", success=True)

    async def StopMusic(self, request):  # noqa: N802 - gRPC stub name
        self.log.append(("StopMusic", request.track_id))
        return audio_pb2.StopMusicResponse(success=True)

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        self.log.append(("PlaySound", request.file_path))
        return audio_pb2.PlaySoundResponse(success=True)

    async def ChangeTempo(self, request):  # noqa: N802 - gRPC stub name
        self.log.append(("ChangeTempo", round(request.new_tempo, 2)))
        self.durations.append(round(request.transition_duration, 3))
        return audio_pb2.ChangeTempoResponse(success=True)


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[2])
async def test_music_plays_at_countdown_volume_then_game_volume():
    """UC-031 step 2: game music starts at audio_volume.countdown_music, then rises to audio_volume.game."""
    log: list = []
    with patch.object(
        base_module,
        "_read_volume_flag",
        autospec=True,
        side_effect=lambda name, default: FLAG_VOLUMES.get(name, default),
    ):
        game = FFAGame(
            controller_manager_client=MockControllerManagerService(num_controllers=2),
            event_publisher=EventCollector().publish,
            audio_client=RecordingAudio(log),
            game_id="test_uc031_volume",
            initial_players=[game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(2)],
        )

    async def countdown():
        log.append(("countdown", ""))

    game._countdown = countdown
    game._warmup_ema = AsyncMock()
    game._game_loop = AsyncMock()
    game._wait_for_rainbow_effect = AsyncMock(return_value=True)

    await game.run()

    start = [entry for entry in log if entry[0] in ("SetVolume", "PlayMusic", "countdown")]
    assert start[:4] == [
        ("SetVolume", FLAG_VOLUMES["audio_volume.countdown_music"]),
        ("PlayMusic", "Joust/music/*.ogg"),
        ("countdown", ""),
        ("SetVolume", FLAG_VOLUMES["audio_volume.game"]),
    ]
    # Teardown stops the music.
    assert ("StopMusic", "") in log[log.index(("SetVolume", FLAG_VOLUMES["audio_volume.game"])) :]


class FailingAudio:
    """An audio service that is down: every RPC raises."""

    def __init__(self):
        self.attempts: list[str] = []

    def _fail(self, name: str):
        async def rpc(_request):
            self.attempts.append(name)
            raise ConnectionError(f"audio service unavailable ({name})")

        return rpc

    def __getattr__(self, name: str):
        if name in ("PlaySound", "PlayMusic", "StopMusic", "SetVolume", "ChangeTempo"):
            return self._fail(name)
        raise AttributeError(name)


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[4])
async def test_agent_audio_cue_plays_the_sound_id_through_play_sound():
    """UC-031 step 4: the agent's play_audio_cue plays its sound id (agent_cue) through the audio service."""
    log: list = []
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=RecordingAudio(log),
        game_id="test_uc031_cue",
    )
    manager = InterventionManager(event_publisher=EventCollector().publish, get_game=lambda: game)
    ctx = InterventionContext(
        spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "audio_cue"),
        value=Sound.SFX_AGENT_CUE.value,
        payload=Sound.SFX_AGENT_CUE.value,
        target_serial=None,
        game=game,
        objective="balanced",
    )

    await manager._handle_audio_cue(ctx)

    assert log == [("PlaySound", "agent_cue")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[4])
async def test_explosion_on_each_death_then_congratulations_for_the_winner():
    """UC-031 step 4: a full FFA game plays an explosion on each death, then "congratulations" for the winner."""
    log: list = []
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(
            num_controllers=3, death_schedule={1.0: 0, 1.6: 1}, max_duration=8.0
        ),
        event_publisher=EventCollector().publish,
        audio_client=RecordingAudio(log),
        game_id="test_uc031_event_sounds",
        initial_players=[game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(3)],
    )
    config = GamePerformanceConfig(countdown_phase_duration_ms=50, winner_rainbow_duration_ms=200)

    with patch.object(get_config_manager(), "get_config", return_value=config):
        await asyncio.wait_for(game.run(), timeout=15.0)

    sounds = [path for call, path in log if call == "PlaySound"]
    after_start = sounds[sounds.index(Sound.SFX_START3.value) + 1 :]
    assert [s for s in after_start if s in (Sound.SFX_EXPLOSION.value, Sound.VOX_CONGRATULATIONS.value)] == [
        Sound.SFX_EXPLOSION.value,
        Sound.SFX_EXPLOSION.value,
        Sound.VOX_CONGRATULATIONS.value,
    ]


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", steps=[3], br=["BR-001", "BR-005"])
async def test_ffa_run_drives_the_tempo_loop():
    """UC-031 BR-001 / BR-005: a Free-for-All game's own run() drives the step-3 tempo loop.

    The windows (0.2 s) and the ramp flags (0.1 s up, 0.15 s down, as the ci variants) are
    shortened so several segments fit in the game; the audio service sees alternating
    1.3x / 1.0x ChangeTempo glides, each over the flag of its direction.
    """
    log: list = []
    audio = RecordingAudio(log)
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(
            num_controllers=3, death_schedule={1.5: 0, 2.5: 1}, max_duration=8.0
        ),
        event_publisher=EventCollector().publish,
        audio_client=audio,
        game_id="test_uc031_ffa_tempo",
        initial_players=[game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(3)],
    )
    game.music_windows = base_module.MusicWindows(*([0.2] * 8))
    config = GamePerformanceConfig(countdown_phase_duration_ms=50, winner_rainbow_duration_ms=200)
    ramp_flags = {"tempo.ramp_up_seconds": 0.1, "tempo.ramp_down_seconds": 0.15}
    real_read = base_module.read_float_flag

    def read_flag(domain, key, default, game_id=None):
        if key in ramp_flags:
            return ramp_flags[key]
        return real_read(domain, key, default, game_id=game_id)

    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(base_module, "read_float_flag", side_effect=read_flag),
    ):
        await asyncio.wait_for(game.run(), timeout=15.0)

    tempos = [tempo for call, tempo in log if call == "ChangeTempo"]
    assert len(tempos) >= 2
    alternating = [FAST_MUSIC_SPEED if i % 2 == 0 else SLOW_MUSIC_SPEED for i in range(len(tempos))]
    assert tempos == [round(tempo, 2) for tempo in alternating]
    expected = [0.1 if i % 2 == 0 else 0.15 for i in range(len(tempos))]
    assert audio.durations == expected


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main")  # Post-F-1: audio failures never stop a game
async def test_audio_service_down_never_stops_the_game():
    """UC-031 Post-F-1: with every audio RPC failing, a full FFA game still runs to a winner and ends."""
    audio = FailingAudio()
    events = EventCollector()
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(
            num_controllers=3, death_schedule={1.0: 0, 1.6: 1}, max_duration=8.0
        ),
        event_publisher=events.publish,
        audio_client=audio,
        game_id="test_uc031_audio_down",
        initial_players=[game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(3)],
    )
    config = GamePerformanceConfig(countdown_phase_duration_ms=50, winner_rainbow_duration_ms=200)

    with patch.object(get_config_manager(), "get_config", return_value=config):
        await asyncio.wait_for(game.run(), timeout=15.0)

    # The game asked for audio and every call failed ...
    assert {"SetVolume", "PlaySound", "StopMusic"} <= set(audio.attempts)
    # ... yet it ran to the end: two deaths, a winner, game_ended, no game_error.
    assert len(events.get_events_of_type(GameEvent.PLAYER_DEATH)) == 2
    assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "mock_controller_2"}]
    assert events.count_events_of_type(GameEvent.GAME_ENDED) == 1
    assert events.count_events_of_type(GameEvent.GAME_ERROR) == 0
