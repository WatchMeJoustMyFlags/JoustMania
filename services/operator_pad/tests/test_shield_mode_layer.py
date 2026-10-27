"""K6 + knob: the shield mode layer (UC-015 step 9, BR-018, BR-017, A1, A7; #2215).

Gestures (K6 is a layer key, its tap unchanged), the commit on release, the lobby-only refusal,
and the live preview per step with its voice lines and the LED preview (`preview_setting`).
"""

from unittest.mock import create_autospec

import pytest

from lib.setting_ladders import SHIELD_KNOB_LADDER
from lib.types import Sound
from proto import game_coordinator_pb2, menu_pb2
from services.operator_pad.actions import Actions
from services.operator_pad.cues import CuePlayer
from services.operator_pad.gestures import LAYER, LAYER_STEP, TAP, Gesture
from services.operator_pad.tests.fakes import CoordinatorStubSpec, GatedSleep, MenuStubSpec
from services.operator_pad.tests.test_actions import sent, status
from services.operator_pad.tests.test_gestures import Pad
from services.operator_pad.tests.test_preview import Rig, settle

SHIELD = "shield.activation"
VOICES = {
    "instant": Sound.MENU_VOX_SHIELD_INSTANT.value,
    "trigger": Sound.MENU_VOX_SHIELD_TRIGGER.value,
    "hold": Sound.MENU_VOX_SHIELD_HOLD.value,
    "automatic": Sound.MENU_VOX_SHIELD_AUTOMATIC.value,
}


@pytest.fixture
def menu():
    stub = create_autospec(MenuStubSpec, instance=True)
    stub.ProcessInput.return_value = menu_pb2.ProcessInputResponse(success=True)
    return stub


@pytest.fixture
def cues():
    return create_autospec(CuePlayer, instance=True)


@pytest.fixture
def actions(menu, cues):
    coordinator = create_autospec(CoordinatorStubSpec, instance=True)
    coordinator.ForceEndGame.return_value = game_coordinator_pb2.ForceEndGameResponse(success=True)
    return Actions(menu, coordinator, cues, sleep=GatedSleep())


