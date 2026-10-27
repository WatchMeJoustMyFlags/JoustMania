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
    @pytest.mark.uc("UC-031", flow="A1")
    async def test_play_sound_audio_disabled(self, servicer, mock_grpc_context):
        servicer.audio_enabled = False
        servicer.audio_manager.play_sound = MagicMock()
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await servicer.PlaySound(request, mock_grpc_context)
        assert response.success is True
        servicer.audio_manager.play_sound.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="main", steps=[4])
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
    @pytest.mark.uc("UC-031", flow="main", steps=[4])
    async def test_play_sound_voice_selection(self, servicer, mock_grpc_context):
        servicer.menu_voice = "aaron"
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        request = audio_pb2.PlaySoundRequest(file_path="congratulations", volume=1.0, priority=2)
        await servicer.PlaySound(request, mock_grpc_context)
        call_args = servicer.audio_manager.play_sound.call_args
        resolved_path = call_args.kwargs.get("file_path", call_args[0][0] if call_args[0] else "")
        assert "aaron" in resolved_path

    @pytest.mark.asyncio
    async def test_play_sound_respects_audio_disabled(self, mock_grpc_context):
        from services.audio.servicer import AudioServiceServicer

        with patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=False):
            servicer = AudioServiceServicer()
        servicer.audio_enabled = False
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)
        response = await servicer.PlaySound(request, mock_grpc_context)
        assert response.success is True  # Silently succeeds when disabled

    @pytest.mark.asyncio
    async def test_play_sound_default_volume(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        # volume=0 should be treated as default 1.0 by the servicer
        request = audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=0, priority=2)
        await servicer.PlaySound(request, mock_grpc_context)
        call_args = servicer.audio_manager.play_sound.call_args
        volume = call_args.kwargs.get("volume", call_args[0][1] if len(call_args[0]) > 1 else None)
        assert volume == 1.0


class TestPlaySoundDuration:
    """UC-020 A5 step 2: PlaySound reports the clip length so a caller can wait for it (#1550)."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_play_sound_returns_clip_duration(self, servicer, mock_grpc_context):
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        servicer.audio_manager.silent = False
        info = MagicMock(duration=7.25)
        with patch("services.audio.servicer.miniaudio.get_file_info", return_value=info) as get_info:
            response = await servicer.PlaySound(
                audio_pb2.PlaySoundRequest(file_path="FFA-instructions", volume=1.0, priority=2), mock_grpc_context
            )
        assert response.success is True
        assert response.duration_seconds == pytest.approx(7.25)
        assert "FFA-instructions" in get_info.call_args.args[0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_silent_manager_reports_zero(self, servicer, mock_grpc_context):
        # A readable 7 s clip and a "successful" play: 0.0 can only come from the silent guard.
        assert servicer.audio_manager.silent is True
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        with patch("services.audio.servicer.miniaudio.get_file_info", return_value=MagicMock(duration=7.0)):
            response = await servicer.PlaySound(
                audio_pb2.PlaySoundRequest(file_path="FFA-instructions", volume=1.0, priority=2), mock_grpc_context
            )
        assert response.duration_seconds == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_muted_reports_zero(self, servicer, mock_grpc_context):
        servicer.audio_enabled = False
        servicer.audio_manager.silent = False
        # Playing would succeed and the clip is readable: only the muted return can give 0.0.
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        with patch("services.audio.servicer.miniaudio.get_file_info", return_value=MagicMock(duration=7.0)):
            response = await servicer.PlaySound(
                audio_pb2.PlaySoundRequest(file_path="FFA-instructions", volume=1.0, priority=2), mock_grpc_context
            )
        assert response.duration_seconds == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_failed_play_or_unreadable_file_reports_zero(self, servicer, mock_grpc_context):
        servicer.audio_manager.silent = False
        servicer.audio_manager.play_sound = MagicMock(return_value=False)
        # A readable 7 s clip: 0.0 can only come from the failed-play guard.
        with patch("services.audio.servicer.miniaudio.get_file_info", return_value=MagicMock(duration=7.0)):
            response = await servicer.PlaySound(
                audio_pb2.PlaySoundRequest(file_path="FFA-instructions", volume=1.0, priority=2), mock_grpc_context
            )
        assert response.duration_seconds == 0.0
        with patch("services.audio.servicer.miniaudio.get_file_info", side_effect=OSError("bad header")):
            assert servicer.audio_manager.sound_duration("Menu/vox/ivy/FFA-instructions.ogg") == 0.0


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
    @pytest.mark.uc("UC-031", flow="A1")
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

    @pytest.mark.uc("UC-031", flow="main", steps=[3])
    def test_change_tempo_success(self, servicer, mock_grpc_context):
        """UC-031 step 3: the audio service runs the tempo change over the requested 1.5 s transition."""
        servicer.audio_manager.change_tempo = MagicMock(return_value=True)
        request = audio_pb2.ChangeTempoRequest(track_id="track-123", new_tempo=1.3, transition_duration=1.5)
        response = servicer.ChangeTempo(request, mock_grpc_context)
        assert response.success is True
        servicer.audio_manager.change_tempo.assert_called_once_with(
            track_id="track-123", new_tempo=pytest.approx(1.3), transition_duration=pytest.approx(1.5)
        )

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


class TestOnFlagsChanged:
    """Tests for reactive flag change handler."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.uc("UC-031", flow="A1")
    def test_disables_audio_and_stops_music(self, servicer):
        servicer.audio_enabled = True
        servicer.audio_manager.stop_music = MagicMock(return_value=True)
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = False
        mock_client.get_string_value.return_value = "ivy"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            servicer._on_flags_changed(None)

        assert servicer.audio_enabled is False
        servicer.audio_manager.stop_music.assert_called_once()

    @pytest.mark.uc("UC-031", flow="A1")
    def test_enables_audio(self, servicer):
        """UC-031 A1: turning play_audio on opens the hardware of a silent manager (#1352)."""
        servicer.audio_enabled = False
        assert servicer.audio_manager.silent is True
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_client.get_string_value.return_value = "ivy"

        with (
            patch("lib.feature_flags.get_flag_client", return_value=mock_client),
            patch("services.audio.servicer.MusicPlayer", autospec=True),
        ):
            servicer._on_flags_changed(None)

        assert servicer.audio_enabled is True
        assert servicer.audio_manager.silent is False
        assert len(servicer.audio_manager.channels) == servicer.audio_manager.MAX_CHANNELS

    @pytest.mark.uc("UC-031", flow="A4")
    def test_changes_menu_voice(self, servicer):
        servicer.menu_voice = "ivy"
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_client.get_string_value.return_value = "aaron"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            servicer._on_flags_changed(None)

        assert servicer.menu_voice == "aaron"

    @pytest.mark.uc("UC-031", flow="A4")
    def test_rejects_invalid_voice(self, servicer):
        servicer.menu_voice = "ivy"
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_client.get_string_value.return_value = "invalid_voice"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            servicer._on_flags_changed(None)

        assert servicer.menu_voice == "ivy"

    def test_handles_flagd_failure_gracefully(self, servicer):
        servicer.audio_enabled = True
        with patch(
            "lib.feature_flags.get_flag_client",
            side_effect=Exception("flagd unavailable"),
        ):
            servicer._on_flags_changed(None)
        # Should not crash, audio_enabled unchanged
        assert servicer.audio_enabled is True

    def test_no_stop_music_when_audio_already_disabled(self, servicer):
        servicer.audio_enabled = False
        servicer.audio_manager.stop_music = MagicMock()
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = False
        mock_client.get_string_value.return_value = "ivy"

        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            servicer._on_flags_changed(None)

        # No change, so stop_music should not be called
        servicer.audio_manager.stop_music.assert_not_called()


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


