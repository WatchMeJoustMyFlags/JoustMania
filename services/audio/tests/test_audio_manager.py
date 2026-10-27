"""
Unit tests for AudioManager.
"""

import os
from unittest.mock import patch

import pytest

from services.audio.music_player import DummyMusicPlayer


class TestAudioManagerInit:
    """Tests for AudioManager initialization."""

    def test_silent_mode_no_channels(self, mock_audio_manager):
        assert len(mock_audio_manager.channels) == 0

    def test_silent_mode_dummy_player(self, mock_audio_manager):
        assert isinstance(mock_audio_manager.music_player, DummyMusicPlayer)

    def test_default_volume(self, mock_audio_manager):
        assert mock_audio_manager.master_volume == 1.0

    def test_assets_dir_default(self, mock_audio_manager):
        assert mock_audio_manager.assets_dir == "services/audio/assets"

    def test_assets_dir_from_env(self):
        with patch.dict(os.environ, {"AUDIO_ASSETS_DIR": "/custom/path"}):
            from services.audio.servicer import AudioManager

            manager = AudioManager(silent=True)
            assert manager.assets_dir == "/custom/path"


class TestAudioManagerPlaySound:
    """Tests for AudioManager.play_sound."""

    def test_play_sound_silent_mode(self, mock_audio_manager):
        result = mock_audio_manager.play_sound("test/sound.ogg", volume=1.0, priority=2)
        assert result is True

    def test_play_sound_no_channels_available(self):
        """Non-silent mode with all channels busy returns False."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        # Make all channels busy
        for channel in manager.channels:
            channel._playing.set()
            channel.priority = 3  # High priority so they can't be stolen
        # Try to play a sound with a real file
        result = manager.play_sound("nonexistent.ogg", volume=1.0, priority=2)
        # File doesn't exist so it fails before channel check
        assert result is False

    def test_play_sound_missing_file(self):
        """Non-silent mode with missing file returns False."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        result = manager.play_sound("definitely_not_a_real_file.ogg", volume=1.0, priority=2)
        assert result is False

    @pytest.mark.uc("UC-031", flow="main", cond=["Post-F-1"])
    @patch("services.audio.metrics.audio_sounds_played_total")
    def test_play_sound_missing_file_increments_metric(self, mock_metric):
        """UC-031 Failure Postconditions (Post-F-1): a missing file is counted as file_not_found."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        manager.play_sound("definitely_not_a_real_file.ogg", volume=1.0, priority=2)
        mock_metric.labels.assert_called_with(result="file_not_found")
        mock_metric.labels.return_value.inc.assert_called()

    @patch("services.audio.metrics.audio_channel_saturation_total")
    @patch("services.audio.metrics.audio_sounds_played_total")
    @pytest.mark.uc("UC-031", flow="A2")
    def test_play_sound_no_channel_increments_saturation(self, mock_played, mock_saturation):
        """All channels busy increments no_channel and saturation metrics."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        # Create a real file so we get past the file check
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake")
            tmp_path = f.name
        try:
            # Make all channels busy
            for ch in manager.channels:
                ch._playing.set()
                ch.priority = 3
            manager.play_sound(tmp_path, volume=1.0, priority=2)
            mock_played.labels.assert_called_with(result="no_channel")
            mock_saturation.inc.assert_called()
        finally:
            os.unlink(tmp_path)

    @patch("services.audio.metrics.audio_sounds_played_total")
    @patch("services.audio.metrics.audio_channels_active")
    def test_play_sound_success_increments_metric(self, mock_active, mock_played):
        """Successful play increments success metric and updates gauge."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"fake")
            tmp_path = f.name
        try:
            # Mock channel.play to return True
            channel = manager._find_channel()
            with patch.object(channel, "play", return_value=True):
                manager.play_sound(tmp_path, volume=1.0, priority=2)
            mock_played.labels.assert_called_with(result="success")
            mock_active.set.assert_called()
        finally:
            os.unlink(tmp_path)

    def test_play_sound_volume_scaling(self, mock_audio_manager):
        """In silent mode, play_sound succeeds regardless of volume."""
        mock_audio_manager.master_volume = 0.5
        result = mock_audio_manager.play_sound("test.ogg", volume=0.8, priority=2)
        assert result is True


class TestAudioManagerFindChannel:
    """Tests for AudioManager._find_channel."""

    @pytest.mark.uc("UC-031", flow="main", steps=[5])
    def test_find_free_channel(self):
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        channel = manager._find_channel()
        assert channel is not None
        assert channel.is_playing is False

    def test_find_channel_all_busy(self):
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        for ch in manager.channels:
            ch._playing.set()
            ch.priority = 5
        channel = manager._find_channel(force_priority=False)
        assert channel is None

    @pytest.mark.uc("UC-031", flow="A2")
    def test_find_channel_force_priority_steal(self):
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        for ch in manager.channels:
            ch._playing.set()
            ch.priority = 1  # Low priority
        channel = manager._find_channel(force_priority=True, priority=3)
        assert channel is not None

    @pytest.mark.uc("UC-031", flow="A2")
    def test_find_channel_force_priority_no_steal_same_priority(self):
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        for ch in manager.channels:
            ch._playing.set()
            ch.priority = 3  # Same priority
        channel = manager._find_channel(force_priority=True, priority=3)
        assert channel is None


class TestAudioManagerMusic:
    """Tests for AudioManager music playback."""

    def test_play_music_returns_track_id(self, mock_audio_manager):
        track_id = mock_audio_manager.play_music("Joust/music/*.ogg")
        assert track_id is not None
        assert isinstance(track_id, str)
        assert len(track_id) > 0

    def test_stop_music_success(self, mock_audio_manager):
        track_id = mock_audio_manager.play_music("Joust/music/*.ogg")
        result = mock_audio_manager.stop_music(track_id)
        assert result is True

    def test_stop_music_empty_track_id(self, mock_audio_manager):
        mock_audio_manager.play_music("Joust/music/*.ogg")
        result = mock_audio_manager.stop_music("")
        assert result is True

    def test_stop_music_wrong_track_id(self, mock_audio_manager):
        mock_audio_manager.play_music("Joust/music/*.ogg")
        result = mock_audio_manager.stop_music("wrong-id")
        assert result is False

    @patch("services.audio.metrics.audio_music_tempo")
    @patch("services.audio.metrics.audio_music_playing")
    def test_play_music_sets_metrics(self, mock_playing, mock_tempo, mock_audio_manager):
        """play_music sets music_playing=1 and music_tempo."""
        mock_audio_manager.play_music("Joust/music/*.ogg", tempo=1.5)
        mock_playing.set.assert_called_with(1)
        mock_tempo.set.assert_called_with(1.5)

    @patch("services.audio.metrics.audio_music_tempo")
    @patch("services.audio.metrics.audio_music_playing")
    def test_stop_music_clears_metrics(self, mock_playing, mock_tempo, mock_audio_manager):
        """stop_music sets music_playing=0 and music_tempo=0."""
        track_id = mock_audio_manager.play_music("Joust/music/*.ogg")
        mock_audio_manager.stop_music(track_id)
        mock_playing.set.assert_called_with(0)
        mock_tempo.set.assert_called_with(0)

    def test_change_tempo_no_event_loop(self, mock_audio_manager):
        mock_audio_manager.play_music("Joust/music/*.ogg")
        assert mock_audio_manager.event_loop is None
        result = mock_audio_manager.change_tempo("track-123", 1.3)
        assert result is False


class TestUpdateChannelGauge:
    """Tests for AudioManager._update_channel_gauge."""

    @patch("services.audio.metrics.audio_channels_active")
    def test_update_channel_gauge_none_active(self, mock_gauge):
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        manager._update_channel_gauge()
        mock_gauge.set.assert_called_with(0)

    @patch("services.audio.metrics.audio_channels_active")
    def test_update_channel_gauge_some_active(self, mock_gauge):
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        manager.channels[0]._playing.set()
        manager.channels[1]._playing.set()
        manager._update_channel_gauge()
        mock_gauge.set.assert_called_with(2)


class TestAudioManagerSetVolume:
    """Tests for AudioManager.set_volume."""

    def test_set_volume_normal(self, mock_audio_manager):
        result = mock_audio_manager.set_volume(0.5)
        assert result is True
        assert mock_audio_manager.master_volume == 0.5

    def test_set_volume_clamp_high(self, mock_audio_manager):
        result = mock_audio_manager.set_volume(2.0)
        assert result is True
        assert mock_audio_manager.master_volume == 1.0

    def test_set_volume_clamp_low(self, mock_audio_manager):
        result = mock_audio_manager.set_volume(-0.5)
        assert result is True
        assert mock_audio_manager.master_volume == 0.0


class TestMasterVolumeAndMixing:
    """UC-031 step 5: up to 8 effect channels; one master volume for music and effects."""

    @pytest.fixture
    def sound_file(self, tmp_path):
        path = tmp_path / "effect.ogg"
        path.write_bytes(b"fake")
        return str(path)

    @pytest.mark.uc("UC-031", flow="main", steps=[5])
    def test_eight_effect_channels(self):
        """UC-031 step 5: the mixer has 8 effect channels."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        assert AudioManager.MAX_CHANNELS == 8
        assert len(manager.channels) == 8

    @pytest.mark.uc("UC-031", flow="main", steps=[5])
    def test_set_volume_drives_music_and_effects(self, sound_file):
        """UC-031 step 5: SetVolume sets the music volume, and an effect played afterwards is scaled by it."""
        from services.audio.servicer import AudioManager

        manager = AudioManager(silent=False)
        with patch.object(manager.music_player, "set_volume") as music_volume:
            assert manager.set_volume(0.4) is True
        music_volume.assert_called_once_with(0.4)

        channel = manager._find_channel()
        with patch.object(channel, "play", return_value=True) as play:
            assert manager.play_sound(sound_file, volume=0.5, priority=2) is True
        play.assert_called_once()
        assert play.call_args.kwargs["volume"] == pytest.approx(0.2)


