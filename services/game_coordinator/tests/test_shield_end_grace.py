"""
Shield end grace (#2464): a short window after a shield ends in which movement cannot kill.

UC-040 BR-033 (the window: when it opens, no stacking, death, latch), A39 step 1
(hold runs out), A16 (instant shield), A30 / UC-030 A6 (activated charge);
UC-030 A1 (the frame gate), BR-009 (the first unprotected frame after a window
primes the EMA with min(raw, warning)).

Every test runs on a fake clock with the flags patched; the game latches its own
values through ``_latch_shield_hold``, as at the start of the game loop.
"""

import dataclasses
import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from opentelemetry.trace import Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import (
    DEFAULT_SHIELD_END_GRACE_SECONDS,
    HOLD_OUTCOME_EMPTY,
    BaseGameMode,
    Player,
    resolve_end_grace,
)
from services.game_coordinator.games.ffa import FFAGame

T0 = 1000.0
DT = 1.0 / 60.0
IDLE = (0.0, 0.0, 1.0)
G = 0.75


@pytest.fixture(autouse=True)
def _no_shield_limits():
    """Shield limits off (UC-040 BR-022): these tests grant more than once."""
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
    """The shield flags; any other key resolves to the caller's default. Records each read's kwargs."""

    def __init__(self):
        self.values = {
            "shield.activation": "hold",
            "shield.energy_max_seconds": 5.0,
            "shield.energy_budget_seconds": 15.0,
            "shield.energy_initial_seconds": 0.0,
            "shield.recharge_per_second": 0.0,
            "shield.min_burst_seconds": 0.3,
            "shield.end_grace_seconds": G,
        }
        self.reads: list[tuple[str, dict]] = []

    def read(self, _set, key, default, **kw):
        self.reads.append((key, kw))
        return self.values.get(key, default)


@pytest.fixture
def clock():
    c = Clock()
    with (
        patch.object(base_module.time, "time", side_effect=lambda: c.t),
        patch.object(game_clock, "now", side_effect=lambda: c.t),
    ):
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
        game_id="test_shield_end_grace",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


def latch(game, clock):
    params = game._latch_shield_hold()
    game._start_shield_hold_clock(clock.t)
    return params


@pytest.fixture
def game(clock, flags, events):  # noqa: ARG001 - flags patch base for the test
    """A hold game (FFA, ``shield.activation`` = hold)."""
    g = _make_ffa(events)
    latch(g, clock)
    assert g._shield_hold is not None
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


def _plain_game(clock, flags, events, mode):
    flags.values["shield.activation"] = mode
    g = _make_ffa(events)
    latch(g, clock)
    assert g._shield_hold is None
    return g


@pytest.fixture
def instant_game(clock, flags, events):
    g = _plain_game(clock, flags, events, "instant")
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


@pytest.fixture
def trigger_game(clock, flags, events):
    g = _plain_game(clock, flags, events, "trigger")
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


def frame(serial, trigger=False, accel=IDLE):
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=accel[0], y=accel[1], z=accel[2]),
        trigger_pressed=trigger,
    )


async def step(game, clock, serial, trigger, accel=IDLE, dt=DT):
    clock.t += dt
    await game._process_controller_state(frame(serial, trigger, accel))


def g_vector(magnitude):
    return (0.0, 0.0, magnitude)


def hard(game):
    """A sustained movement above the death threshold: kills a few frames after any prime."""
    _, death = game._compute_effective_thresholds(game.players["p1"])
    return g_vector(death * 1.4)


def frames_to_death_after_capped_prime(game, magnitude):
    """UC-030 BR-009: the unprotected frame (1 = the prime) on which a constant ``magnitude`` kills."""
    warn, death = game._compute_effective_thresholds(game.players["p1"])
    weight = game._ema_weight
    smoothed = min(magnitude, warn)
    for k in range(2, 60):
        smoothed = (smoothed * weight + magnitude) / (weight + 1)
        if smoothed > death:
            return k
    raise AssertionError("never kills")


async def hold_until_empty(game, clock, energy=1.0, accel=IDLE):
    """Arm, then hold the trigger until the energy runs out; returns the end (``hold_until``)."""
    p1 = game.players["p1"]
    p1.hold_energy = energy
    await step(game, clock, "p1", False)
    await step(game, clock, "p1", True, accel=accel)
    ended = p1.hold_until
    while clock.t < ended:
        await step(game, clock, "p1", True, accel=accel)
    assert p1.hold_started_at == 0.0, "the activation did not end"
    return ended


async def move_through_window(game, clock, until, accel, trigger=False):
    """Frames with ``accel`` while the window is open; the player must stay alive."""
    p1 = game.players["p1"]
    while clock.t + DT < until:
        await step(game, clock, "p1", trigger, accel=accel)
        assert p1.alive, f"died inside the end grace window at t={clock.t - until:+.3f}"


