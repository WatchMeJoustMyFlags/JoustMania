"""Cues of the operator pad (UC-015 BR-009): existing assets only, played through the audio service.

The menu plays the success voice lines of the commands it runs and the failure cue of a failed
flag write; the pad plays what only it knows: refusals it decides itself, the limit and cancel
cues, the lock directions, the tap announcements read from `GetMenuStatus`, the unknown-key cue,
and the failure cue when the menu itself cannot be reached.
"""

from __future__ import annotations

import asyncio
import logging

from lib.types import Sound
from proto import audio_pb2

logger = logging.getLogger(__name__)

SOUND_VOLUME = 0.8  # the menu's code default for `audio_volume.menu_sound`
VOICE_VOLUME = 0.9  # the menu's code default for `audio_volume.menu_voice`
PLAY_TIMEOUT_SECONDS = 1.0

# Short sounds, each distinct from the two-beep failure cue and from the voice lines.
REFUSAL_CUE = Sound.SFX_DEATH
LIMIT_CUE = Sound.SFX_WOLFDOWN
CANCEL_CUE = Sound.MENU_SFX_SENSITIVITY_SLOW
UNKNOWN_KEY_CUE = Sound.SFX_EXPLOSION_22
FAILURE_CUE = Sound.SFX_BEEP_LOUD
FAILURE_CUE_GAP_SECONDS = 0.3

# Lock directions: own voice lines, distinct from the agent stop / resume lines (ADMINOP_OFF / _ON).
LOCK_VOICES = {True: Sound.MENU_VOX_ADMINOP_TRUE, False: Sound.MENU_VOX_ADMINOP_FALSE}

# Tap announcements (UC-015 BR-011), the same voice tables as the menu (services/menu/settings_ops.py).
SENSITIVITY_VOICES = [
    Sound.MENU_VOX_SENSITIVITY_ULTRA_HIGH,
    Sound.MENU_VOX_SENSITIVITY_HIGH,
    Sound.MENU_VOX_SENSITIVITY_MEDIUM,
    Sound.MENU_VOX_SENSITIVITY_LOW,
    Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW,
]
NUMBER_VOICES = {
    1: Sound.MENU_VOX_ADMINOP_1,
    2: Sound.MENU_VOX_ADMINOP_2,
    3: Sound.MENU_VOX_ADMINOP_3,
    4: Sound.MENU_VOX_ADMINOP_4,
    5: Sound.MENU_VOX_ADMINOP_5,
    6: Sound.MENU_VOX_ADMINOP_6,
}
AGENT_LEVEL_VOICES = {
    "none": Sound.MENU_VOX_ADMINOP_OFF,
    "ambient": Sound.MENU_VOX_ADMINOP_1,
    "standard": Sound.MENU_VOX_ADMINOP_2,
    "full": Sound.MENU_VOX_ADMINOP_3,
}
AGENT_STOPPED_VOICE = Sound.MENU_VOX_ADMINOP_OFF


class CuePlayer:
    """Best-effort `PlaySound` calls; a failed cue is logged, never raised."""

    def __init__(self, audio_stub, sleep=asyncio.sleep) -> None:
        self._stub = audio_stub
        self._sleep = sleep

    async def play(self, sound: Sound, volume: float = SOUND_VOLUME) -> None:
        try:
            await self._stub.PlaySound(
                audio_pb2.PlaySoundRequest(file_path=sound.value, volume=volume, priority=audio_pb2.AudioPriority.HIGH),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(f"Pad cue {sound} failed: {e}")

    async def voice(self, sound: Sound) -> None:
        await self.play(sound, VOICE_VOLUME)

    async def refusal(self) -> None:
        await self.play(REFUSAL_CUE)

    async def limit(self) -> None:
        await self.play(LIMIT_CUE)

    async def cancel(self) -> None:
        await self.play(CANCEL_CUE)

    async def unknown_key(self) -> None:
        await self.play(UNKNOWN_KEY_CUE)

    async def failure(self) -> None:
        """Two loud beeps (UC-015 BR-009), as the menu plays them for a failed write."""
        await self.play(FAILURE_CUE)
        await self._sleep(FAILURE_CUE_GAP_SECONDS)
        await self.play(FAILURE_CUE)

    async def lock(self, locked: bool) -> None:
        await self.voice(LOCK_VOICES[locked])
