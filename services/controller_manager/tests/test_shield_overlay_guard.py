"""
The `_overlay_active` guard of the shield layer (#2354, UC-040 BR-010).

While a real effect (a PULSE, a warning flash) owns the LED, the shield renderer must not paint,
must not restore the base colour when it expires, and must not restore it when it is ended: the
overlay's own finish hands the LED back.
"""

import asyncio
import contextlib

import pytest

from lib.clock import FakeClock
from proto import controller_manager_pb2
from services.controller_manager.effects_base import ActiveEffect
from services.controller_manager.feedback_manager import FeedbackManager

PLAYER = (0, 255, 0)
SERIAL = "MOCK0001"


class _Backend:
    def __init__(self):
        self.history: list[tuple[int, int, int]] = []
        self.effect_active: dict[str, bool] = {}

    async def set_led_color(self, serial, r, g, b):
        self.history.append((r, g, b))
        return True

    async def set_rumble(self, serial, intensity):
        return True

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active


@pytest.fixture
def backend():
    return _Backend()


@pytest.fixture
def manager(backend):
    fm = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {"name": "P1"}})
    fm._clock = FakeClock(start_time=1000.0)
    fm.base_colors[SERIAL] = PLAYER
    return fm


async def _spin(n):
    for _ in range(n):
        await asyncio.sleep(0)


async def _start_shield(manager, duration_ms):
    assert await manager.handle_game_effect(
        serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=duration_ms
    )
    await _spin(3)


@contextlib.asynccontextmanager
async def _overlay(manager):
    """A running effect task registered as the LED owner; yields the event that finishes it."""
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    manager.active_effects[SERIAL] = ActiveEffect(task=task, effect_type=controller_manager_pb2.GAME_EFFECT_PULSE)
    try:
        yield release
    finally:
        release.set()
        await task


class TestUC040ShieldOverlayGuard:
    """UC-040 BR-010: a running overlay effect owns the LED; the shield layer waits under it."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_renderer_does_not_paint_over_a_running_overlay(self, manager, backend):
        await _start_shield(manager, duration_ms=10_000)
        async with _overlay(manager) as release:
            backend.history.clear()
            await _spin(30)
            assert backend.history == []

            release.set()
            await _spin(30)
        assert backend.history, "the shield paints again once the overlay is done"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_expiring_shield_does_not_restore_under_a_running_overlay(self, manager, backend):
        await _start_shield(manager, duration_ms=1000)
        async with _overlay(manager):
            backend.history.clear()
            await _spin(200)  # 1 s of 20 Hz ticks: the shield runs out

            assert not manager.shield_active(SERIAL)
            assert backend.history == []  # no frame and no base-colour restore over the overlay
            assert backend.effect_active[SERIAL] is True

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_ending_the_shield_does_not_restore_under_a_running_overlay(self, manager, backend):
        await _start_shield(manager, duration_ms=10_000)
        async with _overlay(manager):
            backend.history.clear()

            assert await manager.end_shield_effect(SERIAL) is True

            assert not manager.shield_active(SERIAL)
            assert backend.history == []
            assert backend.effect_active[SERIAL] is True