class TestPlaybackFailureIsCounted:
    """UC-031 Post-F-1: a playback error is counted and frees the channel; it never raises to the caller."""

    @pytest.mark.uc("UC-031", flow="main", cond=["Post-F-1"])
    @patch("services.audio.metrics.audio_playback_errors_total")
    def test_playback_error_increments_audio_playback_errors_total(self, mock_errors, tmp_path):
        import threading

        from services.audio.servicer import SoundChannel

        channel = SoundChannel(channel_id=0)
        done = threading.Event()

        def broken_playback(*_args):
            raise RuntimeError("ALSA device gone")

        def count(*_args):
            done.set()

        mock_errors.inc.side_effect = count
        with (
            patch("services.audio.servicer.miniaudio.get_file_info") as file_info,
            patch.object(channel, "_play_full_volume", side_effect=broken_playback),
        ):
            file_info.return_value.duration = 0.1
            assert channel.play(str(tmp_path / "effect.ogg"), volume=1.0, priority=2) is True
            assert done.wait(timeout=2.0), "the playback error was never counted"

        mock_errors.inc.assert_called_once()
        for _ in range(100):  # the playback thread clears the flag right after counting
            if not channel.is_playing:
                break
            threading.Event().wait(0.01)
        assert channel.is_playing is False
