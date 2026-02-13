"""
Unit tests for extracted helper methods from cognitive complexity refactoring.

Tests cover:
- SoundChannel._wait_for_playback
- AudioServiceServicer._scan_directory_for_sounds
- AudioServiceServicer._register_sound
- AudioServiceServicer._resolve_simple_name
- AudioServiceServicer._resolve_vox_path
"""

import threading
from pathlib import Path

import pytest

from services.audio.servicer import SoundChannel


class TestWaitForPlayback:
    """Tests for SoundChannel._wait_for_playback."""

    def test_stops_when_playing_cleared(self):
        """Playback wait exits when _playing event is cleared."""
        channel = SoundChannel(channel_id=0)
        channel._playing.set()

        # Clear after a short delay
        def clear_later():
            import time

            time.sleep(0.1)
            channel._playing.clear()

        threading.Thread(target=clear_later, daemon=True).start()
        # Should return quickly after clearing (not wait full duration)
        channel._wait_for_playback(duration=10.0)
        assert not channel.is_playing

    def test_stops_when_duration_exceeded(self):
        """Playback wait exits when duration + 0.5s is exceeded."""
        channel = SoundChannel(channel_id=0)
        channel._playing.set()
        # Very short duration so it completes quickly
        channel._wait_for_playback(duration=0.0)
        # After duration+0.5s, should have exited (playing still set from our side)
        # The wait loop checks elapsed < duration + 0.5, so with 0.0 duration
        # it should exit after ~0.5s
        assert channel.is_playing  # _wait_for_playback doesn't clear _playing

    def test_zero_duration_exits_quickly(self):
        """With zero duration, wait loop runs for at most ~0.5 seconds."""
        import time

        channel = SoundChannel(channel_id=0)
        channel._playing.set()
        start = time.monotonic()
        channel._wait_for_playback(duration=0.0)
        elapsed = time.monotonic() - start
        # Should take about 0.5s (duration + 0.5 margin), give some tolerance
        assert elapsed < 1.5


class TestScanDirectoryForSounds:
    """Tests for AudioServiceServicer._scan_directory_for_sounds."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        return mock_audio_servicer

    def test_nonexistent_directory_is_skipped(self, servicer):
        """Non-existent directory adds nothing to registry."""
        initial_count = len(servicer.sound_registry)
        servicer._scan_directory_for_sounds(Path("/nonexistent/path"), "vox", "Test")
        assert len(servicer.sound_registry) == initial_count

    def test_scans_ogg_files(self, servicer, tmp_path):
        """Scans .ogg files and registers them."""
        # Create test .ogg files
        (tmp_path / "test_sound_a.ogg").touch()
        (tmp_path / "test_sound_b.ogg").touch()
        (tmp_path / "not_audio.txt").touch()  # Should be ignored

        servicer._scan_directory_for_sounds(tmp_path, "sound", "TestDir")

        assert "test_sound_a" in servicer.sound_registry
        assert "test_sound_b" in servicer.sound_registry
        assert "not_audio" not in servicer.sound_registry

    def test_registers_correct_type_and_base_dir(self, servicer, tmp_path):
        """Registered entries have correct type and base_dir."""
        (tmp_path / "mysound.ogg").touch()

        servicer._scan_directory_for_sounds(tmp_path, "vox", "Zombie")

        sound_type, base_dir = servicer.sound_registry["mysound"]
        assert sound_type == "vox"
        assert base_dir == "Zombie"


class TestRegisterSound:
    """Tests for AudioServiceServicer._register_sound."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        # Clear registry for isolated tests
        servicer = mock_audio_servicer
        servicer.sound_registry = {}
        return servicer

    def test_registers_original_case(self, servicer):
        servicer._register_sound("MySound", "sound", "Joust")
        assert "MySound" in servicer.sound_registry
        assert servicer.sound_registry["MySound"] == ("sound", "Joust")

    def test_registers_lowercase_variant(self, servicer):
        servicer._register_sound("MySound", "sound", "Joust")
        assert "mysound" in servicer.sound_registry
        assert servicer.sound_registry["mysound"] == ("sound", "Joust")

    def test_first_found_wins_original_case(self, servicer):
        """First registration wins - second call doesn't overwrite."""
        servicer._register_sound("beep", "vox", "Menu")
        servicer._register_sound("beep", "sound", "Joust")

        assert servicer.sound_registry["beep"] == ("vox", "Menu")

    def test_first_found_wins_lowercase(self, servicer):
        """Lowercase variant also uses first-found-wins."""
        servicer._register_sound("Beep", "vox", "Menu")
        servicer._register_sound("beep", "sound", "Joust")

        # "beep" lowercase was registered from "Beep" first
        assert servicer.sound_registry["beep"] == ("vox", "Menu")

    def test_already_lowercase_name(self, servicer):
        """Name that is already lowercase registers both entries as same."""
        servicer._register_sound("explosion", "sound", "Joust")
        assert "explosion" in servicer.sound_registry
        assert servicer.sound_registry["explosion"] == ("sound", "Joust")

    def test_mixed_case_registers_both(self, servicer):
        """Mixed case name registers both original and lowercase."""
        servicer._register_sound("Explosion34", "sound", "Joust")
        assert "Explosion34" in servicer.sound_registry
        assert "explosion34" in servicer.sound_registry


