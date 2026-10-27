"""The `automatic` rung of sensitivity and num_teams (UC-015 BR-018, BR-017, A1, A3, A7; #2222).

Generalises the shield mode's `automatic` (#2215): the knob ladder is `automatic` followed by the
value ladder, the knob starts at the operator's persisted value (`automatic` when none), a value is
written persisted and `automatic` clears it (DELETE ?persist=true), so the flag file's default (or an
agent promotion) applies again. The menu says "Automatic" and double-flashes the lobby; lobby only.
`GetMenuStatus.operator_settings` tells operator-set from default per setting.
"""

import pytest

from lib import setting_ladders
from lib.types import Sound
from proto import menu_pb2
from services.menu import settings_ops
from services.menu.tests import test_pad_commands
from services.menu.tests.test_pad_commands import _pulses, pad
from services.menu.tests.test_pad_shield_mode import (  # noqa: F401
    AUTOMATIC_FLASHES,
    GAME_FLAGS,
    lobby,
    no_flash_gap,
    use_clients,
)

flags = test_pad_commands.flags
servicer = test_pad_commands.servicer

# option -> (a value below the top of the ladder, the next rung up, the flag file default of the fake service)
CASES = {"sensitivity": ("slow", "medium"), "num_teams": ("three", "four")}


def operator_set(flags, option: str, value: str) -> None:
    flags.admin[("game", option)] = flags.values[("game", option)] = value


@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-005"])
def test_knob_ladders_are_pinned_automatic_first_then_the_value_ladder():
    assert setting_ladders.KNOB_LADDERS["sensitivity"] == [
        "automatic",
        "ultra_slow",
        "slow",
        "medium",
        "fast",
        "ultra_fast",
    ]
    assert setting_ladders.KNOB_LADDERS["num_teams"] == ["automatic", "two", "three", "four", "five", "six"]
    assert setting_ladders.KNOB_LADDERS["shield.activation"] == ["automatic", "instant", "trigger", "hold"]
    assert {"sensitivity", "num_teams", "shield.activation"} == setting_ladders.AUTOMATIC_OPTIONS
    for option in ("sensitivity", "num_teams"):
        assert settings_ops.STEP_OPTIONS[option].order == setting_ladders.KNOB_LADDERS[option]


@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018"])
def test_settings_without_a_meaningful_no_operator_value_have_no_automatic_rung():
    # two-state toggles and the volume (its mute / loud ladder is a free control, not an operator override)
    for option in ("force_all_start", "game_instructions", "audio_volume.master"):
        assert option not in setting_ladders.KNOB_LADDERS
        assert "automatic" not in setting_ladders.FLAG_LADDERS[option]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-005"])
@pytest.mark.parametrize("option", sorted(CASES))
async def test_automatic_clears_the_persisted_value_says_automatic_and_flashes(servicer, flags, option):
    lobby(servicer)
    operator_set(flags, option, CASES[option][0])
    flags.values[("game", option)] = CASES[option][0]
    reply = await pad(servicer, "step_setting", option=option, direction="down", steps=9)

    assert (reply.success, reply.error) == (True, "")
    assert flags.deletes == [("game", option, "persist=true")]  # the persisted operator value is cleared
    assert flags.puts == []  # nothing written
    assert ("game", option) not in flags.admin
    assert servicer.settings_ops.operator_settings[option] == "automatic"
    servicer.state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_SHIELD_AUTOMATIC)
    assert sorted(_pulses(servicer)) == AUTOMATIC_FLASHES


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-005"])
@pytest.mark.parametrize("option", sorted(CASES))
async def test_the_knob_starts_at_automatic_not_at_the_effective_value(servicer, flags, option):
    # No operator value, the flag file's default is some middle rung: one step up is the first value rung.
    reply = await pad(servicer, "step_setting", option=option, direction="up", steps=1)

    assert (reply.success, reply.error) == (True, "")
    first = setting_ladders.FLAG_LADDERS[option][0]
    assert flags.written() == [("game", option, first)]
    assert flags.puts[0][2].get("persist") is True


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", steps=[4, 5], br=["BR-018", "BR-005"])
@pytest.mark.parametrize("option", sorted(CASES))
async def test_automatic_is_the_bottom_of_the_ladder_a_further_step_down_is_at_limit(servicer, flags, option):
    reply = await pad(servicer, "step_setting", option=option, direction="down", steps=1)

    assert (reply.success, reply.error) == (False, "at_limit")
    assert flags.deletes == [] and flags.puts == []
    assert test_pad_commands.failure_cues(servicer) == 0


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1", steps=[4, 5], br=["BR-018", "BR-012"])
@pytest.mark.parametrize("option", sorted(CASES))
async def test_automatic_is_refused_while_a_game_is_live(servicer, flags, option):
    operator_set(flags, option, CASES[option][0])
    live = test_pad_commands.live_lifecycle(servicer)
    reply = await pad(servicer, "step_setting", option=option, direction="down", steps=9)

    assert (reply.success, reply.error) == (False, "game_active")
    assert flags.deletes == [] and flags.puts == []
    assert flags.admin[("game", option)] == CASES[option][0]
    live.cancel()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", steps=[4, 5], br=["BR-018", "BR-010"])
@pytest.mark.parametrize("option", sorted(CASES))
async def test_a_failed_clear_fails_closed_with_the_failure_cue(servicer, flags, option):
    lobby(servicer)
    operator_set(flags, option, CASES[option][0])
    flags.failing.add(("game", option))
    reply = await pad(servicer, "step_setting", option=option, direction="down", steps=9)

    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert flags.admin[("game", option)] == CASES[option][0]
    assert test_pad_commands.failure_cues(servicer) == 2
    assert option not in servicer.settings_ops.operator_settings  # unknown until the next read


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-017"])
async def test_menu_status_tells_operator_set_from_default_per_setting(servicer, flags):
    use_clients(servicer, dict(GAME_FLAGS))
    operator_set(flags, "sensitivity", "fast")  # num_teams and the shield mode stay at their defaults
    first = await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)
    assert dict(first.operator_settings) == {}  # not read yet: unknown, never a guess
    await servicer.settings_ops.operator_refresh_task  # the background read GetMenuStatus started
    second = await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)

    assert dict(second.operator_settings) == {
        "sensitivity": "fast",
        "num_teams": "automatic",
        "shield.activation": "automatic",
    }


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-017"])
async def test_a_setting_that_cannot_be_read_is_left_out_not_guessed(servicer, flags):
    operator_set(flags, "sensitivity", "not_a_variant")
    await servicer.settings_ops.refresh_operator_settings()

    assert "sensitivity" not in servicer.settings_ops.operator_settings
    assert servicer.settings_ops.operator_settings["num_teams"] == "automatic"
