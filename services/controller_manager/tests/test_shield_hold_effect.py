"""
Shield hold feedback on the controller manager (#2096, UC-040 BR-026).

GAME_EFFECT_SHIELD_ENERGY is the idle energy layer (four LED states in the player's
colour, the cue of a grant or an empty activation, the recharge-to-full pattern);
GAME_EFFECT_SHIELD_HOLD is the running activation (white by the energy left, a
background rumble restored after every one-shot rumble, the "ending soon" pulse).
Timings run on a FakeClock; LED frames are checked as pure functions of the time.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.clock import FakeClock
from proto import controller_manager_pb2 as pb
from services.controller_manager import feedback_manager as fm_module
from services.controller_manager import shield_effects as shield_module
from services.controller_manager.effects_base import (
    HOLD_STATE_CHARGED,
    HOLD_STATE_EMPTY,
    HOLD_STATE_FULL,
    HOLD_STATE_LOW,
    ChargeCues,
    HoldCues,
    hold_active_frame,
    hold_energy_frame,
    hold_energy_state,
)
from services.controller_manager.feedback_manager import FeedbackManager

PLAYER = (0, 200, 0)
WHITE = (255, 255, 255)
SERIAL = "MOCK0001"
HOLD = pb.GAME_EFFECT_SHIELD_HOLD
ENERGY = pb.GAME_EFFECT_SHIELD_ENERGY
SHIELD = pb.GAME_EFFECT_SHIELD
START = 1000.0


def _cues(**overrides) -> pb.ShieldCueParams:
    values = {
        "rumble_intensity": 255,
        "pulse_ms": 150,
        "ending_lead_ms": 1000,
        "hold_rumble_intensity": 70,
        "empty_pulse_ms": 400,
    }
    values.update(overrides)
    return pb.ShieldCueParams(**values)


def _state(energy=0, cap=5000, full=5000, min_burst=300, rate=0, cue=pb.SHIELD_HOLD_CUE_NONE, cues=None):
    return pb.ShieldHoldState(
        energy_ms=energy,
        cap_ms=cap,
        full_ms=full,
        min_burst_ms=min_burst,
        rate_ms_per_s=rate,
        cue=cue,
        cues=cues if cues is not None else _cues(),
    )


class _Backend:
    def __init__(self, clock):
        self._clock = clock
        self.colors: dict[str, tuple[int, int, int]] = {}
        self.rumble: list[tuple[float, int]] = []  # (clock time, intensity)
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
    """No flagd: a command that ships every cue value must not read a flag at all."""

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
    """Stand-in renderer: the cue tests check rumble only, and a real renderer would move the shared clock."""
    await asyncio.Event().wait()


@pytest.fixture
def cue_manager(manager, monkeypatch):
    monkeypatch.setattr(FeedbackManager, "_shield_render_loop", _no_render)
    return manager


async def _spin(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def _effect(manager, effect, duration_ms, state=None):
    assert await manager.handle_game_effect(serial=SERIAL, effect=effect, duration_ms=duration_ms, shield_hold=state)
    await asyncio.sleep(0)


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
    task = manager.vibration_tasks.pop(SERIAL, None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await _spin()


def _on_times(backend, intensity=255):
    return [round(t - START, 3) for t, i in backend.rumble if i == intensity]


class TestUC040HoldLedStates:
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    @pytest.mark.parametrize(
        ("energy", "full", "want"),
        [
            (0.0, 5.0, HOLD_STATE_EMPTY),
            (0.29, 5.0, HOLD_STATE_EMPTY),  # below the minimum burst: cannot activate
            (0.3, 5.0, HOLD_STATE_LOW),
            (1.6, 5.0, HOLD_STATE_LOW),  # below a third of the 5 s cap
            (1.7, 5.0, HOLD_STATE_CHARGED),
            (4.9, 5.0, HOLD_STATE_CHARGED),
            (4.96, 5.0, HOLD_STATE_FULL),
            (2.0, 2.0, HOLD_STATE_FULL),  # the budget caps the pool at 2 s: that is full
            (0.2, 0.2, HOLD_STATE_EMPTY),  # a remainder below the minimum burst is empty, never full
        ],
    )
    def test_energy_state_by_level(self, energy, full, want):
        """UC-040 BR-026: empty below the minimum burst, low below a third of the cap, full at min(cap, budget)."""
        assert hold_energy_state(energy, 5.0, full, 0.3) == want

    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    @pytest.mark.parametrize("state", [HOLD_STATE_EMPTY, HOLD_STATE_LOW, HOLD_STATE_CHARGED, HOLD_STATE_FULL])
    def test_idle_frames_never_look_like_death_or_an_active_shield(self, state):
        """UC-040 BR-026: an idle state is never off or red (death) and never plain white (protected)."""
        for tick in range(0, 80):
            frame = hold_energy_frame(tick / 20, state, PLAYER)
            assert frame != (0, 0, 0)
            assert frame[1] > 0  # the player's green stays visible
            assert frame != WHITE
            assert not (frame[0] > 0 and frame[1] == 0 and frame[2] == 0)

    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    def test_each_idle_state_has_its_own_look(self):
        """UC-040 BR-026: empty steady, low deep 1 Hz, charged 60-100 % 0.5 Hz, full towards a 50 % white blend."""
        assert {hold_energy_frame(t / 10, HOLD_STATE_EMPTY, PLAYER) for t in range(40)} == {PLAYER}
        assert hold_energy_frame(0.0, HOLD_STATE_LOW, PLAYER) == PLAYER
        assert hold_energy_frame(0.5, HOLD_STATE_LOW, PLAYER) == (0, 40, 0)  # 20 % at the bottom, 1 Hz
        assert hold_energy_frame(1.0, HOLD_STATE_LOW, PLAYER) == PLAYER
        assert hold_energy_frame(0.0, HOLD_STATE_CHARGED, PLAYER) == (0, 120, 0)  # 60 %
        assert hold_energy_frame(1.0, HOLD_STATE_CHARGED, PLAYER) == PLAYER
        assert hold_energy_frame(0.0, HOLD_STATE_FULL, PLAYER) == PLAYER
        assert hold_energy_frame(1.0, HOLD_STATE_FULL, PLAYER) == (128, 228, 128)  # 50 % towards white

    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    def test_active_frame_is_white_by_the_energy_left_and_blinks_in_the_last_second(self):
        """UC-040 BR-026: 30 % + 70 % x (left / cap) white; the last second blinks at 4 Hz; None at 0."""
        assert hold_active_frame(5.0, 5.0) == WHITE
        assert hold_active_frame(2.5, 5.0) == (166, 166, 166)  # 0.3 + 0.7 * 0.5 = 65 %
        assert hold_active_frame(1.5, 5.0) == (130, 130, 130)  # 0.3 + 0.7 * 0.3 = 51 %
        assert hold_active_frame(1.0, 5.0) == WHITE  # the blink window starts on
        assert hold_active_frame(0.85, 5.0) == (0, 0, 0)  # second half of the first 250 ms period
        assert hold_active_frame(0.7, 5.0) == WHITE
        assert hold_active_frame(0.0, 5.0) is None

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A38", br=["BR-026"])
    async def test_a_recharge_moves_the_led_through_the_states_without_a_command(self, cue_manager, clock):
        """UC-040 A38 / BR-026: 1 s at 0.5 s/s: low, then charged (after 2 s), then full (after 8 s)."""
        await _effect(cue_manager, ENERGY, 5000, _state(energy=1000, rate=500))
        layer = cue_manager._shield_layers[SERIAL]["energy"]
        energy = cue_manager._hold_energy[SERIAL]

        def state_at(offset):
            level = energy.level(START + offset)
            return hold_energy_state(level, energy.cap_s, energy.full_s, energy.min_burst_s)

        assert state_at(0.0) == HOLD_STATE_LOW
        assert state_at(2.0) == HOLD_STATE_CHARGED
        assert state_at(8.0) == HOLD_STATE_FULL
        assert energy.level(START + 60.0) == pytest.approx(5.0)  # stops at full
        assert cue_manager._shield_layer_frame(SERIAL, layer, START, 0.0) == PLAYER  # low starts at 100 %
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_the_activation_draws_over_the_energy_layer(self, cue_manager, clock):
        """UC-040 BR-026: SHIELD_HOLD is painted over the idle energy; SHIELD_HOLD 0 hands the LED back to it."""
        await _effect(cue_manager, ENERGY, 5000, _state(energy=5000))
        await _effect(cue_manager, HOLD, 5000, _state(energy=5000))
        top = cue_manager._top_shield(SERIAL, START)
        assert top.kind == "hold"
        assert cue_manager._shield_layer_frame(SERIAL, top, START, 0.0) == WHITE

        await _effect(cue_manager, HOLD, 0)
        top = cue_manager._top_shield(SERIAL, START)
        assert top.kind == "energy"
        await _finish(cue_manager, clock)


class TestUC040HoldRumble:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_the_background_rumble_comes_back_after_a_warning(self, cue_manager, backend, clock):
        """UC-040 BR-026: the hold rumble (70) runs while active; a one-shot warning ends back at 70, not 0."""
        await _effect(cue_manager, HOLD, 5000, _state(energy=5000, cues=_cues(ending_lead_ms=0)))
        assert backend.rumble[-1][1] == 70

        await cue_manager.set_vibration(SERIAL, 100, 20)  # the warning rumble
        assert backend.rumble[-1][1] == 100
        await asyncio.wait_for(cue_manager.vibration_tasks[SERIAL], timeout=1)
        assert backend.rumble[-1][1] == 70

        await _effect(cue_manager, HOLD, 0)
        assert backend.rumble[-1][1] == 0
        assert SERIAL not in cue_manager._background_rumble
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_death_ends_the_hold_and_keeps_the_death_rumble(self, cue_manager, backend, clock, monkeypatch):
        """UC-040 A39 / BR-026: death ends every layer and the background level; the death rumble stops at 0."""
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 30)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 1)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 1)
        await _effect(cue_manager, ENERGY, 5000, _state(energy=5000))
        await _effect(cue_manager, HOLD, 5000, _state(energy=5000, cues=_cues(ending_lead_ms=0)))

        await _effect(cue_manager, pb.GAME_EFFECT_PLAYER_DEATH, 0)
        await asyncio.sleep(0.1)

        assert not cue_manager.shield_active(SERIAL)
        assert SERIAL not in cue_manager._background_rumble
        assert SERIAL not in cue_manager._hold_energy
        assert [i for _, i in backend.rumble][-2:] == [255, 0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    async def test_ending_soon_double_pulse_before_the_energy_runs_out(self, cue_manager, backend, clock):
        """UC-040 BR-026: 3 s of energy, lead 1 s: two 150 ms pulses from 2.0 s, the motor back at 70 between."""
        clock.enable_blocking()
        await _effect(cue_manager, HOLD, 3000, _state(energy=3000))
        await _step_until(clock, START + 2.4)
        clock.disable_blocking()  # the last pulse's off step
        await _spin()

        assert _on_times(backend) == [2.0, 2.25]
        after_pulses = [i for t, i in backend.rumble if t > START + 2.0]
        assert 0 not in after_pulses and after_pulses.count(70) == 2
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    async def test_a_grant_during_the_hold_moves_the_ending_pulse(self, cue_manager, backend, clock):
        """UC-040 A36 step 2 / BR-026: a restart with a later end moves the pulse; the background is not restarted."""
        clock.enable_blocking()
        # Both commands before the first ending loop runs: the shared fake clock moves at each sleep call.
        assert await cue_manager.handle_game_effect(
            serial=SERIAL, effect=HOLD, duration_ms=3000, shield_hold=_state(energy=3000)
        )
        first = cue_manager._hold_ending_tasks[SERIAL]
        await _effect(cue_manager, HOLD, 4000, _state(energy=4000))
        await _step_until(clock, START + 3.4)
        clock.disable_blocking()
        await _spin()

        assert first.cancelled() or first.done()
        assert _on_times(backend) == [3.0, 3.25]  # 4 s end minus the 1 s lead, not 2.0
        assert [i for _, i in backend.rumble].count(70) == 3  # once at the start, after each pulse
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    async def test_a_top_up_during_the_hold_is_one_pulse_over_the_background(self, cue_manager, backend, clock):
        """UC-040 A36 step 4 / BR-026: a partial grant while held: one pulse, then back to the hold rumble."""
        await _effect(cue_manager, HOLD, 3000, _state(energy=3000, cues=_cues(ending_lead_ms=0)))
        await _effect(
            cue_manager, HOLD, 4000, _state(energy=4000, cue=pb.SHIELD_HOLD_CUE_TOP_UP, cues=_cues(ending_lead_ms=0))
        )
        await asyncio.wait_for(cue_manager._hold_cue_tasks[SERIAL], timeout=2)

        assert [i for _, i in backend.rumble] == [70, 255, 70]
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_empty_is_one_pulse_of_empty_pulse_ms(self, cue_manager, backend, clock):
        """UC-040 A39 / BR-026: the EMPTY cue is one 400 ms pulse at the cue intensity."""
        await _effect(cue_manager, ENERGY, 5000, _state(energy=0, cue=pb.SHIELD_HOLD_CUE_EMPTY))
        await asyncio.wait_for(cue_manager._hold_cue_tasks.get(SERIAL) or asyncio.sleep(0), timeout=2)

        assert [(round(t - START, 3), i) for t, i in backend.rumble] == [(0.0, 255), (0.4, 0)]
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-026"])
    @pytest.mark.parametrize(
        ("cue", "want"), [(pb.SHIELD_HOLD_CUE_FULL, [0.0, 0.25, 0.5]), (pb.SHIELD_HOLD_CUE_TOP_UP, [0.0])]
    )
    async def test_full_plays_the_grant_pattern_and_a_top_up_one_pulse(self, cue_manager, backend, clock, cue, want):
        """UC-040 A36 step 4 (maintainer decision): the full pattern only when the pool is full, else one pulse."""
        await _effect(
            cue_manager, ENERGY, 5000, _state(energy=5000 if cue == pb.SHIELD_HOLD_CUE_FULL else 2000, cue=cue)
        )
        await asyncio.wait_for(cue_manager._hold_cue_tasks.get(SERIAL) or asyncio.sleep(0), timeout=2)

        assert _on_times(backend) == want
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A38", br=["BR-026"])
    async def test_recharge_to_full_plays_the_grant_pattern_once(self, cue_manager, backend, clock):
        """UC-040 A38 step 3: 4 s + 0.5 s/s fills (minus the 0.05 s room) at 1.9 s: the grant pattern, once."""
        await _effect(cue_manager, ENERGY, 5000, _state(energy=4000, rate=500))
        await asyncio.wait_for(cue_manager._hold_cue_tasks[SERIAL], timeout=2)

        assert _on_times(backend) == [1.9, 2.15, 2.4]
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A38", br=["BR-026"])
    async def test_no_recharge_pattern_without_a_rate_or_a_usable_budget(self, cue_manager, backend, clock):
        """UC-040 A38 step 4: rate 0, or a budget remainder below the minimum burst, never plays the pattern."""
        await _effect(cue_manager, ENERGY, 5000, _state(energy=1000, rate=0))
        await _spin()
        assert SERIAL not in cue_manager._hold_cue_tasks
        await _effect(cue_manager, ENERGY, 5000, _state(energy=0, full=200, rate=500))
        await _spin()
        assert SERIAL not in cue_manager._hold_cue_tasks
        assert backend.rumble == []
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026", "BR-027"])
    async def test_the_shipped_cue_values_win_over_the_flags(self, cue_manager, backend, clock, monkeypatch):
        """UC-040 BR-026: the game's latched values play; a missing field falls back to the flag read."""
        await _effect(
            cue_manager, HOLD, 5000, _state(energy=5000, cues=_cues(hold_rumble_intensity=110, ending_lead_ms=0))
        )
        assert backend.rumble[-1][1] == 110
        await _effect(cue_manager, HOLD, 0)

        monkeypatch.setattr(shield_module, "read_hold_cues", lambda: HoldCues(hold_rumble=40, ending_lead_s=0))
        partial = pb.ShieldCueParams(rumble_intensity=255, pulse_ms=150, empty_pulse_ms=400, ending_lead_ms=0)
        await _effect(cue_manager, HOLD, 5000, _state(energy=5000, cues=partial))
        assert backend.rumble[-1][1] == 40
        await _finish(cue_manager, clock)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-026"])
    async def test_a_disconnect_drops_every_hold_state(self, cue_manager, clock):
        """UC-040 BR-026: a disconnected controller keeps no hold layer, energy, cue values or background level."""
        await _effect(cue_manager, ENERGY, 5000, _state(energy=4000, rate=500))
        await _effect(cue_manager, HOLD, 4000, _state(energy=4000))
        cue_manager.clear_controller(SERIAL)
        await _spin()
        for table in (
            cue_manager._shield_layers,
            cue_manager._hold_energy,
            cue_manager._hold_cues,
            cue_manager._background_rumble,
            cue_manager._hold_ending_tasks,
            cue_manager._hold_cue_tasks,
        ):
            assert SERIAL not in table


