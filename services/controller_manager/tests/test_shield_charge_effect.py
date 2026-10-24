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
    ChargeCues,
    charge_frame,
    swell_intensity,
)
from services.controller_manager.feedback_manager import FeedbackManager

_REAL_READ_CHARGE_CUES = fm_module.read_charge_cues  # before the autouse fixture patches it

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


@pytest.fixture(autouse=True)
def no_charge_cues(monkeypatch):
    """No flagd provider; the charge rumble pattern is off (intensity 0) unless a test turns it on.

    The pattern's sleeps move the shared FakeClock, which would distort the LED timing tests.
    """
    monkeypatch.setattr(fm_module, "read_charge_cues", lambda: ChargeCues(intensity=0))


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
    async def test_charge_breathes_the_players_colour(self, manager, backend, clock):
        """UC-040 A30: first frame 60 % of the base colour, then a rising breath."""
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _advance(clock, 1.0)

        assert backend.history[0] == (0, 153, 0)
        assert backend.colors[SERIAL] == (0, 255, 0) or max(c[1] for c in backend.history) >= 250
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
    async def test_resend_extends_without_a_second_grant_pattern_or_phase_jump(
        self, manager, backend, clock, monkeypatch
    ):
        """UC-040 BR-021: the coordinator re-sends while the charge is held; same layer, one pattern."""
        patterns = []
        monkeypatch.setattr(fm_module, "read_charge_cues", lambda: patterns.append(1) or ChargeCues(intensity=0))
        clock.enable_blocking()
        await _effect(manager, CHARGE, 20_000)
        await _advance(clock, 1.0)
        layer = manager._shield_layers[SERIAL]["charge"]
        start = layer.start

        await _effect(manager, CHARGE, 20_000)
        await _spin()

        assert manager._shield_layers[SERIAL]["charge"] is layer
        assert layer.start == start  # breathing phase continues
        assert layer.total_s == pytest.approx(clock.time() - start + 20.0)
        assert patterns == [1]  # the pattern (grant included) started once
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
    async def test_warning_during_the_grant_pattern_does_not_leave_rumble_on(
        self, manager, backend, clock, monkeypatch
    ):
        """UC-040 BR-021: grant pulse (255) then a warning (100, 200 ms): the rumble ends at 0."""
        monkeypatch.setattr(fm_module, "read_charge_cues", lambda: ChargeCues())
        clock.enable_blocking()
        await _effect(manager, CHARGE, 60_000)
        await _spin()
        assert backend.rumble[-1][1] == 255  # inside the first grant pulse
        await manager.set_vibration(SERIAL, 100, 200)
        clock.disable_blocking()

        await asyncio.sleep(0.35)

        assert backend.rumble[-1][1] == 0
        await manager.end_shield_effect(SERIAL, restore=False)


class TestUC040DeathRumbleIsNeverShortened:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_swell_and_charge_grant_skip_a_running_death_rumble(self, manager, backend, clock, monkeypatch):
        """UC-040 BR-021: while the death rumble runs, a swell or a new charge's grant leaves its timer alone."""
        monkeypatch.setattr(
            fm_module, "read_charge_cues", lambda: ChargeCues(reminder_s=0, pre_expiry_s=0)
        )  # grant only
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
        assert len(backend.rumble) == mark  # no swell step, no grant pulse
        await asyncio.sleep(0.35)
        assert backend.rumble[-1][1] == 0  # the death rumble ran its full length
        await manager.end_shield_effect(SERIAL, restore=False)


# ---------------------------------------------------------------------------
# Charge rumble pattern (#2094, UC-040 A30 / A32, BR-021)
# ---------------------------------------------------------------------------


async def _no_render(*_args):
    """Stand-in renderer: it neither paints nor sleeps, so only the cue loop moves the fake clock."""
    await asyncio.Event().wait()


@pytest.fixture
def cue_manager(manager, monkeypatch):
    monkeypatch.setattr(FeedbackManager, "_shield_render_loop", _no_render)
    monkeypatch.setattr(fm_module, "read_charge_cues", lambda: ChargeCues())  # the flag defaults
    return manager


def _on_times(backend, intensity=255):
    """Offsets (s from the charge start at 1000.0) of every charge pulse switched on."""
    return [round(t - 1000.0, 3) for t, i in backend.rumble if i == intensity]


async def _run_cues_to_end(manager):
    task = manager._charge_cue_tasks[SERIAL]
    await asyncio.wait_for(task, timeout=5)


