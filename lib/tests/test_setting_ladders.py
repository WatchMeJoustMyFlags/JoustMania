"""Shared setting ladders and voice tables (#2106, UC-015 BR-004, BR-005, A7, BR-011).

The exhaustive preview-equals-commit check against the menu's `step_setting` is
`services/menu/tests/test_preview_equals_commit.py`; this file covers the pure helpers.
"""

import json
from pathlib import Path

import pytest
from proto import menu_pb2

from lib import setting_ladders, voice_prompts
from lib.setting_ladders import Preview, clamp_step, preview_value
from lib.types import Games

FLAGD_DIR = Path(__file__).resolve().parent.parent.parent / "services" / "flagd"
LADDER = ["a", "b", "c", "d"]


@pytest.mark.uc("UC-015", br=["BR-004", "BR-005"], flow="A7")
@pytest.mark.parametrize(
    ("current", "steps", "expected"),
    [
        ("b", 1, ("c", False)),
        ("b", 2, ("d", False)),
        ("b", 9, ("d", False)),  # passes the end: stops there and moves (BR-005)
        ("b", -1, ("a", False)),
        ("b", -5, ("a", False)),
        ("d", 1, ("d", True)),  # already at the end: at_limit (A7)
        ("a", -3, ("a", True)),
        ("b", 0, ("b", False)),
        ("x", 1, ("a", False)),  # outside: the first move lands on the first rung ...
        ("x", 3, ("c", False)),  # ... and the rest go on from there (`_ladder_step`)
        ("x", -3, ("a", False)),
        (None, 2, ("b", False)),
    ],
)
def test_clamp_step(current, steps, expected):
    assert clamp_step(LADDER, current, steps) == expected


@pytest.mark.uc("UC-015", br=["BR-004"], flow="A7")
@pytest.mark.parametrize("steps", [1, 3, -2])
def test_clamp_step_restart_outside_lands_on_the_first_rung(steps):
    # `FlagServiceWriter.step_variant`: a variant outside the ladder moves to its first entry.
    assert clamp_step(LADDER, "x", steps, restart_outside=True) == ("a", False)


def _value_order(flag_set: str, key: str) -> list[str]:
    variants = json.loads((FLAGD_DIR / f"{flag_set}.json").read_text())["flags"][key]["variants"]
    return sorted(variants, key=lambda name: variants[name])


@pytest.mark.uc("UC-015", br=["BR-004", "BR-006"])
@pytest.mark.parametrize(
    ("ladder", "flag_set", "key"),
    [
        (setting_ladders.SENSITIVITY_LADDER, "game", "sensitivity"),
        (setting_ladders.TEAMS_LADDER, "game", "num_teams"),
        (setting_ladders.VOLUME_LADDER, "user", "audio_volume.master"),
    ],
)
def test_numeric_ladders_follow_the_flag_values(ladder, flag_set, key):
    """`step_variant` orders numeric variants by value; the shared ladder is that order."""
    assert ladder == _value_order(flag_set, key)


@pytest.mark.uc("UC-015", br=["BR-011"])
def test_voice_tables_cover_every_ladder_value():
    assert len(voice_prompts.SENSITIVITY_VOICES) == len(setting_ladders.SENSITIVITY_LADDER)
    teams = range(setting_ladders.TEAMS_MIN, setting_ladders.TEAMS_MIN + len(setting_ladders.TEAMS_LADDER))
    assert set(voice_prompts.NUM_TEAMS_VOICES) == set(teams)
    assert set(voice_prompts.AGENT_LEVEL_VOICES) == set(setting_ladders.AGENT_POLICY_LEVELS)
    assert set(setting_ladders.MODE_LADDER) <= set(voice_prompts.GAME_MODE_VOICE)
    assert set(voice_prompts.GAME_MODE_VOICE) <= {g.name for g in Games}


@pytest.mark.uc("UC-015", br=["BR-004"])
def test_preview_of_a_status_field_out_of_range_is_unknown():
    assert preview_value("sensitivity", menu_pb2.GetMenuStatusResponse(sensitivity=7), 1) == Preview(None)
    assert preview_value("num_teams", menu_pb2.GetMenuStatusResponse(num_teams=0), 1) == Preview(None)
    assert preview_value("decision.backend", menu_pb2.GetMenuStatusResponse(agent_backend="x"), 1) == Preview(None)
    assert preview_value("current_game", menu_pb2.GetMenuStatusResponse(), 1) == Preview(None)
    assert preview_value("sensitivity", None, 1) == Preview(None)
    assert preview_value("bogus", menu_pb2.GetMenuStatusResponse(), 1) == Preview(None)


@pytest.mark.uc("UC-015", br=["BR-013"])
@pytest.mark.parametrize(("level", "enabled"), [("", "on"), ("standard", "unknown"), ("standard", "")])
def test_agent_level_preview_with_an_unresolved_flag_is_unknown(level, enabled):
    """The menu fails closed when the level or the kill switch cannot be read: no guessed value."""
    status = menu_pb2.GetMenuStatusResponse(agent_level=level, agent_enabled_state=enabled)
    assert preview_value("agent_level", status, 1) == Preview(None)


@pytest.mark.uc("UC-015", br=["BR-002", "BR-003", "BR-004"])
def test_agent_level_preview_of_a_stopped_agent_moves_the_saved_level():
    stopped = menu_pb2.GetMenuStatusResponse(agent_level="none", agent_enabled_state="on", agent_saved_level="")
    assert preview_value("agent_level", stopped, 1) == Preview("standard")  # from the fallback `ambient`
    assert preview_value("agent_level", stopped, -1) == Preview("ambient", at_limit=True)


@pytest.mark.uc("UC-015", br=["BR-009", "BR-011"])
def test_pad_words_name_every_backend_and_every_preview_option():
    """#2106: K7 says the backend by name; every setting the pad announces or previews has a label."""
    assert list(voice_prompts.BACKEND_VOICES) == setting_ladders.BACKENDS
    assert setting_ladders.PREVIEW_OPTIONS - {"force_all_start"} <= set(voice_prompts.LABEL_VOICES)
    assert voice_prompts.LIMIT_VOICES[True] != voice_prompts.LIMIT_VOICES[False]
    assert set(voice_prompts.LOCK_VOICES.values()).isdisjoint(voice_prompts.AGENT_SWITCH_VOICES.values())
