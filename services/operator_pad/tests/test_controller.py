"""Codes -> gestures -> actions (UC-015 A4, A5): fresh status at every press and release."""

from unittest.mock import create_autospec, patch

import pytest

from proto import menu_pb2
from services.operator_pad import controller as controller_module
from services.operator_pad.actions import Actions
from services.operator_pad.config import DEFAULT_KEYMAP, PadConfig, Timings, parse_keymap
from services.operator_pad.controller import PadController
from services.operator_pad.cues import CuePlayer
from services.operator_pad.gestures import HOLD, PRESS, RELEASE, REPEAT, Gesture
from services.operator_pad.state import MenuStatusReader

KEYMAP = parse_keymap(DEFAULT_KEYMAP)
F13, F19, F16 = 183, 189, 186  # K1, K4, knob turn A
UNKNOWN = 115  # KEY_VOLUMEUP of a stock encoder


@pytest.fixture
def parts():
    config = create_autospec(PadConfig, instance=True)
    config.enabled.return_value = True
    config.timings.return_value = Timings()
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
    pad, _, reader, actions, _ = parts
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse()
    await pad.on_code(F19, PRESS, 0)
    pad.detached()
    actions.cancel_pending.assert_called_once()
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
