"""
Shield charge and activation swell on the controller manager (#1837, UC-040 BR-021).

SHIELD_CHARGE is a persistent LED layer (the lowest shield layer) that breathes the
player's colour; SHIELD_SWELL is a rumble that shares the rumble slot with every other
rumble. Timings run on a FakeClock.
"""

import asyncio
import contextlib

import pytest

from lib.clock import FakeClock
from proto import controller_manager_pb2
from services.controller_manager import feedback_manager as fm_module
from services.controller_manager.effects_base import (
    CHARGE_MAX_DURATION_MS,
    charge_frame,
    swell_intensity,
)
from services.controller_manager.feedback_manager import FeedbackManager

WHITE = (255, 255, 255)
PLAYER = (0, 255, 0)
TEAM_RED = (255, 0, 0)
SERIAL = "MOCK0001"
CHARGE = controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE
SWELL = controller_manager_pb2.GAME_EFFECT_SHIELD_SWELL
SHIELD = controller_manager_pb2.GAME_EFFECT_SHIELD
BOOST = controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST


class _Backend:
    def __init__(self, clock):
        self._clock = clock
        self.colors: dict[str, tuple[int, int, int]] = {}
        self.history: list[tuple[int, int, int]] = []
        self.rumble: list[tuple[float, int]] = []  # (clock time, intensity)
        self.effect_active: dict[str, bool] = {}

    async def set_led_color(self, serial, r, g, b):
        self.colors[serial] = (r, g, b)
        self.history.append((r, g, b))
        return True

    async def set_rumble(self, serial, intensity):
        self.rumble.append((self._clock.time(), intensity))
        return True

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active


@pytest.fixture
def clock():
    return FakeClock(start_time=1000.0)


@pytest.fixture
def backend(clock):
    return _Backend(clock)


@pytest.fixture
def manager(backend, clock):
    fm = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {"name": "P1"}})
    fm._clock = clock
    fm.base_colors[SERIAL] = PLAYER
    return fm


