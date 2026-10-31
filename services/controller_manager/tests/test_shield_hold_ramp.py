"""
Shield hold rumble ramp and LED dimming on the controller manager (#2463, UC-040 BR-026).

Ramp mode (``hold_rumble_max_intensity`` above ``hold_rumble_intensity``): the background
rumble rises from the hold rumble to the ramp top as the energy drains against the
activation energy the coordinator ships (``ShieldHoldState.activation_ms``); no ending
and no empty pulse. The white LED dims from 100 % to the floor when the last second starts.
Flat mode is pinned by test_shield_hold_effect.py.
"""

import asyncio
import contextlib

import pytest

from lib.clock import FakeClock
from proto import controller_manager_pb2 as pb
from services.controller_manager import feedback_manager as fm_module
from services.controller_manager import shield_effects as shield_module
from services.controller_manager.effects_base import (
    ChargeCues,
    HoldCues,
    hold_active_frame,
    hold_fill,
    hold_ramp_intensity,
    resolve_hold_led_floor_pct,
    resolve_hold_rumble_max_intensity,
)
from services.controller_manager.feedback_manager import FeedbackManager

PLAYER = (0, 200, 0)
WHITE = (255, 255, 255)
SERIAL = "MOCK0001"
HOLD = pb.GAME_EFFECT_SHIELD_HOLD
ENERGY = pb.GAME_EFFECT_SHIELD_ENERGY
START = 1000.0


def _ramp_cues(**overrides) -> pb.ShieldCueParams:
    values = {
        "rumble_intensity": 255,
        "pulse_ms": 150,
        "ending_lead_ms": 1000,
        "hold_rumble_intensity": 70,
        "empty_pulse_ms": 400,
        "hold_rumble_max_intensity": 200,
        "hold_led_floor_pct": 20,
    }
    values.update(overrides)
    return pb.ShieldCueParams(**values)


def _state(energy=2000, cap=5000, activation=2000, cue=pb.SHIELD_HOLD_CUE_NONE, cues=None):
    return pb.ShieldHoldState(
        energy_ms=energy,
        cap_ms=cap,
        full_ms=cap,
        min_burst_ms=300,
        cue=cue,
        cues=cues if cues is not None else _ramp_cues(),
        activation_ms=activation,
    )


class _Backend:
    def __init__(self, clock):
        self._clock = clock
        self.colors: dict[str, tuple[int, int, int]] = {}
        self.rumble: list[tuple[float, int]] = []
        self.effect_active: dict[str, bool] = {}

    async def set_led_color(self, serial, r, g, b):
        self.colors[serial] = (r, g, b)
        return True

    async def set_rumble(self, serial, intensity):
        self.rumble.append((self._clock.time(), intensity))
        return True

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active


@pytest.fixture(autouse=True)
def no_flag_reads(monkeypatch):
    def _unexpected_read():
        raise AssertionError("read_hold_cues called although the command shipped every cue value")

    monkeypatch.setattr(shield_module, "read_hold_cues", _unexpected_read)
    monkeypatch.setattr(shield_module, "read_charge_cues", lambda: ChargeCues(intensity=0))


@pytest.fixture
def clock():
    return FakeClock(start_time=START)


@pytest.fixture
def backend(clock):
    return _Backend(clock)


@pytest.fixture
def manager(backend, clock):
    fm = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {"name": "P1"}})
    fm._clock = clock
    fm.base_colors[SERIAL] = PLAYER
    return fm


