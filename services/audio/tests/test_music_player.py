"""
Unit tests for music_player module extracted helper functions.
"""

from unittest.mock import MagicMock

import numpy as np

from services.audio.music_player import (
    _clamp_ratio,
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
