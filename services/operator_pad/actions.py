"""Gestures to menu and coordinator commands (UC-015 steps 1 to 8, A1, A3, A5 to A7, BR-002 to BR-011).

The pad writes no flag itself (BR-001): every setting goes to the menu as a `ProcessInput`
`web_command` (services.md §3.1), force-end goes to the game coordinator (UC-027). A command that
cannot be sent now is dropped with a cue, never queued or retried (BR-010).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from opentelemetry import trace

from lib import setting_ladders
from lib.telemetry import get_tracer
from proto import game_coordinator_pb2, menu_pb2
from services.operator_pad import cues, metrics
from services.operator_pad.config import K1, K2, K3, K4, K5, K6, K7, K8
from services.operator_pad.gestures import (
    CANCEL,
    HOLD,
    KNOB_PRESS_GESTURE,
    KNOB_TURN,
    LAYER,
    LOCK,
    TAP,
    Gesture,
)
from services.operator_pad.state import ENABLED_STATES, is_known, is_stopped

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

# Every command but the agent switch: at most two flag reads and one write settled by a re-read (#2095).
COMMAND_TIMEOUT_SECONDS = 4.0
# The resume is the menu's slowest command (#2093): from level `none` two flag reads (0.5 s each) and three
# writes whose timeout is settled by a re-read (0.5 s + 0.5 s each, #2095), 4.0 s of flag-service deadlines,
# plus the menu's own work. A cut still leaves the agent stopped: `enabled` is written last (UC-015 BR-003).
AGENT_SWITCH_TIMEOUT_SECONDS = 5.0
VOLUME_PAUSE_SECONDS = 0.25  # BR-005: the volume is committed after this pause in turning
FORCE_END_REASON = "operator_pad"

# Menu error codes (services.md §3.1, UC-015 BR-012).
OK = "ok"
GAME_ACTIVE = "game_active"
AT_LIMIT = "at_limit"
STALE = "stale"
BAD_OPTION = "bad_option"
FLAG_SERVICE_UNAVAILABLE = "flag_service_unavailable"
FAILED = "failed"
PARTIAL_PREFIX = "partial:"
# Pad-side results (telemetry.md §6).
REFUSED = "refused"
CANCELLED = "cancelled"
MENU_UNAVAILABLE = "menu_unavailable"
NOOP = "noop"

# Layer keys and the setting each one steps (UC-015 BR-011).
LAYER_OPTIONS = {K1: "current_game", K2: "sensitivity", K3: "agent_level", K5: "num_teams"}
# Tap keys that announce a setting (BR-011) and the setting whose label they say first.
ANNOUNCE_OPTIONS = {K2: "sensitivity", K3: "agent_level", K5: "num_teams", K7: "decision.backend"}
LOBBY_ONLY_OPTIONS = frozenset({"current_game", "sensitivity", "num_teams", "game_instructions"})
# UC-044 decision backends in knob order (BR-004; clamped, no wrap) and the `user/audio_volume.master`
# variants low to high (flags.md §9; `mute` is the pad's mute, BR-006): lib/setting_ladders.py, the
# ladders the menu commits along (#2106).
BACKENDS = setting_ladders.BACKENDS
VOLUME_VARIANTS = setting_ladders.VOLUME_LADDER
MUTE = "mute"
UNMUTE_DEFAULT = "max"


def result_label(error: str) -> str:
    """A menu error code as a metric label: `partial:<key>` folds to `partial` (bounded cardinality)."""
    if error in ("", OK):
        return OK
    if error.startswith(PARTIAL_PREFIX):
        return "partial"
    return error


class Actions:
    def __init__(
        self,
        menu_stub,
        coordinator_stub,
        cue_player: cues.CuePlayer,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        set_lock_led: Callable[[bool], None] | None = None,
    ) -> None:
        self._menu = menu_stub
        self._coordinator = coordinator_stub
        self._cues = cue_player
        self._sleep = sleep
        self._set_lock_led = set_lock_led
        self._volume_steps = 0
        self._volume_task: asyncio.Task | None = None  # waiting out the pause; cancelled by a new turn
        self._volume_sends: set[asyncio.Task] = set()  # past the pause: never cancelled mid-command
        self._pre_mute_variant: str | None = None

    # --- entry points ---------------------------------------------------------------------

    async def dispatch(
        self,
        gesture: Gesture,
        status: menu_pb2.GetMenuStatusResponse | None,
        press_status: menu_pb2.GetMenuStatusResponse | None = None,
    ) -> tuple[str, str]:
        """Run one gesture; (action, result). ``status`` is read at the event, ``press_status`` at the press."""
        if gesture.kind == KNOB_TURN:
            self._queue_volume_step(gesture.steps)
            return "step_setting", "pending"
        with tracer.start_as_current_span("operator_pad.gesture") as span:
            span.set_attribute("pad.key", gesture.key)
            span.set_attribute("pad.gesture", gesture.kind)
            action, result = await self._run(gesture, status, press_status, span)
            self._record(span, action, result)
            return action, result

    def cancel_pending(self) -> None:
        """Device gone: a volume turn not yet committed is dropped, not sent later (BR-010)."""
        self._volume_steps = 0
        if self._volume_task is not None:
            self._volume_task.cancel()
            self._volume_task = None

    async def drain(self) -> None:
        """Wait for a pending volume commit (tests, shutdown)."""
        tasks = [t for t in (self._volume_task, *self._volume_sends) if t is not None]
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- dispatch -------------------------------------------------------------------------

    async def _run(self, gesture: Gesture, status, press_status, span) -> tuple[str, str]:
        kind, key = gesture.kind, gesture.key
        if kind == CANCEL:
            await self._cues.cancel()
            return "cancel", CANCELLED
        if kind == LOCK:
            if self._set_lock_led is not None:
                try:
                    self._set_lock_led(gesture.locked)
                except Exception as e:
                    logger.warning(f"Scroll Lock LED failed: {e}")
            await self._cues.lock(gesture.locked)
            return ("lock" if gesture.locked else "unlock"), OK
        if kind == KNOB_PRESS_GESTURE:
            return await self._mute_toggle(status)
        if kind == TAP:
            return await self._tap(key, status)
        if kind == HOLD:
            if key == K3:
                return await self._agent_switch(status, press_status, gesture.locked)
            if key == K4:
                return await self._end_or_start(status, press_status)
            return "none", NOOP
        if kind == LAYER:
            span.set_attribute("pad.steps", gesture.steps)
            return await self._layer(key, gesture.steps, status)
        return "none", NOOP

    async def _tap(self, key: str, status) -> tuple[str, str]:
        if key in (K4, K8):
            return "none", NOOP  # K4 has no tap; K8 tap is reserved for phase 2
        if status is None:
            await self._cues.failure()
            return ("start_game" if key == K1 else "announce"), MENU_UNAVAILABLE
        if key == K1:
            if status.lifecycle_live:
                await self._cues.refusal()  # A1: the pad refuses itself; the menu's A8 guard is the backstop
                return "start_game", REFUSED
            return "start_game", await self._send({"command": "start_game"})
        if key == K6:
            if status.lifecycle_live:
                await self._cues.refusal()
                return "step_setting", REFUSED
            direction = "down" if status.instructions else "up"
            return "step_setting", await self._step("game_instructions", direction, 1)
        await self._announce(key, status)
        return "announce", OK

    async def _announce(self, key: str, status) -> None:
        """Taps of K2, K3, K5 and K7 announce the label, then the value read from `GetMenuStatus` (BR-011)."""
        voice = None
        if key == K2 and 0 <= status.sensitivity < len(cues.SENSITIVITY_VOICES):
            voice = cues.SENSITIVITY_VOICES[status.sensitivity]
        elif key == K3:
            # BR-013: an unresolved kill switch or write gate is never announced as stopped (a false
            # all-clear) nor as running (#2093); the level is not checked here, since a resolved `off`
            # with an empty level is really stopped.
            if (
                status.agent_enabled_state not in ENABLED_STATES
                or status.agent_interventions_enabled_state not in ENABLED_STATES
            ):
                voice = None
            elif is_stopped(status):
                voice = cues.AGENT_STOPPED_VOICE
            else:
                voice = cues.AGENT_LEVEL_VOICES.get(status.agent_level)
        elif key == K5:
            voice = cues.NUM_TEAMS_VOICES.get(status.num_teams)
        elif key == K7:
            voice = cues.BACKEND_VOICES.get(status.agent_backend)  # by name (#2106)
        if voice is None:
            await self._cues.refusal()
            return
        await self._cues.announce(cues.LABEL_VOICES[ANNOUNCE_OPTIONS[key]], voice)  # in the background (BR-009)

    async def _agent_switch(self, status, press_status, locked: bool) -> tuple[str, str]:
        """K3 hold (step 6, BR-002, BR-003, BR-013, A10); while locked always the stop (A6).

        The direction is the intent at the press; the release read only confirms it. Any unknown
        read allows only the stop (fail closed): it is idempotent and attempts both writes.
        """
        if locked:
            # A6: a locked pad is kill only, whatever the status; the menu's A9 makes the stop a
            # no-op for a stopped agent, and with only the write gate closed it ends decide calls.
            direction = "stop"
        elif is_known(press_status) and is_known(status):
            if is_stopped(press_status) != is_stopped(status):
                await self._cues.cancel()  # A10: changed during the hold, by someone else
                return "agent_switch", CANCELLED
            direction = "resume" if is_stopped(press_status) else "stop"
        else:
            direction = "stop"
        return "agent_switch", await self._send(
            {"command": "agent_switch", "direction": direction}, deadline_seconds=AGENT_SWITCH_TIMEOUT_SECONDS
        )

    async def _end_or_start(self, status, press_status) -> tuple[str, str]:
        """K4 hold (step 7, A5): cancel when the state changed between press and release."""
        if status is None or press_status is None:
            await self._cues.failure()
            return "force_end", MENU_UNAVAILABLE
        if (
            status.lifecycle_generation != press_status.lifecycle_generation
            or status.lifecycle_live != press_status.lifecycle_live
        ):
            await self._cues.cancel()
            return ("force_end" if press_status.lifecycle_live else "force_start"), CANCELLED
        if status.lifecycle_live:
            return "force_end", await self._force_end()
        # The generation captured at the press, not the one read at release: the menu judges the whole hold.
        return "force_start", await self._send(
            {"command": "force_start", "expected_generation": str(press_status.lifecycle_generation)}
        )

    async def _layer(self, key: str, steps: int, status) -> tuple[str, str]:
        """Layer release (steps 4, 5, BR-004, BR-005): one write for the net turns, none for zero."""
        if key == K7:
            return await self._backend(steps, status)
        option = LAYER_OPTIONS.get(key)
        if option is None:
            return "none", NOOP
        if steps == 0:
            return "step_setting", NOOP
        if status is None:
            await self._cues.failure()
            return "step_setting", MENU_UNAVAILABLE
        if option in LOBBY_ONLY_OPTIONS and status.lifecycle_live:
            await self._cues.refusal()
            return "step_setting", REFUSED
        return "step_setting", await self._step(option, "up" if steps > 0 else "down", abs(steps))

    async def _backend(self, steps: int, status) -> tuple[str, str]:
        if steps == 0:
            return "set_backend", NOOP
        if status is None:
            await self._cues.failure()
            return "set_backend", MENU_UNAVAILABLE
        if status.agent_backend not in BACKENDS:
            logger.warning(f"Unknown decision backend {status.agent_backend!r}: nothing to step from")
            await self._cues.refusal()
            return "set_backend", REFUSED
        target, at_limit = setting_ladders.clamp_step(BACKENDS, status.agent_backend, steps)
        if at_limit:
            await self._cues.limit(steps > 0)  # A7: already at the end
            return "set_backend", AT_LIMIT
        return "set_backend", await self._send({"command": "set_backend", "backend": target})

    # --- volume and mute (step 8, BR-005, BR-006) ----------------------------------------

    def _queue_volume_step(self, step: int) -> None:
        self._volume_steps += step
        if self._volume_task is not None:
            self._volume_task.cancel()
        self._volume_task = asyncio.create_task(self._commit_volume_after_pause())

    async def _commit_volume_after_pause(self) -> None:
        await self._sleep(VOLUME_PAUSE_SECONDS)
        steps, self._volume_steps = self._volume_steps, 0
        me = asyncio.current_task()
        if self._volume_task is me:
            self._volume_task = None
        if me is not None:
            self._volume_sends.add(me)
            me.add_done_callback(self._volume_sends.discard)
        with tracer.start_as_current_span("operator_pad.gesture") as span:
            span.set_attribute("pad.key", "KNOB")
            span.set_attribute("pad.gesture", KNOB_TURN)
            span.set_attribute("pad.steps", steps)
            if steps == 0:
                result = NOOP
            else:
                result = await self._step("audio_volume.master", "up" if steps > 0 else "down", abs(steps))
                if result == OK:
                    self._pre_mute_variant = None  # a turn after a mute is the new level
            self._record(span, "step_setting", result)

    async def _mute_toggle(self, status) -> tuple[str, str]:
        self.cancel_pending()  # the mute wins over a turn not yet committed
        if status is None:
            await self._cues.failure()
            return "mute", MENU_UNAVAILABLE
        current = status.audio_volume_master
        if current == MUTE:
            target = self._pre_mute_variant or UNMUTE_DEFAULT
            steps = VOLUME_VARIANTS.index(target) if target in VOLUME_VARIANTS else len(VOLUME_VARIANTS) - 1
            result = await self._step("audio_volume.master", "up", steps)
            if result == OK:
                self._pre_mute_variant = None
            return "unmute", result
        # An unreadable variant ("") steps down past the end, which stops at `mute` (A7).
        known = current in VOLUME_VARIANTS
        steps = VOLUME_VARIANTS.index(current) if known else len(VOLUME_VARIANTS)
        result = await self._step("audio_volume.master", "down", steps)
        if result == OK:
            self._pre_mute_variant = current if known else None
        return "mute", result

    # --- sending --------------------------------------------------------------------------

    async def _step(self, option: str, direction: str, steps: int) -> str:
        return await self._send(
            {"command": "step_setting", "option": option, "direction": direction, "steps": str(steps)}
        )

    async def _send(self, data: dict[str, str], deadline_seconds: float = COMMAND_TIMEOUT_SECONDS) -> str:
        """One `ProcessInput` web command; the result label. Never retried (BR-010)."""
        try:
            response = await self._menu.ProcessInput(
                menu_pb2.ProcessInputRequest(input_type="web_command", data=data), timeout=deadline_seconds
            )
        except Exception as e:
            logger.warning(f"Pad command {data.get('command')} not sent: {e}")
            await self._cues.failure()
            return MENU_UNAVAILABLE
        error = response.error if not response.success else (response.error or OK)
        await self._cue_for(error, up=data.get("direction") != "down")
        return result_label(error)

    async def _force_end(self) -> str:
        try:
            response = await self._coordinator.ForceEndGame(
                game_coordinator_pb2.ForceEndGameRequest(reason=FORCE_END_REASON), timeout=COMMAND_TIMEOUT_SECONDS
            )
        except Exception as e:
            logger.warning(f"ForceEndGame not sent: {e}")
            await self._cues.failure()
            return MENU_UNAVAILABLE
        if not response.success:
            logger.warning(f"ForceEndGame refused: {response.error}")
            await self._cues.refusal()
            return REFUSED
        return OK

    async def _cue_for(self, error: str, up: bool = True) -> None:
        """Menu error code -> pad cue. Success and failed writes are voiced by the menu itself."""
        if error in (GAME_ACTIVE, BAD_OPTION):
            await self._cues.refusal()
        elif error == AT_LIMIT:
            await self._cues.limit(up)  # "Maximum" stepping up, "Minimum" stepping down
        elif error == STALE:
            await self._cues.cancel()
        # ok, flag_service_unavailable, failed, partial:<key>: the menu played the voice or the failure cue

    @staticmethod
    def _record(span, action: str, result: str) -> None:
        span.set_attribute("pad.action", action)
        span.set_attribute("pad.result", result)
        if result == MENU_UNAVAILABLE:
            span.set_status(trace.Status(trace.StatusCode.ERROR, result))
        metrics.gestures_total.labels(action=action, result=result).inc()
