"""Voice prompts shared by the menu and the operator pad (UC-011, UC-012, UC-013, UC-015 BR-009, BR-011; #2106).

One table per setting from its value to the `Sound` id the audio service resolves to a voice
line of the selected `menu_voice` (UC-031). The menu voices its commits from here and the pad
its announcements, so the two never drift apart.

Pure module: no I/O, no service imports.
"""

from __future__ import annotations

from lib.types import Games, Sound

# Voice line per sensitivity level index (0 = ultra_slow ... 4 = ultra_fast), as original JoustMania.
SENSITIVITY_VOICES: list[Sound] = [
    Sound.MENU_VOX_SENSITIVITY_ULTRA_HIGH,  # ULTRA_SLOW (0)
    Sound.MENU_VOX_SENSITIVITY_HIGH,  # SLOW (1)
    Sound.MENU_VOX_SENSITIVITY_MEDIUM,  # MEDIUM (2)
    Sound.MENU_VOX_SENSITIVITY_LOW,  # FAST (3)
    Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW,  # ULTRA_FAST (4)
]
# The number voices 1 to 6 (the admin-option recordings).
NUMBER_VOICES: dict[int, Sound] = {
    1: Sound.MENU_VOX_ADMINOP_1,
    2: Sound.MENU_VOX_ADMINOP_2,
    3: Sound.MENU_VOX_ADMINOP_3,
    4: Sound.MENU_VOX_ADMINOP_4,
    5: Sound.MENU_VOX_ADMINOP_5,
    6: Sound.MENU_VOX_ADMINOP_6,
}
# `num_teams` 2 to 6.
NUM_TEAMS_VOICES: dict[int, Sound] = {n: NUMBER_VOICES[n] for n in range(2, 7)}
# "off" for the panic-off level, a numbered level (1..3) for the permissive ones.
AGENT_LEVEL_VOICES: dict[str, Sound] = {
    "none": Sound.MENU_VOX_ADMINOP_OFF,
    "ambient": Sound.MENU_VOX_ADMINOP_1,
    "standard": Sound.MENU_VOX_ADMINOP_2,
    "full": Sound.MENU_VOX_ADMINOP_3,
}
# UC-015 BR-002: a stopped agent is announced as "off".
AGENT_STOPPED_VOICE = Sound.MENU_VOX_ADMINOP_OFF
# UC-015 step 6, BR-009: one distinct voice line per direction of the agent switch.
AGENT_SWITCH_VOICES: dict[str, Sound] = {
    "stop": Sound.MENU_VOX_ADMINOP_OFF,
    "resume": Sound.MENU_VOX_ADMINOP_ON,
}
# UC-012: the on / off voices of `force_all_start` and `game_instructions`.
FORCE_ALL_START_VOICES: dict[bool, Sound] = {True: Sound.MENU_VOX_ADMINOP_TRUE, False: Sound.MENU_VOX_ADMINOP_FALSE}
INSTRUCTIONS_VOICES: dict[bool, Sound] = {True: Sound.MENU_VOX_INSTRUCTIONS_ON, False: Sound.MENU_VOX_INSTRUCTIONS_OFF}
# UC-015 A6, BR-009: the pad's lock directions, distinct from the agent stop / resume lines (#2106).
LOCK_VOICES: dict[bool, Sound] = {True: Sound.MENU_VOX_PAD_LOCKED, False: Sound.MENU_VOX_PAD_UNLOCKED}

# UC-015 BR-011 (#2106): the label a pad announcement plays before the value, per setting.
LABEL_VOICES: dict[str, Sound] = {
    "current_game": Sound.MENU_VOX_LABEL_GAME_MODE,
    "sensitivity": Sound.MENU_VOX_LABEL_SENSITIVITY,
    "num_teams": Sound.MENU_VOX_LABEL_TEAMS,
    "agent_level": Sound.MENU_VOX_LABEL_AGENT,
    "decision.backend": Sound.MENU_VOX_LABEL_BACKEND,
    "audio_volume.master": Sound.MENU_VOX_LABEL_VOLUME,
    "game_instructions": Sound.MENU_VOX_LABEL_INSTRUCTIONS,
    "shield.activation": Sound.MENU_VOX_LABEL_SHIELD_MODE,
}
# UC-015 BR-018 (#2215): the shield activation modes (`game/shield.activation` variants) by name.
SHIELD_ACTIVATION_VOICES: dict[str, Sound] = {
    "instant": Sound.MENU_VOX_SHIELD_INSTANT,
    "trigger": Sound.MENU_VOX_SHIELD_TRIGGER,
    "hold": Sound.MENU_VOX_SHIELD_HOLD,
}
# UC-044 decision backends by name (UC-015 BR-011), not by knob position.
BACKEND_VOICES: dict[str, Sound] = {
    "rules": Sound.MENU_VOX_BACKEND_RULES,
    "claude": Sound.MENU_VOX_BACKEND_CLAUDE,
    "openai": Sound.MENU_VOX_BACKEND_OPENAI,
    "jev": Sound.MENU_VOX_BACKEND_JEV,
}
# UC-015 BR-009 (#2106): the pad's status words.
NOT_POSSIBLE_VOICE = Sound.MENU_VOX_STATUS_NOT_POSSIBLE  # a refusal
LIMIT_VOICES: dict[bool, Sound] = {True: Sound.MENU_VOX_STATUS_MAXIMUM, False: Sound.MENU_VOX_STATUS_MINIMUM}  # by "up"
CANCELLED_VOICE = Sound.MENU_VOX_STATUS_CANCELLED
NO_CONNECTION_VOICE = Sound.MENU_VOX_STATUS_NO_CONNECTION  # the menu cannot be reached

# UC-011 step 3: the voice line of each mode (keys are `Games` member names).
GAME_MODE_VOICE: dict[str, Sound] = {
    "JoustFFA": Sound.MENU_VOX_JOUST_FFA,
    "JoustTeams": Sound.MENU_VOX_JOUST_TEAMS,
    "JoustRandomTeams": Sound.MENU_VOX_RANDOM_TEAMS,
    "Swapper": Sound.MENU_VOX_SWAPPER,
    "Werewolf": Sound.MENU_VOX_WEREWOLVES,
    "Traitor": Sound.MENU_VOX_TRAITOR,
    "Zombies": Sound.MENU_VOX_ZOMBIES,
    "FightClub": Sound.MENU_VOX_FIGHT_CLUB,
    "Tournament": Sound.MENU_VOX_TOURNAMENT,
    "NonStop": Sound.MENU_VOX_NONSTOP_JOUST,
}
_unknown_modes = set(GAME_MODE_VOICE) - {g.name for g in Games}
if _unknown_modes:  # a renamed mode fails at import, not as a silent announcement (#1815)
    raise ValueError(f"GAME_MODE_VOICE has keys that are not Games members: {sorted(_unknown_modes)}")
