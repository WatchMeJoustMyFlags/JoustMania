"""The admin option selection helper survives a dropped Move press (#1845).

No stack: the press and the LED are fakes. The menu debounces admin buttons on
processing time, so a stalled loop can drop one of the three Move presses that
reach agent_control; the fixed-count version then edits another option.
"""

import pytest

from tests.integration.helpers import select_admin_option

pytestmark = pytest.mark.tier("service")

OPTION_COLORS = [(0, 0, 255), (0, 255, 200), (255, 128, 0), (96, 0, 255)]  # sensitivity .. agent_control
WHITE = (255, 255, 255)


class FakeMenu:
    """Option index advances per processed Move; presses in ``drop`` are debounced away."""

    def __init__(self, drop=()):
        self.index = 0
        self.color = WHITE
        self.presses = 0
        self._drop = set(drop)

    async def press_move(self):
        self.presses += 1
        if self.presses in self._drop:
            return
        self.index = (self.index + 1) % len(OPTION_COLORS)
        self.color = OPTION_COLORS[self.index]

    async def read_color(self):
        return self.color


@pytest.mark.asyncio
@pytest.mark.uc("UC-013", flow="main", steps=[1], tc="TC-006")
@pytest.mark.parametrize("drop", [(), (2,), (1, 3)], ids=["none_dropped", "second_dropped", "first_and_third_dropped"])
async def test_select_admin_option_reaches_agent_control_despite_dropped_presses(drop):
    menu = FakeMenu(drop=drop)
    await select_admin_option(menu.press_move, menu.read_color, OPTION_COLORS[3], settle_seconds=0.2)
    assert menu.index == 3, f"selected index {menu.index}, expected agent_control (3) after {menu.presses} presses"


@pytest.mark.asyncio
async def test_select_admin_option_fails_when_the_colour_never_shows():
    menu = FakeMenu(drop=range(1, 20))
    with pytest.raises(AssertionError, match="never showed"):
        await select_admin_option(menu.press_move, menu.read_color, OPTION_COLORS[3], max_presses=3, settle_seconds=0.2)