async def assert_dies_after_capped_prime(game, clock, accel, trigger=False, done=0):
    """The first unprotected frame primes (BR-009); the EMA then kills as UC-030 step 4.

    ``done``: unprotected frames with ``accel`` already processed (the frame that ended the shield).
    """
    p1 = game.players["p1"]
    expected = frames_to_death_after_capped_prime(game, accel[2])
    for k in range(done + 1, expected):
        await step(game, clock, "p1", trigger, accel=accel)
        assert p1.alive, f"died on unprotected frame {k}, expected frame {expected} (BR-009 prime)"
    await step(game, clock, "p1", trigger, accel=accel)
    assert not p1.alive, "a sustained hard movement after the window must still kill"


# --------------------------------------------------------------------------- #
# BR-033: a window after the energy runs out, an instant expiry and a charge expiry
# --------------------------------------------------------------------------- #
class TestUC040BR033WindowOpens:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-009"])
    async def test_hold_runs_out_while_moving_hard_then_the_window_then_the_prime(self, game, clock):
        """A39 step 1: no death for G, none on the first frame after it (prime), death a few frames later."""
        accel = hard(game)
        ended = await hold_until_empty(game, clock, accel=accel)
        p1 = game.players["p1"]
        assert p1.end_grace_until == pytest.approx(ended + G)
        await move_through_window(game, clock, ended + G, accel, trigger=True)
        await assert_dies_after_capped_prime(game, clock, accel, trigger=True)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    async def test_budget_exhausted_opens_the_window_too(self, game, clock, flags):  # noqa: ARG002
        """``budget_exhausted`` is a run-out like ``empty``."""
        game._shield_hold = dataclasses.replace(game._shield_hold, budget=1.0)
        ended = await hold_until_empty(game, clock, energy=3.0)
        assert game.players["p1"].end_grace_until == pytest.approx(ended + G)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-033"])
    async def test_an_instant_shield_expiry_gets_a_window(self, instant_game, clock):
        """A16: the instant shield's end is known at the grant, so the window follows it."""
        assert await instant_game.grant_shield("p1", 1.0) is True
        p1 = instant_game.players["p1"]
        shield_end = p1.grace_until
        assert p1.end_grace_until == pytest.approx(shield_end + G)
        accel = hard(instant_game)
        await move_through_window(instant_game, clock, shield_end + G, accel)
        await assert_dies_after_capped_prime(instant_game, clock, accel)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-033"])
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-009"])
    async def test_an_activated_charge_expiry_gets_a_window(self, trigger_game, clock):
        """UC-030 A6: the activated charge's protection ends, then the window."""
        p1 = trigger_game.players["p1"]
        p1.shield_charge_seconds = 0.5
        p1.shield_charge_granted_at = clock.t
        p1.shield_charge_layer_until = clock.t + 100.0
        await step(trigger_game, clock, "p1", False)
        await step(trigger_game, clock, "p1", True)
        assert p1.shield_charge_seconds == 0.0, "the charge was not activated"
        assert p1.end_grace_until == pytest.approx(p1.grace_until + G)
        accel = hard(trigger_game)
        await move_through_window(trigger_game, clock, p1.grace_until + G, accel)
        await assert_dies_after_capped_prime(trigger_game, clock, accel)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-033"])
    async def test_windows_never_stack(self, instant_game, clock):
        """No stacking: a new shield end assigns the window, it never adds to the one left."""
        assert await instant_game.grant_shield("p1", 1.0) is True
        p1 = instant_game.players["p1"]
        clock.t = p1.grace_until + 0.5  # inside the first window
        assert await instant_game.grant_shield("p1", 1.0) is True
        assert p1.end_grace_until == pytest.approx(clock.t + 1.0 + G)


