"""Cues of the operator pad (UC-015 BR-009): existing assets only, played through the audio service.

The menu plays the success voice lines of the commands it runs and the failure cue of a failed
flag write; the pad plays what only it knows: refusals it decides itself, the limit and cancel
cues, the lock directions, the tap announcements read from `GetMenuStatus`, the unknown-key cue,
and the failure cue when the menu itself cannot be reached.
"""

from __future__ import annotations

import asyncio
import logging
import time

from lib.types import Sound
from lib.voice_prompts import (  # noqa: F401 (re-exported for the action layer)
    AGENT_LEVEL_VOICES,
    AGENT_STOPPED_VOICE,
    LOCK_VOICES,
    NUMBER_VOICES,
    SENSITIVITY_VOICES,
)
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
# UC-015 BR-009: the limit cue plays at most once per this gap, so a burst of `at_limit` refusals
# (a knob turned on at the end of the range) gives one cue, not one per refusal (#2105). A constant,
# not an `operator` flag: it paces one fixed asset (about as long as the cue itself) and is nothing
# an operator tunes per venue.
LIMIT_CUE_MIN_GAP_SECONDS = 1.0

# Lock directions (A6) and the tap announcements (UC-015 BR-011): the voice tables shared with the
# menu, lib/voice_prompts.py (#2106).


class CuePlayer:
    """Best-effort `PlaySound` calls; a failed cue is logged, never raised."""

    def __init__(self, audio_stub, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self._stub = audio_stub
        self._sleep = sleep
        self._clock = clock
        self._last_limit_cue: float | None = None

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
        """The limit cue, skipped within LIMIT_CUE_MIN_GAP_SECONDS of the last one played."""
        now = self._clock()
        if self._last_limit_cue is not None and now - self._last_limit_cue < LIMIT_CUE_MIN_GAP_SECONDS:
            logger.debug("Limit cue skipped: played less than the minimum gap ago")
            return
        self._last_limit_cue = now
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
