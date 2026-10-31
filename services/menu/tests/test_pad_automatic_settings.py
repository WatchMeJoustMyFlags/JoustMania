"""The `automatic` rung of sensitivity and num_teams (UC-015 BR-018, BR-017, A1, A3, A7; #2222).

Generalises the shield mode's `automatic` (#2215): the knob ladder is `automatic` followed by the
value ladder, the knob starts at the operator's persisted value, or at the effective value when
none is set (`automatic` is the bottom rung), a value is
written persisted and `automatic` clears it (DELETE ?persist=true), so the flag file's default (or an
agent promotion) applies again. The menu says "Automatic" and double-flashes the lobby; lobby only.
`GetMenuStatus.operator_settings` tells operator-set from default per setting.
"""

import asyncio
import threading

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
@pytest.mark.parametrize(
    ("option", "effective", "direction", "steps", "written"),
    [
        ("sensitivity", "medium", "up", 1, ("game", "sensitivity", "fast")),  # up means more, from where it stands
        ("sensitivity", "medium", "down", 2, ("game", "sensitivity", "ultra_slow")),
        ("num_teams", "three", "up", 1, ("game", "num_teams", "four")),
        ("num_teams", "three", "down", 1, ("game", "num_teams", "two")),
    ],
)
async def test_with_no_operator_value_the_knob_starts_at_the_effective_value(
    servicer, flags, option, effective, direction, steps, written
):
    flags.values[("game", option)] = effective  # the flag file's default; no persisted operator value
    reply = await pad(servicer, "step_setting", option=option, direction=direction, steps=steps)

    assert (reply.success, reply.error) == (True, "")
    assert flags.written() == [written]
    assert flags.deletes == []
    assert flags.puts[0][2].get("persist") is True


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-005"])
@pytest.mark.parametrize(("option", "effective", "steps"), [("sensitivity", "slow", 3), ("num_teams", "three", 3)])
async def test_automatic_is_reached_by_turning_down_past_the_lowest_value(servicer, flags, option, effective, steps):
    flags.values[("game", option)] = effective
    reply = await pad(servicer, "step_setting", option=option, direction="down", steps=steps)

    assert (reply.success, reply.error) == (True, "")
    assert flags.deletes == [("game", option, "persist=true")] and flags.puts == []
    assert reply.error == "" and servicer.settings_ops.operator_settings[option] == "automatic"


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", steps=[4, 5], br=["BR-018", "BR-005"])
@pytest.mark.parametrize(("option", "top"), [("sensitivity", "ultra_fast"), ("num_teams", "six")])
async def test_with_no_operator_value_at_the_top_a_step_up_is_at_limit(servicer, flags, option, top):
    flags.values[("game", option)] = top
    reply = await pad(servicer, "step_setting", option=option, direction="up", steps=1)

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


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-017"])
async def test_a_partial_map_is_read_again_within_the_refresh_window(servicer, flags):
    use_clients(servicer, dict(GAME_FLAGS))
    operator_set(flags, "sensitivity", "not_a_variant")  # unreadable: left out of the map
    await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)
    first = servicer.settings_ops.operator_refresh_task
    await first
    assert "sensitivity" not in servicer.settings_ops.operator_settings

    await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)  # well within 2 s

    assert servicer.settings_ops.operator_refresh_task is not first  # a new read, not a trusted partial map


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-017"])
async def test_a_complete_fresh_map_is_not_read_again(servicer):
    use_clients(servicer, dict(GAME_FLAGS))
    await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)
    first = servicer.settings_ops.operator_refresh_task
    await first
    await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)

    assert servicer.settings_ops.operator_refresh_task is first


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", steps=[4, 5], br=["BR-018", "BR-010"])
async def test_a_failed_commit_drops_the_cached_value_from_the_status(servicer, flags):
    use_clients(servicer, dict(GAME_FLAGS))
    lobby(servicer)
    operator_set(flags, "sensitivity", "slow")
    await servicer.settings_ops.refresh_operator_settings()
    assert servicer.settings_ops.operator_settings["sensitivity"] == "slow"
    flags.failing.add(("game", "sensitivity"))
    await pad(servicer, "step_setting", option="sensitivity", direction="up", steps=1)

    status = servicer._menu_status()
    assert "sensitivity" not in status.operator_settings  # unknown, not the old value


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4, 5], br=["BR-018", "BR-017"])
async def test_a_read_started_during_the_write_never_puts_the_old_value_back(servicer, flags):
    ops = servicer.settings_ops
    lobby(servicer)
    operator_set(flags, "sensitivity", "slow")
    in_write, release_write = threading.Event(), threading.Event()
    in_read, release_read = threading.Event(), threading.Event()
    real_write = ops._write

    def slow_write(writer, key, variant):
        in_write.set()
        release_write.wait(5)
        return real_write(writer, key, variant)

    def stale_read():
        in_read.set()
        release_read.wait(5)
        return {"sensitivity": "slow"}  # what the flag service served before the write landed

    ops._write = slow_write
    step = asyncio.create_task(pad(servicer, "step_setting", option="sensitivity", direction="up", steps=1))
    await asyncio.to_thread(in_write.wait, 5)
    ops._read_all_operators = stale_read
    refresh = asyncio.create_task(ops.refresh_operator_settings())  # starts during the write
    await asyncio.to_thread(in_read.wait, 5)
    release_write.set()
    assert (await step).success
    release_read.set()  # the read now lands after the commit
    await refresh

    assert ops.operator_settings["sensitivity"] == "medium"  # the commit's value, not the stale "slow"