# --- Gestures --------------------------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-007", "BR-005"], steps=[9])
def test_k6_with_knob_is_a_layer_and_its_release_commits_not_the_instructions_tap():
    pad = Pad()
    pad.at(0).press("K6")
    assert pad.turn("B") == [Gesture(LAYER_STEP, "K6", steps=1)]
    assert pad.turn("B") == [Gesture(LAYER_STEP, "K6", steps=2)]
    assert pad.machine.preview_key() == "K6"
    assert pad.at(200).release("K6") == [Gesture(LAYER, "K6", steps=2, held_ms=200)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-011"], steps=[9])
def test_k6_tap_without_a_turn_is_still_the_instructions_tap():
    pad = Pad()
    pad.at(0).press("K6")
    assert pad.at(100).release("K6") == [Gesture(TAP, "K6", held_ms=100)]


# --- Commit and refusal ----------------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-005"], steps=[9])
@pytest.mark.parametrize(("steps", "direction", "count"), [(1, "up", "1"), (2, "up", "2"), (-1, "down", "1")])
async def test_k6_release_commits_one_shield_step_setting(actions, menu, steps, direction, count):
    await actions.dispatch(Gesture(LAYER, "K6", steps=steps), status(operator_settings={SHIELD: "trigger"}))
    assert sent(menu) == [{"command": "step_setting", "option": SHIELD, "direction": direction, "steps": count}]


@pytest.mark.uc("UC-015", flow="A1", br=["BR-018", "BR-009"], steps=[9])
async def test_k6_release_while_a_game_is_live_is_refused_and_sends_nothing(actions, menu, cues):
    _action, result = await actions.dispatch(
        Gesture(LAYER, "K6", steps=1), status(lifecycle_live=True, operator_settings={SHIELD: "instant"})
    )
    assert result == "refused"
    menu.ProcessInput.assert_not_awaited()
    cues.refusal.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A7", br=["BR-018", "BR-009"], steps=[9])
async def test_k6_at_the_end_of_the_ladder_plays_the_limit_cue(actions, menu, cues):
    menu.ProcessInput.return_value = menu_pb2.ProcessInputResponse(success=False, error="at_limit")
    await actions.dispatch(Gesture(LAYER, "K6", steps=1), status(operator_settings={SHIELD: "hold"}))
    cues.limit.assert_awaited_once_with(True)


# --- Live preview per step (BR-017) ---------------------------------------------------------


def step_shield(rig: Rig, net: int, start: str = "instant", live: bool = False) -> None:
    rig.step(
        net,
        status=menu_pb2.GetMenuStatusResponse(operator_settings={SHIELD: start}, lifecycle_live=live),
        key="K6",
        option=SHIELD,
        lobby_only=True,
    )


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-017"], steps=[9])
async def test_k6_preview_speaks_the_label_then_each_mode_per_step_and_pulses_it():
    rig = Rig()
    step_shield(rig, 1)
    await rig.time.advance(1.0)
    step_shield(rig, 2)
    await rig.time.advance(1.0)
    assert rig.files() == [Sound.MENU_VOX_LABEL_SHIELD_MODE.value, VOICES["trigger"], VOICES["hold"]]
    assert rig.mode_previews() == [
        {"command": "preview_setting", "option": SHIELD, "mode": "trigger"},
        {"command": "preview_setting", "option": SHIELD, "mode": "hold"},
    ]


@pytest.mark.uc("UC-015", flow="A7", br=["BR-018", "BR-017"], steps=[9])
async def test_k6_preview_past_the_end_says_maximum_and_sends_no_pulse():
    rig = Rig()
    step_shield(rig, 1, start="hold")
    await rig.time.advance(1.0)
    assert rig.files() == [Sound.MENU_VOX_LABEL_SHIELD_MODE.value, Sound.MENU_VOX_STATUS_MAXIMUM.value]
    rig.menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A1", br=["BR-018", "BR-017"], steps=[9])
async def test_k6_preview_while_a_game_is_live_says_not_possible_once_and_shows_nothing():
    rig = Rig()
    step_shield(rig, 1, live=True)
    step_shield(rig, 2, live=True)
    await rig.time.advance(1.0)
    assert rig.files() == [Sound.MENU_VOX_STATUS_NOT_POSSIBLE.value]
    rig.menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-017"], steps=[9])
async def test_k6_hold_ending_without_a_commit_pulses_the_current_mode_again():
    rig = Rig()
    step_shield(rig, 2, start="instant")
    await rig.time.advance(0.6)
    rig.previewer.end_hold()
    await settle()
    assert rig.shown() == ["hold", "instant"]


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-017"], steps=[9])
async def test_k6_preview_down_to_automatic_says_automatic_and_shows_it():
    rig = Rig()
    step_shield(rig, -1, start="instant")
    await rig.time.advance(1.0)
    assert rig.files() == [Sound.MENU_VOX_LABEL_SHIELD_MODE.value, VOICES["automatic"]]
    assert rig.mode_previews() == [{"command": "preview_setting", "option": SHIELD, "mode": "automatic"}]


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-017"], steps=[9])
async def test_k6_preview_with_the_operator_value_unknown_is_silent():
    rig = Rig()
    step_shield(rig, 1, start="")
    await rig.time.advance(1.0)
    assert rig.files() == []
    rig.menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-018"], steps=[9])
def test_shield_voice_table_covers_the_knob_ladder():
    assert set(VOICES) == set(SHIELD_KNOB_LADDER)


# --- The `automatic` rung of sensitivity (K2) and num_teams (K5), #2222 -------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-017"], steps=[4, 5])
@pytest.mark.parametrize(("key", "option", "value"), [("K2", "sensitivity", "slow"), ("K5", "num_teams", "three")])
async def test_preview_down_to_automatic_speaks_the_shared_automatic_line(key, option, value):
    rig = Rig()
    rig.step(
        -9,
        status=menu_pb2.GetMenuStatusResponse(operator_settings={option: value}),
        key=key,
        option=option,
        lobby_only=True,
    )
    await rig.time.advance(1.0)
    assert rig.files()[-1] == Sound.MENU_VOX_SHIELD_AUTOMATIC.value


@pytest.mark.uc("UC-015", flow="main", br=["BR-018", "BR-005"], steps=[4, 5])
@pytest.mark.parametrize(("key", "option"), [("K2", "sensitivity"), ("K5", "num_teams")])
async def test_release_to_automatic_sends_one_step_setting(actions, menu, key, option):
    await actions.dispatch(Gesture(LAYER, key, steps=-9), status(operator_settings={option: "slow"}))
    assert sent(menu) == [{"command": "step_setting", "option": option, "direction": "down", "steps": "9"}]
