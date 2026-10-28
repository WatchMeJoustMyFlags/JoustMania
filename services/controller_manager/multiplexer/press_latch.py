"""Button presses seen between two polls (#2312, UC-010 BR-012).

The HID stream thread stores only the latest sample per controller, and the
100 Hz state loop finds button edges between two polls. A tap whose press and
release both arrive between two polls left no edge, so the menu never saw it.
The stream thread records every button it sees down (``latch_presses``) and
``poll()`` reports those buttons as pressed once (``merge_latched_presses``);
the next poll shows the real state, which gives the release edge.

Both helpers run under the adapter's ``_data_lock``.
"""

from __future__ import annotations

from lib.controller_constants import BUTTON_TRACKING_TO_STATE

PRESS_KEYS = tuple(BUTTON_TRACKING_TO_STATE.values())


def latch_presses(latch: dict[str, set[str]], serial: str, state: dict) -> None:
    """Remember every button that is down in ``state`` until the next poll of ``serial``."""
    for key in PRESS_KEYS:
        if state.get(key):
            latch.setdefault(serial, set()).add(key)


def merge_latched_presses(latch: dict[str, set[str]], serial: str, state: dict | None) -> dict | None:
    """``state`` with every button latched since the last poll set pressed; clears the latch.

    Returns ``state`` itself when nothing has to change, otherwise a copy, so the
    cached last state keeps the real sample and the next poll shows the release.
    """
    pressed = latch.pop(serial, None)
    if not pressed or state is None:
        return state
    missing = [key for key in pressed if not state.get(key)]
    if not missing:
        return state
    merged = dict(state)
    for key in missing:
        merged[key] = True
    return merged
