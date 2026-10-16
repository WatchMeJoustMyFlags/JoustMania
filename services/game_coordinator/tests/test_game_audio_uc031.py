"""UC-031 Hear Game Audio, coordinator side: the volume sequence at game start (#1362).

UC-031 step 2: at game start the music plays at ``audio_volume.countdown_music``
during the countdown, then at ``audio_volume.game``. Drives the real
``FFAGame.run()`` with the countdown, EMA warm-up and game loop stubbed, so only
the audio calls ``run()`` makes are observed (in order, interleaved with the
countdown).
"""

from unittest.mock import AsyncMock, patch

import pytest

from proto import audio_pb2, game_coordinator_pb2
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

# Edge values (not the defaults 0.15 / 0.7), so the test proves the flags are used.
FLAG_VOLUMES = {"audio_volume.countdown_music": 0.25, "audio_volume.game": 0.9}


class RecordingAudio:
    """Stands in for ``audio_pb2_grpc.AudioServiceStub``; ``log`` is shared with the countdown stub."""

    def __init__(self, log: list):
        self.log = log

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
