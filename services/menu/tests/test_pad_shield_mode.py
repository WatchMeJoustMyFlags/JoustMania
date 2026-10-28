"""The pad's shield mode in the menu (UC-015 step 9, BR-018, BR-017, A1, A7; #2215, services.md §3.1).

`step_setting` option `shield.activation` steps the operator's value along automatic, instant,
trigger, hold: a mode is written as a persisted admin value (it survives a flag-service restart),
`automatic` clears it (DELETE ?persist=true). The menu says the result and shows it on the lobby
controllers; it is lobby only. `preview_setting` shows a rung without writing. `GetMenuStatus`
carries the effective variant and the operator's value the pad's live preview starts from.
"""

import asyncio
import threading
from unittest.mock import patch

import pytest

from lib import setting_ladders
from lib.flag_service_writer import FlagServiceWriter
from lib.types import Sound
from proto import controller_manager_pb2, menu_pb2
from services.menu import settings_ops
from services.menu.handlers.base import ControllerState
from services.menu.tests import test_pad_commands
from services.menu.tests.test_menu_status import FLAGS, in_process_client
from services.menu.tests.test_pad_commands import _pulses, live_lifecycle, pad

SHIELD = "shield.activation"
PULSE = controller_manager_pb2.GAME_EFFECT_PULSE
FLASH = controller_manager_pb2.GAME_EFFECT_FLASH
WHITE = (255, 255, 255)
AUTOMATIC_FLASHES = [
    ("dim", FLASH, WHITE, settings_ops.AUTOMATIC_FLASH_MS),
    ("dim", FLASH, WHITE, settings_ops.AUTOMATIC_FLASH_MS),
    ("ready", FLASH, WHITE, settings_ops.AUTOMATIC_FLASH_MS),
    ("ready", FLASH, WHITE, settings_ops.AUTOMATIC_FLASH_MS),
]
# The real-servicer fixtures of the pad command tests (a fake flag service behind real writers).
flags = test_pad_commands.flags
servicer = test_pad_commands.servicer
GAME_FLAGS = {k: v for k, v in FLAGS["game"].items() if k != SHIELD}


def use_clients(menu, game: dict) -> None:
    """In-process flag clients as in test_menu_status, with ``game`` for the game set."""
    menu.game_settings_client = in_process_client(game)
    menu.user_prefs_client = in_process_client(FLAGS["user"])
    menu.agent_settings_client = in_process_client(FLAGS["agent"])


def operator_set(flags, value: str) -> None:
    """The pad's persisted value in the fake flag service (`automatic` = none)."""
    if value != "automatic":
        flags.admin[("game", SHIELD)] = flags.values[("game", SHIELD)] = value


@pytest.fixture(autouse=True)
def no_flash_gap(monkeypatch):
    monkeypatch.setattr(settings_ops, "AUTOMATIC_FLASH_GAP_SECONDS", 0)


def lobby(menu) -> None:
    menu.state_manager.controller_states = {
        "dim": ControllerState.CONNECTED,
        "ready": ControllerState.READY,
        "admin": ControllerState.ADMIN,
        "idle": ControllerState.IDLE,
    }


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-001", "BR-005"])
@pytest.mark.parametrize(
    ("start", "direction", "steps", "new", "voice"),
    [
        ("automatic", "up", 1, "hold", Sound.MENU_VOX_SHIELD_HOLD),  # no operator value: from the effective trigger
        ("instant", "up", 1, "trigger", Sound.MENU_VOX_SHIELD_TRIGGER),
        ("trigger", "up", 1, "hold", Sound.MENU_VOX_SHIELD_HOLD),
        ("automatic", "up", 3, "hold", Sound.MENU_VOX_SHIELD_HOLD),
        ("hold", "down", 2, "instant", Sound.MENU_VOX_SHIELD_INSTANT),
    ],
)
async def test_shield_step_writes_once_persisted_says_and_pulses_the_mode(
    servicer, flags, start, direction, steps, new, voice
):
    lobby(servicer)
    operator_set(flags, start)
    reply = await pad(servicer, "step_setting", option=SHIELD, direction=direction, steps=steps)

    assert (reply.success, reply.error) == (True, "")
    assert flags.deletes == []
    assert flags.written() == [("game", SHIELD, new)]
    assert flags.puts[0][2].get("persist") is True  # persisted: survives a flag-service restart (BR-001)
    servicer.state_manager.audio.play_voice.assert_awaited_once_with(voice)
    color = settings_ops.SHIELD_MODE_COLORS[new]
    assert sorted(_pulses(servicer)) == [
        ("dim", PULSE, color, settings_ops.MODE_PULSE_MS),
        ("ready", PULSE, color, settings_ops.MODE_PULSE_MS),
    ]


