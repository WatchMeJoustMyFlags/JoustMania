"""The pad's shield mode in the menu (UC-015 step 9, BR-018, BR-017, A1, A7; #2215, services.md §3.1).

`step_setting` option `shield.activation` writes `game/shield.activation` as a persisted admin value
(like the other pad settings, so it survives a flag-service restart), says the mode and pulses
its colour on the lobby controllers; it is lobby only. `preview_setting` pulses a shield mode
without writing. `GetMenuStatus` carries the variant the pad's live preview starts from.
"""

import pytest

from lib.types import Sound
from proto import controller_manager_pb2, menu_pb2
from services.menu import settings_ops
from services.menu.handlers.base import ControllerState
from services.menu.tests import test_pad_commands
from services.menu.tests.test_menu_status import FLAGS, in_process_client
from services.menu.tests.test_pad_commands import _pulses, live_lifecycle, pad

SHIELD = "shield.activation"
PULSE = controller_manager_pb2.GAME_EFFECT_PULSE
# The real-servicer fixtures of the pad command tests (a fake flag service behind real writers).
flags = test_pad_commands.flags
servicer = test_pad_commands.servicer
GAME_FLAGS = {k: v for k, v in FLAGS["game"].items() if k != SHIELD}


def use_clients(menu, game: dict) -> None:
    """In-process flag clients as in test_menu_status, with ``game`` for the game set."""
    menu.game_settings_client = in_process_client(game)
    menu.user_prefs_client = in_process_client(FLAGS["user"])
    menu.agent_settings_client = in_process_client(FLAGS["agent"])


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
        ("instant", "up", 1, "trigger", Sound.MENU_VOX_SHIELD_TRIGGER),
        ("trigger", "up", 1, "hold", Sound.MENU_VOX_SHIELD_HOLD),
        ("instant", "up", 2, "hold", Sound.MENU_VOX_SHIELD_HOLD),
        ("hold", "down", 2, "instant", Sound.MENU_VOX_SHIELD_INSTANT),
    ],
)
async def test_shield_step_writes_once_persisted_says_and_pulses_the_mode(
    servicer, flags, start, direction, steps, new, voice
):
    lobby(servicer)
    flags.values[("game", SHIELD)] = start
    reply = await pad(servicer, "step_setting", option=SHIELD, direction=direction, steps=steps)

    assert (reply.success, reply.error) == (True, "")
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
@pytest.mark.parametrize(("start", "direction"), [("hold", "up"), ("instant", "down")])
async def test_shield_step_at_the_end_writes_nothing_and_shows_nothing(servicer, flags, start, direction):
    lobby(servicer)
    flags.values[("game", SHIELD)] = start
    reply = await pad(servicer, "step_setting", option=SHIELD, direction=direction, steps=1)

    assert (reply.success, reply.error) == (False, "at_limit")
    assert flags.puts == []
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
