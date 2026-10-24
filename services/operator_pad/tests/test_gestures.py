"""Gesture state machine (UC-015 BR-007, BR-011, A4, A6): fake events, injected clock and timings."""

import pytest

from services.operator_pad.config import Timings
from services.operator_pad.gestures import (
    CANCEL,
    HOLD,
    KNOB,
    KNOB_PRESS_GESTURE,
    KNOB_TURN,
    LAYER,
    LOCK,
    PRESS,
    RELEASE,
    REPEAT,
    TAP,
    Gesture,
    GestureMachine,
    KeyEvent,
)

TIMINGS = Timings()  # tap 400 ms; hold K3 1000, K4 1500, K8 3000


class Pad:
    """Drives a GestureMachine with an injected clock (ms)."""

    def __init__(self, timings: Timings = TIMINGS) -> None:
        self.machine = GestureMachine()
        self.timings = timings
        self.now = 0
        self.out: list[Gesture] = []

    def at(self, t_ms: int) -> "Pad":
        self.now = t_ms
        return self

    def ev(self, name: str, value: int) -> list[Gesture]:
        got = self.machine.feed(KeyEvent(name, value, self.now), self.timings)
        self.out.extend(got)
        return got

    def press(self, name: str) -> list[Gesture]:
        return self.ev(name, PRESS)

    def release(self, name: str) -> list[Gesture]:
        return self.ev(name, RELEASE)

    def turn(self, direction: str) -> list[Gesture]:
        name = "KNOB_TURN_B" if direction == "B" else "KNOB_TURN_A"
        got = self.ev(name, PRESS)
        self.ev(name, RELEASE)
        return got

    def kinds(self) -> list[tuple[str, str]]:
        return [(g.kind, g.key) for g in self.out]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007", "BR-011"], steps=[1])