def test_each_shield_mode_has_its_own_colour():
    assert len(set(settings_ops.SHIELD_MODE_COLORS.values())) == 3


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-005"])
@pytest.mark.parametrize(("start", "steps"), [("instant", 1), ("trigger", 2), ("hold", 3), ("hold", 9)])
async def test_shield_step_to_automatic_clears_the_persisted_value(servicer, flags, start, steps):
    lobby(servicer)
    operator_set(flags, start)
    reply = await pad(servicer, "step_setting", option=SHIELD, direction="down", steps=steps)

    assert (reply.success, reply.error) == (True, "")
    assert flags.deletes == [("game", SHIELD, "persist=true")]  # the persisted operator value is cleared
    assert flags.puts == []  # nothing written
    assert ("game", SHIELD) not in flags.admin
    assert flags.values[("game", SHIELD)] == "trigger"  # the flag file's default applies again
    assert servicer.settings_ops.operator_settings[SHIELD] == "automatic"
    servicer.state_manager.audio.play_voice.assert_awaited_once_with(Sound.MENU_VOX_SHIELD_AUTOMATIC)
    assert sorted(_pulses(servicer)) == AUTOMATIC_FLASHES  # a double white flash, no mode pulse


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", steps=[9], br=["BR-018", "BR-010"])
async def test_a_failed_clear_fails_closed_with_the_failure_cue(servicer, flags):
    lobby(servicer)
    operator_set(flags, "instant")
    flags.failing.add(("game", SHIELD))
    reply = await pad(servicer, "step_setting", option=SHIELD, direction="down", steps=1)

    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert flags.admin[("game", SHIELD)] == "instant"
    assert test_pad_commands.failure_cues(servicer) == 2
    assert _pulses(servicer) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", steps=[9], br=["BR-018", "BR-010"])
async def test_an_unreadable_operator_value_writes_nothing(servicer, flags):
    flags.failing_get.add(("game", SHIELD))
    reply = await pad(servicer, "step_setting", option=SHIELD, direction="up", steps=1)

    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert flags.puts == [] and flags.deletes == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1", steps=[9], br=["BR-018", "BR-012"])
async def test_shield_step_is_refused_while_a_game_is_live(servicer, flags):
    lobby(servicer)
    live_lifecycle(servicer)
    reply = await pad(servicer, "step_setting", option=SHIELD, direction="up", steps=1)

    assert (reply.success, reply.error) == (False, "game_active")
    assert flags.puts == []
    servicer.state_manager.audio.play_voice.assert_not_awaited()
    assert _pulses(servicer) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", steps=[9], br=["BR-018", "BR-005"])
@pytest.mark.parametrize(("start", "direction"), [("hold", "up")])
async def test_shield_step_at_the_end_writes_nothing_and_shows_nothing(servicer, flags, start, direction):
    lobby(servicer)
    operator_set(flags, start)
    reply = await pad(servicer, "step_setting", option=SHIELD, direction=direction, steps=1)

    assert (reply.success, reply.error) == (False, "at_limit")
    assert flags.puts == [] and flags.deletes == []
    assert _pulses(servicer) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
async def test_shield_preview_pulses_the_mode_and_writes_nothing(servicer, flags):
    lobby(servicer)
    reply = await pad(servicer, "preview_setting", option=SHIELD, mode="hold")

    assert (reply.success, reply.error) == (True, "")
    assert sorted(_pulses(servicer)) == [
        ("dim", PULSE, settings_ops.SHIELD_MODE_COLORS["hold"], settings_ops.MODE_PULSE_MS),
        ("ready", PULSE, settings_ops.SHIELD_MODE_COLORS["hold"], settings_ops.MODE_PULSE_MS),
    ]
    assert flags.puts == []
    servicer.state_manager.audio.play_voice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
async def test_shield_preview_of_automatic_double_flashes_and_clears_nothing(servicer, flags):
    lobby(servicer)
    operator_set(flags, "hold")
    reply = await pad(servicer, "preview_setting", option=SHIELD, mode="automatic")

    assert (reply.success, reply.error) == (True, "")
    assert sorted(_pulses(servicer)) == AUTOMATIC_FLASHES
    assert flags.puts == [] and flags.deletes == []
    assert flags.admin[("game", SHIELD)] == "hold"


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1", steps=[9], br=["BR-018", "BR-017"])
async def test_shield_preview_is_refused_while_a_game_is_live(servicer):
    lobby(servicer)
    live_lifecycle(servicer)
    reply = await pad(servicer, "preview_setting", option=SHIELD, mode="hold")

    assert (reply.success, reply.error) == (False, "game_active")
    assert _pulses(servicer) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A8", steps=[9], br=["BR-018"])
