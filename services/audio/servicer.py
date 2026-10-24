"""
Audio gRPC Servicer for JoustMania

Handles audio playback with priority-based mixing and real-time tempo control.
- Sound effects: miniaudio for distroless compatibility (Phase 80)
- Background music: MusicPlayer with soxr for real-time tempo control
"""

import asyncio
import logging
import os
import threading
from pathlib import Path

import miniaudio
import numpy as np
from opentelemetry import trace

from proto import audio_pb2, audio_pb2_grpc
from services.audio import metrics, sound_catalog
from services.audio.music_player import DummyMusicPlayer, MusicPlayer

logger = logging.getLogger(__name__)


class SoundChannel:
    """
    Represents a single sound channel for concurrent playback.

    Handles decoding and streaming of a single sound effect using miniaudio.
    """

    def __init__(self, channel_id: int):
        """Initialize a sound channel."""
        self.channel_id = channel_id
        self._playing = threading.Event()
        self.priority = 0
        self._lock = threading.Lock()

    @property
    def is_playing(self) -> bool:
        """Thread-safe check if channel is currently playing."""
        return self._playing.is_set()

    def play(self, file_path: str, volume: float = 1.0, priority: int = 2) -> bool:
        """
        Play a sound on this channel.

        Args:
            file_path: Path to the audio file
            volume: Volume level (0.0 to 1.0)
            priority: Priority level for channel selection

        Returns:
            True if sound started successfully
        """
        with self._lock:
            # Stop any currently playing sound
            self.stop()

            try:
                # Get file info for duration
                file_info = miniaudio.get_file_info(file_path)

                # Start playback in a separate thread
                self.priority = priority
                self._playing.set()

                # Clamp volume to valid range
                volume = max(0.0, min(1.0, volume))

                def play_thread():
                    try:
                        if volume < 1.0:
                            self._play_with_volume(file_path, volume, file_info.duration)
                        else:
                            self._play_full_volume(file_path, file_info.duration)
                    except Exception as e:
                        logger.warning(f"Playback error on channel {self.channel_id}: {e}")
                        metrics.audio_playback_errors_total.inc()
                    finally:
                        self._playing.clear()

                threading.Thread(target=play_thread, daemon=True).start()
                return True

            except Exception as e:
                logger.error(f"Error playing sound on channel {self.channel_id}: {e}")
                self._playing.clear()
                return False

    def _play_with_volume(self, file_path: str, volume: float, duration: float):
        """Play a sound with volume scaling by decoding and resampling.

        Args:
            file_path: Path to the audio file
            volume: Volume level (0.0 to 1.0, exclusive of 1.0)
            duration: Duration in seconds from file info
        """
        decoded = miniaudio.decode_file(file_path)
        samples = np.frombuffer(decoded.samples, dtype=np.int16)  # 16-bit signed

        # Apply volume scaling (truncates toward zero, like int())
        scaled_data = (samples * np.float32(volume)).astype(np.int16).tobytes()

        # Calculate bytes per frame for proper alignment
        # Frame = one sample per channel
        bytes_per_sample = decoded.sample_width
        bytes_per_frame = bytes_per_sample * decoded.nchannels

        # Create generator that responds to framecount requests
        # miniaudio calls send(framecount) to request specific frames
        def scaled_stream():
            idx = 0
            required_frames = yield b""  # Priming yield
            while idx < len(scaled_data) and self._playing.is_set():
                bytes_needed = required_frames * bytes_per_frame
                end = min(idx + bytes_needed, len(scaled_data))
                required_frames = yield scaled_data[idx:end]
                idx = end

        device = miniaudio.PlaybackDevice(
            output_format=decoded.sample_format,
            nchannels=decoded.nchannels,
            sample_rate=decoded.sample_rate,
        )
        stream = scaled_stream()
        next(stream)  # Prime the generator for send()
        with device:
            device.start(stream)
            self._wait_for_playback(duration)

    def _play_full_volume(self, file_path: str, duration: float):
        """Play a sound at full volume using streaming for efficiency.

        Args:
            file_path: Path to the audio file
            duration: Duration in seconds from file info
        """
        stream = miniaudio.stream_file(file_path)
        device = miniaudio.PlaybackDevice()
        with device:
            device.start(stream)
            self._wait_for_playback(duration)

    def _wait_for_playback(self, duration: float):
        """Wait for playback to complete or be stopped.

        Args:
            duration: Expected duration in seconds
        """
        import time

        elapsed = 0.0
        while self._playing.is_set() and elapsed < duration + 0.5:
            time.sleep(0.05)
            elapsed += 0.05

    def stop(self):
        """Stop playback on this channel."""
        self._playing.clear()


