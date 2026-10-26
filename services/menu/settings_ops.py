"""Settings core shared by the controller admin mode and the operator pad commands (#2050, UC-015).

Admin mode (UC-012, UC-013, UC-014) and the pad's ``ProcessInput`` commands
(services.md §3.1) change the same settings. This module holds what both need
and nothing that is tied to a controller: the force-start roster with ghost
pruning, the clamped setting step, the agent level ladder, the kill switch and
resume of UC-015 BR-002 / BR-003, the backend write, and the voice tables.

``AdminModeHandler`` keeps its LEDs, its admin session span and its wrapping
``cycle_variant`` steps, and delegates the rest here. The pad commands pass no
serial: no LED pulse, no roster entry for the caller, an error code instead of
a log line.

Flag-service calls are blocking with a 0.5 s deadline and fail closed; they run
through ``asyncio.to_thread`` and are never retried or queued (UC-015 BR-010).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.flag_service_writer import STEP_AT_LIMIT
from lib.types import Sound
from proto import controller_manager_pb2
from services.menu.utils.led import DEFAULT_COLOR, GAME_MODE_COLORS

if TYPE_CHECKING:
    from lib.flag_service_writer import FlagServiceWriter
    from services.menu.state_manager import StateManager

logger = logging.getLogger(__name__)

# --- Agent set keys -------------------------------------------------------------------------

# UC-013 BR-001: the only agent key admin mode writes (persisted).
AGENT_POLICY_FLAG = "interventions_allowed"
# UC-015 BR-001: the pad also writes the kill switch (in memory only) and the backend (persisted).
AGENT_ENABLED_FLAG = "enabled"
DECISION_BACKEND_FLAG = "decision.backend"

# Ordered policy levels, most restrictive first. The ladder clamps and never wraps (UC-013, UC-015 BR-004).
AGENT_POLICY_LEVELS = ["none", "ambient", "standard", "full"]
# Levels a resume can restore (BR-002 saves a level only when it is not "none").
AGENT_SAVED_LEVELS = AGENT_POLICY_LEVELS[1:]
# UC-015 BR-003: the resume level when no level is saved (never "full").
AGENT_RESUME_FALLBACK_LEVEL = "ambient"
# UC-044 / UC-015 BR-004.
DECISION_BACKENDS = ("rules", "claude", "openai", "jev")

# --- Error codes (UC-015 BR-012, services.md §3.1) ------------------------------------------

GAME_ACTIVE = "game_active"
AT_LIMIT = "at_limit"
FLAG_SERVICE_UNAVAILABLE = "flag_service_unavailable"
PARTIAL_PREFIX = "partial:"
FAILED = "failed"
STALE = "stale"
BAD_OPTION = "bad_option"
# agent_switch reply when both writes (or the no-op of UC-015 A9) succeeded.
OK = "ok"


def is_failure(error: str | None) -> bool:
    """A failed write (UC-015 A3): the error codes that get the failure cue (BR-009)."""
    return bool(error) and (error in (FLAG_SERVICE_UNAVAILABLE, FAILED) or error.startswith(PARTIAL_PREFIX))


# --- Voice tables ---------------------------------------------------------------------------

# Voice line per sensitivity level index (0 = ultra_slow ... 4 = ultra_fast), as original JoustMania.
SENSITIVITY_VOICES = [
    Sound.MENU_VOX_SENSITIVITY_ULTRA_HIGH,  # ULTRA_SLOW (0)
    Sound.MENU_VOX_SENSITIVITY_HIGH,  # SLOW (1)
    Sound.MENU_VOX_SENSITIVITY_MEDIUM,  # MEDIUM (2)
    Sound.MENU_VOX_SENSITIVITY_LOW,  # FAST (3)
    Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW,  # ULTRA_FAST (4)
]
NUM_TEAMS_VOICES = {
    2: Sound.MENU_VOX_ADMINOP_2,
    3: Sound.MENU_VOX_ADMINOP_3,
    4: Sound.MENU_VOX_ADMINOP_4,
    5: Sound.MENU_VOX_ADMINOP_5,
    6: Sound.MENU_VOX_ADMINOP_6,
}
# "off" for the panic-off level, a numbered level (1..3) for the permissive ones.
AGENT_LEVEL_VOICES = {
    "none": Sound.MENU_VOX_ADMINOP_OFF,
    "ambient": Sound.MENU_VOX_ADMINOP_1,
    "standard": Sound.MENU_VOX_ADMINOP_2,
    "full": Sound.MENU_VOX_ADMINOP_3,
}
# UC-015 step 6, BR-009: one distinct voice line per direction of the agent switch.
AGENT_SWITCH_VOICES = {
    "stop": Sound.MENU_VOX_ADMINOP_OFF,
    "resume": Sound.MENU_VOX_ADMINOP_ON,
}
# UC-015 BR-009: the failure cue is two loud beeps, this far apart so they do not merge.
FAILURE_CUE_SOUND = Sound.SFX_BEEP_LOUD
FAILURE_CUE_GAP_SECONDS = 0.3
# UC-015 step 8: a short sound at the new master level.
VOLUME_STEP_SOUND = Sound.SFX_JOIN

# --- Variant values -------------------------------------------------------------------------

SENSITIVITY_VALUES = {"ultra_slow": 0, "slow": 1, "medium": 2, "fast": 3, "ultra_fast": 4}
_VARIANT_VALUES: dict[str, dict[str, Any]] = {
    "sensitivity": SENSITIVITY_VALUES,
    "num_teams": {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6},
    "force_all_start": {"on": True, "off": False},
    "game_instructions": {"on": True, "off": False},
}


def variant_to_value(option_name: str, variant_name: str) -> int | float | bool:
    """A flag variant name as the typed value used for feedback (0 when unknown)."""
    return _VARIANT_VALUES.get(option_name, {}).get(variant_name, 0)


def agent_level_next(current: str | None, forward: bool = True) -> str:
    """The next level along AGENT_POLICY_LEVELS, clamped at both ends (UC-013).

    The ladder never wraps: restrict at ``none`` stays ``none`` (a kill switch that
    escalates to ``full`` when pressed while off is unsafe) and escalate at ``full``
    stays ``full``. A current value outside the ladder (``probe``, UC-013 A1) collapses to
    ``none``, the benign fail-safe. ``None`` (an unreadable level) also maps to ``none``, but
    only the restrict uses it: the callers refuse an escalation from an unreadable level (UC-013 A5).
    """
    levels = AGENT_POLICY_LEVELS
    if current not in levels:
        return levels[0] if forward else "none"
    idx = levels.index(current)
    new_idx = min(idx + 1, len(levels) - 1) if forward else max(idx - 1, 0)
    return levels[new_idx]


def _ladder_step(ladder: list[str], current: str | None, forward: bool, steps: int) -> str:
    """``steps`` clamped moves along ``ladder``; a value outside it starts at the first move's result."""
    # ``steps`` comes unbounded from ProcessInput: more moves than rungs only repeat the clamp.
    steps = min(steps, len(ladder))
    new = current
    for _ in range(steps):
        if ladder is AGENT_POLICY_LEVELS:
            new = agent_level_next(new, forward)
        elif new not in ladder:
            new = ladder[0]
        else:
            idx = ladder.index(new)
            new = ladder[min(idx + 1, len(ladder) - 1) if forward else max(idx - 1, 0)]
    return new