async def _spin(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def _effect(manager, effect, duration_ms=0):
    assert await manager.handle_game_effect(serial=SERIAL, effect=effect, duration_ms=duration_ms)
    await asyncio.sleep(0)


async def _advance(clock, seconds, step=0.05):
    """Run a blocked renderer forward ``seconds`` of fake time."""
    for _ in range(round(seconds / step)):
        clock.release_all()
        await _spin(4)


async def _stop(manager, clock):
    clock.disable_blocking()
    await manager.end_shield_effect(SERIAL, restore=False)
    task = manager.vibration_tasks.pop(SERIAL, None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestUC040ChargeSchedule:
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    def test_breathing_is_60_to_100_percent_at_half_a_hertz(self):
        """UC-040 BR-021: 60 % at 0 s, 80 % at 0.5 s, 100 % at 1 s, back to 60 % at 2 s."""
        assert charge_frame(0.0, WHITE) == (153, 153, 153)
        assert charge_frame(0.5, WHITE) == (204, 204, 204)
        assert charge_frame(1.0, WHITE) == WHITE
        assert charge_frame(2.0, WHITE) == (153, 153, 153)
        for t in range(0, 80):
            assert 153 <= charge_frame(t / 20, WHITE)[0] <= 255

    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    def test_swell_rises_to_180_and_back_over_600_ms(self):
        """UC-040 BR-021: 0 -> 180 (at 300 ms) -> 0 (at 600 ms)."""
        assert swell_intensity(0.0) == 0
        assert 179 <= swell_intensity(0.3) <= 180
        assert swell_intensity(0.6) == 0
        assert swell_intensity(1.0) == 0
        samples = [swell_intensity(t / 20) for t in range(0, 12)]
        assert max(samples) == 180
        assert samples[:7] == sorted(samples[:7])  # rising to the peak
        assert samples[6:] == sorted(samples[6:], reverse=True)  # falling after it


class TestUC040ChargeLayer:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_charge_breathes_the_players_colour_and_ticks_once(self, manager, backend, clock):
        """UC-040 A30: first frame 60 % of the base colour, then a rising breath; one 120 tick."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _advance(clock, 1.0)

        assert backend.history[0] == (0, 153, 0)
        assert backend.colors[SERIAL] == (0, 255, 0) or max(c[1] for c in backend.history) >= 250
        assert [i for _, i in backend.rumble][:1] == [fm_module.CHARGE_TICK_INTENSITY]
        assert sum(1 for _, i in backend.rumble if i == fm_module.CHARGE_TICK_INTENSITY) == 1
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_charge_renders_at_most_10_hz_and_only_on_change(self, manager, backend, clock):
        """UC-040 BR-021: battery: no 20 Hz writes, no repeated identical frame."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _advance(clock, 2.0, step=0.1)

        assert len(backend.history) <= 21  # 10 Hz over 2 s (+ first frame)
        assert all(a != b for a, b in zip(backend.history, backend.history[1:], strict=False))
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_charge_follows_a_base_colour_change(self, manager, backend, clock):
        """UC-040 BR-021: the layer uses the player's CURRENT base colour."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _advance(clock, 0.5)
        manager.base_colors[SERIAL] = TEAM_RED
        backend.history.clear()
        await _advance(clock, 0.5)

        assert backend.history and all(c[1] == 0 and c[0] > 0 for c in backend.history)
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_resend_extends_without_a_second_tick_or_phase_jump(self, manager, backend, clock):
        """UC-040 BR-021: the coordinator re-sends while the charge is held; same layer, one tick."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 20_000)
        await _advance(clock, 1.0)
        layer = manager._shield_layers[SERIAL]["charge"]
        start = layer.start

        await _effect(manager, CHARGE, 20_000)

        assert manager._shield_layers[SERIAL]["charge"] is layer
        assert layer.start == start  # breathing phase continues
        assert layer.total_s == pytest.approx(clock.time() - start + 20.0)
        assert sum(1 for _, i in backend.rumble if i == fm_module.CHARGE_TICK_INTENSITY) == 1
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_duration_is_capped_at_120_seconds(self, manager, clock):
        """UC-040 BR-021: a longer duration_ms is capped (the game re-sends)."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 600_000)

        assert manager._shield_layers[SERIAL]["charge"].total_s == CHARGE_MAX_DURATION_MS / 1000.0
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_duration_bound_ends_the_layer_and_restores_the_base_colour(self, manager, backend, clock):
        """UC-040 A32: a charge nobody ends stops by its own bound and restores the base colour."""
        await _effect(manager, CHARGE, 1000)
        task = manager._shield_tasks[SERIAL]
        await task

        assert not manager.shield_active(SERIAL)
        assert backend.colors[SERIAL] == PLAYER
        assert backend.effect_active[SERIAL] is False


class TestUC040ChargeLayering:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_full_shield_hides_the_charge_and_the_charge_resumes_after_it(self, manager, backend, clock):
        """UC-040 BR-021: priority full > toughness boost > charge; the charge is not lost under a shield."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _advance(clock, 0.5)
        await _effect(manager, SHIELD, 2000)
        backend.history.clear()
        await _advance(clock, 1.0)
        assert backend.history and all(c[0] == c[1] == c[2] for c in backend.history)  # white, no green

        await _advance(clock, 2.0)  # the shield is over
        assert manager.shield_active(SERIAL)
        assert "charge" in manager._shield_layers[SERIAL]
        backend.history.clear()
        await _advance(clock, 1.0)
        assert backend.history and all(c[0] == 0 and c[1] >= 153 for c in backend.history)
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_charge_zero_ends_only_the_charge_layer(self, manager, clock):
        """UC-040 BR-021: SHIELD_CHARGE 0 keeps a toughness boost painting."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _effect(manager, BOOST, 60_000)

        await _effect(manager, CHARGE, 0)

        assert set(manager._shield_layers[SERIAL]) == {"toughness_boost"}
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_charge_zero_under_a_full_shield_leaves_no_breathing_afterwards(self, manager, backend, clock):
        """UC-040 BR-021: the full shield ends and the base colour is shown, not a breath."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _effect(manager, SHIELD, 2000)
        await _effect(manager, CHARGE, 0)
        task = manager._shield_tasks[SERIAL]
        clock.disable_blocking()
        await task

        assert not manager.shield_active(SERIAL)
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-021"])
    async def test_shield_zero_ends_every_layer(self, manager, clock):
        """UC-040 A17: SHIELD 0 (game end) ends full, toughness boost and charge."""
        clock.enable_blocking()
        for effect in (CHARGE, BOOST, SHIELD):
            await _effect(manager, effect, 60_000)

        await _effect(manager, SHIELD, 0)

        assert not manager.shield_active(SERIAL)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_death_and_clear_controller_end_the_charge(self, manager, clock, monkeypatch):
        """UC-040 A32: the death effect and a disconnect (clear_controller) end the charge."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 20)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _effect(manager, controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert not manager.shield_active(SERIAL)
        clock.disable_blocking()

        await _effect(manager, CHARGE, 60_000)
        manager.clear_controller(SERIAL)
        assert not manager.shield_active(SERIAL)
        assert SERIAL not in manager._shield_tasks


class TestUC040SwellRumble:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_swell_rises_to_180_at_300_ms_and_ends_at_zero_at_600_ms(self, manager, backend, clock):
        """UC-040 BR-021: sampled every 50 ms on the clock, last write 0 at 600 ms, no LED touched."""
        await _effect(manager, SWELL)
        task = manager.vibration_tasks[SERIAL]
        await task

        offsets = [(round(t - 1000.0, 2), i) for t, i in backend.rumble]
        at = dict(offsets)
        assert 179 <= at[0.3] <= 180
        assert max(i for _, i in offsets) <= 180
        assert offsets[-1] == (0.6, 0)
        assert backend.history == []  # rumble only
        assert not manager.shield_active(SERIAL)
        assert SERIAL not in manager.vibration_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_swell_does_not_end_or_hide_a_shield_layer(self, manager, backend, clock):
        """UC-040 BR-021: the swell (LED-less) leaves the white shield running."""
        clock.enable_blocking()
        await _effect(manager, SHIELD, 60_000)
        task = manager._shield_tasks[SERIAL]

        await _effect(manager, SWELL)

        assert manager._shield_tasks[SERIAL] is task and not task.done()
        assert SERIAL not in manager.active_effects
        await _stop(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_death_cancels_the_swell_and_its_rumble_runs_in_full(self, manager, backend, clock, monkeypatch):
        """UC-040 BR-021 / #1836: a death during a swell holds 255 for its whole duration, no swell step after."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 300)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        clock.enable_blocking()
        await _effect(manager, SWELL)
        swell = manager.vibration_tasks[SERIAL]
        await _advance(clock, 0.2)
        assert any(0 < i < 255 for _, i in backend.rumble)  # the swell is stepping
        mark = len(backend.rumble)

        await _effect(manager, controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        clock.disable_blocking()  # a surviving swell would now run on
        await _spin(40)
        await asyncio.sleep(0.1)  # still inside the 300 ms death rumble

        after = [i for _, i in backend.rumble[mark:]]
        assert swell.done()
        assert after[0] == 255
        assert set(after) == {255}  # no swell step, no early stop
        await asyncio.sleep(0.35)
        assert backend.rumble[-1][1] == 0  # the death rumble's own stop

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_a_later_rumble_replaces_a_running_swell(self, manager, backend, clock):
        """UC-040 BR-021: a warning rumble cancels the swell (shared rumble slot)."""
        clock.enable_blocking()
        await _effect(manager, SWELL)
        swell = manager.vibration_tasks[SERIAL]
        await _advance(clock, 0.1)

        await manager.set_vibration(SERIAL, 100, 60)
        clock.disable_blocking()
        await _spin(20)
        mark = len(backend.rumble)
        await _advance(clock, 0.5)

        assert swell.done()
        assert backend.rumble[mark - 1][1] == 100
        await asyncio.sleep(0.15)
        assert backend.rumble[-1][1] == 0
        assert all(i in (100, 0) for _, i in backend.rumble[mark - 1 :])

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_warning_during_the_start_tick_does_not_leave_rumble_on(self, manager, backend, clock):
        """UC-040 BR-021: tick (120, 100 ms) then a warning (100, 200 ms): the rumble ends at 0."""
        await _effect(manager, CHARGE, 60_000)
        await manager.set_vibration(SERIAL, 100, 200)

        await asyncio.sleep(0.35)

        assert backend.rumble[-1][1] == 0
        await manager.end_shield_effect(SERIAL, restore=False)


class TestUC040DeathRumbleIsNeverShortened:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_swell_and_charge_tick_skip_a_running_death_rumble(self, manager, backend, clock, monkeypatch):
        """UC-040 BR-021: while the death rumble runs, a swell or a new charge tick leaves its timer alone."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 300)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        await _effect(manager, controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        death_stop = manager.vibration_tasks[SERIAL]
        mark = len(backend.rumble)

        await _effect(manager, SWELL)
        await _effect(manager, CHARGE, 60_000)
        await _spin(40)

        assert manager.vibration_tasks[SERIAL] is death_stop and not death_stop.done()
        assert len(backend.rumble) == mark  # no swell step, no 120 tick
        await asyncio.sleep(0.35)
        assert backend.rumble[-1][1] == 0  # the death rumble ran its full length
        await manager.end_shield_effect(SERIAL, restore=False)
