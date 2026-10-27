"""Setting ladders shared by the menu and the operator pad (UC-015 BR-004, BR-005, A7; #2106).

One ordering and one clamping for every setting the pad steps, so the value the pad computes
from `GetMenuStatus` plus the knob steps (the live preview, #2106) is the value the menu's
`step_setting` commits on release. The menu steps the flag ladders (sensitivity, teams,
instructions, volume, shield mode) through `FlagServiceWriter.step_variant`, which reads the variant order
from the flag service; these ladders are that order, and a menu test pins them to the flag
files (`services/flagd/*.json`).

Pure module: no I/O, no service imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lib.types import Games

# --- Ladders --------------------------------------------------------------------------------

# UC-011 BR-001: the selectable modes, in `Games` order. Unimplemented (Commander, Ninja) and the
# meta mode (Random) are not selectable.
_UNSELECTABLE_MODES = {Games.Commander, Games.Ninja, Games.Random}
MODE_LADDER: list[str] = [g.name for g in Games if g not in _UNSELECTABLE_MODES]

# `game/sensitivity` variants low to high by value (0 = ultra_slow ... 4 = ultra_fast).
SENSITIVITY_LADDER: list[str] = ["ultra_slow", "slow", "medium", "fast", "ultra_fast"]
# `game/num_teams` variants low to high by value (2 ... 6).
TEAMS_LADDER: list[str] = ["two", "three", "four", "five", "six"]
TEAMS_MIN = 2
# On / off options step along this order, so "up" means on (UC-015 BR-004).
ON_OFF_LADDER: list[str] = ["off", "on"]
# `user/audio_volume.master` variants low to high (flags.md §9); `mute` is the pad's mute (BR-006).
VOLUME_LADDER: list[str] = ["mute", "quiet", "normal", "loud", "max"]

# `game/shield.activation` variants (UC-015 BR-018, #2215): the shield activation modes, the order the
# knob steps them in (clamped, never wraps; flags.md §4).
SHIELD_ACTIVATION_LADDER: list[str] = ["instant", "trigger", "hold"]

# UC-013 / UC-015 BR-004: the intervention levels, most restrictive first; clamped, never wraps.
AGENT_POLICY_LEVELS: list[str] = ["none", "ambient", "standard", "full"]
# Levels a resume can restore (BR-002 saves a level only when it is not "none").
AGENT_SAVED_LEVELS: list[str] = AGENT_POLICY_LEVELS[1:]
# UC-015 BR-003: the resume level when no level is saved (never "full").
AGENT_RESUME_FALLBACK_LEVEL = "ambient"

# UC-044 decision backends, in knob order (UC-015 BR-004; clamped, no wrap).
BACKENDS: list[str] = ["rules", "claude", "openai", "jev"]

# Options of the menu's `step_setting` whose ladder is a flag's variants (`step_variant`).
FLAG_LADDERS: dict[str, list[str]] = {
    "sensitivity": SENSITIVITY_LADDER,
    "num_teams": TEAMS_LADDER,
    "force_all_start": ON_OFF_LADDER,
    "game_instructions": ON_OFF_LADDER,
    "audio_volume.master": VOLUME_LADDER,
    "shield.activation": SHIELD_ACTIVATION_LADDER,
}


# --- Stepping -------------------------------------------------------------------------------


def clamp_step(ladder: list[str], current: str | None, steps: int, *, restart_outside: bool = False) -> tuple:
    """``steps`` moves along ``ladder`` (negative = back), clamped at both ends: (new, at_limit).

    ``at_limit`` is true when a move was asked for but the value cannot change (already at the end
    it moves towards, A7). A current value outside the ladder (or None) never reports ``at_limit``:
    - ``restart_outside=False`` (the mode and agent ladders, the menu's `_ladder_step`): the first
      move lands on ``ladder[0]`` and the remaining moves go on from there;
    - ``restart_outside=True`` (the flag ladders, `FlagServiceWriter.step_variant`): it lands on
      ``ladder[0]`` whatever the number of moves.
    Zero steps return ``(current, False)``.
    """
    if steps == 0:
        return current, False
    last = len(ladder) - 1
    if current not in ladder:
        if restart_outside:
            return ladder[0], False
        index = 0
        moves = abs(steps) - 1
    else:
        index = ladder.index(current)
        moves = abs(steps)
    target = index + moves if steps > 0 else index - moves
    new = ladder[min(max(target, 0), last)]
    return new, new == current


# --- Preview: the value a release would commit ------------------------------------------------


class _Status(Protocol):
    """The fields of `GetMenuStatusResponse` (services.md §3.1) a preview reads."""

    selection: str
    sensitivity: int
    num_teams: int
    instructions: bool
    force_all_start: bool
    audio_volume_master: str
    agent_level: str
    agent_saved_level: str
    agent_enabled_state: str
    agent_backend: str
    shield_activation: str


@dataclass(frozen=True)
class Preview:
    """The variant a commit of the net steps would write, and whether it stops at a limit (A7).

    ``value`` is None when the status does not say where the step starts (unknown, never guessed).
    ``at_limit`` matches the menu's ``at_limit`` error: nothing would be written.
    """

    value: str | None
    at_limit: bool = False


UNKNOWN = Preview(None)

# Options `preview_value` knows: the `step_setting` options plus the backend (`set_backend`).
PREVIEW_OPTIONS = frozenset({*FLAG_LADDERS, "current_game", "agent_level", "decision.backend"})


def _flag_variant(option: str, status: _Status) -> str | None:
    """The status field of a flag option as its variant name, None when out of range."""
    if option == "sensitivity":
        index = status.sensitivity
        return SENSITIVITY_LADDER[index] if 0 <= index < len(SENSITIVITY_LADDER) else None
    if option == "num_teams":
        index = status.num_teams - TEAMS_MIN
        return TEAMS_LADDER[index] if 0 <= index < len(TEAMS_LADDER) else None
    if option == "game_instructions":
        return "on" if status.instructions else "off"
    if option == "force_all_start":
        return "on" if status.force_all_start else "off"
    if option == "audio_volume.master":
        return status.audio_volume_master or None
    if option == "shield.activation":
        return status.shield_activation or None
    return None


def preview_value(option: str, status: _Status | None, net_steps: int) -> Preview:
    """The value a release of ``net_steps`` on ``option`` commits, from the status read at the press.

    The same ladder and clamping as the menu's `step_setting` and the pad's backend step, so the
    preview and the commit agree for every start and every step count (a menu test checks all).
    Lobby-only refusals (A1) and failed writes are the caller's: this only computes the target.
    """
    if status is None or option not in PREVIEW_OPTIONS:
        return UNKNOWN
    if option in FLAG_LADDERS:
        current = _flag_variant(option, status)
        if current is None:
            return UNKNOWN
        new, at_limit = clamp_step(FLAG_LADDERS[option], current, net_steps, restart_outside=True)
        return Preview(new, at_limit)
    if option == "current_game":
        if not status.selection:
            return UNKNOWN
        new, at_limit = clamp_step(MODE_LADDER, status.selection, net_steps)
        return Preview(new, at_limit)
    if option == "decision.backend":
        if status.agent_backend not in BACKENDS:
            return UNKNOWN  # the pad refuses a step from an unknown backend
        new, at_limit = clamp_step(BACKENDS, status.agent_backend, net_steps)
        return Preview(new, at_limit)
    # agent_level: UC-015 BR-002 / BR-003, as the menu's `_step_agent_level`.
    level, enabled = status.agent_level, status.agent_enabled_state
    if not level or enabled not in ("on", "off"):
        return UNKNOWN  # the menu fails closed with these unreadable
    if enabled != "on" or level == "none":
        # Stopped: only the saved level moves, along the saved ladder.
        saved = status.agent_saved_level or AGENT_RESUME_FALLBACK_LEVEL
        new, at_limit = clamp_step(AGENT_SAVED_LEVELS, saved, net_steps)
        return Preview(new, at_limit)
    new, at_limit = clamp_step(AGENT_POLICY_LEVELS, level, net_steps)
    return Preview(new, at_limit)