# UC-015 BR-015: the mode pulse on every lobby controller, the pulse admin mode shows on its own
# controller (UC-011 step 3, ``AdminModeHandler.handle_game_mode``).
MODE_PULSE_MS = 600
MODE_PULSE_SPEED = 6


# --- Options of step_setting ----------------------------------------------------------------


@dataclass(frozen=True)
class _Option:
    writer_attr: str | None  # StateManager attribute of the writer; None = handled specially
    order: list[str] | None = None  # ladder instead of the declared variants ("up" = later entry)
    lobby_only: bool = False


STEP_OPTIONS: dict[str, _Option] = {
    "current_game": _Option(None, lobby_only=True),
    "sensitivity": _Option("game_settings_writer", lobby_only=True),
    "num_teams": _Option("game_settings_writer", lobby_only=True),
    "force_all_start": _Option("game_settings_writer", order=["off", "on"], lobby_only=True),
    "game_instructions": _Option("user_prefs_writer", order=["off", "on"], lobby_only=True),
    "audio_volume.master": _Option("user_prefs_writer"),
    "agent_level": _Option(None),
}
# UC-015 A1: refused with game_active while the game lifecycle is live.
LOBBY_ONLY_OPTIONS = frozenset(name for name, opt in STEP_OPTIONS.items() if opt.lobby_only)
DIRECTIONS = {"up": True, "down": False}
AGENT_SWITCH_DIRECTIONS = ("stop", "resume")


