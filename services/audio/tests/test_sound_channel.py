"""
Unit tests for SoundChannel.
"""

from __future__ import annotations

from services.audio.servicer import SoundChannel


class TestSoundChannel:
    """Tests for SoundChannel class."""

    def test_init_state(self):
        channel = SoundChannel(channel_id=3)
        assert channel.channel_id == 3
        assert channel.is_playing is False
        assert channel.priority == 0

    def test_stop_sets_not_playing(self):
        channel = SoundChannel(channel_id=0)
        channel._playing.set()
        assert channel.is_playing is True
        channel.stop()
        assert channel.is_playing is False

    def test_play_nonexistent_file_returns_false(self):
        channel = SoundChannel(channel_id=0)
        result = channel.play("/nonexistent/path/to/file.wav", volume=1.0, priority=2)
        assert result is False
        assert channel.is_playing is False

    def test_stop_idempotent(self):
        channel = SoundChannel(channel_id=0)
        channel.stop()
        channel.stop()
        assert channel.is_playing is False