async def test_shield_preview_of_an_unknown_mode_is_bad_option(servicer):
    lobby(servicer)
    reply = await pad(servicer, "preview_setting", option=SHIELD, mode="JoustFFA")

    assert (reply.success, reply.error) == (False, "bad_option")
    assert _pulses(servicer) == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
@pytest.mark.parametrize("variant", ["instant", "trigger", "hold"])
async def test_menu_status_carries_the_shield_mode_variant(servicer, variant):
    game = dict(GAME_FLAGS)
    game[SHIELD] = (variant, variant)
    use_clients(servicer, game)
    status = await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)

    assert status.shield_activation == variant


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
async def test_menu_status_shield_mode_is_empty_when_unresolvable(servicer):
    use_clients(servicer, dict(GAME_FLAGS))  # no shield.activation
    status = await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)

    assert status.shield_activation == ""


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
@pytest.mark.parametrize("operator", ["automatic", "instant", "trigger", "hold"])
async def test_menu_status_carries_the_operator_value_read_in_the_background(servicer, flags, operator):
    use_clients(servicer, dict(GAME_FLAGS))
    operator_set(flags, operator)
    first = await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)
    assert SHIELD not in first.operator_settings  # not read yet: unknown, never a guess
    await servicer.settings_ops.operator_refresh_task  # the background read GetMenuStatus started
    second = await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)
    assert second.operator_settings[SHIELD] == operator


@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-005"])
def test_shield_knob_ladder_order_is_pinned():
    # automatic first, then the modes; the commit steps along it whatever order the flag service lists.
    assert setting_ladders.SHIELD_KNOB_LADDER == ["automatic", "instant", "trigger", "hold"]
    assert settings_ops.STEP_OPTIONS[SHIELD].order == setting_ladders.SHIELD_KNOB_LADDER


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
async def test_the_operator_value_is_read_once_at_menu_start(servicer, flags):
    operator_set(flags, "hold")
    servicer.read_operator_settings_at_start()
    await servicer.settings_ops.operator_refresh_task

    assert servicer.settings_ops.operator_settings[SHIELD] == "hold"  # the first K6 hold has a start value


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018", "BR-017"])
async def test_a_background_read_in_flight_during_a_commit_never_puts_the_old_value_back(servicer, flags):
    ops = servicer.settings_ops
    operator_set(flags, "instant")
    real_read = ops._read_all_operators
    started, release = threading.Event(), threading.Event()

    def stale_read():
        started.set()
        release.wait(5)
        return {SHIELD: "instant"}  # what the flag service served before the commit

    ops._read_all_operators = stale_read
    refresh = asyncio.create_task(ops.refresh_operator_settings())
    await asyncio.to_thread(started.wait, 5)
    ops._read_all_operators = real_read
    reply = await pad(servicer, "step_setting", option=SHIELD, direction="up", steps=1)
    assert (reply.success, reply.error) == (True, "")
    release.set()
    await refresh

    assert ops.operator_settings[SHIELD] == "trigger"


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", steps=[9], br=["BR-018", "BR-010"])
async def test_an_exception_while_clearing_fails_closed(servicer, flags):
    lobby(servicer)
    operator_set(flags, "instant")
    with patch.object(FlagServiceWriter, "clear_persisted", autospec=True, side_effect=RuntimeError("boom")):
        reply = await pad(servicer, "step_setting", option=SHIELD, direction="down", steps=1)

    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert test_pad_commands.failure_cues(servicer) == 2
    assert _pulses(servicer) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["sensitivity", "num_teams"])
@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-017"])
async def test_k2_k5_preview_of_automatic_double_flashes_and_a_value_is_bad_option(servicer, flags, option):
    lobby(servicer)
    reply = await pad(servicer, "preview_setting", option=option, mode="automatic")
    assert (reply.success, reply.error) == (True, "")
    assert sorted(_pulses(servicer)) == AUTOMATIC_FLASHES
    assert flags.puts == [] and flags.deletes == []

    bad = await pad(servicer, "preview_setting", option=option, mode="2")
    assert (bad.success, bad.error) == (False, "bad_option")
