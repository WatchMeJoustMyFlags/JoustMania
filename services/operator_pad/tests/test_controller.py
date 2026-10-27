"""Codes -> gestures -> actions (UC-015 A4, A5): fresh status at every press and release."""

import asyncio
from unittest.mock import create_autospec, patch

import pytest

from lib.types import Sound
from proto import menu_pb2
from services.operator_pad import controller as controller_module
from services.operator_pad.actions import Actions
from services.operator_pad.config import DEFAULT_KEYMAP, PadConfig, Timings, parse_keymap
from services.operator_pad.controller import PadController
from services.operator_pad.cues import CuePlayer
from services.operator_pad.gestures import HOLD, PRESS, RELEASE, REPEAT, Gesture
from services.operator_pad.state import MenuStatusReader
from services.operator_pad.tests.fakes import AudioStubSpec, CoordinatorStubSpec, MenuStubSpec

KEYMAP = parse_keymap(DEFAULT_KEYMAP)
F13, F19, F16 = 183, 189, 186  # K1, K4, knob turn A
UNKNOWN = 115  # KEY_VOLUMEUP of a stock encoder


@pytest.fixture
def parts():
    config = create_autospec(PadConfig, instance=True)
    config.enabled.return_value = True
    config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    actions = create_autospec(Actions, instance=True)
    actions.dispatch.return_value = ("none", "noop")
    cues = create_autospec(CuePlayer, instance=True)
    pad = PadController(config, reader, actions, cues)
    pad.attached(KEYMAP)
    return pad, config, reader, actions, cues


@pytest.mark.uc("UC-015", flow="A5", br=["BR-011"], steps=[7])
async def test_k4_reads_a_fresh_status_at_press_and_release_and_passes_both(parts):
    pad, _, reader, actions, _ = parts
    at_press = menu_pb2.GetMenuStatusResponse(lifecycle_generation=2)
    at_release = menu_pb2.GetMenuStatusResponse(lifecycle_generation=4)
    reader.fetch.side_effect = [at_press, at_release]
    await pad.on_code(F19, PRESS, 0)
    await pad.on_code(F19, REPEAT, 500)
    await pad.on_code(F19, RELEASE, 1600)
    assert reader.fetch.await_count == 2
    actions.dispatch.assert_awaited_once_with(Gesture(HOLD, "K4", held_ms=1600), at_release, at_press)


@pytest.mark.uc("UC-015", flow="main", br=["BR-005"], steps=[8])
async def test_knob_turns_read_no_status(parts):
    pad, _, reader, actions, _ = parts
    await pad.on_code(F16, PRESS, 0)
    await pad.on_code(F16, RELEASE, 5)
    reader.fetch.assert_not_awaited()
    actions.dispatch.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A4", br=["BR-009"])
async def test_unknown_codes_are_ignored_counted_and_cued_once(parts):
    pad, _, reader, actions, cues = parts
    with patch.object(controller_module.metrics, "unknown_keys_total") as counter:
        for t in range(3):
            await pad.on_code(UNKNOWN, PRESS, t * 10)
            await pad.on_code(UNKNOWN, RELEASE, t * 10 + 5)
    assert counter.inc.call_count == 3
    cues.unknown_key.assert_awaited_once()
    actions.dispatch.assert_not_awaited()
    reader.fetch.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A4")
async def test_no_unknown_key_cue_once_a_known_key_was_seen(parts):
    pad, _, reader, _, cues = parts
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse()
    await pad.on_code(F13, PRESS, 0)
    await pad.on_code(UNKNOWN, PRESS, 10)
    cues.unknown_key.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A4", br=["BR-010"])
async def test_detach_mid_hold_drops_the_gesture_and_the_pending_volume(parts):
    pad, _, reader, actions, cues = parts
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse()
    await pad.on_code(F19, PRESS, 0)
    pad.detached()
    actions.cancel_pending.assert_called_once()
    cues.cancel_all.assert_called_once()  # no announcement plays on after an unplug
    await pad.on_code(F19, RELEASE, 2000)
    actions.dispatch.assert_not_awaited()


@pytest.mark.uc("UC-015", cond=["Pre-1"])
async def test_pad_disabled_by_flag_does_nothing(parts):
    pad, config, reader, actions, _ = parts
    config.enabled.return_value = False
    await pad.on_code(F13, PRESS, 0)
    await pad.on_code(F13, RELEASE, 100)
    reader.fetch.assert_not_awaited()
    actions.dispatch.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A4", br=["BR-007"])