def test_tap_within_tap_limit():
    pad = Pad()
    pad.at(0).press("K1")
    assert pad.at(400).release("K1") == [Gesture(TAP, "K1", held_ms=400)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007", "BR-011"], steps=[6, 7])
@pytest.mark.parametrize(("key", "hold_ms"), [("K3", 1000), ("K4", 1500)])
def test_hold_from_the_keys_hold_time(key, hold_ms):
    pad = Pad()
    pad.at(0).press(key)
    assert pad.at(hold_ms).release(key) == [Gesture(HOLD, key, held_ms=hold_ms)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
@pytest.mark.parametrize(("key", "release_at"), [("K3", 401), ("K3", 999), ("K4", 1499), ("K1", 401), ("K6", 2000)])
def test_release_between_tap_limit_and_hold_time_cancels(key, release_at):
    pad = Pad()
    pad.at(0).press(key)
    assert pad.at(release_at).release(key) == [Gesture(CANCEL, key, held_ms=release_at)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_timings_come_from_the_caller():
    pad = Pad(Timings(tap_ms=100, hold_ms={"K3": 300, "K4": 500, "K8": 900}))
    pad.at(0).press("K1")
    assert pad.at(150).release("K1")[0].kind == CANCEL
    pad.at(1000).press("K3")
    assert pad.at(1300).release("K3")[0].kind == HOLD


@pytest.mark.uc("UC-015", flow="main", br=["BR-005", "BR-007"], steps=[4, 5])
def test_knob_turn_in_a_layer_marks_the_key_used_and_commits_on_release():
    pad = Pad()
    pad.at(0).press("K2")
    assert pad.turn("B") == []
    pad.turn("B")
    pad.turn("A")
    assert pad.at(100).release("K2") == [Gesture(LAYER, "K2", steps=1, held_ms=100)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"], steps=[4])
def test_k1_with_a_turn_does_not_start_a_game():
    pad = Pad()
    pad.at(0).press("K1")
    pad.turn("B")
    got = pad.at(200).release("K1")
    assert [g.kind for g in got] == [LAYER]
    assert TAP not in [g.kind for g in pad.out]


@pytest.mark.uc("UC-015", flow="main", br=["BR-005"], steps=[5])
def test_layer_turns_that_net_to_zero_commit_zero_steps():
    pad = Pad()
    pad.at(0).press("K5")
    pad.turn("B")
    pad.turn("A")
    assert pad.at(300).release("K5") == [Gesture(LAYER, "K5", steps=0, held_ms=300)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-006"], steps=[8])
def test_knob_turn_alone_is_a_volume_step():
    pad = Pad()
    assert pad.turn("B") == [Gesture(KNOB_TURN, KNOB, steps=1)]
    assert pad.turn("A") == [Gesture(KNOB_TURN, KNOB, steps=-1)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-006", "BR-007"], steps=[8])
def test_knob_press_alone_fires_mute_on_press_and_swallows_release():
    pad = Pad()
    assert pad.press("KNOB_PRESS") == [Gesture(KNOB_PRESS_GESTURE, KNOB)]
    assert pad.release("KNOB_PRESS") == []


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_knob_press_in_a_layer_is_bound_on_press_even_when_the_layer_key_goes_up_first():
    pad = Pad()
    pad.at(0).press("K3")
    assert pad.at(50).press("KNOB_PRESS") == []
    assert pad.at(100).release("K3") == [Gesture(LAYER, "K3", steps=0, held_ms=100)]
    assert pad.at(150).release("KNOB_PRESS") == []
    assert KNOB_PRESS_GESTURE not in [g.kind for g in pad.out]
    assert TAP not in [g.kind for g in pad.out]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_chord_blocks_every_key_until_all_keys_are_up():
    pad = Pad()
    pad.at(0).press("K2")
    pad.at(10).press("K5")
    pad.turn("B")
    assert pad.press("KNOB_PRESS") == []
    pad.release("KNOB_PRESS")
    assert pad.at(100).release("K2") == []
    pad.at(110).press("K6")  # K5 still down: still a chord
    assert pad.at(120).release("K6") == []
    assert pad.at(1500).release("K5") == []
    assert pad.out == []
    # all keys up: the next tap works again
    pad.at(2000).press("K2")
    assert pad.at(2100).release("K2") == [Gesture(TAP, "K2", held_ms=100)]


@pytest.mark.uc("UC-015", flow="A6", br=["BR-011"])
def test_k8_hold_locks_and_only_the_k3_hold_and_the_unlock_pass():
    pad = Pad()
    pad.at(0).press("K8")
    assert pad.at(3000).release("K8") == [Gesture(LOCK, "K8", held_ms=3000, locked=True)]
    assert pad.machine.locked
    pad.at(4000).press("K1")
    assert pad.at(4100).release("K1") == []
    assert pad.turn("B") == []
    assert pad.press("KNOB_PRESS") == []
    pad.release("KNOB_PRESS")
    pad.at(5000).press("K2")
    pad.turn("B")
    assert pad.at(5100).release("K2") == []
    pad.at(6000).press("K3")
    assert pad.at(6100).release("K3") == []  # tap: nothing while locked
    pad.at(7000).press("K3")
    assert pad.at(8000).release("K3") == [Gesture(HOLD, "K3", held_ms=1000, locked=True)]
    pad.at(9000).press("K4")
    assert pad.at(11000).release("K4") == []
    pad.at(12000).press("K8")
    assert pad.at(15000).release("K8") == [Gesture(LOCK, "K8", held_ms=3000, locked=False)]
    assert not pad.machine.locked


@pytest.mark.uc("UC-015", flow="A6", br=["BR-011"])
def test_k8_tap_is_reserved_and_a_short_hold_does_not_lock():
    pad = Pad()
    pad.at(0).press("K8")
    assert pad.at(100).release("K8") == [Gesture(TAP, "K8", held_ms=100)]
    pad.at(200).press("K8")
    assert pad.at(2000).release("K8")[0].kind == CANCEL
    assert not pad.machine.locked


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_auto_repeat_is_ignored():
    pad = Pad()
    pad.at(0).press("K4")
    for t in range(500, 1500, 33):
        assert pad.at(t).ev("K4", REPEAT) == []
    assert pad.at(1600).release("K4") == [Gesture(HOLD, "K4", held_ms=1600)]
    assert pad.ev("K2", REPEAT) == []


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_release_without_press_and_a_second_press_are_ignored():
    pad = Pad()
    assert pad.release("K1") == []
    assert pad.release("KNOB_PRESS") == []
    pad.at(0).press("K3")
    pad.at(900).press("K3")  # duplicate press keeps the first press time
    assert pad.at(1000).release("K3") == [Gesture(HOLD, "K3", held_ms=1000)]


@pytest.mark.uc("UC-015", flow="A4", br=["BR-010"])
def test_disconnect_mid_hold_resets_the_gesture():
    pad = Pad()
    pad.at(0).press("K4")
    pad.at(100).press("K2")  # a chord in progress is dropped too
    pad.machine.reset()
    assert pad.at(2000).release("K4") == []
    assert pad.release("K2") == []
    pad.at(3000).press("K1")
    assert pad.at(3100).release("K1") == [Gesture(TAP, "K1", held_ms=100)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-007"])
def test_knob_turn_while_a_non_layer_key_is_down_does_nothing():
    pad = Pad()
    pad.at(0).press("K4")
    assert pad.turn("B") == []
    assert pad.at(1500).release("K4") == [Gesture(HOLD, "K4", held_ms=1500)]
