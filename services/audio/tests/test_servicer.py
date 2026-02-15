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
        from services.audio.servicer import AudioServiceServicer

        with patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=False):
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
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.0)
        response = await servicer.PlayMusic(request, mock_grpc_context)
        assert response.success is True
        assert response.track_id == "track-123"

    @pytest.mark.asyncio
    async def test_play_music_audio_disabled(self, servicer, mock_grpc_context):
        servicer.audio_enabled = False
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.0)
        response = await servicer.PlayMusic(request, mock_grpc_context)
        assert response.success is True
        assert response.track_id == "muted"

    @pytest.mark.asyncio
    async def test_play_music_with_tempo(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_music = MagicMock(return_value="track-456")
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.3)
        await servicer.PlayMusic(request, mock_grpc_context)
        call_args = servicer.audio_manager.play_music.call_args
        assert call_args.kwargs.get("tempo") == pytest.approx(1.3)

    @pytest.mark.asyncio
    async def test_play_music_failure(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_music = MagicMock(return_value=None)
        request = audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.0)
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
            assert path.endswith("congratulations.ogg")

    def test_resolve_sfx_name(self, servicer):
        # "Explosion34" should be in registry as sound
        if "Explosion34" in servicer.sound_registry:
            path = servicer._resolve_sound_path("Explosion34")
            assert "sounds" in path
            assert path.endswith("Explosion34.ogg")
            assert "vox" not in path

    def test_resolve_path_with_vox_inserts_voice(self, servicer):
        servicer.menu_voice = "ivy"
        path = servicer._resolve_sound_path("Joust/vox/congratulations.ogg")
        assert "ivy" in path

    def test_resolve_path_with_voice_already_present(self, servicer):
        path = servicer._resolve_sound_path("Joust/vox/aaron/congratulations.ogg")
        assert path == "Joust/vox/aaron/congratulations.ogg"

    def test_resolve_unknown_sound_falls_back(self, servicer):
        servicer.menu_voice = "ivy"
        path = servicer._resolve_sound_path("nonexistent_sound_xyz")
        assert "vox" in path
        assert "ivy" in path


class TestResolvePath:
    """Tests for AudioManager._resolve_path."""

    def test_resolve_path_absolute_unchanged(self, mock_audio_manager):
        result = mock_audio_manager._resolve_path("/tmp/sound.ogg")
        assert result == "/tmp/sound.ogg"

    def test_resolve_path_with_assets_dir_unchanged(self, mock_audio_manager):
        path = "services/audio/assets/Joust/sounds/beep.ogg"
        result = mock_audio_manager._resolve_path(path)
        assert result == path

    def test_resolve_path_relative_prepends_assets(self, mock_audio_manager):
        result = mock_audio_manager._resolve_path("Joust/sounds/beep.ogg")
        assert result == os.path.join(mock_audio_manager.assets_dir, "Joust/sounds/beep.ogg")


