"""
Music Player with Real-Time Tempo Control

Phase 70: Dynamic Music System
Phase 80: Migrated from scipy to resampy for distroless compatibility

Uses resampy for real-time tempo changes without pitch shifting.
Runs audio playback in a separate process to avoid blocking the gRPC server.
"""

import asyncio
import contextlib
import glob
import io
import logging
import os
import random
import time
import wave
from multiprocessing import Array, Process, Value
from sys import platform

import numpy as np
import resampy
from pydub import AudioSegment

logger = logging.getLogger(__name__)

# Platform-specific audio backend
if platform == "linux" or platform == "linux2":
    try:
        import alsaaudio

        HAS_ALSA = True
    except ImportError:
        HAS_ALSA = False
        logger.warning("alsaaudio not available, music playback disabled")
else:
    HAS_ALSA = False
    logger.info("Non-Linux platform, using pygame for music")


def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation between a and b by t (0.0 to 1.0)."""
    return a * (1 - t) + b * t


def _try_set_process_priority():
    """Try to set higher process priority for smoother audio. Silently ignored on failure."""
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        proc.nice(-5)
    except Exception:
        pass


def _load_song_from_pattern(pattern: str) -> bytes | None:
    """
    Load a random song matching the glob pattern and return WAV bytes.

    Returns None if no files match or loading fails.
    """
    files = glob.glob(pattern)
    if not files:
        logger.error(f"No files match pattern: {pattern}")
        return None

    random_song = random.choice(files)
    logger.info(f"Loading music: {random_song}")

    segment = AudioSegment.from_file(random_song)
    segment = segment.set_channels(2).set_frame_rate(44100).set_sample_width(2)

    buf = io.BytesIO()
    segment.export(buf, format="wav")
    wav_bytes = buf.getvalue()
    logger.info(f"Music loaded: {random_song} ({len(wav_bytes)} bytes)")
    return wav_bytes


def _read_samples(wf, read_size):
    """Generator to read audio frames from a wave file."""
    while True:
        sample = wf.readframes(read_size)
        if len(sample) > 0:
            yield sample
        else:
            return


def _clamp_ratio(ratio: float) -> float:
    """Clamp playback ratio to safe range, defaulting to 1.0 if out of bounds."""
    if ratio <= 0.5 or ratio > 2.0:
        return 1.0
    return ratio


def _resample_chunk(data: bytes, ratio: float, volume: float) -> bytes:
    """
    Resample a single stereo audio chunk for tempo change with volume.

    Args:
        data: Raw PCM bytes (int16, stereo interleaved)
        ratio: Playback speed ratio (1.0 = normal)
        volume: Volume multiplier (0.0 to 1.0)

    Returns:
        Resampled PCM bytes, or original data if chunk is too small.
    """
    array = np.frombuffer(data, dtype=np.int16)

    num_input_frames = len(array) // 2  # Stereo
    num_output_frames = int(num_input_frames / ratio) & (~0x1F)
    if num_output_frames < 32:
        return data

    # Split stereo channels and convert to float for resampy
    left_in = array[0::2].astype(np.float64)
    right_in = array[1::2].astype(np.float64)

    # Resample using resampy (audio-quality resampling)
    sr_in = 44100
    sr_out = int(44100 / ratio)
    left = resampy.resample(left_in, sr_in, sr_out, filter="kaiser_fast")
    right = resampy.resample(right_in, sr_in, sr_out, filter="kaiser_fast")

    # Apply volume and convert back to int16
    left = (left * volume).astype(np.int16)
    right = (right * volume).astype(np.int16)

    # Interleave channels
    final = np.empty(len(left) * 2, dtype=np.int16)
    final[0::2] = left
    final[1::2] = right

    return final.tobytes()


def _resample_audio(samples, ratio_val, vol_val):
    """Generator that resamples audio chunks for tempo change with volume using resampy."""
    for data in samples:
        try:
            ratio = _clamp_ratio(ratio_val.value)
            yield _resample_chunk(data, ratio, vol_val.value)
        except Exception as e:
            logger.warning(f"Resample error: {e}")
            yield data


def _write_samples(device, write_size, samples, stop_proc, generation, play_gen):
    """Write resampled audio samples to an ALSA device.

    Stops early if stop_proc is set or if the generation counter has changed
    (indicating a new start() call, so this playback session is stale).
    """
    buf = bytearray()
    for sample in samples:
        buf.extend(sample)
        while len(buf) >= write_size:
            try:
                device.write(bytes(buf[:write_size]))
            except Exception as e:
                logger.warning(f"ALSA write error: {e}")
                break
            del buf[:write_size]
        if stop_proc.value or generation.value != play_gen:
            return


def _play_loaded_song(wav_data, period, period_bytes, ratio, volume, stop_proc, generation, play_gen):
    """
    Open and play a loaded WAV through ALSA with real-time resampling.

    Returns True if playback completed normally, False if WAV was invalid.
    """
    wf = wave.open(io.BytesIO(wav_data), "rb")
    if len(wf.readframes(1)) == 0:
        logger.error("Empty WAV data")
        return False
    wf.rewind()

    device = alsaaudio.PCM(
        channels=wf.getnchannels(),
        rate=wf.getframerate(),
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=period,
        device="default",
    )

    _write_samples(
        device, period_bytes, _resample_audio(_read_samples(wf, period), ratio, volume), stop_proc, generation, play_gen
    )

    wf.close()
    device.close()

    if stop_proc.value == 0:
        logger.debug("Music track finished, looping")
    return True


def _get_song_pattern(song_array: Array) -> str:
    """Read the song pattern from a shared byte array."""
    return song_array.value.decode("utf-8")


def _set_song_pattern(song_array: Array, pattern: str) -> None:
    """Write a song pattern to a shared byte array."""
    song_array.value = pattern.encode("utf-8")


def _linux_audio_loop(song_array: Array, ratio: Value, volume: Value, stop_proc: Value, generation: Value):
    """
    Linux audio playback loop using ALSA with real-time resampling.

    Runs in a separate process for non-blocking playback.

    Args:
        song_array: Shared byte array containing glob pattern for current song
        ratio: Shared Value for playback speed (1.0 = normal)
        volume: Shared Value for volume (0.0 to 1.0)
        stop_proc: Shared Value to control playback (0=play, 1=stop)
        generation: Shared Value incremented on each start(), used to detect stale playback
    """
    period = 1024 * 4
    period_bytes = period * 2 * 2  # Two channels, two bytes per sample

    time.sleep(0.1)  # Brief startup delay
    _try_set_process_priority()

    song_loaded = False
    wav_data = None
    loaded_pattern = ""
    last_gen = generation.value

    while True:
        try:
            # Detect generation change (new start() was called) — force reload
            if generation.value != last_gen:
                song_loaded = False
                loaded_pattern = ""
                last_gen = generation.value

            if stop_proc.value == 1:
                song_loaded = False
                loaded_pattern = ""
                time.sleep(0.05)
                continue

            current_pattern = _get_song_pattern(song_array)

            if current_pattern == "":
                song_loaded = False
                loaded_pattern = ""
                time.sleep(0.05)
                continue

            # Detect pattern change and force reload. This handles the race
            # where stop()+load()+start() are called in quick succession:
            # stop() sets stop_proc=1, but start() may reset it to 0 before
            # this loop checks, so song_loaded would never be cleared. Tracking
            # the pattern ensures we always reload when the song changes.
            if current_pattern != loaded_pattern:
                song_loaded = False

            if not song_loaded:
                wav_data = _load_song_from_pattern(current_pattern)
                if wav_data is None:
                    time.sleep(0.5)
                    continue
                song_loaded = True
                loaded_pattern = current_pattern
                continue

            play_gen = generation.value
            if not _play_loaded_song(wav_data, period, period_bytes, ratio, volume, stop_proc, generation, play_gen):
                song_loaded = False

        except Exception as e:
            logger.error(f"Audio loop error: {e}")
            time.sleep(0.5)
            song_loaded = False


class MusicPlayer:
    """
    Music player with real-time tempo control.

    Uses a separate process for audio playback with resampy resampling.
    """

    def __init__(self, name: str = "music"):
        """
        Initialize music player.

        Args:
            name: Player instance name for logging
        """
        self.name = name
        self._use_alsa = HAS_ALSA and (platform == "linux" or platform == "linux2")

        # Shared state for inter-process communication
        self._stop_proc = Value("i", 1)  # stopped; set to zero to play
        self._ratio = Value("d", 1.0)  # Playback speed
        self._volume = Value("d", 0.7)  # Volume level
        self._generation = Value("i", 0)  # Incremented on each start() to break stale playback

        # Shared byte array for song pattern (replaces Manager().dict() to avoid
        # BrokenPipeError when the Manager subprocess crashes)
        self._song_array = Array("c", 4096)

        self._process = None
        self._track_id = None
        self._transitioning = False
        self._transition_task = None

        if self._use_alsa:
            self._spawn_audio_process()
            logger.info(f"MusicPlayer '{name}' initialized with ALSA backend")
        else:
            logger.info(f"MusicPlayer '{name}' initialized (no tempo control - using pygame fallback)")

    def _spawn_audio_process(self):
        """Spawn (or respawn) the audio playback subprocess."""
        if self._process and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)
        args = (self._song_array, self._ratio, self._volume, self._stop_proc, self._generation)
        self._process = Process(target=_linux_audio_loop, args=args, daemon=True)
        self._process.start()

    def _ensure_process_alive(self):
        """Check if the audio process is alive; respawn if dead."""
        if not self._use_alsa:
            return
        if self._process and self._process.is_alive():
            return
        exit_code = self._process.exitcode if self._process else None
        logger.warning(f"MusicPlayer '{self.name}': audio process died (exit code {exit_code}), respawning")
        self._spawn_audio_process()

    def load(self, file_pattern: str):
        """
        Load music file pattern.

        Args:
            file_pattern: Glob pattern for music files
        """
        _set_song_pattern(self._song_array, file_pattern)
        logger.debug(f"Music pattern loaded: {file_pattern}")

    def start(self) -> str:
        """
        Start music playback.

        Returns:
            Track ID for this playback session
        """
        import uuid

        self._ensure_process_alive()
        self._track_id = str(uuid.uuid4())
        self._generation.value += 1
        self._stop_proc.value = 0
        logger.info(f"Music started: {self._track_id}")
        return self._track_id

    def stop(self):
        """Stop music playback."""
        self._stop_proc.value = 1
        _set_song_pattern(self._song_array, "")

        # Cancel any ongoing transition
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
        self._transitioning = False

        logger.info(f"Music stopped: {self._track_id}")
        self._track_id = None

    def set_ratio(self, ratio: float):
        """
        Set playback speed immediately.

        Args:
            ratio: Playback speed (1.0 = normal, 1.3 = 30% faster)
        """
        self._ratio.value = max(0.5, min(2.0, ratio))
        logger.debug(f"Music ratio set: {ratio}")

    def set_volume(self, volume: float):
        """
        Set playback volume.

        Args:
            volume: Volume level (0.0 to 1.0)
        """
        self._volume.value = max(0.0, min(1.0, volume))
        logger.debug(f"Music volume set: {volume}")

    async def transition_ratio(self, new_ratio: float, duration: float = 1.0):
        """
        Smoothly transition to a new playback speed.

        Args:
            new_ratio: Target playback speed
            duration: Transition duration in seconds
        """
        # Cancel any existing transition
        if self._transition_task and not self._transition_task.done():
            self._transition_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._transition_task

        async def do_transition():
            num_steps = 20
            old_ratio = self._ratio.value
            step_duration = duration / num_steps

            for i in range(num_steps):
                if self._stop_proc.value == 1:  # Stopped
                    return
                t = (i + 1) / num_steps
                ratio = lerp(old_ratio, new_ratio, t)
                self._ratio.value = ratio
                await asyncio.sleep(step_duration)

            self._ratio.value = new_ratio
            logger.info(f"Music tempo transition complete: {old_ratio:.2f} -> {new_ratio:.2f}")

        self._transition_task = asyncio.create_task(do_transition())
        return self._transition_task

    @property
    def ratio(self) -> float:
        """Current playback ratio."""
        return self._ratio.value

    @property
    def volume(self) -> float:
        """Current volume level."""
        return self._volume.value

    @property
    def is_playing(self) -> bool:
        """Whether music is currently playing."""
        return self._stop_proc.value == 0

    @property
    def track_id(self) -> str | None:
        """Current track ID."""
        return self._track_id

    def cleanup(self):
        """Clean up resources."""
        self.stop()
        if self._process and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)
        logger.info(f"MusicPlayer '{self.name}' cleaned up")


class DummyMusicPlayer:
    """Dummy music player for mock mode or when audio is unavailable."""

    def __init__(self, name: str = "dummy"):
        self.name = name
        self._ratio = 1.0
        self._volume = 0.7
        self._playing = False
        self._track_id = None

    def load(self, file_pattern: str):
        # No-op: dummy player has no audio engine to load files into
        pass

    def start(self) -> str:
        import uuid

        self._track_id = str(uuid.uuid4())
        self._playing = True
        return self._track_id

    def stop(self):
        self._playing = False
        self._track_id = None

    def set_ratio(self, ratio: float):
        self._ratio = ratio

    def set_volume(self, volume: float):
        self._volume = volume

    async def transition_ratio(self, new_ratio: float, _duration: float = 1.0):
        self._ratio = new_ratio

    @property
    def ratio(self) -> float:
        return self._ratio

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def track_id(self) -> str | None:
        return self._track_id

    def cleanup(self):
        # No-op: dummy player holds no resources that need releasing
        pass