class TestUC040HoldRenderer:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    async def test_an_activation_that_runs_out_without_its_end_command_stops_its_rumble(self, manager, backend, clock):
        """UC-040 BR-026: the renderer drops an expired hold layer and its background rumble with it."""
        await _effect(manager, ENERGY, 5000, _state(energy=300))
        await _effect(manager, HOLD, 300, _state(energy=300, cues=_cues(ending_lead_ms=0)))
        assert backend.rumble[-1][1] == 70
        await _spin(60)  # the renderer ticks 50 ms per sleep on the fake clock

        assert "hold" not in manager._shield_layers.get(SERIAL, {})
        assert SERIAL not in manager._background_rumble
        assert backend.rumble[-1][1] == 0
        assert manager.shield_active(SERIAL)  # the energy layer stays
        await _finish(manager, clock)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", br=["BR-026"])
async def test_the_servicer_forwards_the_hold_state():
    """UC-040 BR-026: a GameEffectCommand's shield_hold reaches the feedback manager."""
    from services.controller_manager.servicer import ControllerManagerServicer

    servicer = MagicMock()
    servicer.feedback_manager.handle_game_effect = AsyncMock(return_value=True)
    state = _state(energy=1500)
    cmd = pb.GameEffectCommand(serial=SERIAL, effect=ENERGY, duration_ms=5000, shield_hold=state)
    await ControllerManagerServicer._process_game_effect_command(servicer, cmd, "sub")
    assert servicer.feedback_manager.handle_game_effect.await_args.kwargs["shield_hold"] == state

    plain = pb.GameEffectCommand(serial=SERIAL, effect=SHIELD, duration_ms=5000)
    await ControllerManagerServicer._process_game_effect_command(servicer, plain, "sub")
    assert servicer.feedback_manager.handle_game_effect.await_args.kwargs["shield_hold"] is None
