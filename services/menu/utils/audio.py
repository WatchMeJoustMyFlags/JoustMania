"""Audio utilities for the Menu service."""

import asyncio
import logging

import grpc
import grpc.aio

from lib.types import Sound
from lib.voice_prompts import GAME_MODE_VOICE
from services.menu import metrics
from services.menu.utils.detached import run_detached
from services.menu.utils.settings import validate_mode_keys

logger = logging.getLogger(__name__)

# Deadline of every audio RPC the menu sends (#2378, UC-010 BR-013). A recovery bound,
# not a stage knob, so a code constant and no flag (C-035). PlaySound returns when the
# clip is queued, not when it ends, so a healthy audio service answers well inside it.
AUDIO_RPC_TIMEOUT_SECONDS = 2.0


def _count_failure(rpc: str, error: Exception) -> None:
    """Count a failed audio RPC: ``timeout`` when its deadline passed, else ``error``."""
    timed_out = isinstance(error, grpc.aio.AioRpcError) and error.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    timed_out = timed_out or isinstance(error, TimeoutError)
    metrics.audio_rpc_failures_total.labels(rpc=rpc, reason="timeout" if timed_out else "error").inc()


# Default per-channel volumes (F7, #766). These double as the flagd user-domain
# defaults, so promotion is behavior-neutral.
DEFAULT_SOUND_VOLUME = 0.8
DEFAULT_VOICE_VOLUME = 0.9
DEFAULT_LOBBY_MUSIC_VOLUME = 0.4


def _read_volume(client, flag_name: str, default: float) -> float:
    """Read a per-channel volume from the user domain with validation.

    Falls back to the hardcoded default on malformed values (not a number in
    [0.0, 1.0]) or any read error / missing client.
    """
    if client is None:
        return default
    try:
        value = client.get_float_value(flag_name, default)
        if not isinstance(value, int | float) or isinstance(value, bool) or not (0.0 <= value <= 1.0):
            logger.warning(f"Malformed volume {flag_name}={value!r}, using default {default}")
            return default
        return float(value)
    except Exception as e:
        logger.warning(f"Failed to read volume {flag_name}, using default {default}: {e}")
        return default


# Game mode voice announcements: lib/voice_prompts.py, shared with the operator pad (#2106).
validate_mode_keys(GAME_MODE_VOICE, "GAME_MODE_VOICE")