class TestMutedSpanAttribute:
    """UC-031 A1 step 1: PlaySound and PlayMusic spans carry ``audio.muted`` while play_audio is off."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @staticmethod
    def _span():
        from unittest.mock import create_autospec

        from opentelemetry.trace import Span

        return create_autospec(Span, instance=True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1", steps=[1])
    @pytest.mark.parametrize(
        ("rpc", "request_message"),
        [
            ("PlaySound", audio_pb2.PlaySoundRequest(file_path="beep_loud", volume=1.0, priority=2)),
            ("PlayMusic", audio_pb2.PlayMusicRequest(file_pattern="Joust/music/*.ogg", loop=True, tempo=1.0)),
        ],
    )
    async def test_muted_request_marks_span(self, servicer, mock_grpc_context, rpc, request_message):
        span = self._span()
        servicer.audio_enabled = False
        with (
            patch.object(servicer, "_refresh_play_audio", autospec=True),
            patch("services.audio.servicer.trace.get_current_span", return_value=span),
        ):
            response = await getattr(servicer, rpc)(request_message, mock_grpc_context)

        assert response.success is True
        span.set_attribute.assert_any_call("audio.muted", True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A1", steps=[1])
    async def test_unmuted_request_has_no_muted_attribute(self, servicer, mock_grpc_context):
        span = self._span()
        servicer.audio_manager.play_sound = MagicMock(return_value=True)
        with (
            patch.object(servicer, "_refresh_play_audio", autospec=True),
            patch("services.audio.servicer.trace.get_current_span", return_value=span),
        ):
            await servicer.PlaySound(audio_pb2.PlaySoundRequest(file_path="beep_loud"), mock_grpc_context)

        assert "audio.muted" not in [c.args[0] for c in span.set_attribute.call_args_list]


class TestPlaySoundAllChannelsBusy:
    """UC-031 A2: with every effect channel busy, only a CRITICAL PlaySound steals a lower-priority one."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer, tmp_path):
        from services.audio.servicer import AudioManager

        sound_file = tmp_path / "effect.ogg"
        sound_file.write_bytes(b"fake")
        mock_audio_servicer.audio_manager = AudioManager(silent=False)
        mock_audio_servicer._resolve_sound_path = lambda _name: str(sound_file)
        for channel in mock_audio_servicer.audio_manager.channels:
            channel._playing.set()
            channel.priority = 1  # busy with lower-priority effects
        return mock_audio_servicer

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A2", steps=[1])
    async def test_critical_play_sound_steals_a_lower_priority_channel(self, servicer, mock_grpc_context):
        from services.audio.servicer import SoundChannel

        with patch.object(SoundChannel, "play", autospec=True, return_value=True) as channel_play:
            response = await servicer.PlaySound(
                audio_pb2.PlaySoundRequest(file_path="effect", volume=1.0, priority=3), mock_grpc_context
            )

        assert response.success is True
        channel_play.assert_called_once()
        assert channel_play.call_args.kwargs["priority"] == 3

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-031", flow="A2", steps=[1])
    async def test_non_critical_play_sound_is_dropped_and_counted(self, servicer, mock_grpc_context):
        from services.audio.servicer import SoundChannel

        with (
            patch.object(SoundChannel, "play", autospec=True, return_value=True) as channel_play,
            patch("services.audio.metrics.audio_channel_saturation_total") as saturation,
        ):
            response = await servicer.PlaySound(
                audio_pb2.PlaySoundRequest(file_path="effect", volume=1.0, priority=2), mock_grpc_context
            )

        assert response.success is False
        channel_play.assert_not_called()
        saturation.inc.assert_called_once()