# --------------------------------------------------------------------------- #
# BR-033: inside the window the player can act at once
# --------------------------------------------------------------------------- #
class TestUC040BR033ActInsideTheWindow:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
    async def test_a_re_hold_inside_the_window_starts_at_once_and_clears_it(self, game, clock):
        """Release, then press inside the window: the activation starts on that frame; the window is gone."""
        await hold_until_empty(game, clock)
        p1 = game.players["p1"]
        assert clock.t < p1.end_grace_until, "precondition: the window is open"
        p1.hold_energy = 1.0  # e.g. a grant landed in the window
        await step(game, clock, "p1", False)
        await step(game, clock, "p1", True)
        assert p1.hold_started_at == pytest.approx(clock.t), "the re-hold was deferred"
        assert p1.end_grace_until == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-033"])
    @pytest.mark.uc("UC-030", flow="A6")
    async def test_a_charge_press_inside_the_window_activates(self, trigger_game, clock):
        """A charge granted in the window and pressed in it activates; it is not ignored as 'in grace'."""
        assert await trigger_game.grant_shield("p1", 0.5) is True
        p1 = trigger_game.players["p1"]
        await step(trigger_game, clock, "p1", False)
        await step(trigger_game, clock, "p1", True)
        clock.t = p1.grace_until + 0.1
        assert clock.t < p1.end_grace_until, "precondition: inside the window of the first charge"
        assert await trigger_game.grant_shield("p1", 2.0) is True
        assert p1.shield_charge_seconds == 2.0
        await step(trigger_game, clock, "p1", False)
        await step(trigger_game, clock, "p1", True)
        assert p1.shield_charge_seconds == 0.0, "the press inside the window did not activate the charge"
        assert p1.grace_until == pytest.approx(clock.t + 2.0)
        assert p1.end_grace_until == pytest.approx(p1.grace_until + G)


# --------------------------------------------------------------------------- #
# BR-033: what never opens a window
# --------------------------------------------------------------------------- #
class TestUC040BR033NoWindow:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-033"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-009"])
    async def test_a_voluntary_release_gets_no_window(self, game, clock):
        """``released`` (the minimum-burst tail included): BR-009 only, so a hard movement kills on time."""
        p1 = game.players["p1"]
        p1.hold_energy = 2.0
        await step(game, clock, "p1", False)
        await step(game, clock, "p1", True)
        await step(game, clock, "p1", False)  # a tap: the minimum-burst tail runs
        tail_end = p1.hold_until
        while clock.t + DT < tail_end:
            await step(game, clock, "p1", False)
        accel = hard(game)
        await step(game, clock, "p1", False, accel=accel)  # the tail ends: the first unprotected frame
        assert p1.hold_started_at == 0.0
        assert p1.end_grace_until == 0.0
        await assert_dies_after_capped_prime(game, clock, accel, done=1)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-033"])
    async def test_toughness_spawn_and_startup_grace_open_no_window(self, instant_game, clock):
        p1 = instant_game.players["p1"]
        await instant_game.grant_toughness_boost("p1", 2.0)
        assert p1.end_grace_until == 0.0
        p1.grace_until = clock.t + 0.3  # the startup guard (UC-030 BR-003)
        clock.t += 1.0
        await step(instant_game, clock, "p1", False)
        assert p1.end_grace_until == 0.0
        await instant_game._kill_player("p1", 0.0, reason="agent_intervention")
        assert await instant_game.revive_player("p1", spawn_grace=1.0) is True
        assert p1.end_grace_until == 0.0
        clock.t = p1.grace_until + DT
        await step(instant_game, clock, "p1", False, accel=g_vector(10.0))
        assert not p1.alive, "the spawn grace ended without a window"


# --------------------------------------------------------------------------- #
# BR-033: G = 0, the latch, the death
# --------------------------------------------------------------------------- #
class TestUC040BR033LatchAndDeath:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    @pytest.mark.uc("UC-030", flow="A7", br=["BR-009"])
    async def test_g_zero_is_today(self, clock, flags, events):
        """G = 0: no window; the release tail's prime and the EMA kill as before #2464."""
        flags.values["shield.end_grace_seconds"] = 0.0
        g = _make_ffa(events)
        latch(g, clock)
        with patch.object(g, "_play_sound", new_callable=AsyncMock):
            accel = hard(g)
            await hold_until_empty(g, clock, accel=accel)
            assert g.players["p1"].end_grace_until == 0.0
            # The frame that ended the hold was already the first unprotected one.
            await assert_dies_after_capped_prime(g, clock, accel, trigger=True, done=1)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-033", "BR-027"])
    async def test_a_flag_flip_mid_game_changes_nothing(self, game, clock, flags):
        flags.values["shield.end_grace_seconds"] = 0.0
        ended = await hold_until_empty(game, clock)
        assert game.players["p1"].end_grace_until == pytest.approx(ended + G)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    async def test_eliminate_in_the_window_kills_and_a_respawn_starts_without_one(self, game, clock):
        await hold_until_empty(game, clock)
        p1 = game.players["p1"]
        assert clock.t < p1.end_grace_until
        assert await game._kill_player("p1", 0.0, reason="agent_intervention") is True
        assert not p1.alive
        assert p1.end_grace_until == 0.0
        assert await game.revive_player("p1", spawn_grace=0.5) is True
        assert p1.end_grace_until == 0.0

    @pytest.mark.uc("UC-040", br=["BR-033", "BR-027"])
    def test_every_mode_and_a_shadow_game_latch_it_in_the_game_context(self, clock, flags, events):
        """Latched in every branch of the latch (a shadow game is never a hold game), with the game id."""
        flags.values["shield.end_grace_seconds"] = 1.5
        g = _make_ffa(events)
        g.game_kind = "shadow"
        assert g._latch_shield_hold() is None
        assert g._shield_end_grace == 1.5
        reads = [kw for key, kw in flags.reads if key == "shield.end_grace_seconds"]
        assert reads == [{"game_id": g.game_id}]
        g._latch_shield_hold()
        assert len([k for k, _ in flags.reads if k == "shield.end_grace_seconds"]) == 1

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.75, 0.75),
            (0, 0.0),
            (1.5, 1.5),
            (5.0, 2.0),
            (-1.0, DEFAULT_SHIELD_END_GRACE_SECONDS),
            (float("nan"), DEFAULT_SHIELD_END_GRACE_SECONDS),
            (float("inf"), DEFAULT_SHIELD_END_GRACE_SECONDS),
            ("0.5", DEFAULT_SHIELD_END_GRACE_SECONDS),
            (True, DEFAULT_SHIELD_END_GRACE_SECONDS),
        ],
    )
    @pytest.mark.uc("UC-040", br=["BR-033"])
    def test_resolver(self, raw, expected):
        assert DEFAULT_SHIELD_END_GRACE_SECONDS == 0.75
        assert resolve_end_grace(raw) == expected


