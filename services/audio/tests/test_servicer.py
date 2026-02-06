"""
Unit tests for Audio gRPC Servicer.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from proto import audio_pb2


class TestPlaySound:
    """Tests for PlaySound RPC."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.asyncio
    async def test_play_sound_success(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await servicer.PlaySound(request, mock_grpc_context)
        assert response.success is True
        assert response.error == ""

    @pytest.mark.asyncio
    async def test_play_sound_audio_disabled(self, servicer, mock_grpc_context):
        servicer.audio_enabled = False
        servicer.audio_manager.play_sound = MagicMock()
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await servicer.PlaySound(request, mock_grpc_context)
        assert response.success is True
        servicer.audio_manager.play_sound.assert_not_called()

    @pytest.mark.asyncio
    async def test_play_sound_resolves_vox_path(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        # "congratulations" is in the registry as a vox sound
        request = audio_pb2.PlaySoundRequest(file_path="congratulations", volume=1.0, priority=2)
        await servicer.PlaySound(request, mock_grpc_context)
        # Verify the resolved path was passed to play_sound
        call_args = servicer.audio_manager.play_sound.call_args
        resolved_path = call_args.kwargs.get("file_path", call_args[0][0] if call_args[0] else "")
        assert "vox" in resolved_path
        assert "congratulations" in resolved_path

    @pytest.mark.asyncio
    async def test_play_sound_voice_selection(self, servicer, mock_grpc_context):
        servicer.menu_voice = "aaron"
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        request = audio_pb2.PlaySoundRequest(file_path="congratulations", volume=1.0, priority=2)
        await servicer.PlaySound(request, mock_grpc_context)
        call_args = servicer.audio_manager.play_sound.call_args
        resolved_path = call_args.kwargs.get("file_path", call_args[0][0] if call_args[0] else "")
        assert "aaron" in resolved_path

    @pytest.mark.asyncio
    async def test_play_sound_lazy_settings_load(self, mock_grpc_context):
        with patch.dict(os.environ, {"MOCK_MODE": "true"}):
            from services.audio.servicer import AudioServiceServicer

            servicer = AudioServiceServicer()
            servicer._settings_loaded = False
            servicer.audio_manager.play_sound = MagicMock(return_value=True)

            with patch.object(servicer, "_load_audio_setting") as mock_load:
                request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
                await servicer.PlaySound(request, mock_grpc_context)
                mock_load.assert_called_once()

    @pytest.mark.asyncio
    async def test_play_sound_default_volume(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        # volume=0 should be treated as default 1.0 by the servicer
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=0, priority=2)
        await servicer.PlaySound(request, mock_grpc_context)
        call_args = servicer.audio_manager.play_sound.call_args
        volume = call_args.kwargs.get("volume", call_args[0][1] if len(call_args[0]) > 1 else None)
        assert volume == 1.0


class TestPlayMusic:
    """Tests for PlayMusic RPC."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.asyncio
    async def test_play_music_success(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_music = MagicMock(return_value="track-123")
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.wav", loop=True, tempo=1.0)
        response = await servicer.PlayMusic(request, mock_grpc_context)
        assert response.success is True
        assert response.track_id == "track-123"

    @pytest.mark.asyncio
    async def test_play_music_audio_disabled(self, servicer, mock_grpc_context):
        servicer.audio_enabled = False
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.wav", loop=True, tempo=1.0)
        response = await servicer.PlayMusic(request, mock_grpc_context)
        assert response.success is True
        assert response.track_id == "muted"

    @pytest.mark.asyncio
    async def test_play_music_with_tempo(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_music = MagicMock(return_value="track-456")
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.wav", loop=True, tempo=1.3)
        await servicer.PlayMusic(request, mock_grpc_context)
        call_args = servicer.audio_manager.play_music.call_args
        assert call_args.kwargs.get("tempo") == pytest.approx(1.3)

    @pytest.mark.asyncio
    async def test_play_music_failure(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_music = MagicMock(return_value=None)
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.wav", loop=True, tempo=1.0)
        response = await servicer.PlayMusic(request, mock_grpc_context)
        assert response.success is False
        assert response.track_id == ""


class TestStopMusic:
    """Tests for StopMusic RPC."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.asyncio
    async def test_stop_music_success(self, servicer, mock_grpc_context):
        servicer.audio_manager.stop_music = MagicMock(return_value=True)
        request = audio_pb2.StopMusicRequest(track_id="track-123")
        response = await servicer.StopMusic(request, mock_grpc_context)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_stop_music_track_mismatch(self, servicer, mock_grpc_context):
        servicer.audio_manager.stop_music = MagicMock(return_value=False)
        request = audio_pb2.StopMusicRequest(track_id="wrong-track")
        response = await servicer.StopMusic(request, mock_grpc_context)
        assert response.success is False


class TestChangeTempo:
    """Tests for ChangeTempo RPC."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    def test_change_tempo_success(self, servicer, mock_grpc_context):
        servicer.audio_manager.change_tempo = MagicMock(return_value=True)
        request = audio_pb2.ChangeTempoRequest(track_id="track-123", new_tempo=1.3, transition_duration=1.5)
        response = servicer.ChangeTempo(request, mock_grpc_context)
        assert response.success is True

    def test_change_tempo_track_mismatch(self, servicer, mock_grpc_context):
        servicer.audio_manager.change_tempo = MagicMock(return_value=False)
        request = audio_pb2.ChangeTempoRequest(track_id="wrong-track", new_tempo=1.3, transition_duration=1.0)
        response = servicer.ChangeTempo(request, mock_grpc_context)
        assert response.success is False


class TestSetVolume:
    """Tests for SetVolume RPC."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    def test_set_volume_success(self, servicer, mock_grpc_context):
        servicer.audio_manager.set_volume = MagicMock(return_value=True)
        request = audio_pb2.SetVolumeRequest(volume=0.5)
        response = servicer.SetVolume(request, mock_grpc_context)
        assert response.success is True

    def test_set_volume_failure(self, servicer, mock_grpc_context):
        servicer.audio_manager.set_volume = MagicMock(return_value=False)
        request = audio_pb2.SetVolumeRequest(volume=0.5)
        response = servicer.SetVolume(request, mock_grpc_context)
        assert response.success is False


class TestSoundPathResolution:
    """Tests for _resolve_sound_path."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    def test_resolve_simple_vox_name(self, servicer):
        servicer.menu_voice = "ivy"
        # "congratulations" should be in registry as vox
        if "congratulations" in servicer.sound_registry:
            path = servicer._resolve_sound_path("congratulations")
            assert "vox" in path
            assert "ivy" in path
            assert path.endswith("congratulations.wav")

    def test_resolve_sfx_name(self, servicer):
        # "Explosion34" should be in registry as sound
        if "Explosion34" in servicer.sound_registry:
            path = servicer._resolve_sound_path("Explosion34")
            assert "sounds" in path
            assert path.endswith("Explosion34.wav")
            assert "vox" not in path

    def test_resolve_path_with_vox_inserts_voice(self, servicer):
        servicer.menu_voice = "ivy"
        path = servicer._resolve_sound_path("Joust/vox/congratulations.wav")
        assert "ivy" in path

    def test_resolve_path_with_voice_already_present(self, servicer):
        path = servicer._resolve_sound_path("Joust/vox/aaron/congratulations.wav")
        assert path == "Joust/vox/aaron/congratulations.wav"

    def test_resolve_unknown_sound_falls_back(self, servicer):
        servicer.menu_voice = "ivy"
        path = servicer._resolve_sound_path("nonexistent_sound_xyz")
        assert "vox" in path
        assert "ivy" in path
