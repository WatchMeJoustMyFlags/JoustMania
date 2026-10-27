"""
Shield activation mode ``hold`` in the game coordinator (#2096, FR-161).

UC-030 A7 (the player holds the shield), BR-008 (trigger hold), BR-009 (release
tail); UC-040 A36 (energy granted), A37 (grant into a full pool or an exhausted
budget), A38 (passive recharge), A39 (energy ends), BR-019 (mode allow-list),
BR-024 (pool and budget), BR-025 (sources), BR-027 (latched per game).

Every test runs on a fake clock (``base.time.time``) with the flags patched. The
game is latched to hold by its own ``_latch_shield_hold`` reading the patched
flags, as at the start of the game loop.
"""

import contextlib
import random
import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from opentelemetry.trace import Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import (
    HOLD_OUTCOME_BUDGET_EXHAUSTED,
    HOLD_OUTCOME_DEATH,
    HOLD_OUTCOME_EMPTY,
    HOLD_OUTCOME_GAME_END,
    HOLD_OUTCOME_RELEASED,
    SHIELD_HOLD_FLAGS,
    BaseGameMode,
    Player,
    resolve_hold_value,
    resolve_shield_activation,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.intervention_errors import InterventionNotApplied

T0 = 1000.0
DT = 1.0 / 60.0
IDLE = (0.0, 0.0, 1.0)


@pytest.fixture(autouse=True)
def _no_shield_limits():
    """Shield limits off (UC-040 BR-022) unless a test sets them."""
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


class RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)


class Clock:
    def __init__(self, t=T0):
        self.t = t


class Flags:
    """The shield flags of a hold game; any other key resolves to the caller's default."""

    def __init__(self):
        self.values = {
            "shield.activation": "hold",
            "shield.energy_max_seconds": 5.0,
            "shield.energy_budget_seconds": 15.0,
            "shield.energy_initial_seconds": 0.0,
            "shield.recharge_per_second": 0.0,
            "shield.min_burst_seconds": 0.3,
        }
        self.reads: list[str] = []

    def read(self, _set, key, default, **_kw):
        self.reads.append(key)
        return self.values.get(key, default)


@pytest.fixture
def clock():
    c = Clock()
    with patch.object(base_module.time, "time", side_effect=lambda: c.t):
        yield c


@pytest.fixture
def flags():
    f = Flags()
    with (
        patch.object(base_module, "read_string_flag", side_effect=f.read),
        patch.object(base_module, "read_float_flag", side_effect=f.read),
    ):
        yield f


@pytest.fixture
def events():
    return EventCollector()


def _make_ffa(events, game_cls=FFAGame):
    g = game_cls(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_shield_hold",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


def latch(game, clock):
    """What the start of the game loop does (UC-040 BR-027)."""
    params = game._latch_shield_hold()
    game._start_shield_hold_clock(clock.t)
    return params


@pytest.fixture
def game(clock, flags, events):  # noqa: ARG001 - flags patch base for the test
    g = _make_ffa(events)
    latch(g, clock)
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


def frame(serial, trigger=False, accel=IDLE):
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=accel[0], y=accel[1], z=accel[2]),
        trigger_pressed=trigger,
    )


async def step(game, clock, serial, trigger, accel=IDLE, dt=DT):
    """Advance the clock one frame, then process the frame."""
    clock.t += dt
    await game._process_controller_state(frame(serial, trigger, accel))


async def arm(game, clock, serial="p1"):
    """One released frame: the trigger read released once in the game loop (UC-030 BR-005)."""
    await step(game, clock, serial, False)


def fill(game, serial, seconds):
    game.players[serial].hold_energy = seconds


def g_vector(magnitude):
    return (0.0, 0.0, magnitude)


def thresholds(game, serial="p1"):
    return game._compute_effective_thresholds(game.players[serial])


@pytest.fixture
def counter():
    with patch.object(base_module.metrics, "shield_hold_activations_total") as c:
        yield c


def outcomes(counter):
    return [call.kwargs["outcome"] for call in counter.labels.call_args_list]