# --------------------------------------------------------------------------- #
# Telemetry (telemetry.md §4, §6)
# --------------------------------------------------------------------------- #
class TestUC040BR033Telemetry:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    async def test_the_hold_span_carries_the_window(self, game, clock):
        p1 = game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        hold_span = create_autospec(Span, instance=True)
        with patch.object(base_module.tracer, "start_span", return_value=hold_span):
            await hold_until_empty(game, clock)
        attrs = {c.args[0]: c.args[1] for c in hold_span.set_attribute.call_args_list}
        assert attrs["shield.hold_outcome"] == HOLD_OUTCOME_EMPTY
        assert attrs["shield.end_grace_s"] == G

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    async def test_a_released_hold_span_carries_zero(self, game, clock):
        p1 = game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        hold_span = create_autospec(Span, instance=True)
        p1.hold_energy = 2.0
        with patch.object(base_module.tracer, "start_span", return_value=hold_span):
            await step(game, clock, "p1", False)
            await step(game, clock, "p1", True)
            await step(game, clock, "p1", False)
            await step(game, clock, "p1", False, dt=0.4)
        attrs = {c.args[0]: c.args[1] for c in hold_span.set_attribute.call_args_list}
        assert attrs["shield.end_grace_s"] == 0.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-033"])
    async def test_shield_granted_carries_the_window(self, instant_game):
        p1 = instant_game.players["p1"]
        p1.span = create_autospec(Span, instance=True)
        assert await instant_game.grant_shield("p1", 1.0) is True
        granted = [c for c in p1.span.add_event.call_args_list if c.args[0] == "shield_granted"]
        assert granted[0].kwargs["attributes"]["shield.end_grace_s"] == G

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-033"])
    async def test_player_death_carries_the_time_since_the_shield_ended(self, game, clock):
        ended = await hold_until_empty(game, clock)
        p1 = game.players["p1"]
        span = create_autospec(Span, instance=True)
        p1.span = span
        clock.t = ended + 1.2
        with patch.object(base_module, "inject_trace_context", return_value=("", "")):
            assert await game._kill_player("p1", 0.0, reason="agent_intervention") is True
        death = [c for c in span.add_event.call_args_list if c.args[0] == "player_death"]
        assert death[0].kwargs["attributes"]["shield.since_end_ms"] == 1200

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", br=["BR-033"])
    async def test_player_death_omits_it_three_seconds_after_the_end(self, game, clock):
        ended = await hold_until_empty(game, clock)
        p1 = game.players["p1"]
        span = create_autospec(Span, instance=True)
        p1.span = span
        clock.t = ended + 3.0
        with patch.object(base_module, "inject_trace_context", return_value=("", "")):
            assert await game._kill_player("p1", 0.0) is True
        death = [c for c in span.add_event.call_args_list if c.args[0] == "player_death"]
        assert "shield.since_end_ms" not in death[0].kwargs["attributes"]

    @pytest.mark.uc("UC-040", br=["BR-033", "BR-027"])
    def test_the_setting_gauge_carries_it_in_a_hold_game(self, clock, flags, events):  # noqa: ARG002
        with patch.object(base_module.metrics, "shield_hold_setting") as gauge:
            g = _make_ffa(events)
            latch(g, clock)
        settings = {c.kwargs["setting"]: c for c in gauge.labels.call_args_list}
        assert "shield.end_grace_seconds" in settings
        values = [c.args[0] for c in gauge.labels.return_value.set.call_args_list]
        assert G in values