class TestLoadAudioSetting:
    """Tests for _load_audio_setting lazy flagd loading."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.asyncio
    async def test_skips_load_if_already_loaded(self, servicer):
        servicer._settings_loaded = True
        original_voice = servicer.menu_voice
        # Even if flagd would return different values, we skip the load
        await servicer._load_audio_setting()
        assert servicer.menu_voice == original_voice

    @pytest.mark.asyncio
    async def test_loads_settings_from_flagd(self, servicer):
        servicer._settings_loaded = False
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_client.get_string_value.return_value = "aaron"

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("lib.feature_flags.init_flag_domain"),
        ):
            await servicer._load_audio_setting()

        assert servicer.audio_enabled is True
        assert servicer.menu_voice == "aaron"
        assert servicer._settings_loaded is True

    @pytest.mark.asyncio
    async def test_handles_flagd_failure_gracefully(self, servicer):
        servicer._settings_loaded = False
        with patch(
            "lib.feature_flags.get_flag_client",
            side_effect=Exception("flagd unavailable"),
        ):
            await servicer._load_audio_setting()
        # Should still mark as loaded to avoid retries
        assert servicer._settings_loaded is True

    @pytest.mark.asyncio
    async def test_rejects_invalid_voice(self, servicer):
        servicer._settings_loaded = False
        servicer.menu_voice = "ivy"
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_client.get_string_value.return_value = "invalid_voice"

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("lib.feature_flags.init_flag_domain"),
        ):
            await servicer._load_audio_setting()

        # Invalid voice should be rejected, keeping the default
        assert servicer.menu_voice == "ivy"


class TestSoundRegistry:
    """Tests for _register_sound and _scan_directory_for_sounds."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    def test_register_sound_adds_both_cases(self, servicer):
        servicer.sound_registry.clear()
        servicer._register_sound("TestSound", "vox", "Joust")
        assert "TestSound" in servicer.sound_registry
        assert "testsound" in servicer.sound_registry

    def test_register_sound_first_wins(self, servicer):
        servicer.sound_registry.clear()
        servicer._register_sound("boom", "sound", "Joust")
        servicer._register_sound("boom", "vox", "Menu")
        assert servicer.sound_registry["boom"] == ("sound", "Joust")

    def test_resolve_vox_path_no_vox_in_path(self, servicer):
        path = "Joust/sounds/beep.ogg"
        assert servicer._resolve_vox_path(path) == path

    def test_resolve_vox_path_inserts_voice(self, servicer):
        servicer.menu_voice = "aaron"
        result = servicer._resolve_vox_path("Joust/vox/hello.ogg")
        assert result == "Joust/vox/aaron/hello.ogg"

    def test_resolve_vox_path_preserves_existing_voice(self, servicer):
        result = servicer._resolve_vox_path("Joust/vox/ivy/hello.ogg")
        assert result == "Joust/vox/ivy/hello.ogg"


class TestAudioManagerFindChannel:
    """Tests for AudioManager._find_channel."""

    def test_returns_free_channel(self, mock_audio_manager):
        from services.audio.servicer import SoundChannel

        ch = SoundChannel(0)
        mock_audio_manager.channels = [ch]
        result = mock_audio_manager._find_channel()
        assert result is ch

    def test_returns_none_when_all_busy(self, mock_audio_manager):
        from services.audio.servicer import SoundChannel

        ch = SoundChannel(0)
        ch._playing.set()  # mark as playing
        mock_audio_manager.channels = [ch]
        result = mock_audio_manager._find_channel()
        assert result is None

    def test_force_priority_steals_lower_channel(self, mock_audio_manager):
        from services.audio.servicer import SoundChannel

        ch = SoundChannel(0)
        ch._playing.set()
        ch.priority = 1  # low priority
        mock_audio_manager.channels = [ch]
        result = mock_audio_manager._find_channel(force_priority=True, priority=3)
        assert result is ch

    def test_force_priority_no_steal_if_equal_or_higher(self, mock_audio_manager):
        from services.audio.servicer import SoundChannel

        ch = SoundChannel(0)
        ch._playing.set()
        ch.priority = 3  # same priority
        mock_audio_manager.channels = [ch]
        result = mock_audio_manager._find_channel(force_priority=True, priority=3)
        assert result is None


class TestAudioManagerPlaySound:
    """Tests for AudioManager.play_sound."""

    def test_silent_mode_returns_true(self, mock_audio_manager):
        assert mock_audio_manager.silent is True
        result = mock_audio_manager.play_sound("Joust/sounds/beep.ogg")
        assert result is True

    def test_file_not_found_returns_false(self):
        from services.audio.servicer import AudioManager, SoundChannel

        mgr = AudioManager(silent=True)
        # Override silent to test the file-not-found path
        mgr.silent = False
        mgr.channels = [SoundChannel(0)]
        result = mgr.play_sound("/nonexistent/file.ogg")
        assert result is False


class TestAudioManagerChangeTempo:
    """Tests for AudioManager.change_tempo."""

    def test_track_id_mismatch_returns_false(self, mock_audio_manager):
        mock_audio_manager.music_player._track_id = "track-abc"
        result = mock_audio_manager.change_tempo("wrong-id", 1.5)
        assert result is False

    def test_no_event_loop_returns_false(self, mock_audio_manager):
        mock_audio_manager.music_player._track_id = "track-abc"
        mock_audio_manager.event_loop = None
        result = mock_audio_manager.change_tempo("track-abc", 1.5)
        assert result is False