class AudioHelper:
    """
    Manages audio playback for the Menu service.

    Provides methods for playing sounds, voice announcements, and lobby music.
    """

    def __init__(self, audio_channel: grpc.aio.Channel, user_prefs_client=None):
        """
        Initialize audio helper.

        Args:
            audio_channel: gRPC channel to Audio service
            user_prefs_client: OpenFeature client for the user domain (F7, #766).
                Per-channel volumes are read once at init; falls back to the
                hardcoded defaults when None or on malformed flag values.
        """
        self.audio_channel = audio_channel
        self.lobby_music_track_id: str | None = None
        # Detached PlaySound tasks, held until they end (run_detached, #2378).
        self._sound_tasks: set[asyncio.Task] = set()

        # Per-channel volumes — read once at menu init (init-frozen).
        self.sound_volume = _read_volume(user_prefs_client, "audio_volume.menu_sound", DEFAULT_SOUND_VOLUME)
        self.voice_volume = _read_volume(user_prefs_client, "audio_volume.menu_voice", DEFAULT_VOICE_VOLUME)
        self.lobby_music_volume = _read_volume(
            user_prefs_client, "audio_volume.lobby_music", DEFAULT_LOBBY_MUSIC_VOLUME
        )

    async def play_sound(self, sound: str | Sound, volume: float | None = None) -> None:
        """
        Play a sound effect via the audio service, without waiting for the RPC (#2378).

        The request is sent from a detached task (``run_detached``), so a slow or hung
        audio service never holds back the caller: the menu's serial button loop, a
        handler or the game start (UC-010 BR-013). The RPC has a deadline of
        ``AUDIO_RPC_TIMEOUT_SECONDS``; a failure is logged and counted.

        Args:
            sound: Sound enum or relative path to audio file
            volume: Volume level 0.0-1.0; None uses the configured sound volume
        """
        if volume is None:
            volume = self.sound_volume
        run_detached(self._send_sound(sound, volume), tasks=self._sound_tasks, what=f"Sound {sound}")

    async def _send_sound(self, sound: str | Sound, volume: float) -> None:
        try:
            from proto import audio_pb2, audio_pb2_grpc

            # Convert Sound enum to string value if needed
            sound_name = sound.value if isinstance(sound, Sound) else sound

            stub = audio_pb2_grpc.AudioServiceStub(self.audio_channel)
            await stub.PlaySound(
                audio_pb2.PlaySoundRequest(
                    file_path=sound_name,
                    volume=volume,
                    priority=audio_pb2.AudioPriority.HIGH,
                ),
                timeout=AUDIO_RPC_TIMEOUT_SECONDS,
            )
            logger.debug(f"Played sound: {sound_name}")
        except Exception as e:
            _count_failure("PlaySound", e)
            logger.warning(f"Could not play sound {sound}: {e}")

    async def play_voice(self, voice: str | Sound, volume: float | None = None) -> None:
        """
        Play a voice announcement (detached, like ``play_sound``).

        Args:
            voice: Sound enum or voice file name (audio service resolves the path)
            volume: Volume level 0.0-1.0; None uses the configured voice volume
        """
        if volume is None:
            volume = self.voice_volume
        await self.play_sound(voice, volume)

    async def play_game_mode_voice(self, game_mode: str) -> None:
        """
        Play the voice announcement for a game mode (detached, like ``play_sound``).

        Args:
            game_mode: Name of the game mode
        """
        voice = GAME_MODE_VOICE.get(game_mode)
        if voice:
            await self.play_voice(voice)

    async def start_lobby_music(self) -> None:
        """
        Start quiet background music for the lobby/menu.

        Uses a lower volume than game music for a relaxed atmosphere. Awaited (the
        lobby restore orders it), but each RPC has the ``AUDIO_RPC_TIMEOUT_SECONDS``
        deadline; a failure is logged and counted.
        """
        rpc = "SetVolume"
        try:
            from proto import audio_pb2, audio_pb2_grpc

            stub = audio_pb2_grpc.AudioServiceStub(self.audio_channel)

            # Set lobby volume (quieter than game)
            await stub.SetVolume(
                audio_pb2.SetVolumeRequest(volume=self.lobby_music_volume), timeout=AUDIO_RPC_TIMEOUT_SECONDS
            )

            # Start lobby music
            rpc = "PlayMusic"
            response = await stub.PlayMusic(
                audio_pb2.PlayMusicRequest(
                    file_pattern="Menu/music/*.ogg",
                    loop=True,
                    tempo=1.0,
                    priority=audio_pb2.AudioPriority.LOW,
                ),
                timeout=AUDIO_RPC_TIMEOUT_SECONDS,
            )

            if response.success:
                self.lobby_music_track_id = response.track_id
                logger.info(f"Lobby music started: {response.track_id}")
            else:
                logger.warning(f"Failed to start lobby music: {response.error}")

        except Exception as e:
            _count_failure(rpc, e)
            logger.warning(f"Could not start lobby music ({rpc}): {e}")

    async def stop_lobby_music(self) -> None:
        """Stop lobby music when game starts.

        Awaited, so the stop reaches the audio service before the game's own music
        starts; the ``AUDIO_RPC_TIMEOUT_SECONDS`` deadline bounds how long a hung audio
        service can hold the game start (UC-010 BR-013).
        """
        try:
            from proto import audio_pb2, audio_pb2_grpc

            stub = audio_pb2_grpc.AudioServiceStub(self.audio_channel)

            # Stop music (empty track_id stops any playing music)
            await stub.StopMusic(audio_pb2.StopMusicRequest(track_id=""), timeout=AUDIO_RPC_TIMEOUT_SECONDS)
            self.lobby_music_track_id = None
            logger.info("Lobby music stopped")

        except Exception as e:
            _count_failure("StopMusic", e)
            logger.warning(f"Could not stop lobby music: {e}")
