"""Live preview of a layer setting while the knob turns (UC-015 BR-017, FR-197, #2106).

While a layer key is the only key down, each knob step (BR-005) tells the `Previewer` the net
steps so far. It computes the value the release would commit from the status read at the press
(`lib/setting_ladders.py` `preview_value`, the menu's own ladders and clamping) and speaks it:

- the label once, at the first step of the hold;
- the value `operator_pad.preview_debounce_ms` after the last step, never before the label has
  ended, in the audio clip group `pad_preview` (a new value stops the previous clip);
- no repeat of the value last spoken; a step that pushes past the end gives the limit word
  (paced by the cue player, BR-009);
- silence with no status or a value the status does not tell (never a guess, BR-013);
- one "Not possible" for a lobby-only layer while a game is live (A1).

It writes nothing and sends no `step_setting` (BR-005): the release commits as before. Every clip
plays in a background task, so a knob step never waits on the audio service (BR-009).

The mode (K1) also shows on the LEDs (LED first, BR-009): after the debounce, without waiting for
the label, the menu's `preview_setting` pulses the previewed mode's colour on every lobby
controller (services.md §3.1; no write, no selection). A hold that ends without committing another
mode, after it showed one, pulses the selected mode once more.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from lib import setting_ladders
from lib.types import Sound
from lib.voice_prompts import (
    AGENT_LEVEL_VOICES,
    BACKEND_VOICES,
    GAME_MODE_VOICE,
    LABEL_VOICES,
    NUM_TEAMS_VOICES,
    SENSITIVITY_VOICES,
)
from proto import menu_pb2
from services.operator_pad import metrics
from services.operator_pad.config import PadConfig
from services.operator_pad.cues import ANNOUNCE_GROUP, CuePlayer

logger = logging.getLogger(__name__)

# The audio clip group of the preview (PlaySoundRequest.group / StopSoundRequest.group, #2107).
PREVIEW_GROUP = "pad_preview"
# The option whose preview also shows on the lobby LEDs (menu `preview_setting`, BR-017).
MODE_OPTION = "current_game"
# A `preview_setting` is cosmetic: a short deadline, never retried (BR-010), no cue on failure.
MODE_PREVIEW_TIMEOUT_SECONDS = 1.0

# Metric results (telemetry.md §6, `operator_pad_previews_total{result}`).
SPOKEN = "spoken"
REPLACED = "replaced"
SUPPRESSED = "suppressed"
LIMIT = "limit"
REFUSED = "refused"
UNKNOWN = "unknown"


def value_voice(option: str, value: str | None) -> Sound | None:
    """The voice line of a ladder value, the same tables the menu voices its commits with."""
    if value is None:
        return None
    if option == "sensitivity" and value in setting_ladders.SENSITIVITY_LADDER:
        return SENSITIVITY_VOICES[setting_ladders.SENSITIVITY_LADDER.index(value)]
    if option == "num_teams" and value in setting_ladders.TEAMS_LADDER:
        return NUM_TEAMS_VOICES.get(setting_ladders.TEAMS_LADDER.index(value) + setting_ladders.TEAMS_MIN)
    if option == "current_game":
        return GAME_MODE_VOICE.get(value)
    if option == "agent_level":
        return AGENT_LEVEL_VOICES.get(value)
    if option == "decision.backend":
        return BACKEND_VOICES.get(value)
    return None


@dataclass(frozen=True)
class _Pending:
    """What the debounced fire plays: a value clip, or the limit word in direction ``up``."""

    value: str
    sound: Sound | None  # None: the limit word
    up: bool = True


class Previewer:
    def __init__(
        self,
        cue_player: CuePlayer,
        config: PadConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        menu_stub=None,
    ) -> None:
        self._cues = cue_player
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._menu = menu_stub  # None: no LED preview of the mode
        self._tasks: set[asyncio.Task] = set()  # this hold's label, timer, value and mode pulses
        # StopSound calls and the mode restore: they outlive the hold they end.
        self._stops: set[asyncio.Task] = set()
        self._reset()

    def _reset(self) -> None:
        self.hold_key: str | None = None
        self._silent = False  # this hold says nothing more (refused, unknown, disabled)
        self._refused = False  # this hold played the A1 refusal (the release does not repeat it)
        self._label_task: asyncio.Task | None = None
        self._label_done_at = 0.0
        self._last_value: str | None = None  # the value of the previous step (or the start)
        self._last_net = 0
        self._last_spoken: str | None = None
        self._pending: _Pending | None = None
        self._timer: asyncio.Task | None = None  # waiting out the debounce; restarted by a step
        self._option: str | None = None
        self._status = None  # the status read at the press
        self._start_mode: str | None = None  # K1: the mode selected at the press
        self._shown_mode: str | None = None  # K1: the mode the lobby LEDs last pulsed in this hold

    # --- entry points ---------------------------------------------------------------------

    def step(self, key: str, option: str, status, net_steps: int, lobby_only: bool) -> None:
        """One completed knob step of the held layer key ``key``; never waits."""
        if not self._config.preview_enabled():
            return  # `operator_pad.preview_enabled` off: nothing, read per step (C-035)
        if key != self.hold_key:
            self._drop_hold()
            self.hold_key = key
            self._start_hold(option, status, lobby_only)
            if self._silent:
                return
        elif self._silent:
            return
        preview = setting_ladders.preview_value(option, status, net_steps)
        sound = value_voice(option, preview.value)
        if preview.value is None or sound is None:
            metrics.previews_total.labels(result=UNKNOWN).inc()
            return
        pushed_further = abs(net_steps) > abs(self._last_net)
        if pushed_further and preview.value == self._last_value:
            pending = _Pending(preview.value, None, up=net_steps > 0)  # past the end (A7)
        else:
            pending = _Pending(preview.value, sound)
        self._last_value, self._last_net = preview.value, net_steps
        if self._timer is not None:
            metrics.previews_total.labels(result=REPLACED).inc()
            self._timer.cancel()
        self._pending = pending
        self._timer = self._spawn(self._speak_after_debounce())

    def refused(self, key: str) -> bool:
        """The hold of ``key`` already played the A1 refusal, so its release need not repeat it."""
        return self._refused and self.hold_key == key

    def end_hold(self, commit_steps: int = 0) -> None:
        """Release, chord or lost input: drop what is still to come and stop the preview clip.

        ``commit_steps`` is the net the release commits (0: nothing is sent). A K1 hold that showed
        another mode on the LEDs and commits no other mode pulses the selected mode again.
        """
        if self.hold_key is None:
            return
        restore = self._mode_to_restore(commit_steps)
        self._drop_hold()
        self._stop(PREVIEW_GROUP)
        if restore is not None:
            self._detach(self._show_mode(restore))

    def cancel(self) -> None:
        """Drop every preview task and pending stop (device gone, shutdown); nothing plays on afterwards."""
        self._drop_hold()
        for task in list(self._stops):
            task.cancel()
        self._stops.clear()

    async def drain(self) -> None:
        """Wait for the preview tasks (tests)."""
        while self._tasks or self._stops:
            await asyncio.gather(*self._tasks, *self._stops, return_exceptions=True)

    def _drop_hold(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._reset()

    def _stop(self, group: str) -> None:
        self._detach(self._cues.stop_group(group))

    def _detach(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._stops.add(task)
        task.add_done_callback(self._stop_finished)

    def _mode_to_restore(self, commit_steps: int) -> str | None:
        if self._option != MODE_OPTION or self._start_mode is None or self._shown_mode == self._start_mode:
            return None
        committed = setting_ladders.preview_value(MODE_OPTION, self._status, commit_steps).value
        if commit_steps != 0 and committed != self._start_mode:
            return None  # the menu's commit pulses the new mode itself (BR-015)
        return self._start_mode

    def _stop_finished(self, task: asyncio.Task) -> None:
        self._stops.discard(task)
        _log_failure(task)

    # --- internals ------------------------------------------------------------------------

    def _start_hold(self, option: str, status, lobby_only: bool) -> None:
        # A tap announcement still playing stops: the preview never talks over it (BR-017, BR-011).
        self._cues.cancel_announcement()
        self._stop(ANNOUNCE_GROUP)
        if status is None:
            self._silent = True  # no status at the press: silent, never a guess
            metrics.previews_total.labels(result=UNKNOWN).inc()
            return
        if lobby_only and status.lifecycle_live:
            self._silent = True  # A1: one refusal for the hold, no values
            self._refused = True
            metrics.previews_total.labels(result=REFUSED).inc()
            self._spawn(self._cues.refusal())
            return
        start = setting_ladders.preview_value(option, status, 0)
        if start.value is None or value_voice(option, start.value) is None:
            self._silent = True
            metrics.previews_total.labels(result=UNKNOWN).inc()
            return
        self._last_value = start.value
        self._option, self._status = option, status
        if option == MODE_OPTION and self._menu is not None:
            self._start_mode = self._shown_mode = start.value
        label = LABEL_VOICES.get(option)
        if label is not None:
            self._label_task = self._spawn(self._play_label(label))

    async def _play_label(self, label: Sound) -> None:
        started = self._clock()
        seconds = await self._cues.voice(label, PREVIEW_GROUP)
        self._label_done_at = started + seconds

    async def _speak_after_debounce(self) -> None:
        await self._sleep(self._config.preview_debounce_ms() / 1000)
        self._show_pending_mode()  # LED first: never waits for the label (BR-009)
        if self._label_task is not None and not self._label_task.done():
            await asyncio.wait({self._label_task})  # never cancels the label
        remaining = self._label_done_at - self._clock()
        if remaining > 0:
            await self._sleep(remaining)
        # Past the wait: a later step no longer cancels this one mid-call.
        if self._timer is asyncio.current_task():
            self._timer = None
        pending, self._pending = self._pending, None
        if pending is None:
            return
        if pending.sound is None:
            metrics.previews_total.labels(result=LIMIT).inc()
            await self._cues.limit(pending.up)
            return
        if pending.value == self._last_spoken:
            metrics.previews_total.labels(result=SUPPRESSED).inc()
            return
        self._last_spoken = pending.value
        metrics.previews_total.labels(result=SPOKEN).inc()
        await self._cues.voice(pending.sound, PREVIEW_GROUP)

    def _show_pending_mode(self) -> None:
        pending = self._pending
        if self._start_mode is None or pending is None or pending.sound is None:
            return  # not a K1 hold with the LED preview, or the limit word (the mode is unchanged)
        if pending.value == self._shown_mode:
            return
        self._shown_mode = pending.value
        self._spawn(self._show_mode(pending.value))

    async def _show_mode(self, mode: str) -> None:
        """One `preview_setting` (services.md §3.1): the menu pulses ``mode`` on the lobby; never retried."""
        try:
            response = await self._menu.ProcessInput(
                menu_pb2.ProcessInputRequest(
                    input_type="web_command",
                    data={"command": "preview_setting", "option": MODE_OPTION, "mode": mode},
                ),
                timeout=MODE_PREVIEW_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.debug(f"Mode preview {mode} not sent: {e}")
            return
        if not response.success:
            logger.debug(f"Mode preview {mode} refused: {response.error}")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        _log_failure(task)


def _log_failure(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError):
        if task.exception() is not None:
            logger.warning(f"Pad preview task failed: {task.exception()!r}")