class AudioManager:
    """
    Manages audio playback with priority-based mixing and tempo control.

    - Sound effects: miniaudio with multi-channel support (Phase 80)
    - Background music: MusicPlayer with soxr for real-time tempo control
    """

    MAX_CHANNELS = 8  # Maximum concurrent sound effects

    def __init__(self, *, silent: bool = False):
        """Initialize audio systems.

        Args:
            silent: If True, skip hardware initialization (no channels, dummy music player).
                    Controlled by the play_audio flagd flag at the servicer level.
        """
        self.silent = silent

        # Assets directory - clients send relative paths, we resolve to full path
        self.assets_dir = os.getenv("AUDIO_ASSETS_DIR", "services/audio/assets")

        # Initialize sound channels for concurrent playback
        self.channels: list[SoundChannel] = []
        if not self.silent:
            self.channels = [SoundChannel(i) for i in range(self.MAX_CHANNELS)]
            logger.info(f"Miniaudio initialized with {self.MAX_CHANNELS} channels")
        else:
            logger.info("AudioManager running in silent mode - no actual audio playback")

        # Initialize music player with tempo control
        if self.silent:
            self.music_player = DummyMusicPlayer("background")
        else:
            self.music_player = MusicPlayer("background")

        self.current_music_file: str | None = None
        self.master_volume: float = 1.0  # relative level set by SetVolume (game ramp, agent override)
        self.operator_gain: float = 1.0  # operator's audio_volume.master, multiplies master_volume (#2050)
        self.music_lock = threading.Lock()
        self.event_loop: asyncio.AbstractEventLoop | None = None  # Set from async context

        logger.info(f"AudioManager initialized (assets_dir={self.assets_dir}, silent={self.silent})")

        # Track currently playing sounds for status
        self.active_sounds: dict[str, dict] = {}

    @property
    def effective_volume(self) -> float:
        """Level every play path uses: the relative SetVolume level times the operator gain."""
        return self.master_volume * self.operator_gain

    def set_operator_gain(self, gain: float) -> None:
        """Set the operator's master gain (0.0 to 1.0) and re-apply it to the running music."""
        self.operator_gain = max(0.0, min(1.0, gain))
        try:
            self.music_player.set_volume(self.effective_volume)
        except Exception as e:
            logger.error(f"Error applying operator gain: {e}", exc_info=True)

    def enable_hardware(self) -> bool:
        """Open the audio hardware of a manager that started silent (#1352).

        Mutates this manager in place (rather than replacing it) so the event
        loop set by the server and the master volume set while muted are kept.

        Returns:
            True if the manager switched from silent to live, False if it was
            already live.
        """
        with self.music_lock:
            if not self.silent:
                return False
            self.channels = [SoundChannel(i) for i in range(self.MAX_CHANNELS)]
            self.music_player = MusicPlayer("background")
            self.music_player.set_volume(self.effective_volume)
            self.silent = False
        logger.info(f"AudioManager left silent mode: {self.MAX_CHANNELS} channels and music player initialized")
        return True

    def _update_channel_gauge(self):
        """Update the active channels gauge."""
        active = sum(1 for c in self.channels if c.is_playing)
        metrics.audio_channels_active.set(active)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for async operations (call from async context)."""
        self.event_loop = loop
        logger.debug("Event loop set for AudioManager")

    def _resolve_path(self, relative_path: str) -> str:
        """Resolve relative path to full path using assets directory."""
        # If already absolute or starts with assets dir, use as-is
        if os.path.isabs(relative_path) or relative_path.startswith(self.assets_dir):
            return relative_path
        return os.path.join(self.assets_dir, relative_path)

    def _find_channel(self, force_priority: bool = False, priority: int = 2) -> SoundChannel | None:
        """
        Find an available channel for sound playback.

        Args:
            force_priority: If True, steal lowest priority channel if all busy
            priority: Priority of the new sound

        Returns:
            Available SoundChannel or None if no channel available
        """
        # First, look for a free channel
        for channel in self.channels:
            if not channel.is_playing:
                return channel

        # If force_priority (critical sound), steal lowest priority channel
        if force_priority:
            lowest_priority_channel = min(self.channels, key=lambda c: c.priority)
            if lowest_priority_channel.priority < priority:
                return lowest_priority_channel

        return None

    def play_sound(self, file_path: str, volume: float = 1.0, priority: int = 2) -> bool:
        """
        Play a sound effect (one-shot).

        Args:
            file_path: Relative path to audio file (e.g., "Joust/sounds/beep.ogg")
            volume: Volume level (0.0 to 1.0)
            priority: Priority level (0=LOW, 3=CRITICAL)

        Returns:
            True if sound played successfully
        """
        # Resolve relative path to full path
        full_path = self._resolve_path(file_path)

        if self.silent:
            logger.debug(f"Silent mode: skipping sound: {file_path}")
            return True

        effective = self.effective_volume
        if effective <= 0.0:
            logger.debug(f"Effective volume is 0, skipping sound: {file_path}")
            return True

        try:
            if not os.path.exists(full_path):
                logger.error(f"Audio file not found: {full_path} (requested: {file_path})")
                metrics.audio_sounds_played_total.labels(result="file_not_found").inc()
                return False

            # Find available channel (or force if critical priority)
            channel = self._find_channel(force_priority=(priority == 3), priority=priority)
            if channel:
                adjusted_volume = volume * effective
                if channel.play(full_path, volume=adjusted_volume, priority=priority):
                    logger.debug(
                        f"Playing sound: {file_path} (channel={channel.channel_id}, volume={adjusted_volume:.2f})"
                    )
                    metrics.audio_sounds_played_total.labels(result="success").inc()
                    self._update_channel_gauge()
                    return True
                metrics.audio_sounds_played_total.labels(result="error").inc()
                return False

            logger.warning(f"No available channel for sound: {file_path}")
            metrics.audio_sounds_played_total.labels(result="no_channel").inc()
            metrics.audio_channel_saturation_total.inc()
            return False

        except Exception as e:
            logger.error(f"Error playing sound {file_path}: {e}", exc_info=True)
            metrics.audio_sounds_played_total.labels(result="error").inc()
            return False

    def stop_sounds(self) -> int:
        """Stop every playing sound effect (music keeps playing).

        Returns:
            Number of channels that were playing and are now stopped.
        """
        stopped = 0
        for channel in self.channels:
            if channel.is_playing:
                channel.stop()
                stopped += 1
        if stopped:
            self._update_channel_gauge()
            logger.info(f"Stopped {stopped} playing sound effect(s)")
        return stopped

    def sound_duration(self, file_path: str) -> float:
        """Return the length of a sound in seconds, or 0.0 when nothing is heard.

        Silent mode, a missing file or an unreadable header all return 0.0, so a
        caller waiting on the clip (UC-020 A5) never waits for silence.
        """
        if self.silent:
            return 0.0
        full_path = self._resolve_path(file_path)
        try:
            return float(miniaudio.get_file_info(full_path).duration)
        except Exception as e:
            logger.debug(f"No duration for {file_path}: {e}")
            return 0.0

    def play_music(
        self,
        file_pattern: str,
        _loop: bool = True,
        tempo: float = 1.0,
        _priority: int = 1,
    ) -> str | None:
        """
        Play background music with tempo control.

        Args:
            file_pattern: Glob pattern for music files (e.g., "Joust/music/*.ogg")
            loop: Whether to loop the music (always True for MusicPlayer)
            tempo: Playback speed (1.0 = normal, 1.3 = 30% faster)
            priority: Priority level (not used for music)

        Returns:
            Track ID if successful, None otherwise
        """
        # Resolve pattern to full path
        full_pattern = self._resolve_path(file_pattern)

        try:
            with self.music_lock:
                # Stop current music if playing
                if self.music_player.is_playing:
                    self.music_player.stop()

                # Load and configure music
                self.music_player.load(full_pattern)
                # At effective 0 the track still runs (silent), so unmuting brings the music back
                self.music_player.set_volume(self.effective_volume)
                self.music_player.set_ratio(tempo)

                # Start playback
                track_id = self.music_player.start()
                self.current_music_file = full_pattern

                metrics.audio_music_playing.set(1)
                metrics.audio_music_tempo.set(tempo)
                logger.info(f"Playing music: {file_pattern} (track_id={track_id}, tempo={tempo})")
                return track_id

        except Exception as e:
            logger.error(f"Error playing music {file_pattern}: {e}", exc_info=True)
            return None

    def stop_music(self, track_id: str = "") -> bool:
        """
        Stop music track.

        Args:
            track_id: ID of track to stop (optional, stops current if empty)

        Returns:
            True if stopped successfully
        """
        try:
            with self.music_lock:
                current_track = self.music_player.track_id
                # Stop if no track_id specified or if it matches
                if not track_id or current_track == track_id:
                    self.music_player.stop()
                    self.current_music_file = None
                    metrics.audio_music_playing.set(0)
                    metrics.audio_music_tempo.set(0)
                    logger.info(f"Stopped music track: {current_track}")
                    return True

                logger.warning(f"Track ID mismatch: {track_id} != {current_track}")
                return False

        except Exception as e:
            logger.error(f"Error stopping music {track_id}: {e}", exc_info=True)
            return False

    def change_tempo(self, track_id: str, new_tempo: float, transition_duration: float = 1.0) -> bool:
        """
        Change music tempo with smooth transition.

        Uses soxr for real-time tempo changes (#911).

        Args:
            track_id: ID of track to modify
            new_tempo: New playback speed (1.0 = normal, 1.3 = 30% faster)
            transition_duration: Seconds to smoothly transition

        Returns:
            True if tempo change started successfully
        """
        try:
            current_track = self.music_player.track_id
            if track_id and current_track != track_id:
                logger.warning(f"Track ID mismatch: {track_id} != {current_track}")
                return False

            # Start async tempo transition using stored event loop
            if self.event_loop is None:
                logger.error("Event loop not set - cannot change tempo")
                return False

            asyncio.run_coroutine_threadsafe(
                self.music_player.transition_ratio(new_tempo, transition_duration),
                self.event_loop,
            )

            metrics.audio_music_tempo.set(new_tempo)
            logger.debug(f"Tempo transition started: {self.music_player.ratio:.2f} -> {new_tempo:.2f}")
            return True

        except Exception as e:
            logger.error(f"Error changing tempo: {e}", exc_info=True)
            return False

    def set_volume(self, volume: float) -> bool:
        """
        Set master volume for both music and sound effects.

        Args:
            volume: Volume level (0.0 to 1.0)

        Returns:
            True if volume set successfully
        """
        try:
            self.master_volume = max(0.0, min(1.0, volume))
            self.music_player.set_volume(self.effective_volume)
            logger.debug(f"Master volume set to {self.master_volume:.2f} (effective {self.effective_volume:.2f})")
            return True
        except Exception as e:
            logger.error(f"Error setting volume: {e}", exc_info=True)
            return False


class AudioServiceServicer(audio_pb2_grpc.AudioServiceServicer):
    """gRPC servicer for Audio service."""

    def __init__(self):
        """Initialize audio servicer."""
        # Load play_audio flag eagerly to decide whether to initialize audio hardware.
        # If flagd is unavailable, default to audio enabled (hardware initialized).
        self.audio_enabled = self._load_play_audio_flag()
        self.audio_manager = AudioManager(silent=not self.audio_enabled)
        self.menu_voice = self._load_menu_voice_flag()
        self._refresh_operator_gain()
        self.sound_registry: dict[str, tuple[str, str]] = {}  # sound_name -> (type, base_dir)
        self._build_sound_registry()
        self._register_flag_change_handler()
        logger.info("AudioServiceServicer initialized (audio_enabled=%s)", self.audio_enabled)

    @staticmethod
    def _load_play_audio_flag() -> bool:
        """Load play_audio flag from flagd at startup.

        Returns True (audio enabled) if flagd is unavailable or flag is not set.
        """
        try:
            from lib.feature_flags import get_flag_client, init_flag_domain

            init_flag_domain("user")
            client = get_flag_client("user")
            value = client.get_boolean_value("play_audio", True)
            logger.info("play_audio flag loaded at startup: %s", value)
            return value
        except Exception as e:
            logger.debug("Could not load play_audio flag from flagd: %s, defaulting to enabled", e)
            return True

    @staticmethod
    def _load_menu_voice_flag() -> str:
        """Load menu_voice flag from flagd at startup.

        Returns "ivy" if flagd is unavailable or flag is not set.
        """
        try:
            from lib.feature_flags import get_flag_client, init_flag_domain

            init_flag_domain("user")
            client = get_flag_client("user")
            voice = client.get_string_value("menu_voice", "ivy")
            if voice in ("aaron", "ivy"):
                logger.info("menu_voice flag loaded at startup: %s", voice)
                return voice
            logger.warning("Invalid menu_voice value '%s', defaulting to ivy", voice)
            return "ivy"
        except Exception as e:
            logger.debug("Could not load menu_voice flag from flagd: %s, defaulting to ivy", e)
            return "ivy"

    def _register_flag_change_handler(self):
        """Register event handler for reactive flag updates from flagd."""
        try:
            from openfeature import api
            from openfeature.provider import ProviderEvent

            api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
            logger.info("Registered flag change handler for reactive audio settings")
        except Exception as e:
            logger.debug("Could not register flag change handler: %s", e)

    def _on_flags_changed(self, _event_details):
        """Handle flag configuration changes from flagd.

        Re-reads play_audio and menu_voice flags. play_audio off stops any
        currently playing music immediately; play_audio on opens the audio
        hardware if the service started muted (#1352). Runs on the OpenFeature
        event thread, so opening the hardware here never blocks the RPC loop.
        """
        try:
            from lib.feature_flags import get_flag_client

            client = get_flag_client("user")

            # Check play_audio
            self._apply_play_audio(client.get_boolean_value("play_audio", True))

            # Operator master volume: re-apply to the running music at once
            self._refresh_operator_gain()

            # Check menu_voice
            new_voice = client.get_string_value("menu_voice", "ivy")
            if new_voice in ("aaron", "ivy") and new_voice != self.menu_voice:
                logger.info("menu_voice flag changed: %s -> %s", self.menu_voice, new_voice)
                self.menu_voice = new_voice

        except Exception as e:
            logger.warning("Error handling flag change: %s", e)

    @staticmethod
    def _read_operator_gain() -> float | None:
        """Read audio_volume.master (the operator's gain); None when flagd cannot be read."""
        try:
            from lib.feature_flags import get_flag_client

            return float(get_flag_client("user").get_float_value("audio_volume.master", 1.0))
        except Exception as e:
            logger.debug("Could not read audio_volume.master flag: %s", e)
            return None

    def _refresh_operator_gain(self) -> None:
        """Apply audio_volume.master to the audio manager; an unreadable flag keeps the last gain."""
        gain = self._read_operator_gain()
        if gain is not None and gain != self.audio_manager.operator_gain:
            logger.info("audio_volume.master changed: %.2f -> %.2f", self.audio_manager.operator_gain, gain)
            self.audio_manager.set_operator_gain(gain)

    def _read_play_audio_flag(self) -> bool:
        """Read play_audio at use time (in-process flagd resolver, so cheap).

        A startup read can miss the configured value (flagd not yet reachable)
        and a change event can be missed, so the RPCs re-read the flag. When
        flagd cannot be read, the last known value is kept.
        """
        try:
            from lib.feature_flags import get_flag_client

            return bool(get_flag_client("user").get_boolean_value("play_audio", self.audio_enabled))
        except Exception as e:
            logger.debug("Could not read play_audio flag: %s, keeping %s", e, self.audio_enabled)
            return self.audio_enabled

    def _apply_play_audio(self, enabled: bool) -> None:
        """Apply a play_audio value: off stops the music, on opens the hardware if the service started muted.

        ``audio_enabled`` turns True only once the hardware is open: if opening it
        fails, the service stays muted and the next use-time read retries (#1352).
        """
        if enabled == self.audio_enabled:
            return
        if not enabled:
            logger.info("play_audio flag changed: True -> False")
            self.audio_enabled = False
            self.audio_manager.stop_music()
            return
        try:
            self.audio_manager.enable_hardware()
        except Exception as e:
            logger.error("Could not open audio hardware for play_audio (will retry): %s", e, exc_info=True)
            return
        logger.info("play_audio flag changed: False -> True")
        self.audio_enabled = True

    async def _refresh_play_audio(self) -> None:
        """Re-read play_audio at use time and apply it (#1352).

        Opening the hardware spawns the music player process (seconds on a Pi on
        the first start), so that path runs in a worker thread, off the event loop.
        """
        enabled = self._read_play_audio_flag()
        if enabled and not self.audio_enabled and self.audio_manager.silent:
            await asyncio.to_thread(self._apply_play_audio, enabled)
        else:
            self._apply_play_audio(enabled)

    def _build_sound_registry(self):
        """
        Build registry of available sounds by scanning asset directories.

        Populates self.sound_registry with mappings from sound name to type.
        Registry values are tuples of (type, base_dir) where:
        - type: "vox" or "sound"
        - base_dir: "Joust", "Menu", "Zombie", "Fight_Club", or "Commander"

        Aliases (``sound_catalog.SOUND_ALIASES``, e.g. ``agent_cue``) are resolved
        at lookup time, so they always follow the file they name.
        """
        base_assets_dir = Path(__file__).parent / "assets"

        for base_dir in sound_catalog.SOUND_BASE_DIRS:
            assets_dir = base_assets_dir / base_dir
            # Scan vox directory (use aaron as reference - all voices should have same files)
            self._scan_directory_for_sounds(assets_dir / "vox" / sound_catalog.REFERENCE_VOICE, "vox", base_dir)
            # Scan sounds directory
            self._scan_directory_for_sounds(assets_dir / "sounds", "sound", base_dir)

        logger.info(f"Sound registry built: {len(self.sound_registry)} sounds indexed")

    def _scan_directory_for_sounds(self, directory: Path, sound_type: str, base_dir: str):
        """Scan a directory for .ogg files and register them.

        Args:
            directory: Directory to scan for .ogg files
            sound_type: Type of sound ("vox" or "sound")
            base_dir: Base directory name (e.g., "Joust", "Menu")
        """
        sound_catalog.scan_directory(self.sound_registry, directory, sound_type, base_dir)

    def _register_sound(self, sound_name: str, sound_type: str, base_dir: str):
        """Register a sound name in the registry if not already present.

        First-found wins: existing entries are not overwritten.
        Both original case and lowercase variants are registered.

        Args:
            sound_name: The sound name (filename without extension)
            sound_type: Type of sound ("vox" or "sound")
            base_dir: Base directory name (e.g., "Joust", "Menu")
        """
        sound_catalog.register_sound(self.sound_registry, sound_name, sound_type, base_dir)

    def _resolve_sound_path(self, sound_input: str) -> str:
        """
        Resolve a sound name or path to a full file path.

        Accepts multiple input formats:
        - Simple name: "congratulations" -> {base_dir}/vox/{voice}/congratulations.ogg
        - Name with extension: "congratulations.ogg" -> {base_dir}/vox/{voice}/congratulations.ogg
        - Partial path: "Joust/vox/congratulations.ogg" -> Joust/vox/{voice}/congratulations.ogg
        - Full path with voice: "Joust/vox/aaron/congratulations.ogg" -> used as-is

        The registry tracks both sound type (vox/sound) and base directory (Joust/Menu).

        Args:
            sound_input: Sound name or path

        Returns:
            Full resolved path to the sound file
        """
        # Strip .ogg extension if present for lookup
        lookup_name = sound_input
        if lookup_name.endswith(".ogg"):
            lookup_name = lookup_name[:-4]

        # If it's a simple name (no path separators), look up in registry
        if "/" not in lookup_name and "\\" not in lookup_name:
            return self._resolve_simple_name(lookup_name)

        # Handle paths - check if voice insertion is needed
        return self._resolve_vox_path(sound_input)

    def _resolve_simple_name(self, lookup_name: str) -> str:
        """Resolve a simple sound name (no path separators) via the registry.

        Args:
            lookup_name: Sound name without extension or path separators

        Returns:
            Resolved path to the sound file
        """
        resolved = sound_catalog.relative_path(self.sound_registry, lookup_name, voice=self.menu_voice)
        if resolved is not None:
            return resolved
        # Unknown sound id: a caller asked for something that will most likely
        # play nothing (the agent_cue gap in #1301 was invisible at DEBUG). Keep
        # the legacy Joust-vox fallback, but say so loudly (UC-040 BR-007).
        logger.warning(
            f"Unknown sound id '{lookup_name}': not in the sound registry or its aliases; "
            f"trying Joust/vox/{self.menu_voice}/{lookup_name}.ogg"
        )
        return f"Joust/vox/{self.menu_voice}/{lookup_name}.ogg"

    def _resolve_vox_path(self, sound_input: str) -> str:
        """Resolve a path that may need voice directory insertion.

        If the path contains '/vox/' but no voice folder, inserts the current
        menu voice. Paths with voice already present are returned as-is.

        Args:
            sound_input: Sound path potentially containing '/vox/'

        Returns:
            Path with voice directory inserted if needed
        """
        if "/vox/" not in sound_input:
            return sound_input
        parts = sound_input.split("/vox/")
        if len(parts) != 2:
            return sound_input
        remainder = parts[1]
        if remainder.startswith("aaron/") or remainder.startswith("ivy/"):
            return sound_input
        return f"{parts[0]}/vox/{self.menu_voice}/{remainder}"

    async def PlaySound(self, request, _context):
        """Play a sound effect."""

        # Resolve sound name/path to full path with voice selection
        resolved_path = self._resolve_sound_path(request.file_path)

        # Extract sound name for span (e.g., "congratulations" from "Joust/vox/ivy/congratulations.ogg")
        sound_name = Path(resolved_path).stem

        # Enrich the server span created by the gRPC interceptor
        span = trace.get_current_span()
        span.update_name(f"PlaySound:{sound_name}")
        span.set_attribute("audio.file", resolved_path)

        # Check if audio is disabled via settings (read at use time, #1352)
        await self._refresh_play_audio()
        self._refresh_operator_gain()
        if not self.audio_enabled:
            span.set_attribute("audio.muted", True)
            return audio_pb2.PlaySoundResponse(success=True, error="")  # Silently succeed
        if self.audio_manager.silent:
            # Enabled, but the hardware is not open: nothing will be heard.
            span.set_attribute("audio.silent", True)

        success = self.audio_manager.play_sound(
            file_path=resolved_path, volume=request.volume or 1.0, priority=request.priority
        )
        duration = self.audio_manager.sound_duration(resolved_path) if success else 0.0
        span.set_attribute("audio.duration_seconds", duration)

        return audio_pb2.PlaySoundResponse(
            success=success, error="" if success else "Failed to play sound", duration_seconds=duration
        )

    async def PlayMusic(self, request, _context):
        """Play background music."""

        # Extract music directory for span (e.g., "Joust" from "Joust/music/*.ogg")
        music_dir = request.file_pattern.split("/")[0] if "/" in request.file_pattern else "music"

        # Enrich the server span created by the gRPC interceptor
        span = trace.get_current_span()
        span.update_name(f"PlayMusic:{music_dir}")
        span.set_attribute("audio.pattern", request.file_pattern)

        # Check if audio is disabled via settings (read at use time, #1352)
        await self._refresh_play_audio()
        self._refresh_operator_gain()
        if not self.audio_enabled:
            span.set_attribute("audio.muted", True)
            return audio_pb2.PlayMusicResponse(track_id="muted", success=True, error="")

        track_id = self.audio_manager.play_music(
            file_pattern=request.file_pattern,
            _loop=request.loop,
            tempo=request.tempo or 1.0,
            _priority=request.priority,
        )

        if track_id:
            return audio_pb2.PlayMusicResponse(track_id=track_id, success=True, error="")
        return audio_pb2.PlayMusicResponse(track_id="", success=False, error="Failed to play music")

    async def StopSound(self, _request, _context):
        """Stop every playing sound effect (UC-020 A5 force-end, #1586)."""
        span = trace.get_current_span()
        try:
            stopped = self.audio_manager.stop_sounds()
        except Exception as e:
            logger.error(f"Error stopping sounds: {e}", exc_info=True)
            return audio_pb2.StopSoundResponse(success=False, error="Failed to stop sounds")
        span.set_attribute("audio.stopped", stopped)
        return audio_pb2.StopSoundResponse(success=True, error="", stopped=stopped)

    async def StopMusic(self, request, _context):
        """Stop music track."""
        success = self.audio_manager.stop_music(request.track_id)
        return audio_pb2.StopMusicResponse(success=success, error="" if success else "Failed to stop music")

    def ChangeTempo(self, request, _context):
        """Change music tempo."""
        span = trace.get_current_span()
        span.update_name(f"ChangeTempo:{request.new_tempo:.1f}x")
        span.set_attribute("audio.new_tempo", request.new_tempo)
        success = self.audio_manager.change_tempo(
            track_id=request.track_id,
            new_tempo=request.new_tempo,
            transition_duration=request.transition_duration or 1.0,
        )
        return audio_pb2.ChangeTempoResponse(success=success, error="" if success else "Failed to change tempo")

    def SetVolume(self, request, _context):
        """Set master volume."""
        span = trace.get_current_span()
        span.update_name(f"SetVolume:{request.volume:.0%}")
        success = self.audio_manager.set_volume(request.volume)
        return audio_pb2.SetVolumeResponse(success=success, error="" if success else "Failed to set volume")