async def test_events_dropped_resets_the_gesture_in_progress(parts):
    pad, _, reader, actions, _ = parts
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse()
    await pad.on_code(F19, PRESS, 0)
    pad.events_dropped()  # the K4 release may be among the dropped events
    await pad.on_code(F19, RELEASE, 2000)
    actions.dispatch.assert_not_awaited()


# --- smooth knob end to end (BR-004, BR-005) -----------------------------------------------------


@pytest.fixture
def real_pad():
    """The real gesture machine and Actions behind the controller; only the stubs are fakes."""
    from services.operator_pad.tests.fakes import GatedSleep, MenuStubSpec

    config = create_autospec(PadConfig, instance=True)
    config.enabled.return_value = True
    config.timings.return_value = Timings(knob_events_per_step=4)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(sensitivity=2)
    menu = create_autospec(MenuStubSpec, instance=True)
    menu.ProcessInput.return_value = menu_pb2.ProcessInputResponse(success=True)
    sleep = GatedSleep()
    sleep.gate.set()
    actions = Actions(menu, None, create_autospec(CuePlayer, instance=True), sleep=sleep)
    pad = PadController(config, reader, actions, create_autospec(CuePlayer, instance=True))
    pad.attached(KEYMAP)
    return pad, actions, menu


F17, F14 = 187, 184  # knob turn B, K2


@pytest.mark.uc("UC-015", flow="main", br=["BR-005"], steps=[8])
async def test_25_raw_events_at_n4_write_the_volume_with_steps_6(real_pad):
    pad, actions, menu = real_pad
    for i in range(25):
        await pad.on_code(F17, PRESS, i * 10)
        await pad.on_code(F17, RELEASE, i * 10 + 1)
    await actions.drain()
    (call,) = menu.ProcessInput.await_args_list
    assert dict(call.args[0].data) == {
        "command": "step_setting",
        "option": "audio_volume.master",
        "direction": "up",
        "steps": "6",
    }


@pytest.mark.uc("UC-015", flow="main", br=["BR-004", "BR-005"], steps=[5])
async def test_25_raw_events_at_n4_write_a_layer_setting_with_steps_6(real_pad):
    pad, actions, menu = real_pad
    await pad.on_code(F14, PRESS, 0)
    for i in range(25):
        await pad.on_code(F17, PRESS, 10 + i * 10)
        await pad.on_code(F17, RELEASE, 11 + i * 10)
    await pad.on_code(F14, RELEASE, 400)
    await actions.drain()
    (call,) = menu.ProcessInput.await_args_list
    assert dict(call.args[0].data) == {
        "command": "step_setting",
        "option": "sensitivity",
        "direction": "up",
        "steps": "6",
    }


@pytest.mark.uc("UC-015", br=["BR-009", "BR-011"])
async def test_a_hanging_announcement_never_holds_up_the_next_key():
    """#2106: an announcement (label, then value) runs in the background; the input reader goes on at once."""
    release_audio = asyncio.Event()
    requested: list[str] = []

    async def hanging_play(request, **_kwargs):
        requested.append(request.file_path)
        await release_audio.wait()  # an audio service that never answers (longer than any cue timeout)

    audio = create_autospec(AudioStubSpec, instance=True)
    audio.PlaySound.side_effect = hanging_play
    cue_player = CuePlayer(audio)
    actions = Actions(
        create_autospec(MenuStubSpec, instance=True), create_autospec(CoordinatorStubSpec, instance=True), cue_player
    )
    config = create_autospec(PadConfig, instance=True)
    config.enabled.return_value = True
    config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(
        sensitivity=2, num_teams=3, agent_enabled_state="on", agent_interventions_enabled_state="on"
    )
    pad = PadController(config, reader, actions, cue_player)
    pad.attached(KEYMAP)
    code = {name: c for c, name in KEYMAP.items()}

    for t, key in ((0, "K2"), (100, "K5")):
        await asyncio.wait_for(pad.on_code(code[key], PRESS, t), 0.2)
        await asyncio.wait_for(pad.on_code(code[key], RELEASE, t + 50), 0.2)  # the tap: would block if awaited
        await asyncio.sleep(0)
    # Both labels were asked for while the first one still hangs.
    assert requested == [Sound.MENU_VOX_LABEL_SENSITIVITY.value, Sound.MENU_VOX_LABEL_TEAMS.value]
    release_audio.set()
    await cue_player.drain()
    # The latest announcement wins: the first one was cancelled, only the second value plays.
    assert requested[2:] == [Sound.MENU_VOX_ADMINOP_3.value]
