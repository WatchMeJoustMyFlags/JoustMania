"""
In-process gRPC integration tests for AudioServiceServicer.

Spins up a real grpc.aio.server() on an ephemeral port, connects with
real stubs, and tests RPCs through the actual gRPC wire protocol.
AudioManager runs in silent mode (no hardware needed).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc.aio
import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from proto import audio_pb2, audio_pb2_grpc
from services.audio.servicer import AudioServiceServicer


@pytest_asyncio.fixture
async def grpc_service():
    """
    Start a real gRPC server with AudioServiceServicer in silent mode.

    Yields (servicer, stub) for direct servicer access and gRPC client calls.
    """
    # Bypass flagd lookup → AudioManager in silent mode (no hardware)
    with patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=False):
        servicer = AudioServiceServicer()
    servicer.audio_enabled = True
    servicer._settings_loaded = True

    server = grpc.aio.server()
    audio_pb2_grpc.add_AudioServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    stub = audio_pb2_grpc.AudioServiceStub(channel)

    yield servicer, stub

    await channel.close()
    await server.stop(grace=0)


class TestPlaySound:
    """PlaySound through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_play_sound_silent_mode(self, grpc_service):
        """PlaySound succeeds in silent mode (no actual audio)."""
        _servicer, stub = grpc_service

        response = await stub.PlaySound(audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2))

        assert response.success is True
        assert response.error == ""

    @pytest.mark.asyncio
    async def test_play_sound_audio_disabled(self, grpc_service):
        """PlaySound silently succeeds when audio is disabled."""
        servicer, stub = grpc_service
        servicer.audio_enabled = False

        response = await stub.PlaySound(audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=0.5, priority=1))

        assert response.success is True
        assert response.error == ""

    @pytest.mark.asyncio
    async def test_play_sound_resolves_path_via_grpc(self, grpc_service):
        """Sound name resolution works through the gRPC serialization layer."""
        servicer, stub = grpc_service
        servicer.audio_manager.play_sound = MagicMock(return_value=True)

        response = await stub.PlaySound(audio_pb2.PlaySoundRequest(file_path="congratulations", volume=1.0, priority=2))

        assert response.success is True
        call_args = servicer.audio_manager.play_sound.call_args
        resolved_path = call_args.kwargs.get("file_path", call_args[0][0] if call_args[0] else "")
        assert "congratulations" in resolved_path


class TestPlayMusic:
    """PlayMusic through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_play_music_audio_disabled(self, grpc_service):
        """PlayMusic returns muted track_id when audio disabled."""
        servicer, stub = grpc_service
        servicer.audio_enabled = False

        response = await stub.PlayMusic(
            audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.0)
        )

        assert response.success is True
        assert response.track_id == "muted"

    @pytest.mark.asyncio
    async def test_play_music_silent_mode(self, grpc_service):
        """PlayMusic delegates to AudioManager through gRPC."""
        servicer, stub = grpc_service
        servicer.audio_manager.play_music = MagicMock(return_value="track_123")

        response = await stub.PlayMusic(
            audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.2)
        )

        assert response.success is True
        assert response.track_id == "track_123"


class TestStopMusic:
    """StopMusic through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_stop_music_no_track(self, grpc_service):
        """StopMusic with no track playing succeeds (DummyMusicPlayer)."""
        _servicer, stub = grpc_service

        response = await stub.StopMusic(audio_pb2.StopMusicRequest(track_id=""))

        assert response.success is True

    @pytest.mark.asyncio
    async def test_stop_music_wrong_track_id(self, grpc_service):
        """StopMusic with mismatched track_id returns failure."""
        servicer, stub = grpc_service
        # Set a current track
        servicer.audio_manager.music_player._track_id = "active_track"

        response = await stub.StopMusic(audio_pb2.StopMusicRequest(track_id="wrong_id"))

        assert response.success is False


class TestSetVolume:
    """SetVolume through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_set_volume_success(self, grpc_service):
        """SetVolume updates master volume through gRPC."""
        servicer, stub = grpc_service

        response = await stub.SetVolume(audio_pb2.SetVolumeRequest(volume=0.75))

        assert response.success is True
        assert servicer.audio_manager.master_volume == 0.75

    @pytest.mark.asyncio
    async def test_set_volume_clamped(self, grpc_service):
        """SetVolume clamps to 0.0-1.0 range."""
        servicer, stub = grpc_service

        await stub.SetVolume(audio_pb2.SetVolumeRequest(volume=2.0))
        assert servicer.audio_manager.master_volume == 1.0

        await stub.SetVolume(audio_pb2.SetVolumeRequest(volume=-0.5))
        assert servicer.audio_manager.master_volume == 0.0


class TestChangeTempo:
    """ChangeTempo through real gRPC wire protocol."""

    @pytest.mark.asyncio
    async def test_change_tempo_no_event_loop(self, grpc_service):
        """ChangeTempo fails gracefully when no event loop is set."""
        _servicer, stub = grpc_service

        response = await stub.ChangeTempo(
            audio_pb2.ChangeTempoRequest(track_id="", new_tempo=1.3, transition_duration=1.0)
        )

        # DummyMusicPlayer has no event loop set, so this fails gracefully
        assert response.success is False