@dataclass(frozen=True)
class Result:
    """Outcome of a settings operation: the value before and after, and an error code (or None)."""

    old: str | None
    new: str | None
    error: str | None = None


class SettingsOps:
    """The settings core; one instance per menu, so the saved agent level is menu memory (BR-003)."""

    def __init__(
        self,
        *,
        get_state_manager: Callable[[], StateManager | None],
        controller_channel,
        metrics,
        callbacks=None,
        agent_enabled_writer: FlagServiceWriter | None = None,
    ):
        self._get_state_manager = get_state_manager
        self.controller_channel = controller_channel
        self.metrics = metrics
        # AdminModeCallbacks: get_game_options and select_game_mode for the mode step.
        self.callbacks = callbacks
        # UC-015 BR-001: `enabled` cannot persist (the registry row has no persist), so it has its own writer.
        self.agent_enabled_writer = agent_enabled_writer
        # UC-015 BR-002 / BR-003: the level a resume restores; lost on a menu restart.
        self.saved_agent_level: str | None = None
        # Serializes the kill, the resume and the ladder, so a saved level is never read half-written.
        self._agent_lock = asyncio.Lock()

    @property
    def state_manager(self) -> StateManager | None:
        return self._get_state_manager()

    # --- Force-start roster (UC-014 steps 3-4, UC-015 BR-008) -------------------------------

    async def prune_ghosts(self, controllers: list[str], admin_serial: str | None, span) -> list[str]:
        """Drop ghost controllers from ``controllers`` using the controller manager's live roster.

        Queries ``GetConnectedControllers`` (#1153) and removes any serial the backend no
        longer reports (the #551 ghost window). Fail-soft: on any RPC error, or with no
        channel, the list is returned unchanged so a force-start is never blocked. The admin
        serial, when there is one, is kept even if the live roster omits it; the pad has none.
        """
        if self.controller_channel is None or not controllers:
            return controllers

        from proto import controller_manager_pb2, controller_manager_pb2_grpc

        try:
            stub = controller_manager_pb2_grpc.ControllerManagerServiceStub(self.controller_channel)
            response = await stub.GetConnectedControllers(controller_manager_pb2.GetConnectedControllersRequest())
        except Exception as e:
            logger.warning(f"Live roster query failed, using menu roster as-is: {e}")
            span.set_attribute("force_start.roster_reconciled", False)
            self.metrics.force_start_reconcile_failed_total.inc()
            return controllers

        live_serials = set(response.connected_serials)
        pruned = [s for s in controllers if s in live_serials]
        ghosts = [s for s in controllers if s not in live_serials]

        if ghosts:
            logger.warning(f"Force-start: pruning {len(ghosts)} ghost controller(s) via live roster: {ghosts}")
            for _ in ghosts:
                self.metrics.ghost_controllers_pruned_total.inc()

        span.set_attribute("force_start.roster_reconciled", True)
        span.set_attribute("force_start.ghosts_pruned", len(ghosts))

        if admin_serial and admin_serial not in pruned:
            pruned.append(admin_serial)
        return pruned

    async def force_start_roster(self, admin_serial: str | None, span) -> list[str]:
        """The roster of a force-start: all connected with force_all_start, else the ready ones.

        UC-012 BR-001: force_all_start is read now, at the force start. The admin serial is
        added to a ready roster and survives ghost pruning; ``None`` (the pad) adds nobody.
        """
        from lib.feature_flags import get_flag_client

        manager = self.state_manager
        force_all = get_flag_client("game").get_boolean_value("force_all_start", False)
        span.set_attribute("force_all_start", force_all)
        if manager is None:
            controllers: list[str] = []
        elif force_all:
            controllers = list(manager.connected_controllers)
        else:
            controllers = list(manager.ready_controllers)
        if admin_serial and not force_all and admin_serial not in controllers:
            controllers.append(admin_serial)
        return await self.prune_ghosts(controllers, admin_serial, span)

    # --- Agent level reads ---------------------------------------------------------------

    def current_agent_level(self) -> str | None:
        """The agent level the flag service serves (``effective_default``), None when it cannot be read.

        UC-013 BR-003 (#2095): never the in-process flagd value. The flag service applies a
        write before it answers, while flagd catches up only within NFR-050, so a step read
        from flagd right after a write (or after a write that timed out but landed) steps
        from the old level again. Blocking (one flag-service read): run via to_thread.
        """
        view = self.state_manager.agent_settings_writer.get_flag_view(AGENT_POLICY_FLAG)
        return view.get("effective_default") if view else None

    def _agent_enabled(self) -> str | None:
        """The kill switch as the flag service serves it (``on`` / ``off``), None when unreadable."""
        writer = self.agent_enabled_writer or self.state_manager.agent_settings_writer
        try:
            view = writer.get_flag_view(AGENT_ENABLED_FLAG)
        except Exception as e:
            logger.warning(f"Reading agent/{AGENT_ENABLED_FLAG} failed: {e}")
            return None
        return view.get("effective_default") if view else None

    def _read_agent_state(self) -> tuple[str | None, str | None]:
        """(level, enabled); either is None when unreadable. Blocking."""
        try:
            level = self.current_agent_level()
        except Exception as e:
            logger.warning(f"Reading agent/{AGENT_POLICY_FLAG} failed: {e}")
            level = None
        return level, self._agent_enabled()

    @staticmethod
    def _write(writer, key: str, variant: str) -> bool:
        """One flag-service write; any error counts as a failed write (fail closed, never retried)."""
        if writer is None:
            return False
        try:
            return bool(writer.update_default_variant(key, variant))
        except Exception as e:
            logger.error(f"Write {key}={variant} failed: {e}")
            return False

    # --- step_setting (UC-015 steps 4, 5, 8, A7, A8, BR-004, BR-005) --------------------------

    async def step_setting(self, option: str, direction: str, steps: int) -> Result:
        """Step ``option`` by ``steps`` in ``direction`` with one write, stopping at the ends.

        Errors: ``bad_option``, ``at_limit`` (nothing written), ``flag_service_unavailable``.
        The caller checks the lobby-only refusal (``game_active``) first.
        """
        spec = STEP_OPTIONS.get(option)
        if spec is None or direction not in DIRECTIONS or steps < 1:
            return Result(None, None, BAD_OPTION)
        forward = DIRECTIONS[direction]

        if option == "current_game":
            return await self._step_game_mode(forward, steps)
        if option == "agent_level":
            return await self._step_agent_level(forward, steps)

        writer = getattr(self.state_manager, spec.writer_attr, None)
        if writer is None:
            return Result(None, None, FLAG_SERVICE_UNAVAILABLE)
        step = await asyncio.to_thread(writer.step_variant, option, forward, False, steps=steps, order=spec.order)
        if step.error == STEP_AT_LIMIT:
            return Result(step.old, step.old, AT_LIMIT)
        if step.error is not None:
            return Result(step.old, None, FLAG_SERVICE_UNAVAILABLE)
        return Result(step.old, step.new)

    async def _step_game_mode(self, forward: bool, steps: int) -> Result:
        """The mode step of UC-011 steps 2-5 over the selectable modes, clamped (no wrap)."""
        options = list(self.callbacks.get_game_options()) if self.callbacks is not None else []
        manager = self.state_manager
        current = manager.current_game_mode.name if manager is not None else None
        if not options:
            return Result(current, None, BAD_OPTION)
        new = _ladder_step(options, current, forward, steps)
        if new == current:
            return Result(current, current, AT_LIMIT)
        if not await self.callbacks.select_game_mode(new, source="operator_pad"):
            return Result(current, None, BAD_OPTION)
        return Result(current, new)

    async def _step_agent_level(self, forward: bool, steps: int) -> Result:
        """The level ladder of BR-004; while the agent is stopped only the saved level moves (BR-003)."""
        async with self._agent_lock:
            level, enabled = await asyncio.to_thread(self._read_agent_state)
            if level is None or enabled is None:
                # Fail closed in both directions: with the switch unreadable it is unknown whether
                # the step moves the live level or the saved one, so nothing is written (BR-010).
                return Result(None, None, FLAG_SERVICE_UNAVAILABLE)
            # BR-002 "stopped": enabled off or level none; only the saved level moves (BR-003).
            if enabled != "on" or level == "none":
                old = self.saved_agent_level or AGENT_RESUME_FALLBACK_LEVEL
                new = _ladder_step(AGENT_SAVED_LEVELS, old, forward, steps)
                if new == old:
                    return Result(old, old, AT_LIMIT)
                self.saved_agent_level = new
                return Result(old, new)

            new = _ladder_step(AGENT_POLICY_LEVELS, level, forward, steps)
            if new == level:
                return Result(level, level, AT_LIMIT)
            writer = self.state_manager.agent_settings_writer
            if not await asyncio.to_thread(self._write, writer, AGENT_POLICY_FLAG, new):
                return Result(level, None, FLAG_SERVICE_UNAVAILABLE)
            if new == "none" and level in AGENT_SAVED_LEVELS:
                # BR-002 / BR-003: a step down to "none" stops the agent like the kill does, so a
                # resume restores the level it left.
                self.saved_agent_level = level
            return Result(level, new)

    # --- agent_switch (UC-015 step 6, A3, A9, BR-002, BR-003) --------------------------------

    async def agent_switch(self, direction: str) -> Result:
        """Stop or resume the agent; ``error`` is ``ok``, ``partial:<key>``, ``failed`` or ``bad_option``."""
        if direction not in AGENT_SWITCH_DIRECTIONS:
            return Result(None, None, BAD_OPTION)
        async with self._agent_lock:
            if direction == "stop":
                return await self._stop_agent()
            return await self._resume_agent()

    async def _stop_agent(self) -> Result:
        level, enabled = await asyncio.to_thread(self._read_agent_state)
        if enabled == "off" or level == "none":
            # A9: already stopped; the saved level is not touched.
            return Result("stopped", "stopped", OK)
        if level in AGENT_SAVED_LEVELS:
            self.saved_agent_level = level
        # BR-002 order: the level first (it blocks the agent at the coordinator gate and ends
        # remote decide calls), then the kill switch. Both are attempted whatever the first did.
        level_ok = await asyncio.to_thread(
            self._write, self.state_manager.agent_settings_writer, AGENT_POLICY_FLAG, "none"
        )
        enabled_ok = await asyncio.to_thread(self._write, self.agent_enabled_writer, AGENT_ENABLED_FLAG, "off")
        return Result("running", "stopped", _pair_result(level_ok, enabled_ok))

    async def _resume_agent(self) -> Result:
        level, enabled = await asyncio.to_thread(self._read_agent_state)
        if level is None:
            # The resume is the permissive direction: never enable an agent whose level is unknown.
            return Result(None, None, FAILED)
        if enabled == "on" and level != "none":
            # A9: already running.
            return Result("running", "running", OK)
        level_ok: bool | None = None
        if level == "none":
            # BR-003: restore the saved level only while the level is still "none" (last write wins).
            target = self.saved_agent_level or AGENT_RESUME_FALLBACK_LEVEL
            level_ok = await asyncio.to_thread(
                self._write, self.state_manager.agent_settings_writer, AGENT_POLICY_FLAG, target
            )
        enabled_ok = await asyncio.to_thread(self._write, self.agent_enabled_writer, AGENT_ENABLED_FLAG, "on")
        result = _pair_result(level_ok, enabled_ok)
        if result == OK:
            self.saved_agent_level = None
        return Result("stopped", "running", result)

    # --- set_backend (UC-015 BR-004) -------------------------------------------------------

    async def set_backend(self, backend: str) -> Result:
        """Write ``decision.backend`` (persisted); ``bad_option`` or ``flag_service_unavailable`` on error."""
        if backend not in DECISION_BACKENDS:
            return Result(None, None, BAD_OPTION)
        writer = self.state_manager.agent_settings_writer
        if not await asyncio.to_thread(self._write, writer, DECISION_BACKEND_FLAG, backend):
            return Result(None, None, FLAG_SERVICE_UNAVAILABLE)
        return Result(None, backend)

    # --- Feedback ---------------------------------------------------------------------------

    async def announce(self, option: str, variant: str | None) -> None:
        """The voice line for a new value (UC-012 voices, UC-015 step 4, 5, 8). Best effort.

        A new mode also pulses its colour on every lobby controller (UC-015 BR-015).
        """
        manager = self.state_manager
        if manager is None or variant is None:
            return
        audio = manager.audio
        try:
            if option == "current_game":
                await self.show_mode_on_lobby(variant)
                await audio.play_game_mode_voice(variant)
            elif option == "sensitivity":
                index = SENSITIVITY_VALUES.get(variant)
                if index is not None:
                    await audio.play_voice(SENSITIVITY_VOICES[index])
            elif option == "num_teams":
                voice = NUM_TEAMS_VOICES.get(variant_to_value("num_teams", variant))
                if voice is not None:
                    await audio.play_voice(voice)
            elif option == "force_all_start":
                await audio.play_voice(Sound.MENU_VOX_ADMINOP_TRUE if variant == "on" else Sound.MENU_VOX_ADMINOP_FALSE)
            elif option == "game_instructions":
                await audio.play_voice(
                    Sound.MENU_VOX_INSTRUCTIONS_ON if variant == "on" else Sound.MENU_VOX_INSTRUCTIONS_OFF
                )
            elif option == "agent_level":
                voice = AGENT_LEVEL_VOICES.get(variant)
                if voice is not None:
                    await audio.play_voice(voice)
            elif option == "audio_volume.master":
                await audio.play_sound(VOLUME_STEP_SOUND)
            elif option == "agent_switch":
                voice = AGENT_SWITCH_VOICES.get(variant)
                if voice is not None:
                    await audio.play_voice(voice)
        except Exception as e:
            logger.warning(f"Announcing {option}={variant} failed: {e}")

    async def show_mode_on_lobby(self, game_mode: str) -> None:
        """UC-015 BR-015: a short pulse of the mode's colour on every CONNECTED or READY controller.

        The controller manager restores each controller's base colour (its dim / bright lobby
        colour) when the pulse ends; the admin controller and IDLE controllers are left alone.
        """
        # Imported here: services.menu.handlers imports this module (admin mode).
        from services.menu.handlers.base import ControllerState

        manager = self.state_manager
        if manager is None:
            return
        color = GAME_MODE_COLORS.get(game_mode, DEFAULT_COLOR)
        for serial, state in list(manager.controller_states.items()):
            if state not in (ControllerState.CONNECTED, ControllerState.READY):
                continue
            try:
                await manager.led.send_game_effect(
                    serial,
                    controller_manager_pb2.GAME_EFFECT_PULSE,
                    color=color,
                    duration_ms=MODE_PULSE_MS,
                    speed=MODE_PULSE_SPEED,
                )
            except Exception as e:
                logger.warning(f"Mode pulse for {serial} failed: {e}")

    async def play_failure_cue(self) -> None:
        """UC-015 BR-009: two loud beeps for a write that failed. Best effort."""
        manager = self.state_manager
        if manager is None:
            return
        try:
            await manager.audio.play_sound(FAILURE_CUE_SOUND)
            await asyncio.sleep(FAILURE_CUE_GAP_SECONDS)
            await manager.audio.play_sound(FAILURE_CUE_SOUND)
        except Exception as e:
            logger.warning(f"Failure cue failed: {e}")


def _pair_result(first_ok: bool | None, enabled_ok: bool) -> str:
    """BR-002 result of a level write (None = not attempted) and an ``enabled`` write."""
    if first_ok is None:
        return OK if enabled_ok else FAILED
    if first_ok and enabled_ok:
        return OK
    if first_ok:
        return PARTIAL_PREFIX + AGENT_ENABLED_FLAG
    if enabled_ok:
        return PARTIAL_PREFIX + AGENT_POLICY_FLAG
    return FAILED