async def _step_until(clock, until):
    """Blocking clock: release sleeps one at a time until the fake time reaches ``until``."""
    for _ in range(10_000):
        await _spin(8)
        if clock.time() >= until - 1e-9:
            return
        clock.release_all()
    raise AssertionError(f"clock stuck at {clock.time()}")


async def _finish(manager, clock):
    clock.disable_blocking()
    await manager.end_shield_effect(SERIAL, restore=False)
    await _spin()


class TestUC040ChargeRumblePattern:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_grant_is_three_full_strength_pulses_of_150_ms_with_100_ms_gaps(self, cue_manager, backend):
        """UC-040 BR-021: on grant 255 on/off at 0/0.15, 0.25/0.40, 0.50/0.65 s; nothing else then."""
        await _effect(cue_manager, CHARGE, 3500)  # shorter than the grant plus the 3 s lead: no pre-expiry
        await _run_cues_to_end(cue_manager)

        offsets = [(round(t - 1000.0, 3), i) for t, i in backend.rumble]
        assert offsets == [(0.0, 255), (0.15, 0), (0.25, 255), (0.4, 0), (0.5, 255), (0.65, 0)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_reminder_pulses_every_5_seconds_while_the_charge_is_held(self, cue_manager, backend):
        """UC-040 BR-021: one 150 ms pulse at 5, 10, 15 s after the grant; then the pre-expiry pulse."""
        await _effect(cue_manager, CHARGE, 20_000)
        await _run_cues_to_end(cue_manager)

        reminders = [t for t in _on_times(backend) if 1.0 <= t < 16.0]
        assert reminders == [5.0, 10.0, 15.0]
        reminder_off = [round(t - 1000.0, 3) for t, i in backend.rumble if i == 0 and 5.0 <= t - 1000.0 < 5.5]
        assert reminder_off == [5.15]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_pre_expiry_double_pulse_3_seconds_before_the_expiry_is_the_last_cue(self, cue_manager, backend):
        """UC-040 BR-021: a 20 s charge pulses twice at 17.00 and 17.25 s and stays quiet until it ends."""
        await _effect(cue_manager, CHARGE, 20_000)
        await _run_cues_to_end(cue_manager)

        assert [t for t in _on_times(backend) if t >= 16.0] == [17.0, 17.25]
        assert backend.rumble[-1] == (pytest.approx(1017.4), 0)
        assert SERIAL not in cue_manager._charge_cue_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_flag_values_shape_the_pattern(self, cue_manager, backend, monkeypatch):
        """UC-040 BR-021: intensity, pulse length, reminder interval and lead come from the flags."""
        cues = ChargeCues(intensity=180, pulse_ms=200, reminder_s=3.0, pre_expiry_s=2.0)
        monkeypatch.setattr(fm_module, "read_charge_cues", lambda: cues)
        await _effect(cue_manager, CHARGE, 10_000)
        await _run_cues_to_end(cue_manager)

        assert _on_times(backend, 180) == [0.0, 0.3, 0.6, 3.0, 6.0, 8.0, 8.3]
        assert {i for _, i in backend.rumble} == {180, 0}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_a_resend_moves_the_pre_expiry_pulse_with_the_layer(self, cue_manager, backend, clock):
        """UC-040 BR-021: a layer re-sent before the lead (expiry never) gives no early double pulse."""
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _step_until(clock, 1010.0)  # the coordinator re-sends 10 s before the layer's end
        await _effect(cue_manager, CHARGE, 20_000)  # layer now ends at 1030
        clock.disable_blocking()
        await _run_cues_to_end(cue_manager)

        on = _on_times(backend)
        assert 17.0 not in on
        assert [t for t in on if t >= 16.0] == [20.0, 25.0, 27.0, 27.25]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_no_reminder_after_activation(self, cue_manager, backend, clock):
        """UC-040 A32 / UC-030 A6: SHIELD_CHARGE 0 stops the pattern at once."""
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _step_until(clock, 1006.0)  # the 5 s reminder has played
        await _effect(cue_manager, CHARGE, 0)  # the activation
        assert SERIAL not in cue_manager._charge_cue_tasks  # stopped at once, not at its next wake
        clock.disable_blocking()
        await _spin(40)

        assert _on_times(backend) == [0.0, 0.25, 0.5, 5.0]
        assert SERIAL not in cue_manager._charge_cue_tasks
        assert backend.rumble[-1][1] == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_activation_mid_pulse_switches_the_motor_off(self, cue_manager, backend, clock):
        """UC-040 A32: a pulse in progress is cut, and the motor is not left on."""
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _spin()
        assert backend.rumble[-1][1] == 255  # inside the first grant pulse

        await _effect(cue_manager, CHARGE, 0)
        clock.disable_blocking()
        await _spin(40)

        assert backend.rumble[-1][1] == 0
        assert _on_times(backend) == [0.0]
        assert SERIAL not in cue_manager.vibration_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_charge_zero_under_a_toughness_boost_stops_the_pattern_mid_pulse(self, cue_manager, backend, clock):
        """UC-040 A32: the charge ends while another shield layer keeps painting: the pulse is cut too."""
        clock.enable_blocking()
        await _effect(cue_manager, BOOST, 60_000)
        await _effect(cue_manager, CHARGE, 20_000)
        await _spin()
        assert backend.rumble[-1][1] == 255  # inside the first grant pulse

        await _effect(cue_manager, CHARGE, 0)

        assert backend.rumble[-1][1] == 0
        assert SERIAL not in cue_manager._charge_cue_tasks
        assert "toughness_boost" in cue_manager._shield_layers[SERIAL]
        clock.disable_blocking()
        await _spin(40)
        assert _on_times(backend) == [0.0]
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_game_end_mid_pulse_switches_the_motor_off(self, cue_manager, backend, clock):
        """UC-040 A32: SHIELD 0 at game end cuts a charge pulse in progress at once."""
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _spin()
        assert backend.rumble[-1][1] == 255

        await _effect(cue_manager, SHIELD, 0)

        assert backend.rumble[-1][1] == 0
        assert SERIAL not in cue_manager._charge_cue_tasks
        clock.disable_blocking()
        await _spin(40)
        assert _on_times(backend) == [0.0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_no_reminder_after_expiry(self, cue_manager, backend, monkeypatch):
        """UC-040 A32: reminders at 5 and 10 s of a 12 s charge, none after its end."""
        monkeypatch.setattr(fm_module, "read_charge_cues", lambda: ChargeCues(pre_expiry_s=0))
        await _effect(cue_manager, CHARGE, 12_000)
        await _run_cues_to_end(cue_manager)

        assert _on_times(backend) == [0.0, 0.25, 0.5, 5.0, 10.0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A32", br=["BR-021"])
    async def test_no_reminder_after_death(self, cue_manager, backend, clock, monkeypatch):
        """UC-040 A32: the death effect ends the charge and its pattern; only the death rumble follows."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 50)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _step_until(clock, 1002.0)
        mark = len(backend.rumble)

        await _effect(cue_manager, controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert SERIAL not in cue_manager._charge_cue_tasks  # stopped at once, not at its next wake
        clock.disable_blocking()
        await asyncio.sleep(0.15)

        assert SERIAL not in cue_manager._charge_cue_tasks
        assert [i for _, i in backend.rumble[mark:]] == [255, 0]  # the death rumble and its stop

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_death_rumble_pre_empts_a_grant_pulse_in_progress(self, cue_manager, backend, clock, monkeypatch):
        """UC-040 BR-021: a death during a grant pulse holds 255 for its full length; no charge pulse after."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 300)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _spin()
        pulse = cue_manager.vibration_tasks[SERIAL]

        await _effect(cue_manager, controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        death_stop = cue_manager.vibration_tasks[SERIAL]
        clock.disable_blocking()
        await _spin(40)
        await asyncio.sleep(0.1)  # still inside the 300 ms death rumble

        assert pulse.done()
        assert death_stop.get_name() == fm_module.DEATH_RUMBLE_TASK_NAME and not death_stop.done()
        assert backend.rumble[-1][1] == 255
        await asyncio.sleep(0.3)
        assert backend.rumble[-1][1] == 0  # the death rumble's own stop, at its full length

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_reminder_skips_a_running_warning_rumble(self, cue_manager, backend, clock):
        """UC-040 BR-021: a reminder due during the warning rumble waits up to 1 s, then is skipped."""
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _step_until(clock, 1005.0)  # the cue loop is about to play the 5 s reminder
        await cue_manager.set_vibration(SERIAL, 100, 5000)  # a warning that outlasts the 1 s wait
        warning = cue_manager.vibration_tasks[SERIAL]
        await _step_until(clock, 1007.0)

        assert [t for t in _on_times(backend) if t >= 1.0] == []
        assert cue_manager.vibration_tasks[SERIAL] is warning and not warning.done()
        warning.cancel()
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_a_rumble_stop_cancels_a_pulse_in_progress(self, cue_manager, backend, clock):
        """UC-040 BR-021: a vibration stop (intensity 0) ends the running pulse; its burst writes nothing more."""
        clock.enable_blocking()
        await _effect(cue_manager, CHARGE, 20_000)
        await _spin()
        pulse = cue_manager.vibration_tasks[SERIAL]

        await cue_manager.set_vibration(SERIAL, 0, 0)
        await _spin()

        assert pulse.done()
        assert backend.rumble[-1][1] == 0
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    async def test_intensity_zero_turns_the_pattern_off(self, cue_manager, backend, monkeypatch):
        """UC-040 BR-021: shield.charge_rumble_intensity 0 means no charge rumble at all."""
        monkeypatch.setattr(fm_module, "read_charge_cues", lambda: ChargeCues(intensity=0))
        await _effect(cue_manager, CHARGE, 20_000)
        await _spin(40)

        assert backend.rumble == []
        await cue_manager.end_shield_effect(SERIAL, restore=False)


class TestUC040ChargeRumbleFlags:
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    def test_read_charge_cues_reads_the_four_game_flags(self, monkeypatch):
        """flags.md §4: the four game flags are read per charge, without a gameId."""
        from lib import feature_flags

        values = {
            "shield.charge_rumble_intensity": 200,
            "shield.charge_rumble_pulse_ms": 250,
            "shield.charge_reminder_seconds": 10.0,
            "shield.charge_pre_expiry_seconds": 5.0,
        }
        reads = []

        def fake_read(domain, key, _default, _game_id=None):
            reads.append((domain, key))
            return values[key]

        monkeypatch.setattr(feature_flags, "read_int_flag", fake_read)
        monkeypatch.setattr(feature_flags, "read_float_flag", fake_read)

        cues = _REAL_READ_CHARGE_CUES()

        assert cues == ChargeCues(intensity=200, pulse_ms=250, reminder_s=10.0, pre_expiry_s=5.0)
        assert sorted(reads) == sorted(("game", key) for key in values)

    @pytest.mark.uc("UC-040", flow="A30", br=["BR-021"])
    def test_bad_flag_values_give_the_defaults_and_are_clamped(self):
        from services.controller_manager.effects_base import resolve_charge_cues

        assert resolve_charge_cues(None, "x", float("nan"), -1) == ChargeCues()
        assert resolve_charge_cues(999, 10, 100.0, 20.0) == ChargeCues(
            intensity=255, pulse_ms=50, reminder_s=30.0, pre_expiry_s=8.0
        )
        assert resolve_charge_cues(0, 150, 0, 0) == ChargeCues(intensity=0, reminder_s=0.0, pre_expiry_s=0.0)
        assert resolve_charge_cues(True, 150, 0.2, 3) == ChargeCues(reminder_s=1.0)


# ---------------------------------------------------------------------------
# Shield ending soon (#2094, UC-040 A16 / A17, BR-021)
# ---------------------------------------------------------------------------

_REAL_READ_SHIELD_ENDING_LEAD = fm_module.read_shield_ending_lead


@pytest.fixture
def ending_manager(cue_manager, monkeypatch):
    monkeypatch.setattr(fm_module, "read_shield_ending_lead", lambda: 1.0)  # the flag default
    monkeypatch.setattr(fm_module, "read_charge_cues", lambda: ChargeCues(intensity=0))
    return cue_manager


async def _run_ending_to_end(manager):
    task = manager._shield_ending_tasks.get(SERIAL)
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


class TestUC040ShieldEndingPulse:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_instant_shield_double_pulses_1_second_before_it_ends(self, ending_manager, backend):
        """UC-040 A16 / BR-021: a 5 s shield pulses 150 ms at 4.00 and 4.25 s, nothing else."""
        await _effect(ending_manager, SHIELD, 5000)
        await _run_ending_to_end(ending_manager)

        offsets = [(round(t - 1000.0, 3), i) for t, i in backend.rumble]
        assert offsets == [(4.0, 255), (4.15, 0), (4.25, 255), (4.4, 0)]
        assert SERIAL not in ending_manager._shield_ending_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_activated_charge_shield_gets_the_ending_pulse(self, ending_manager, backend):
        """UC-030 A6 / UC-040 BR-021: SHIELD_CHARGE 0 then SHIELD 5 s: the pulse comes 1 s before its end."""
        await _effect(ending_manager, CHARGE, 20_000)
        await _effect(ending_manager, CHARGE, 0)
        await _effect(ending_manager, SHIELD, 5000)
        await _run_ending_to_end(ending_manager)

        assert _on_times(backend) == [4.0, 4.25]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_a_restart_moves_the_pulse_to_the_new_end(self, ending_manager, backend, clock):
        """UC-040 BR-021: a re-grant at 4 s of a new 5 s shield pulses at 8 s, not at 4 s."""
        clock.enable_blocking()
        await _effect(ending_manager, SHIELD, 5000)
        await _spin()
        assert clock.time() == pytest.approx(1004.0)  # asleep until the old pulse time
        await _effect(ending_manager, SHIELD, 5000)  # restarts at 1004, ends at 1009
        clock.disable_blocking()
        await _run_ending_to_end(ending_manager)

        assert _on_times(backend) == [8.0, 8.25]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_short_shield_and_toughness_boost_get_no_pulse(self, ending_manager, backend):
        """UC-040 BR-021: a shield no longer than the lead, and a toughness boost, give no pulse."""
        await _effect(ending_manager, SHIELD, 1000)
        await _run_ending_to_end(ending_manager)
        await ending_manager.end_shield_effect(SERIAL, restore=False)
        await _effect(ending_manager, BOOST, 5000)
        await _spin(40)

        assert backend.rumble == []
        assert SERIAL not in ending_manager._shield_ending_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-021"])
    async def test_no_pulse_after_death(self, ending_manager, backend, clock, monkeypatch):
        """UC-040 A17: a death during the shield ends it and its pending pulse; only the death rumble follows."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 50)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        clock.enable_blocking()
        await _effect(ending_manager, SHIELD, 5000)
        await _spin()

        await _effect(ending_manager, controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert SERIAL not in ending_manager._shield_ending_tasks
        clock.disable_blocking()
        await asyncio.sleep(0.15)

        assert [i for _, i in backend.rumble] == [255, 0]  # the death rumble and its stop

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-021"])
    async def test_game_end_mid_pulse_switches_the_motor_off(self, ending_manager, backend, clock):
        """UC-040 A17: SHIELD 0 at game end cuts the ending pulse in progress at once."""
        clock.enable_blocking()
        await _effect(ending_manager, SHIELD, 5000)
        await _step_until(clock, 1004.0)
        await _spin()
        clock.release_all()  # wake at the pulse time: the first pulse switches on
        await _spin()
        assert backend.rumble[-1][1] == 255

        await _effect(ending_manager, SHIELD, 0)

        assert backend.rumble[-1][1] == 0
        assert SERIAL not in ending_manager._shield_ending_tasks
        assert SERIAL not in ending_manager.vibration_tasks
        clock.disable_blocking()
        await _spin(40)
        assert _on_times(backend) == [4.0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    async def test_a_running_warning_keeps_the_slot(self, ending_manager, backend, clock):
        """UC-040 BR-021: a rumble that outlasts the 1 s wait suppresses the ending pulse."""
        clock.enable_blocking()
        await _effect(ending_manager, SHIELD, 5000)
        await _spin()
        await ending_manager.set_vibration(SERIAL, 100, 5000)
        warning = ending_manager.vibration_tasks[SERIAL]
        await _step_until(clock, 1004.95)  # the 1 s wait for the slot runs out

        assert _on_times(backend) == []
        assert ending_manager.vibration_tasks[SERIAL] is warning and not warning.done()
        warning.cancel()
        await _finish(ending_manager, clock)


class TestUC040ShieldEndingFlag:
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-021"])
    def test_reads_the_game_flag_and_clamps(self, monkeypatch):
        """flags.md §4: shield.ending_rumble_seconds, bad -> 1 s, clamped to 0..3."""
        from lib import feature_flags
        from services.controller_manager.effects_base import resolve_shield_ending_lead

        reads = []
        monkeypatch.setattr(
            feature_flags,
            "read_float_flag",
            lambda domain, key, _default, _game_id=None: reads.append((domain, key)) or 2.0,
        )

        assert _REAL_READ_SHIELD_ENDING_LEAD() == 2.0
        assert reads == [("game", "shield.ending_rumble_seconds")]
        assert resolve_shield_ending_lead(None) == 1.0
        assert resolve_shield_ending_lead(float("nan")) == 1.0
        assert resolve_shield_ending_lead(-1) == 1.0
        assert resolve_shield_ending_lead(9) == 3.0
        assert resolve_shield_ending_lead(0) == 0.0
