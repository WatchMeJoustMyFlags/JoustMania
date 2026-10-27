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
# The audio clip group of the announcements (PlaySoundRequest.group, #2107): a new announcement's
# label stops the clip of the previous one still playing, so two announcements never overlap.
ANNOUNCE_GROUP = "pad_announce"

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
        self._announce_task: asyncio.Task | None = None  # the latest announcement; a new one replaces it
        self._failure_task: asyncio.Task | None = None  # at most one failure cue at a time

    async def play(self, sound: Sound, volume: float = SOUND_VOLUME, group: str = "") -> float:
        """One clip; its length in seconds as the audio service reports it, 0 when unknown or failed."""
        try:
            response = await self._stub.PlaySound(
                audio_pb2.PlaySoundRequest(
                    file_path=sound.value, volume=volume, priority=audio_pb2.AudioPriority.HIGH, group=group
                ),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(f"Pad cue {sound} failed: {e}")
            return 0.0
        duration = getattr(response, "duration_seconds", 0.0)
        return float(duration) if isinstance(duration, int | float) and duration > 0 else 0.0

    async def voice(self, sound: Sound, group: str = "") -> float:
        return await self.play(sound, VOICE_VOLUME, group)

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
        """The menu cannot be reached: two loud beeps, then "No connection" (BR-009), in the background.

        Skipped while a failure cue is still playing, so repeated presses against a dead menu never stack.
        """
        if self._failure_task is not None and not self._failure_task.done():
            logger.debug("Failure cue skipped: one is still playing")
            return
        self._failure_task = self._start(self._failure_sequence())

    async def lock(self, locked: bool) -> None:
        """ "Pad locked" / "Pad unlocked" (A6); a cue still to come never plays over it."""
        self.cancel_all()
        await self.voice(LOCK_VOICES[locked], ANNOUNCE_GROUP)

    async def announce(self, label: Sound, value: Sound) -> None:
        """BR-011: the label, then the value once the label has ended, in the background.

        The latest announcement wins: it cancels the previous one (whose value then never plays) and
        its label stops the previous clip still playing (ANNOUNCE_GROUP).
        """
        if self._announce_task is not None:
            self._announce_task.cancel()
        self._announce_task = self._start(self._announce_sequence(label, value))

    async def stop_group(self, group: str) -> None:
        """Stop the clips of one audio group (StopSoundRequest.group, #2107); a failure is logged.

        An empty group is refused: the audio service reads it as "stop every effect", game sounds included.
        """
        if not group:
            logger.warning("Pad StopSound without a group refused: it would stop every sound effect")
            return
        try:
            await self._stub.StopSound(audio_pb2.StopSoundRequest(group=group), timeout=PLAY_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning(f"Stopping pad clip group {group} failed: {e}")

    def cancel_announcement(self) -> None:
        """Drop the tap announcement still to come (its value never plays); the caller stops ANNOUNCE_GROUP."""
        if self._announce_task is not None:
            self._announce_task.cancel()
            self._announce_task = None

    def cancel_all(self) -> None:
        """Drop every background cue (pad detached, shutdown): nothing plays on afterwards."""
        for task in list(self._sequences):
            task.cancel()
        self._announce_task = None
        self._failure_task = None

    async def drain(self) -> None:
        """Wait for the background cues (tests, shutdown); their errors are logged by `_finished`."""
        while self._sequences:
            await asyncio.gather(*self._sequences, return_exceptions=True)

    # --- background sequences ---------------------------------------------------------------

    def _start(self, sequence) -> asyncio.Task:
        task = asyncio.create_task(sequence)
        self._sequences.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._sequences.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"Pad cue sequence failed: {task.exception()!r}")

    async def _failure_sequence(self) -> None:
        await self.play(FAILURE_CUE)
        await self._sleep(FAILURE_CUE_GAP_SECONDS)
        beep_seconds = await self.play(FAILURE_CUE)
        await self._sleep(max(beep_seconds, FAILURE_CUE_GAP_SECONDS))  # the words start after the beeps
        await self.voice(NO_CONNECTION_CUE)

    async def _announce_sequence(self, label: Sound, value: Sound) -> None:
        label_seconds = await self.voice(label, ANNOUNCE_GROUP)
        if label_seconds > 0:
            await self._sleep(label_seconds)
        await self.voice(value, ANNOUNCE_GROUP)