# --------------------------------------------------------------------------- #
# UC-030 A7, BR-008: holding the trigger protects; release keeps the rest
# --------------------------------------------------------------------------- #
class TestUC030A7Hold:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_press_activates_and_the_press_frame_survives_a_death_jolt(self, game, clock):
        """A7 steps 1-2: the activation is evaluated before the death check; EMA reset; no grace write."""
        fill(game, "p1", 2.0)
        await arm(game, clock)
        _, death = thresholds(game)
        await step(game, clock, "p1", True, accel=g_vector(death * 3))
        p1 = game.players["p1"]
        assert p1.alive
        assert p1.hold_started_at == pytest.approx(clock.t)
        assert p1.hold_until == pytest.approx(clock.t + 2.0)
        assert p1.smoothed_accel == 0.0
        assert p1.grace_until < clock.t
        assert game.effective_grace_until(p1) == pytest.approx(clock.t + 2.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-024"])
    async def test_release_books_the_time_held_and_keeps_the_rest(self, game, clock, counter):
        """A7 steps 3-4: 1 s held of 3 s: 2 s left, 1 s of budget used, outcome released."""
        fill(game, "p1", 3.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        start = clock.t
        for _ in range(59):
            await step(game, clock, "p1", True)
        await step(game, clock, "p1", False)
        p1 = game.players["p1"]
        held = clock.t - start
        assert p1.hold_started_at == 0.0, "held >= min burst: the release frame ends it"
        assert p1.hold_budget_used == pytest.approx(held)
        assert game._shield_hold_energy(p1, clock.t) == pytest.approx(3.0 - held)
        assert outcomes(counter) == [HOLD_OUTCOME_RELEASED]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-024"])
    async def test_a_tap_costs_the_minimum_burst_and_protects_through_the_tail(self, game, clock):
        """A tap (one held frame) costs 0.3 s; a death jolt inside the tail does not kill."""
        fill(game, "p1", 2.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        start = clock.t
        await step(game, clock, "p1", False)
        p1 = game.players["p1"]
        assert p1.hold_until == pytest.approx(start + 0.3)
        _, death = thresholds(game)
        await step(game, clock, "p1", False, accel=g_vector(death * 3), dt=0.1)
        assert p1.alive, "the minimum-burst tail still protects"
        clock.t = start + 0.31
        await step(game, clock, "p1", False, dt=0.0)
        assert p1.hold_started_at == 0.0
        assert p1.hold_budget_used == pytest.approx(0.3)
        assert game._shield_hold_energy(p1, clock.t) == pytest.approx(1.7)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_a_press_inside_the_tail_continues_the_same_activation(self, game, clock, counter):
        """BR-008 debounce: release then press within the minimum burst is one activation."""
        fill(game, "p1", 2.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        start = clock.t
        await step(game, clock, "p1", False)
        await step(game, clock, "p1", True)
        p1 = game.players["p1"]
        assert p1.hold_started_at == start
        assert p1.hold_until == pytest.approx(start + 2.0)
        for _ in range(30):
            await step(game, clock, "p1", True)
        await step(game, clock, "p1", False)
        assert outcomes(counter) == [HOLD_OUTCOME_RELEASED]
        assert p1.hold_budget_used == pytest.approx(clock.t - start)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-024"])
    async def test_holding_until_empty_ends_exactly_at_the_deadline_and_disarms(self, game, clock, counter):
        """A39 step 1: protection ends at the energy's end; holding on never restarts it."""
        fill(game, "p1", 1.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        deadline = game.players["p1"].hold_until
        clock.t = deadline - 0.001
        await step(game, clock, "p1", True, dt=0.0)
        assert game.is_in_grace(game.players["p1"], clock.t)
        clock.t = deadline
        await step(game, clock, "p1", True, dt=0.0)
        p1 = game.players["p1"]
        assert not game.is_in_grace(p1, clock.t)
        assert outcomes(counter) == [HOLD_OUTCOME_EMPTY]
        assert p1.hold_budget_used == pytest.approx(1.0)
        # Energy arrives while the trigger is still held: no new activation without a release.
        assert await game.grant_shield("p1", 2.0) is True
        await step(game, clock, "p1", True)
        assert p1.hold_started_at == 0.0
        await step(game, clock, "p1", False)
        await step(game, clock, "p1", True)
        assert p1.hold_started_at == clock.t

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008", "BR-005"])
    async def test_trigger_held_from_the_lobby_does_not_activate(self, game, clock):
        """BR-008 arming as BR-005: no released frame yet, no activation."""
        fill(game, "p1", 2.0)
        for _ in range(5):
            await step(game, clock, "p1", True)
        assert game.players["p1"].hold_started_at == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    @pytest.mark.uc("UC-040", br=["BR-024"])
    async def test_less_than_a_minimum_burst_of_energy_does_not_activate(self, game, clock):
        """BR-024: an activation needs a usable energy of at least the minimum burst."""
        fill(game, "p1", 0.2)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        assert game.players["p1"].hold_started_at == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_holding_through_another_grace_does_not_drain_and_starts_after_it(self, game, clock):
        """BR-008: no drain during the match-start grace; the activation starts when it ends."""
        fill(game, "p1", 2.0)
        await arm(game, clock)
        p1 = game.players["p1"]
        p1.grace_until = clock.t + 0.3
        grace_end = p1.grace_until
        while clock.t + DT < grace_end:
            await step(game, clock, "p1", True)
            assert p1.hold_started_at == 0.0
        await step(game, clock, "p1", True)
        assert p1.hold_started_at == clock.t >= grace_end
        assert p1.hold_energy_start == pytest.approx(2.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_no_activation_while_a_kill_of_the_player_is_in_progress(self, game, clock):
        """BR-008: a kill in progress blocks the activation."""
        fill(game, "p1", 2.0)
        await arm(game, clock)
        game._kills_in_progress.add("p1")
        await step(game, clock, "p1", True)
        assert game.players["p1"].hold_started_at == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008", "BR-006"])
    async def test_a_stream_reopen_releases_at_the_last_held_frame(self, game, clock, counter):
        """BR-008: no drain crosses a reconnect."""
        fill(game, "p1", 4.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        start = clock.t
        for _ in range(59):
            await step(game, clock, "p1", True)
        last_held = clock.t
        clock.t += 2.0  # the stream stalls, then reopens
        with patch.object(game, "_create_gameplay_stream", new_callable=AsyncMock):
            await game._reopen_gameplay_stream("test")
        await step(game, clock, "p1", True, dt=0.0)
        p1 = game.players["p1"]
        assert outcomes(counter) == [HOLD_OUTCOME_RELEASED]
        assert p1.hold_budget_used == pytest.approx(last_held - start)
        assert p1.hold_energy == pytest.approx(4.0 - (last_held - start))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_a_release_never_shortens_another_grace(self, game, clock):
        """BR-008 / #817: hold never writes grace_until; the longer window stays."""
        fill(game, "p1", 2.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        p1 = game.players["p1"]
        p1.grace_until = clock.t + 5.0  # e.g. a grace granted elsewhere
        await step(game, clock, "p1", False)
        assert game.effective_grace_until(p1) == pytest.approx(p1.grace_until)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7")
    async def test_activation_span_carries_start_and_end_attributes(self, game, clock):
        """telemetry.md: game.shield_hold under the player span, outcome and cost at the end."""
        p1 = game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        hold_span = create_autospec(Span, instance=True)
        fill(game, "p1", 2.0)
        await arm(game, clock)
        with patch.object(base_module.tracer, "start_span", return_value=hold_span) as start_span:
            await step(game, clock, "p1", True)
            await step(game, clock, "p1", False)
            await step(game, clock, "p1", False, dt=0.4)  # past the minimum-burst tail
        assert start_span.call_args.args[0] == "game.shield_hold"
        assert start_span.call_args.kwargs["attributes"]["shield.energy_start_s"] == 2.0
        attrs = {c.args[0]: c.args[1] for c in hold_span.set_attribute.call_args_list}
        assert attrs["shield.hold_outcome"] == HOLD_OUTCOME_RELEASED
        assert attrs["shield.min_burst_applied"] is True
        hold_span.end.assert_called_once()


# --------------------------------------------------------------------------- #
# UC-030 BR-009: the release tail
# --------------------------------------------------------------------------- #
class TestUC030BR009ReleaseTail:
    async def _held_then_released(self, game, clock):
        fill(game, "p1", 2.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        for _ in range(30):
            await step(game, clock, "p1", True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-009"])
    async def test_a_single_release_jolt_does_not_kill(self, game, clock):
        """BR-009: the first unprotected frame primes with min(raw, warning) and kills nobody."""
        await self._held_then_released(game, clock)
        _, death = thresholds(game)
        await step(game, clock, "p1", False, accel=g_vector(death * 3))
        assert game.players["p1"].alive
        await step(game, clock, "p1", False)
        assert game.players["p1"].alive

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-009"])
    async def test_a_sustained_hard_movement_still_kills_when_the_ema_says(self, game, clock):
        """BR-009: after the capped prime the EMA runs as step 4."""
        await self._held_then_released(game, clock)
        warn, death = thresholds(game)
        raw = death * 1.4
        weight = game._ema_weight
        expected = None
        smoothed = min(raw, warn)
        for k in range(2, 40):
            smoothed = (smoothed * weight + raw) / (weight + 1)
            if smoothed > death:
                expected = k
                break
        assert expected is not None and expected > 1
        for k in range(1, expected + 1):
            assert game.players["p1"].alive, f"died early at unprotected frame {k - 1}"
            await step(game, clock, "p1", False, accel=g_vector(raw))
        assert not game.players["p1"].alive


class TestUC030Pre1:
    """UC-030 Pre-1: a RUNNING game, an alive player outside every grace window: the checks run."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", cond=["Pre-1"])
    async def test_alive_player_outside_grace_in_a_running_game_is_checked(self, game, clock):
        game.state = base_module.GameState.RUNNING
        player = game.players["p1"]
        assert player.alive
        assert player.grace_until <= clock.t
        assert player.hold_until <= clock.t
        _, death = thresholds(game)

        await step(game, clock, "p1", False, accel=g_vector(death * 3))

        assert not player.alive, "the death check did not run for an unprotected frame"


class TestUC030BR009OtherProtectionKeepsThePlainPrime:
    """BR-009 bullet 3: only hold caps the prime; other protection keeps the plain #757 prime."""

    @pytest.fixture
    def plain_game(self, clock, flags, events):
        flags.values["shield.activation"] = "trigger"
        g = _make_ffa(events)
        latch(g, clock)
        assert g._shield_hold is None
        with patch.object(g, "_play_sound", new_callable=AsyncMock):
            yield g

    async def _one_jolt_after(self, game, clock, protected_until):
        # Protected frames up to the last one before the window ends.
        while clock.t + DT < protected_until:
            await step(game, clock, "p1", False)
        player = game.players["p1"]
        assert player.alive
        assert not player.hold_prime_cap
        assert player.smoothed_accel == 0.0, "the last frame before the jolt was not protected"
        _, death = thresholds(game)
        # The first unprotected frame carries the jolt.
        await step(game, clock, "p1", False, accel=g_vector(death * 1.5))
        assert clock.t >= protected_until

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A1", br=["BR-009"])
    async def test_a_jolt_after_a_grace_window_kills(self, plain_game, clock):
        """A grace_until window ends: the raw sample primes the EMA and kills on the first frame."""
        plain_game.players["p1"].grace_until = clock.t + 0.5
        await self._one_jolt_after(plain_game, clock, plain_game.players["p1"].grace_until)
        assert not plain_game.players["p1"].alive

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-009"])
    async def test_a_jolt_after_a_shield_charge_activation_kills(self, plain_game, clock):
        """A shield-charge activation (A6) ends: the plain prime kills on the first unprotected frame."""
        player = plain_game.players["p1"]
        player.shield_charge_seconds = 0.5
        player.shield_charge_granted_at = clock.t
        player.shield_charge_layer_until = clock.t + 100.0
        await arm(plain_game, clock)
        await step(plain_game, clock, "p1", True)
        assert player.shield_charge_seconds == 0.0, "the charge was not activated"
        assert player.grace_until > clock.t
        await self._one_jolt_after(plain_game, clock, player.grace_until)
        assert not player.alive


# --------------------------------------------------------------------------- #
# UC-040 A36, A37: grants add energy; grants that add nothing are refused
# --------------------------------------------------------------------------- #
class TestUC040A36EnergyGranted:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-024", "BR-025"])
    async def test_a_grant_adds_energy_clamped_to_the_cap_without_protection(self, game, clock):
        """A36 step 1: +min(seconds, room); grace_until untouched."""
        fill(game, "p1", 3.0)
        assert await game.grant_shield("p1", 5.0) is True
        p1 = game.players["p1"]
        assert game._shield_hold_energy(p1, clock.t) == pytest.approx(5.0)
        assert p1.grace_until == 0.0
        assert not game.is_in_grace(p1, clock.t)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-024"])
    async def test_a_grant_is_clamped_to_the_budget_remaining(self, game, clock):
        """BR-024: energy never exceeds the budget remaining."""
        game.players["p1"].hold_budget_used = 13.0
        assert await game.grant_shield("p1", 5.0) is True
        assert game._shield_hold_energy(game.players["p1"], clock.t) == pytest.approx(2.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-024"])
    async def test_a_grant_during_a_hold_moves_the_deadline_out(self, game, clock):
        """A36 step 2."""
        fill(game, "p1", 1.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        start = clock.t
        await step(game, clock, "p1", True)
        assert await game.grant_shield("p1", 2.0) is True
        assert game.players["p1"].hold_until == pytest.approx(start + 3.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-024"])
    async def test_a_grant_in_the_tail_only_adds_to_the_pool(self, game, clock):
        """A36 step 2: in the minimum-burst tail the deadline stays."""
        fill(game, "p1", 1.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        start = clock.t
        await step(game, clock, "p1", False)
        assert await game.grant_shield("p1", 2.0) is True
        p1 = game.players["p1"]
        assert p1.hold_until == pytest.approx(start + 0.3)
        clock.t = start + 0.4
        await step(game, clock, "p1", False, dt=0.0)
        assert p1.hold_energy == pytest.approx(3.0 - 0.3)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-022"])
    async def test_an_applied_grant_counts_and_paces_from_the_grant(self, game, clock):
        """BR-022 in hold mode: a grant that added energy counts; cooldown runs from grant + seconds."""
        fill(game, "p1", 4.0)
        assert await game.grant_shield("p1", 3.0) is True  # partial add: +1 s
        assert game._agent_shield_grants["p1"] == 1
        assert game._agent_shield_last_end["p1"] == pytest.approx(clock.t + 3.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36", br=["BR-022"])
    async def test_the_cap_and_cooldown_still_refuse_after_the_pool_checks(self, game, clock):
        """BR-022 / A34: the shield limits apply in hold mode too."""
        with patch.object(BaseGameMode, "_shield_limits", return_value=(1, 0)):
            assert await game.grant_shield("p1", 1.0) is True
            with pytest.raises(InterventionNotApplied) as exc:
                await game.grant_shield("p1", 1.0)
        assert exc.value.reason == "shield_cap_reached"


class TestUC040A37NothingToAdd:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A37", br=["BR-024", "BR-022"])
    async def test_a_full_pool_refuses_and_counts_nothing(self, game, clock):
        """A37 step 2: shield_pool_full, shield_refused, not counted."""
        p1 = game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        fill(game, "p1", 4.98)
        with pytest.raises(InterventionNotApplied) as exc:
            await game.grant_shield("p1", 2.0)
        assert exc.value.reason == "shield_pool_full"
        assert "p1" not in game._agent_shield_grants
        event = p1.span.add_event.call_args
        assert event.args[0] == "shield_refused"
        assert event.kwargs["attributes"]["shield.refusal_reason"] == "shield_pool_full"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A37", br=["BR-024"])
    async def test_an_exhausted_budget_refuses_before_the_pool_check(self, game, clock):
        """A37 step 2: budget first; a pool that is also full still reads shield_budget_exhausted."""
        game.players["p1"].hold_budget_used = 14.8
        with pytest.raises(InterventionNotApplied) as exc:
            await game.grant_shield("p1", 2.0)
        assert exc.value.reason == "shield_budget_exhausted"
        assert "p1" not in game._agent_shield_grants

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A37")
    @pytest.mark.uc("UC-040", flow="A22")
    async def test_manager_records_the_reason_and_refunds_the_backstop(self):
        """A37 step 2 / A22: blocked=true with the reason; the reservation is refunded."""
        from services.game_coordinator.tests.event_list_helpers import edge_event
        from services.game_coordinator.tests.test_interventions import make_manager

        for reason in ("shield_pool_full", "shield_budget_exhausted"):
            mgr, published = make_manager(interventions={"audio_cue": edge_event("audio_cue", "beep")})

            async def not_applied(_ctx, reason=reason):
                raise InterventionNotApplied(reason)

            mgr.register_handler("audio_cue", not_applied)
            with patch("services.game_coordinator.metrics.interventions_total"):
                await mgr.evaluate_all()
            cue = [d for et, d in published if et == GameEvent.AGENT_INTERVENTION]
            assert cue[0]["blocked"] == "true"
            assert cue[0]["block_reason"] == reason
            assert mgr._rate_limiter.current_weight() == 0.0


# --------------------------------------------------------------------------- #
# UC-040 A38: passive recharge
# --------------------------------------------------------------------------- #
class TestUC040A38Recharge:
    @pytest.fixture
    def recharging(self, clock, flags, events):
        flags.values["shield.recharge_per_second"] = 0.5
        g = _make_ffa(events)
        latch(g, clock)
        with patch.object(g, "_play_sound", new_callable=AsyncMock):
            yield g

    @pytest.mark.uc("UC-040", flow="A38", br=["BR-025"])
    def test_recharge_is_a_function_of_time_up_to_the_cap(self, recharging, clock):
        p1 = recharging.players["p1"]
        assert recharging._shield_hold_energy(p1, clock.t + 3.0) == pytest.approx(1.5)
        assert recharging._shield_hold_energy(p1, clock.t + 7.3) == pytest.approx(3.65)
        assert recharging._shield_hold_energy(p1, clock.t + 60.0) == pytest.approx(5.0)

    @pytest.mark.uc("UC-040", flow="A38", br=["BR-024", "BR-025"])
    def test_recharge_stops_at_the_budget_remaining(self, recharging, clock):
        recharging.players["p1"].hold_budget_used = 13.0
        assert recharging._shield_hold_energy(recharging.players["p1"], clock.t + 60.0) == pytest.approx(2.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A38", br=["BR-025"])
    async def test_no_recharge_while_active_and_it_restarts_at_the_end(self, recharging, clock):
        p1 = recharging.players["p1"]
        clock.t += 4.0  # 2 s recharged
        await arm(recharging, clock)
        await step(recharging, clock, "p1", True, dt=0.0)
        start = clock.t
        e0 = p1.hold_energy_start
        clock.t = start + 1.0
        await step(recharging, clock, "p1", True, dt=0.0)
        assert recharging._shield_hold_energy(p1, clock.t) == pytest.approx(e0 - 1.0)
        await step(recharging, clock, "p1", False, dt=0.0)
        assert recharging._shield_hold_energy(p1, start + 3.0) == pytest.approx(e0 - 1.0 + 0.5 * 2.0)

    @pytest.mark.uc("UC-040", flow="A38", br=["BR-025"])
    def test_zero_rate_never_recharges(self, game, clock):
        assert game._shield_hold_energy(game.players["p1"], clock.t + 600.0) == 0.0

    @pytest.mark.uc("UC-040", flow="A38", br=["BR-025"])
    def test_no_recharge_before_the_game_loop_starts(self, clock, flags, events):
        flags.values["shield.recharge_per_second"] = 0.5
        g = _make_ffa(events)
        g._latch_shield_hold()  # a grant latched it during the countdown
        assert g._shield_hold_energy(g.players["p1"], clock.t + 10.0) == 0.0


# --------------------------------------------------------------------------- #
# UC-040 A39: the pool ends
# --------------------------------------------------------------------------- #
class TestUC040A39EnergyEnds:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-024"])
    async def test_holding_until_the_budget_runs_out_ends_budget_exhausted(self, game, clock, counter):
        """A39 step 1: outcome budget_exhausted and the span event once; grants are refused after it."""
        p1 = game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        p1.hold_budget_used = 13.0
        fill(game, "p1", 2.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        clock.t = p1.hold_until
        await step(game, clock, "p1", True, dt=0.0)
        assert outcomes(counter) == [HOLD_OUTCOME_BUDGET_EXHAUSTED]
        names = [c.args[0] for c in p1.span.add_event.call_args_list]
        assert names.count("shield_budget_exhausted") == 1
        with pytest.raises(InterventionNotApplied):
            await game.grant_shield("p1", 2.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-020"])
    async def test_eliminate_player_kills_a_holder_and_drops_the_pool(self, game, clock, counter):
        """A39 step 2: the kill path ignores the hold, ends it with death and drops the pool."""
        fill(game, "p1", 3.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        assert await game._kill_player("p1", 0.0, reason="agent_intervention") is True
        p1 = game.players["p1"]
        assert not p1.alive
        assert outcomes(counter) == [HOLD_OUTCOME_DEATH]
        assert p1.hold_energy == 0.0 and p1.hold_until == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39")
    async def test_energy_granted_while_the_kill_awaits_is_dropped(self, game, clock):
        """A39 step 2: the second drop after the kill path's awaits."""
        fill(game, "p1", 2.0)

        async def sound_then_grant(*_a, **_kw):
            await game.grant_shield("p1", 2.0)

        game._play_sound.side_effect = sound_then_grant
        assert await game._kill_player("p1", 0.0) is True
        assert game.players["p1"].hold_energy == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39")
    async def test_disconnect_drops_the_pool(self, game, clock, counter):
        fill(game, "p1", 3.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        await game._handle_disconnect("p1")
        assert outcomes(counter) == [HOLD_OUTCOME_DEATH]
        assert game.players["p1"].hold_energy == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39")
    async def test_game_end_drops_every_pool(self, game, clock, counter):
        fill(game, "p1", 3.0)
        fill(game, "p2", 1.0)
        await arm(game, clock)
        await step(game, clock, "p1", True)
        await game._end_shield_feedback()
        assert outcomes(counter) == [HOLD_OUTCOME_GAME_END]
        assert all(p.hold_energy == 0.0 and p.hold_started_at == 0.0 for p in game.players.values())

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39")
    async def test_error_teardown_ends_the_open_hold_span(self, game, clock):
        p1 = game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        hold_span = create_autospec(Span, instance=True)
        fill(game, "p1", 3.0)
        await arm(game, clock)
        with patch.object(base_module.tracer, "start_span", return_value=hold_span):
            await step(game, clock, "p1", True)
        game._close_shield_charge_spans()
        hold_span.end.assert_called_once()
        assert p1.hold_started_at == 0.0


# --------------------------------------------------------------------------- #
# UC-040 BR-024: the budget bounds every schedule
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.uc("UC-040", br=["BR-024", "BR-025"])
@pytest.mark.parametrize("seed", range(6))
async def test_protected_time_never_exceeds_the_budget(clock, flags, events, counter, seed):
    """Seeded tap / hold schedules at the maximum recharge with grants: protection <= budget.

    Protection is sampled per frame and attributed forward ([t, t + DT)), so the
    sample over-reads by at most one frame per activation.
    """
    flags.values["shield.recharge_per_second"] = 0.5
    flags.values["shield.energy_initial_seconds"] = 5.0
    g = _make_ffa(events)
    latch(g, clock)
    rng = random.Random(seed)
    p1 = g.players["p1"]
    protected = 0.0
    pressed = False
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        await arm(g, clock)
        for _ in range(60 * 90):  # 90 s of play
            if rng.random() < 0.08:
                pressed = not pressed
            if rng.random() < 0.002:
                with contextlib.suppress(InterventionNotApplied):  # a full pool or a used-up budget (A37)
                    await g.grant_shield("p1", 3.0)
            await step(g, clock, "p1", pressed)
            if g.is_in_grace(p1, clock.t):
                protected += DT
    activations = len(outcomes(counter)) + (1 if p1.hold_started_at else 0)
    assert activations > 3
    assert p1.hold_budget_used <= 15.0 + 1e-9
    assert protected <= 15.0 + activations * DT + 1e-9


# --------------------------------------------------------------------------- #
# UC-040 BR-027 and BR-019: latched per game, Free-for-All real games only
# --------------------------------------------------------------------------- #
class TestUC040BR027Latch:
    @pytest.mark.uc("UC-040", br=["BR-027", "BR-024"])
    def test_latch_reads_the_flags_once_and_clamps_them(self, clock, flags, events):
        flags.values.update(
            {
                "shield.energy_max_seconds": 50.0,
                "shield.energy_budget_seconds": 0,
                "shield.energy_initial_seconds": 4.0,
                "shield.min_burst_seconds": "fast",
            }
        )
        g = _make_ffa(events)
        params = g._latch_shield_hold()
        assert params.cap == 10.0
        assert params.budget == 3.0
        assert params.min_burst == 0.3
        assert g.players["p1"].hold_energy == 3.0, "starting energy clamped to the budget"
        reads = len(flags.reads)
        assert g._latch_shield_hold() is params
        assert len(flags.reads) == reads

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-027"])
    async def test_a_flag_flip_mid_game_changes_nothing(self, game, clock, flags):
        flags.values["shield.activation"] = "instant"
        flags.values["shield.energy_max_seconds"] = 1.0
        assert await game.grant_shield("p1", 4.0) is True
        assert game.players["p1"].grace_until == 0.0
        assert game._shield_hold_energy(game.players["p1"], clock.t) == pytest.approx(4.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-027", "BR-019"])
    async def test_hold_read_in_a_game_latched_to_instant_stays_instant(self, clock, flags, events):
        flags.values["shield.activation"] = "instant"
        g = _make_ffa(events)
        latch(g, clock)
        assert g._shield_hold is None
        flags.values["shield.activation"] = "hold"
        with patch.object(g, "_play_sound", new_callable=AsyncMock):
            assert await g.grant_shield("p1", 4.0) is True
        assert g.players["p1"].grace_until == pytest.approx(clock.t + 4.0)
        assert g.players["p1"].shield_charge_seconds == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-027", "BR-019"])
    async def test_hold_read_in_a_game_latched_to_trigger_keeps_trigger(self, clock, flags, events):
        """BR-027: a running trigger game keeps trigger when the flag flips to hold mid-game."""
        flags.values["shield.activation"] = "trigger"
        g = _make_ffa(events)
        latch(g, clock)
        flags.values["shield.activation"] = "hold"
        with patch.object(g, "_play_sound", new_callable=AsyncMock):
            assert await g.grant_shield("p1", 4.0) is True
        assert g.players["p1"].shield_charge_seconds == 4.0
        assert g.players["p1"].grace_until == 0.0

    @pytest.mark.uc("UC-040", br=["BR-019"])
    def test_hold_is_a_free_for_all_allow_list(self):
        allowed = {cls for cls in _GAME_CLASSES.values() if cls.SHIELD_TRIGGER_ALLOWED}
        assert allowed == {FFAGame}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-019"])
    async def test_another_mode_with_the_hold_flag_gets_the_instant_shield(self, clock, flags, events):
        from services.game_coordinator.games.nonstop_joust import NonstopJoustGame

        g = _make_ffa(events, NonstopJoustGame)
        assert g._latch_shield_hold() is None
        with patch.object(g, "_play_sound", new_callable=AsyncMock):
            assert await g.grant_shield("p1", 4.0) is True
        assert g.players["p1"].grace_until == pytest.approx(clock.t + 4.0)

    @pytest.mark.uc("UC-040", br=["BR-019"])
    def test_a_shadow_game_is_never_a_hold_game(self, clock, flags, events):
        g = _make_ffa(events)
        g.game_kind = "shadow"
        assert g._latch_shield_hold() is None
        assert "shield.activation" not in flags.reads

    @pytest.mark.uc("UC-040", br=["BR-019"])
    def test_resolvers(self):
        assert resolve_shield_activation(" Hold ") == "hold"
        assert resolve_shield_activation("bogus") == "instant"
        assert resolve_hold_value(float("nan"), 5.0, 1.0, 10.0) == 5.0
        assert resolve_hold_value(True, 5.0, 1.0, 10.0) == 5.0
        assert resolve_hold_value(0.05, 0.3, 0.1, 1.0) == 0.1
        assert set(SHIELD_HOLD_FLAGS) == {
            "shield.energy_max_seconds",
            "shield.energy_budget_seconds",
            "shield.energy_initial_seconds",
            "shield.recharge_per_second",
            "shield.min_burst_seconds",
        }

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_hold_frames_await_nothing(self, game, clock):
        """BR-008: the frame path sends nothing for a hold transition (no stream write, no sound)."""
        fill(game, "p1", 3.0)
        stream = create_autospec(RecordingStream, instance=True, spec_set=True)
        game.gameplay_stream = stream
        await arm(game, clock)
        for pressed in (True, True, False, True, False):
            await step(game, clock, "p1", pressed, dt=0.4)
        stream.write.assert_not_called()
        game._play_sound.assert_not_called()
