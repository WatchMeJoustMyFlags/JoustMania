"""Cues of the operator pad (UC-015 BR-009, BR-011), played through the audio service.

The menu plays the success voice lines of the commands it runs and the failure cue of a failed
flag write; the pad plays what only it knows: refusals it decides itself, the limit and cancel
words, the lock directions, the tap announcements (label, then value) read from `GetMenuStatus`,
the unknown-key cue, and the failure cue when the menu itself cannot be reached. The words come
from the voice catalog (UC-031 BR-007) through lib/voice_prompts.py (#2106).

A cue of more than one clip (an announcement, the failure cue) runs as a background task, so it
never holds up the input reader (BR-009).
"""

from __future__ import annotations

import asyncio
import logging
import time

from lib import voice_prompts
from lib.types import Sound
from lib.voice_prompts import (  # noqa: F401 (re-exported for the action layer)
    AGENT_LEVEL_VOICES,
    AGENT_STOPPED_VOICE,
    BACKEND_VOICES,
    LABEL_VOICES,
    LOCK_VOICES,
    NUM_TEAMS_VOICES,
    SENSITIVITY_VOICES,
)
from proto import audio_pb2

logger = logging.getLogger(__name__)

SOUND_VOLUME = 0.8  # the menu's code default for `audio_volume.menu_sound`
VOICE_VOLUME = 0.9  # the menu's code default for `audio_volume.menu_voice`
PLAY_TIMEOUT_SECONDS = 1.0

# Status words (BR-009, #2106), each distinct from the two-beep failure cue and from the value lines.
REFUSAL_CUE = voice_prompts.NOT_POSSIBLE_VOICE
LIMIT_CUES = voice_prompts.LIMIT_VOICES  # "Maximum" stepping up, "Minimum" stepping down
CANCEL_CUE = voice_prompts.CANCELLED_VOICE
NO_CONNECTION_CUE = voice_prompts.NO_CONNECTION_VOICE
UNKNOWN_KEY_CUE = Sound.SFX_EXPLOSION_22
FAILURE_CUE = Sound.SFX_BEEP_LOUD
FAILURE_CUE_GAP_SECONDS = 0.3
# UC-015 BR-009: the limit cue plays at most once per this gap, so a burst of `at_limit` refusals
# (a knob turned on at the end of the range) gives one cue, not one per refusal (#2105). A constant,
# not an `operator` flag: it paces one fixed asset (about as long as the cue itself) and is nothing
# an operator tunes per venue.
LIMIT_CUE_MIN_GAP_SECONDS = 1.0

# Lock directions (A6), labels and the tap announcements (UC-015 BR-011): the voice tables shared
# with the menu, lib/voice_prompts.py (#2106).


class CuePlayer:
    """Best-effort `PlaySound` calls; a failed cue is logged, never raised."""

    def __init__(self, audio_stub, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self._stub = audio_stub
        self._sleep = sleep
        self._clock = clock
        self._last_limit_cue: float | None = None
        self._sequences: set[asyncio.Task] = set()  # multi-clip cues playing in the background

    async def play(self, sound: Sound, volume: float = SOUND_VOLUME) -> float:
        """One clip; its length in seconds as the audio service reports it, 0 when unknown or failed."""
        try:
            response = await self._stub.PlaySound(
                audio_pb2.PlaySoundRequest(file_path=sound.value, volume=volume, priority=audio_pb2.AudioPriority.HIGH),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(f"Pad cue {sound} failed: {e}")
            return 0.0
        duration = getattr(response, "duration_seconds", 0.0)
        return float(duration) if isinstance(duration, int | float) and duration > 0 else 0.0

    async def voice(self, sound: Sound) -> float:
        return await self.play(sound, VOICE_VOLUME)

    async def refusal(self) -> None:
        await self.voice(REFUSAL_CUE)

    async def limit(self, up: bool = True) -> None:
        """ "Maximum" (up) or "Minimum" (down), skipped within LIMIT_CUE_MIN_GAP_SECONDS of the last one."""
        now = self._clock()
        if self._last_limit_cue is not None and now - self._last_limit_cue < LIMIT_CUE_MIN_GAP_SECONDS:
            logger.debug("Limit cue skipped: played less than the minimum gap ago")
            return
        self._last_limit_cue = now
        await self.voice(LIMIT_CUES[up])

    async def cancel(self) -> None:
        await self.voice(CANCEL_CUE)

    async def unknown_key(self) -> None:
        await self.play(UNKNOWN_KEY_CUE)

    async def failure(self) -> None:
        """The menu cannot be reached: two loud beeps, then "No connection" (BR-009), in the background."""
        self._start(self._failure_sequence())

    async def lock(self, locked: bool) -> None:
        """ "Pad locked" / "Pad unlocked" (A6)."""
        await self.voice(LOCK_VOICES[locked])

    async def announce(self, label: Sound, value: Sound) -> None:
        """BR-011: the label, then the value once the label has ended, in the background."""
        self._start(self._announce_sequence(label, value))

    async def drain(self) -> None:
        """Wait for the background cues (tests, shutdown)."""
        while self._sequences:
            await asyncio.gather(*self._sequences, return_exceptions=True)

    # --- background sequences ---------------------------------------------------------------

    def _start(self, sequence) -> None:
        task = asyncio.create_task(sequence)
        self._sequences.add(task)
        task.add_done_callback(self._sequences.discard)

    async def _failure_sequence(self) -> None:
        await self.play(FAILURE_CUE)
        await self._sleep(FAILURE_CUE_GAP_SECONDS)
        beep_seconds = await self.play(FAILURE_CUE)
        await self._sleep(max(beep_seconds, FAILURE_CUE_GAP_SECONDS))  # the words start after the beeps
        await self.voice(NO_CONNECTION_CUE)

    async def _announce_sequence(self, label: Sound, value: Sound) -> None:
        label_seconds = await self.voice(label)
        if label_seconds > 0:
            await self._sleep(label_seconds)
        await self.voice(value)
