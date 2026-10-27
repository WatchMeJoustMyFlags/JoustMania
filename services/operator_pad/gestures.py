"""Gesture state machine of the operator pad (UC-015 BR-007, BR-011, A4, A6).

Pure: no I/O, no timers, no clock. Every event carries its own timestamp (ms) and the caller
passes the timings in force, so a test drives it with fake events and an injected clock.

Rules:
- tap or hold is decided at release from release time minus press time only: up to ``tap_ms``
  a tap, from the key's hold time a hold, in between a cancel (a key with no hold action: any
  release after ``tap_ms`` cancels);
- K1, K2, K3, K5 and K7 are layer keys: a knob turn while one of them is the only key down
  counts toward that layer and marks the key used, so its release commits the layer
  (``layer``) instead of a tap or hold;
- the knob turns are raw events (a smooth knob sends about 25 per turn): ``knob_events_per_step``
  events in one direction make one step, per layer key or, with no layer key, for the volume; a
  change of direction restarts the count, and the remainder is dropped on the layer key's release,
  after a pause of ``KNOB_PAUSE_MS`` in turning (the volume commit pause) and on ``reset()``;
- each completed layer step also emits ``layer_step`` with the running net total, the number the
  release would commit, for the live preview (BR-017); it never commits anything itself;
- the knob press is bound when it goes down: to the held layer key (marks it used, never a
  mute), or alone to a mute (``knob_press``, fired at press); its release is swallowed;
- a chord (a key goes down while another key, or the knob press, is down) blocks every key
  until all keys are up;
- K8 held for its hold time toggles the lock; while locked only the K3 hold (the stop
  direction, decided by the caller) and the K8 unlock produce a gesture;
- auto-repeat (value 2) and a release without a press are ignored; a second press of a key
  already down (its release was lost) re-arms that key: its timing restarts and its layer
  turns are dropped; ``reset()`` (device gone, events dropped) drops every gesture in progress.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.operator_pad.config import (
    K1,
    K2,
    K3,
    K5,
    K7,
    K8,
    KEYS,
    KNOB_PRESS,
    KNOB_TURN_A,
    KNOB_TURN_B,
    Timings,
)

LAYER_KEYS = frozenset({K1, K2, K3, K5, K7})
KNOB = "KNOB"

PRESS, RELEASE, REPEAT = 1, 0, 2

KNOB_PAUSE_MS = 250  # BR-005: a pause this long in turning drops the volume remainder

TAP = "tap"
HOLD = "hold"
CANCEL = "cancel"
LAYER = "layer"
LAYER_STEP = "layer_step"  # BR-017: a completed step of a held layer key (preview only, never a write)
KNOB_TURN = "knob_turn"
KNOB_PRESS_GESTURE = "knob_press"
LOCK = "lock"


@dataclass(frozen=True)
class KeyEvent:
    """One key event already mapped to a pad key name (``K1`` .. ``K8``, ``KNOB_*``)."""

    name: str
    value: int  # 1 press, 0 release, 2 auto-repeat
    t_ms: int


@dataclass(frozen=True)
class Gesture:
    kind: str
    key: str
    steps: int = 0  # knob_turn: +1 / -1; layer / layer_step: net turns while the layer key was down
    held_ms: int = 0
    locked: bool = False  # hold: the pad was locked (only the stop direction may act); lock: new state


class GestureMachine:
    def __init__(self) -> None:
        self.locked = False
        self._down: dict[str, int] = {}
        self._used: set[str] = set()
        self._layer_steps: dict[str, int] = {}
        self._layer_events: dict[str, tuple[int, int]] = {}  # layer key -> (direction, raw events counted)
        self._volume_events: tuple[int, int] = (0, 0)
        self._last_volume_turn_ms: int | None = None
        self._chord = False
        self._knob_down = False

    def reset(self) -> None:
        """Device gone (UC-015 A4 step 2): every gesture in progress is dropped; the lock stays."""
        self._down.clear()
        self._used.clear()
        self._layer_steps.clear()
        self._layer_events.clear()
        self._volume_events = (0, 0)
        self._last_volume_turn_ms = None
        self._chord = False
        self._knob_down = False

    def keys_down(self) -> bool:
        return bool(self._down) or self._knob_down

    def preview_key(self) -> str | None:
        """The layer key whose live preview may run (BR-017): the only key down, used, no chord, unlocked."""
        if self._chord or self.locked:
            return None
        key = self._single_layer_key()
        return key if key is not None and key in self._used else None

    def feed(self, event: KeyEvent, timings: Timings) -> list[Gesture]:
        if event.value == REPEAT:
            return []
        if event.name in KEYS:
            if event.value == PRESS:
                return self._key_press(event)
            return self._key_release(event, timings)
        if event.name in (KNOB_TURN_A, KNOB_TURN_B):
            if event.value != PRESS:
                return []  # one detent = one press; its release carries nothing
            return self._knob_turn(-1 if event.name == KNOB_TURN_A else 1, event.t_ms, timings)
        if event.name == KNOB_PRESS:
            if event.value == PRESS:
                return self._knob_press()
            return self._knob_release()
        return []

    # --- keys ---------------------------------------------------------------------------

    def _key_press(self, event: KeyEvent) -> list[Gesture]:
        if event.name in self._down:
            # Its release was lost (BR-007): re-arm the key, so a later tap is not timed from the old press.
            del self._down[event.name]
            self._used.discard(event.name)
            self._layer_steps.pop(event.name, None)
            self._layer_events.pop(event.name, None)
        if self.keys_down():
            self._chord = True
        self._down[event.name] = event.t_ms
        return []

    def _key_release(self, event: KeyEvent, timings: Timings) -> list[Gesture]:
        key = event.name
        pressed_at = self._down.pop(key, None)
        if pressed_at is None:
            return []
        used = key in self._used
        self._used.discard(key)
        steps = self._layer_steps.pop(key, 0)
        self._layer_events.pop(key, None)  # the remainder of a step is dropped (BR-005)
        if self._chord:
            self._clear_chord_when_idle()
            return []
        held = max(0, event.t_ms - pressed_at)
        if used:
            if self.locked:
                return []
            return [Gesture(LAYER, key, steps=steps, held_ms=held)]
        hold_ms = timings.hold_ms.get(key)
        is_hold = hold_ms is not None and held >= hold_ms
        if self.locked:
            if is_hold and key == K8:
                self.locked = False
                return [Gesture(LOCK, key, held_ms=held, locked=False)]
            if is_hold and key == K3:
                return [Gesture(HOLD, key, held_ms=held, locked=True)]
            return []
        if is_hold:
            if key == K8:
                self.locked = True
                return [Gesture(LOCK, key, held_ms=held, locked=True)]
            return [Gesture(HOLD, key, held_ms=held)]
        if held <= timings.tap_ms:
            return [Gesture(TAP, key, held_ms=held)]
        return [Gesture(CANCEL, key, held_ms=held)]

    # --- knob ---------------------------------------------------------------------------

    def _single_layer_key(self) -> str | None:
        """The layer key that is the only key down, or None."""
        if len(self._down) != 1:
            return None
        (key,) = self._down
        return key if key in LAYER_KEYS else None

    @staticmethod
    def _count(events: tuple[int, int], direction: int, per_step: int) -> tuple[tuple[int, int], int]:
        """One raw event: (new accumulator, steps emitted 0 or +-1); a direction change restarts the count."""
        last_direction, count = events
        count = count + 1 if direction == last_direction else 1
        if count >= per_step:
            return (direction, 0), direction
        return (direction, count), 0

    def _knob_turn(self, direction: int, t_ms: int, timings: Timings) -> list[Gesture]:
        if self._chord or self.locked:
            return []
        layer = self._single_layer_key()
        if layer is not None:
            self._volume_events = (0, 0)  # a layer interaction ends any volume turn in progress
            self._used.add(layer)
            events, step = self._count(self._layer_events.get(layer, (0, 0)), direction, timings.knob_events_per_step)
            self._layer_events[layer] = events
            self._layer_steps[layer] = self._layer_steps.get(layer, 0) + step
            if step:
                return [Gesture(LAYER_STEP, layer, steps=self._layer_steps[layer])]
            return []
        if self._down:
            return []  # a turn while K4, K6 or K8 is down does nothing
        previous_ms, self._last_volume_turn_ms = self._last_volume_turn_ms, t_ms
        if previous_ms is None or t_ms - previous_ms >= KNOB_PAUSE_MS:
            self._volume_events = (0, 0)  # the volume commit pause: a half step never writes
        self._volume_events, step = self._count(self._volume_events, direction, timings.knob_events_per_step)
        return [Gesture(KNOB_TURN, KNOB, steps=step)] if step else []

    def _knob_press(self) -> list[Gesture]:
        if self._knob_down:
            return []
        if self._chord or self.locked:
            self._knob_down = True
            return []
        layer = self._single_layer_key()
        if layer is not None:
            # Bound to the layer: never a mute, and the layer key's release taps nothing.
            self._knob_down = True
            self._used.add(layer)
            return []
        if self._down:
            # The knob pressed while a non-layer key is down: a chord.
            self._knob_down = True
            self._chord = True
            return []
        self._knob_down = True
        self._volume_events = (0, 0)  # the mute wins over a turn not yet a step
        return [Gesture(KNOB_PRESS_GESTURE, KNOB)]

    def _knob_release(self) -> list[Gesture]:
        if not self._knob_down:
            return []
        self._knob_down = False
        self._clear_chord_when_idle()
        return []

    def _clear_chord_when_idle(self) -> None:
        if not self.keys_down():
            self._chord = False