class TestStopSound:
    """UC-020 A5 step 2 (#1586): StopSound cuts a playing effect (the instruction clip) short."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_stop_sound_stops_playing_channels_only(self, servicer, mock_grpc_context):
        from services.audio.servicer import SoundChannel

        playing, idle = SoundChannel(0), SoundChannel(1)
        playing._playing.set()
        servicer.audio_manager.channels = [playing, idle]
        response = await servicer.StopSound(audio_pb2.StopSoundRequest(), mock_grpc_context)
        assert response.success is True
        assert response.stopped == 1
        assert playing.is_playing is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_stop_sound_with_nothing_playing_succeeds(self, servicer, mock_grpc_context):
        servicer.audio_manager.channels = []
        response = await servicer.StopSound(audio_pb2.StopSoundRequest(), mock_grpc_context)
        assert response.success is True
        assert response.stopped == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A5", steps=[2])
    async def test_stop_sound_reports_failure(self, servicer, mock_grpc_context):
        servicer.audio_manager.stop_sounds = MagicMock(side_effect=RuntimeError("boom"))
        response = await servicer.StopSound(audio_pb2.StopSoundRequest(), mock_grpc_context)
        assert response.success is False
        assert response.error


class TestVoiceProfileSelection:
    """UC-031 A4 / BR-007 (#2107): menu_voice accepts the manifest's profiles and plays through the partner."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    @staticmethod
    def _flag_event(servicer, voice):
        mock_client = MagicMock()
        mock_client.get_boolean_value.return_value = True
        mock_client.get_string_value.return_value = voice
        with patch("lib.feature_flags.get_flag_client", return_value=mock_client):
            servicer._on_flags_changed(None)

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    @pytest.mark.parametrize("voice", ["gen_male", "gen_female"])
    def test_accepts_generated_profiles(self, servicer, voice):
        servicer.menu_voice = "ivy"
        self._flag_event(servicer, voice)
        assert servicer.menu_voice == voice
        assert servicer._resolve_sound_path("high") == f"Menu/vox/{voice}/high.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_flag_change_rebuilds_the_partner_table(self, servicer):
        """ivy plays the generated label from gen_female; after a switch to aaron it plays from gen_male."""
        servicer.menu_voice = "ivy"
        assert servicer._resolve_sound_path("label_sensitivity") == "Menu/vox/gen_female/label_sensitivity.ogg"
        self._flag_event(servicer, "aaron")
        assert servicer._resolve_sound_path("label_sensitivity") == "Menu/vox/gen_male/label_sensitivity.ogg"
        assert servicer._resolve_sound_path("high") == "Menu/vox/aaron/high.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_unknown_voice_keeps_the_current_one_and_its_table(self, servicer):
        self._flag_event(servicer, "gen_male")
        self._flag_event(servicer, "robot")
        assert servicer.menu_voice == "gen_male"
        assert servicer._resolve_sound_path("zombie_death") == "Zombie/vox/aaron/zombie_death.ogg"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_unknown_voice_at_start_gives_ivy(self):
        from services.audio.servicer import AudioServiceServicer

        client = MagicMock()
        client.get_string_value.return_value = "robot"
        with (
            patch.object(AudioServiceServicer, "_load_play_audio_flag", return_value=False),
            patch("lib.feature_flags.get_flag_client", return_value=client),
            patch("lib.feature_flags.init_flag_domain"),
        ):
            servicer = AudioServiceServicer()
        assert servicer.menu_voice == "ivy"

    @pytest.mark.uc("UC-031", flow="A4", br=["BR-007"])
    def test_path_naming_a_generated_profile_is_used_as_is(self, servicer):
        servicer.menu_voice = "ivy"
        assert servicer._resolve_sound_path("Menu/vox/gen_male/high.ogg") == "Menu/vox/gen_male/high.ogg"