class TestResolveSimpleName:
    """Tests for AudioServiceServicer._resolve_simple_name."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        servicer = mock_audio_servicer
        servicer.sound_registry = {
            "congratulations": ("vox", "Joust"),
            "Explosion34": ("sound", "Joust"),
            "explosion34": ("sound", "Joust"),
            "zombie_victory": ("vox", "Zombie"),
        }
        servicer.menu_voice = "ivy"
        return servicer

    def test_vox_sound_returns_voice_path(self, servicer):
        result = servicer._resolve_simple_name("congratulations")
        assert result == "Joust/vox/ivy/congratulations.ogg"

    def test_sound_effect_returns_sounds_path(self, servicer):
        result = servicer._resolve_simple_name("Explosion34")
        assert result == "Joust/sounds/Explosion34.ogg"

    def test_vox_with_different_voice(self, servicer):
        servicer.menu_voice = "aaron"
        result = servicer._resolve_simple_name("congratulations")
        assert result == "Joust/vox/aaron/congratulations.ogg"

    def test_case_insensitive_lookup(self, servicer):
        result = servicer._resolve_simple_name("explosion34")
        assert result == "Joust/sounds/explosion34.ogg"

    def test_different_base_dir(self, servicer):
        result = servicer._resolve_simple_name("zombie_victory")
        assert result == "Zombie/vox/ivy/zombie_victory.ogg"

    def test_unknown_sound_falls_back_to_joust_vox(self, servicer):
        result = servicer._resolve_simple_name("nonexistent_xyz")
        assert result == "Joust/vox/ivy/nonexistent_xyz.ogg"

    def test_unknown_sound_uses_current_voice(self, servicer):
        servicer.menu_voice = "aaron"
        result = servicer._resolve_simple_name("nonexistent_xyz")
        assert result == "Joust/vox/aaron/nonexistent_xyz.ogg"


class TestResolveVoxPath:
    """Tests for AudioServiceServicer._resolve_vox_path."""

    @pytest.fixture
    def servicer(self, mock_audio_servicer):
        servicer = mock_audio_servicer
        servicer.menu_voice = "ivy"
        return servicer

    def test_no_vox_in_path_returns_as_is(self, servicer):
        result = servicer._resolve_vox_path("Joust/sounds/beep.ogg")
        assert result == "Joust/sounds/beep.ogg"

    def test_inserts_voice_when_missing(self, servicer):
        result = servicer._resolve_vox_path("Joust/vox/congratulations.ogg")
        assert result == "Joust/vox/ivy/congratulations.ogg"

    def test_preserves_aaron_voice(self, servicer):
        result = servicer._resolve_vox_path("Joust/vox/aaron/congratulations.ogg")
        assert result == "Joust/vox/aaron/congratulations.ogg"

    def test_preserves_ivy_voice(self, servicer):
        result = servicer._resolve_vox_path("Joust/vox/ivy/congratulations.ogg")
        assert result == "Joust/vox/ivy/congratulations.ogg"

    def test_uses_current_menu_voice(self, servicer):
        servicer.menu_voice = "aaron"
        result = servicer._resolve_vox_path("Zombie/vox/zombie_death.ogg")
        assert result == "Zombie/vox/aaron/zombie_death.ogg"

    def test_path_without_vox_segment(self, servicer):
        result = servicer._resolve_vox_path("Joust/music/track.ogg")
        assert result == "Joust/music/track.ogg"

    def test_multiple_vox_segments_returns_as_is(self, servicer):
        """Path with multiple /vox/ segments (unusual) is returned as-is."""
        weird_path = "a/vox/b/vox/c.ogg"
        result = servicer._resolve_vox_path(weird_path)
        # split("/vox/") produces 3 parts, len != 2, so returns as-is
        assert result == weird_path

    def test_plain_filename_without_path(self, servicer):
        """A plain filename without path separators is returned as-is (no /vox/)."""
        result = servicer._resolve_vox_path("congratulations.ogg")
        assert result == "congratulations.ogg"
