"""
Unit tests for music_player module extracted helper functions.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from services.audio.music_player import (
    DummyMusicPlayer,
    _clamp_ratio,
    _load_song_from_pattern,
    _read_samples,
    _resample_audio,
    _resample_chunk,
    _write_samples,
    lerp,
)


class TestLerp:
    """Tests for lerp (linear interpolation)."""

    def test_t_zero_returns_a(self):
        assert lerp(10.0, 20.0, 0.0) == 10.0

    def test_t_one_returns_b(self):
        assert lerp(10.0, 20.0, 1.0) == 20.0

    def test_t_half_returns_midpoint(self):
        assert lerp(0.0, 100.0, 0.5) == 50.0

    def test_negative_values(self):
        assert lerp(-10.0, 10.0, 0.5) == 0.0

    def test_same_values(self):
        assert lerp(5.0, 5.0, 0.7) == 5.0


class TestClampRatio:
    """Tests for _clamp_ratio."""

    def test_normal_ratio_passes_through(self):
        assert _clamp_ratio(1.0) == 1.0

    def test_valid_low_boundary(self):
        assert _clamp_ratio(0.6) == 0.6

    def test_valid_high_boundary(self):
        assert _clamp_ratio(2.0) == 2.0

    def test_too_low_clamps_to_one(self):
        assert _clamp_ratio(0.5) == 1.0

    def test_below_zero_clamps_to_one(self):
        assert _clamp_ratio(0.0) == 1.0

    def test_negative_clamps_to_one(self):
        assert _clamp_ratio(-1.0) == 1.0

    def test_too_high_clamps_to_one(self):
        assert _clamp_ratio(2.1) == 1.0

    def test_just_above_low_boundary(self):
        assert _clamp_ratio(0.51) == 0.51

    def test_exact_high_boundary_allowed(self):
        # 2.0 is the upper boundary (inclusive)
        assert _clamp_ratio(2.0) == 2.0


class TestReadSamples:
    """Tests for _read_samples generator."""

    def test_reads_all_frames(self):
        mock_wf = MagicMock()
        mock_wf.readframes.side_effect = [b"frame1", b"frame2", b""]
        result = list(_read_samples(mock_wf, 1024))
        assert result == [b"frame1", b"frame2"]

    def test_empty_file_yields_nothing(self):
        mock_wf = MagicMock()
        mock_wf.readframes.return_value = b""
        result = list(_read_samples(mock_wf, 1024))
        assert result == []

    def test_single_frame(self):
        mock_wf = MagicMock()
        mock_wf.readframes.side_effect = [b"data", b""]
        result = list(_read_samples(mock_wf, 512))
        assert result == [b"data"]

    def test_passes_read_size_to_readframes(self):
        mock_wf = MagicMock()
        mock_wf.readframes.return_value = b""
        list(_read_samples(mock_wf, 4096))
        mock_wf.readframes.assert_called_with(4096)


class TestResampleChunk:
    """Tests for _resample_chunk."""

    def _make_stereo_data(self, num_frames: int, value: int = 1000) -> bytes:
        """Create stereo int16 PCM data with the given number of frames."""
        # Each frame = 2 samples (left + right), each sample = 2 bytes
        array = np.full(num_frames * 2, value, dtype=np.int16)
        return array.tobytes()

    def test_returns_original_for_tiny_chunk(self):
        # With very few frames, num_output_frames < 32 so data passes through
        data = self._make_stereo_data(16)
        result = _resample_chunk(data, 1.0, 1.0)
        assert result == data

    def test_ratio_one_preserves_length_approximately(self):
        data = self._make_stereo_data(4096)
        result = _resample_chunk(data, 1.0, 1.0)
        # At ratio 1.0, output length should be close to input length
        assert abs(len(result) - len(data)) < len(data) * 0.1

    def test_faster_ratio_produces_shorter_output(self):
        data = self._make_stereo_data(4096)
        normal = _resample_chunk(data, 1.0, 1.0)
        faster = _resample_chunk(data, 1.5, 1.0)
        assert len(faster) < len(normal)

    def test_slower_ratio_produces_longer_output(self):
        data = self._make_stereo_data(4096)
        normal = _resample_chunk(data, 1.0, 1.0)
        slower = _resample_chunk(data, 0.75, 1.0)
        assert len(slower) > len(normal)

    def test_volume_zero_produces_silence(self):
        data = self._make_stereo_data(4096, value=10000)
        result = _resample_chunk(data, 1.0, 0.0)
        result_array = np.frombuffer(result, dtype=np.int16)
        assert np.all(result_array == 0)

    def test_volume_one_preserves_amplitude(self):
        data = self._make_stereo_data(4096, value=10000)
        result = _resample_chunk(data, 1.0, 1.0)
        result_array = np.frombuffer(result, dtype=np.int16)
        # With volume=1.0, amplitudes should be close to original
        assert np.mean(np.abs(result_array)) > 5000

    def test_output_is_bytes(self):
        data = self._make_stereo_data(4096)
        result = _resample_chunk(data, 1.0, 0.8)
        assert isinstance(result, bytes)

    def test_output_has_even_sample_count(self):
        # Stereo output should always have an even number of int16 samples
        data = self._make_stereo_data(4096)
        result = _resample_chunk(data, 1.2, 0.9)
        result_array = np.frombuffer(result, dtype=np.int16)
        assert len(result_array) % 2 == 0


class TestResampleAudio:
    """Tests for _resample_audio generator."""

    def _make_stereo_data(self, num_frames: int) -> bytes:
        array = np.full(num_frames * 2, 1000, dtype=np.int16)
        return array.tobytes()

    def test_passes_through_chunks(self):
        ratio_val = MagicMock()
        ratio_val.value = 1.0
        vol_val = MagicMock()
        vol_val.value = 1.0

        chunk = self._make_stereo_data(4096)
        results = list(_resample_audio([chunk], ratio_val, vol_val))
        assert len(results) == 1
        assert isinstance(results[0], bytes)

    def test_clamps_bad_ratio(self):
        ratio_val = MagicMock()
        ratio_val.value = 0.1  # Out of range, should be clamped to 1.0
        vol_val = MagicMock()
        vol_val.value = 1.0

        chunk = self._make_stereo_data(4096)
        results = list(_resample_audio([chunk], ratio_val, vol_val))
        assert len(results) == 1

    def test_yields_original_on_error(self):
        """If _resample_chunk raises, the original data is yielded."""
        ratio_val = MagicMock()
        ratio_val.value = 1.0
        vol_val = MagicMock()
        vol_val.value = 1.0

        bad_data = b"not valid pcm"
        results = list(_resample_audio([bad_data], ratio_val, vol_val))
        assert results == [bad_data]

    def test_multiple_chunks(self):
        ratio_val = MagicMock()
        ratio_val.value = 1.0
        vol_val = MagicMock()
        vol_val.value = 0.5

        chunks = [self._make_stereo_data(4096) for _ in range(3)]
        results = list(_resample_audio(chunks, ratio_val, vol_val))
        assert len(results) == 3


class TestWriteSamples:
    """Tests for _write_samples."""

    def test_writes_complete_buffers(self):
        device = MagicMock()
        stop_proc = MagicMock()
        stop_proc.value = 0

        # write_size = 8 bytes, send 16 bytes => 2 writes
        samples = [b"\x00" * 16]
        _write_samples(device, 8, samples, stop_proc)
        assert device.write.call_count == 2

    def test_stops_when_stop_proc_set(self):
        device = MagicMock()
        stop_proc = MagicMock()
        stop_proc.value = 1  # Stopped

        # stop_proc is checked after each sample is written, so the first
        # sample's writes complete but processing stops before the second sample.
        samples = [b"\x00" * 8, b"\x00" * 8]
        _write_samples(device, 8, samples, stop_proc)
        # First sample written (1 write), then stop_proc checked => return
        assert device.write.call_count == 1

    def test_handles_write_error(self):
        device = MagicMock()
        device.write.side_effect = Exception("ALSA error")
        stop_proc = MagicMock()
        stop_proc.value = 0

        # Should not raise; error is logged and loop breaks
        samples = [b"\x00" * 16]
        _write_samples(device, 8, samples, stop_proc)

    def test_partial_buffer_not_written(self):
        device = MagicMock()
        stop_proc = MagicMock()
        stop_proc.value = 0

        # 6 bytes with write_size=8 => not enough for a write
        samples = [b"\x00" * 6]
        _write_samples(device, 8, samples, stop_proc)
        device.write.assert_not_called()

    def test_multiple_samples_accumulated(self):
        device = MagicMock()
        stop_proc = MagicMock()
        stop_proc.value = 0

        # Two 6-byte samples = 12 bytes total, write_size=8 => 1 write (4 leftover)
        samples = [b"\x00" * 6, b"\x00" * 6]
        _write_samples(device, 8, samples, stop_proc)
        assert device.write.call_count == 1


class TestLoadSongFromPattern:
    """Tests for _load_song_from_pattern."""

    def test_returns_none_when_no_files_match(self):
        with patch("services.audio.music_player.glob.glob", return_value=[]):
            result = _load_song_from_pattern("/nonexistent/*.ogg")
        assert result is None

    def test_loads_matching_file(self):
        mock_segment = MagicMock()
        mock_segment.set_channels.return_value = mock_segment
        mock_segment.set_frame_rate.return_value = mock_segment
        mock_segment.set_sample_width.return_value = mock_segment

        # Mock export to write fake WAV bytes
        def fake_export(buf, **_kwargs):
            buf.write(b"RIFF_fake_wav_data")

        mock_segment.export.side_effect = fake_export

        with (
            patch("services.audio.music_player.glob.glob", return_value=["/music/song1.ogg"]),
            patch("services.audio.music_player.random.choice", return_value="/music/song1.ogg"),
            patch("services.audio.music_player.AudioSegment.from_file", return_value=mock_segment),
        ):
            result = _load_song_from_pattern("/music/*.ogg")

        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_selects_random_file_from_matches(self):
        mock_segment = MagicMock()
        mock_segment.set_channels.return_value = mock_segment
        mock_segment.set_frame_rate.return_value = mock_segment
        mock_segment.set_sample_width.return_value = mock_segment
        mock_segment.export.side_effect = lambda buf, **_kwargs: buf.write(b"wav")

        files = ["/music/a.ogg", "/music/b.ogg", "/music/c.ogg"]

        with (
            patch("services.audio.music_player.glob.glob", return_value=files),
            patch("services.audio.music_player.random.choice", return_value="/music/b.ogg") as mock_choice,
            patch("services.audio.music_player.AudioSegment.from_file", return_value=mock_segment),
        ):
            _load_song_from_pattern("/music/*.ogg")

        mock_choice.assert_called_once_with(files)


class TestMusicPlayerStartStop:
    """Tests for MusicPlayer.start() and stop() without spawning a real process."""

    @pytest.fixture
    def player(self):
        """Create a MusicPlayer with ALSA disabled (no process spawned)."""
        with patch("services.audio.music_player.HAS_ALSA", False):
            from services.audio.music_player import MusicPlayer

            return MusicPlayer(name="test")

    def test_start_returns_track_id(self, player):
        track_id = player.start()
        assert track_id is not None
        assert isinstance(track_id, str)
        assert len(track_id) > 0

    def test_start_sets_stop_proc_to_zero(self, player):
        player.start()
        assert player._stop_proc.value == 0
        assert player.is_playing is True

    def test_stop_sets_stop_proc_to_one(self, player):
        player.start()
        player.stop()
        assert player._stop_proc.value == 1
        assert player.is_playing is False

    def test_stop_clears_track_id(self, player):
        player.start()
        assert player.track_id is not None
        player.stop()
        assert player.track_id is None

    def test_start_generates_unique_ids(self, player):
        id1 = player.start()
        player.stop()
        id2 = player.start()
        assert id1 != id2


class TestTransitionRatio:
    """Tests for MusicPlayer.transition_ratio async method."""

    @pytest.fixture
    def player(self):
        with patch("services.audio.music_player.HAS_ALSA", False):
            from services.audio.music_player import MusicPlayer

            return MusicPlayer(name="test-transition")

    @pytest.mark.asyncio
    async def test_transition_reaches_target_ratio(self, player):
        player._ratio.value = 1.0
        player._stop_proc.value = 0  # playing
        task = await player.transition_ratio(1.5, duration=0.1)
        await task
        assert player._ratio.value == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_transition_aborts_when_stopped(self, player):
        player._ratio.value = 1.0
        player._stop_proc.value = 1  # stopped
        task = await player.transition_ratio(2.0, duration=0.1)
        await task
        # Ratio should stay at 1.0 since stop_proc was set
        assert player._ratio.value == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_new_transition_cancels_previous(self, player):
        player._ratio.value = 1.0
        player._stop_proc.value = 0
        # Start a long transition
        task1 = await player.transition_ratio(2.0, duration=10.0)
        # Immediately start another, which should cancel the first
        task2 = await player.transition_ratio(0.8, duration=0.1)
        await task2
        assert task1.cancelled()
        assert player._ratio.value == pytest.approx(0.8)


class TestDummyMusicPlayer:
    """Tests for DummyMusicPlayer no-op fallback."""

    def test_start_returns_track_id(self):
        p = DummyMusicPlayer("test")
        track_id = p.start()
        assert isinstance(track_id, str)
        assert len(track_id) > 0
        assert p.is_playing is True

    def test_stop_clears_state(self):
        p = DummyMusicPlayer("test")
        p.start()
        p.stop()
        assert p.is_playing is False
        assert p.track_id is None

    def test_set_ratio_and_volume(self):
        p = DummyMusicPlayer("test")
        p.set_ratio(1.5)
        assert p.ratio == 1.5
        p.set_volume(0.3)
        assert p.volume == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_transition_ratio_sets_immediately(self):
        p = DummyMusicPlayer("test")
        await p.transition_ratio(1.8, _duration=5.0)
        assert p.ratio == pytest.approx(1.8)

    def test_load_is_noop(self):
        p = DummyMusicPlayer("test")
        # Should not raise
        p.load("some/pattern/*.ogg")

    def test_cleanup_is_noop(self):
        p = DummyMusicPlayer("test")
        p.start()
        # Should not raise
        p.cleanup()