async def _no_render(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.fixture
def cue_manager(manager, monkeypatch):
    """No renderer: the test drives the ramp steps itself on a clock it sets."""
    monkeypatch.setattr(FeedbackManager, "_shield_render_loop", _no_render)
    return manager


async def _spin(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def _effect(manager, effect, duration_ms, state=None):
    assert await manager.handle_game_effect(serial=SERIAL, effect=effect, duration_ms=duration_ms, shield_hold=state)
    await asyncio.sleep(0)


async def _finish(manager, clock):
    clock.disable_blocking()
    await manager.end_shield_effect(SERIAL, restore=False)
    task = manager.vibration_tasks.pop(SERIAL, None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await _spin()


async def _ramp_to(manager, clock, offset):
    """Set the clock to START + offset and run one ramp step, as a renderer tick would."""
    clock.set_time(START + offset)
    await manager._step_hold_ramp(SERIAL, clock.time())


def _levels(backend):
    return [i for _, i in backend.rumble]


class TestUC040HoldRampPure:
    @pytest.mark.uc("UC-040", br=["BR-026"])
    def test_ramp_rises_from_the_hold_rumble_to_the_top_and_is_flat_at_or_below_it(self):
        """UC-040 BR-026 (#2463): 70 at a full activation, 200 empty, 135 halfway; top <= base is flat (rollback)."""
        assert hold_ramp_intensity(1.0, 70, 200) == 70
        assert hold_ramp_intensity(0.5, 70, 200) == 135
        assert hold_ramp_intensity(0.0, 70, 200) == 200
        assert hold_ramp_intensity(0.0, 70, 70) == 70
        assert hold_ramp_intensity(0.0, 70, 0) == 70
        assert hold_ramp_intensity(0.0, 0, 200) == 0  # hold rumble off stays silent
        assert not HoldCues(hold_rumble=0, ramp_max=200).ramp
        levels = [hold_ramp_intensity(f / 20, 70, 200) for f in range(20, -1, -1)]
        assert levels == sorted(levels)

    @pytest.mark.uc("UC-040", br=["BR-026"])
    def test_fill_is_the_share_of_the_activation_energy_left(self):
        assert hold_fill(1.0, 2.0) == 0.5
        assert hold_fill(3.0, 2.0) == 1.0
        assert hold_fill(-1.0, 2.0) == 0.0
        assert hold_fill(1.0, 0.0) == 1.0  # no reference: full

    @pytest.mark.uc("UC-040", br=["BR-026"])
    def test_led_starts_white_and_dims_to_the_floor_when_the_last_second_starts(self):
        """UC-040 BR-026 (#2463): measured against the activation energy, not the cap (a 2 s activation was 58 %)."""
        assert hold_active_frame(2.0, 2.0, 0.2) == WHITE
        before_blink = hold_active_frame(1.01, 2.0, 0.2)
        assert before_blink[0] == before_blink[1] == before_blink[2]
        assert 51 <= before_blink[0] <= 53  # about the 20 % floor
        assert hold_active_frame(1.0, 2.0, 0.2) == WHITE  # the blink starts on
        assert hold_active_frame(5.0, 5.0, 1.0) == hold_active_frame(1.5, 5.0, 1.0) == WHITE  # 100 = no dimming

    @pytest.mark.uc("UC-040", br=["BR-026"])
    @pytest.mark.parametrize(
        ("raw", "want"), [(999, 230), (255, 230), (-1, 0), ("loud", 200), (float("nan"), 200), (0, 0), (180, 180)]
    )
    def test_ramp_top_is_validated(self, raw, want):
        assert resolve_hold_rumble_max_intensity(raw) == want

    @pytest.mark.uc("UC-040", br=["BR-026"])
    @pytest.mark.parametrize(("raw", "want"), [(0, 10), (5, 10), (150, 100), ("dim", 20), (None, 20), (30, 30)])
    def test_led_floor_is_validated_and_never_below_10(self, raw, want):
        assert resolve_hold_led_floor_pct(raw) == want


class TestUC040HoldRampLayer:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_the_layer_dims_against_the_activation_energy_not_the_cap(self, cue_manager, clock):
        """UC-040 BR-026 (#2463): a 2 s activation, 5 s cap: 100 % white at the start (58 % before), then the floor."""
        await _effect(cue_manager, HOLD, 2000, _state(energy=2000, cap=5000, activation=2000))
        layer = cue_manager._shield_layers[SERIAL]["hold"]
        assert cue_manager._shield_layer_frame(SERIAL, layer, START, 0.0) == WHITE
        late = cue_manager._shield_layer_frame(SERIAL, layer, START + 0.99, 0.99)
        assert late[0] <= 53
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_ramp_mode_starts_no_ending_pulse_and_plays_no_empty_pulse(self, cue_manager, backend, clock):
        """UC-040 A39 step 4 (#2463): the ramp is the warning; the end is a clean stop, no hold_pulse ever runs."""
        await _effect(cue_manager, HOLD, 3000, _state(energy=3000, activation=3000))
        assert SERIAL not in cue_manager._hold_ending_tasks
        await _effect(cue_manager, HOLD, 0)
        await _effect(cue_manager, ENERGY, 5000, _state(energy=0, activation=0, cue=pb.SHIELD_HOLD_CUE_EMPTY))
        await asyncio.wait_for(cue_manager._hold_cue_tasks.get(SERIAL) or asyncio.sleep(0), timeout=2)
        await _spin()

        assert _levels(backend) == [70, 0]  # the background on and off; no 255 pulse
        assert all(t.get_name() != shield_module.HOLD_PULSE_TASK_NAME for t in cue_manager.vibration_tasks.values())
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    async def test_a_grant_mid_hold_steps_the_ramp_down_without_restarting_it(self, cue_manager, backend, clock):
        """UC-040 A36 step 4 (#2463): more activation energy moves the level down, never above the top, no restart."""
        await _effect(cue_manager, HOLD, 2000, _state(energy=2000, activation=2000))
        await _ramp_to(cue_manager, clock, 1.0)
        assert _levels(backend) == [70, 135]

        await _effect(cue_manager, HOLD, 2000, _state(energy=2000, activation=3000, cue=pb.SHIELD_HOLD_CUE_NONE))
        await _ramp_to(cue_manager, clock, 1.2)
        levels = _levels(backend)
        assert levels[-1] == 122  # 1.8 s of 3 s left: 70 + 130 * 0.4, below the 135 before the grant
        assert 70 not in levels[1:] and max(levels) <= 200
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_a_resend_after_a_reconnect_lands_mid_ramp(self, cue_manager, backend, clock):
        """UC-040 BR-026 (#2463): a new layer with 1 s left of a 4 s activation starts at the ramp level, not at 70."""
        await _effect(cue_manager, HOLD, 1000, _state(energy=1000, activation=4000))
        assert _levels(backend) == [168]  # 70 + 130 * 3 / 4
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_a_warning_during_the_ramp_restores_the_current_level(self, cue_manager, backend, clock):
        """UC-040 BR-026 (#2463): the one-shot keeps the slot; its stop restores the ramp level, not 70."""
        await _effect(cue_manager, HOLD, 2000, _state(energy=2000, activation=2000))
        await _ramp_to(cue_manager, clock, 1.0)
        await cue_manager.set_vibration(SERIAL, 100, 20)
        await _ramp_to(cue_manager, clock, 1.5)  # under the warning: stored, the motor stays with the warning
        assert _levels(backend)[-1] == 100
        await asyncio.wait_for(cue_manager.vibration_tasks[SERIAL], timeout=1)
        assert _levels(backend)[-1] == 168
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_the_ramp_writes_at_most_every_100_ms_and_in_steps_of_8(self, cue_manager, backend, clock):
        """UC-040 BR-026 (#2463): HID budget: no write for a step below 8 or within 100 ms of the last one."""
        await _effect(cue_manager, HOLD, 1000, _state(energy=1000, activation=1000))  # 130 per second
        await _ramp_to(cue_manager, clock, 0.05)  # 76.5: below the step and too early
        await _ramp_to(cue_manager, clock, 0.07)  # 79: a step of 9, but 70 ms after the start
        assert _levels(backend) == [70]
        await _ramp_to(cue_manager, clock, 0.1)  # 83
        await _ramp_to(cue_manager, clock, 0.2)  # 96
        await _ramp_to(cue_manager, clock, 0.25)  # 102.5: a step of 6, skipped
        assert _levels(backend) == [70, 83, 96]
        await _finish(cue_manager, clock)


class TestUC040HoldRampRenderer:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_a_two_second_hold_ramps_from_70_to_the_top_then_stops(self, manager, backend, clock):
        """UC-040 BR-026 (#2463): 70 at t=0, never down, >= 190 before the end, 0 within one tick after it."""
        await _effect(manager, HOLD, 2000, _state(energy=2000, activation=2000))
        for _ in range(400):
            await asyncio.sleep(0)
            if clock.time() > START + 2.5:
                break

        levels = _levels(backend)
        assert levels[0] == 70 and levels[-1] == 0
        assert levels[:-1] == sorted(levels[:-1])
        assert max(levels) >= 190 and max(levels) <= 200
        assert len(levels) <= 21
        stop_at = backend.rumble[-1][0]
        assert START + 2.0 - 1e-6 <= stop_at <= START + 2.05 + 1e-6
        assert 255 not in levels  # no ending or empty pulse
        await _finish(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_hold_rumble_off_stays_silent_and_keeps_the_flat_cues(self, manager, backend, clock):
        """UC-040 BR-026 (#2463 review): hold rumble 0 means no hold rumble; the ramp top changes nothing."""
        off = _ramp_cues(hold_rumble_intensity=0)
        await _effect(manager, HOLD, 2000, _state(energy=2000, activation=4000, cues=off))
        assert manager._hold_cues[SERIAL].ramp is False
        assert SERIAL in manager._hold_ending_tasks  # flat cues: the ending pulse as before #2463
        manager._hold_ending_tasks.pop(SERIAL).cancel()
        for _ in range(400):
            await asyncio.sleep(0)
            if clock.time() > START + 2.5:
                break

        assert _levels(backend) == []  # no background level and no ramp write, mid-ramp re-send included
        await _finish(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_no_ramp_write_after_shield_hold_0_while_the_energy_layer_renders(self, manager, backend, clock):
        """UC-040 A39 (#2463 review probe): a release ends the ramp for good while the energy layer still renders."""
        await _effect(manager, ENERGY, 5000, _state(energy=3000, activation=0))
        await _effect(manager, HOLD, 3000, _state(energy=3000, activation=3000))
        for _ in range(400):
            await asyncio.sleep(0)
            if clock.time() >= START + 1.0:
                break
        assert max(_levels(backend)) > 70  # the ramp ran
        await _effect(manager, HOLD, 0)
        written = len(backend.rumble)
        for _ in range(400):
            await asyncio.sleep(0)
            if clock.time() >= START + 2.0:
                break

        after = _levels(backend)[written:]
        assert all(level == 0 for level in after), after
        assert SERIAL not in manager._background_rumble
        assert manager.shield_active(SERIAL)  # the energy layer still renders
        await _finish(manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_death_mid_ramp_ends_at_0_and_no_ramp_write_follows(self, manager, backend, clock, monkeypatch):
        """UC-040 A39 (#2462, #2463): death at 90 % of the ramp: the death rumble keeps the motor, then 0, no ramp."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 30)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        await _effect(manager, HOLD, 2000, _state(energy=2000, activation=2000))
        for _ in range(400):
            await asyncio.sleep(0)
            if clock.time() >= START + 1.8:
                break
        assert max(_levels(backend)) > 150

        await _effect(manager, pb.GAME_EFFECT_PLAYER_DEATH, 0)
        await asyncio.sleep(0.1)
        await _spin(60)

        assert not manager.shield_active(SERIAL)
        assert SERIAL not in manager._background_rumble
        assert _levels(backend)[-2:] == [255, 0]
