"""
Unit tests for the InterventionManager core (#730, PR A).

Covers the enforcement chain (allowed-membership, weighted rate limit, battery
guard, mode-capability matrix), nonce exactly-once semantics for edge-triggered
flags, state-shaped change detection, and registry dispatch. Handlers are
no-op stubs in PR A; tests assert dispatch + metric/event emission, not game
effects.

Telemetry is disabled via conftest; the intervention metric is patched per the
repo metric-testing convention.
"""

import itertools
from unittest.mock import AsyncMock, patch

import pytest

from lib.types import GameEvent
from services.game_coordinator.interventions import (
    BLOCK_LOW_BATTERY,
    BLOCK_MODE_UNSUPPORTED,
    BLOCK_NOT_ALLOWED,
    BLOCK_RATE_LIMITED,
    INTERVENTION_SPECS,
    WEIGHT_HARD,
    WEIGHT_MEDIUM,
    WEIGHT_SOFT,
    InterventionManager,
    _edge_target_serial,
    _is_none_value,
    _RateLimiter,
    _split_nonce,
)

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeFlagClient:
    """In-memory flag client matching the OpenFeature get_*_value surface."""

    def __init__(self, values: dict | None = None):
        self.values = values or {}

    def set(self, key, value):
        self.values[key] = value

    def get_string_value(self, key, default, _ctx=None):
        return str(self.values.get(key, default))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_object_value(self, key, default, _ctx=None):
        return self.values.get(key, default)


class FakePlayer:
    def __init__(self, serial, alive=True):
        self.serial = serial
        self.alive = alive
        self.sensitivity_factor = 1.0


class FakeGame:
    def __init__(self, name, players=None):
        self._name = name
        # Player-targeted state flags (shield_seconds, player_sensitivity_factor)
        # are resolved per active serial, so tests exercising them need players.
        self.players = players or {}

    def get_game_name(self):
        return self._name


def make_manager(
    *,
    interventions=None,
    allowed=None,
    battery=None,
    game=None,
    backstop=60,
    threshold=20,
    objectives=None,
    time_fn=None,
):
    """Build a manager wired with fake clients, bypassing start().

    ``backstop`` is the coordinator's GLOBAL BACKSTOP budget (#919) — the
    ``policy.coordinator_backstop_per_minute`` flag that ``_refresh_budget``
    reads. It is deliberately generous by default (60) so normal multi-session
    traffic never trips it; tests that exercise the backstop set it low. NOTE the
    coordinator no longer reads ``policy.max_interventions_per_minute`` — that is
    the agent's authoritative per-game budget and is set here only so the value
    is present for any code/tests that still inspect it.
    """
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    agent_values = {
        "interventions_allowed": list(ALL_TYPES) if allowed is None else allowed,
        # Agent-owned per-game budget (NOT read by the coordinator backstop).
        "policy.max_interventions_per_minute": 2,
        # Coordinator-owned generous global backstop (what _refresh_budget reads).
        "policy.coordinator_backstop_per_minute": backstop,
        "policy.battery_threshold": threshold,
    }
    if objectives is not None:
        agent_values["objectives"] = objectives

    mgr = InterventionManager(
        event_publisher=publisher,
        get_game=lambda: game,
        battery_provider=(lambda s: battery.get(s)) if battery is not None else None,
        time_fn=time_fn or _mono(),
    )
    mgr._interventions_client = FakeFlagClient(interventions or {})
    mgr._agent_client = FakeFlagClient(agent_values)
    mgr._rate_limiter.set_budget(float(backstop))
    return mgr, events


def _mono():
    """Deterministic monotonic clock starting at 1000.0; advance via .tick."""
    counter = itertools.count()

    def fn():
        return 1000.0 + next(counter) * 0.0

    return fn


class Clock:
    """Manually-advanceable clock."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_split_nonce():
    assert _split_nonce("5:abc") == ("5", "abc")
    assert _split_nonce("7") == ("7", "")
    assert _split_nonce("") == ("", "")
    assert _split_nonce("9:a:b") == ("9", "a:b")


def test_edge_target_serial():
    elim = next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player")
    eff = next(s for s in INTERVENTION_SPECS if s.flag_key == "controller_effect")
    cue = next(s for s in INTERVENTION_SPECS if s.flag_key == "audio_cue")
    assert _edge_target_serial(elim, "ctrl_1") == "ctrl_1"
    assert _edge_target_serial(eff, "ctrl_2:rumble") == "ctrl_2"
    assert _edge_target_serial(eff, ":rumble") is None  # broadcast
    assert _edge_target_serial(cue, "sound_42") is None  # not player-targeted


def test_is_none_value():
    shield = next(s for s in INTERVENTION_SPECS if s.flag_key == "shield_seconds")
    factor = next(s for s in INTERVENTION_SPECS if s.flag_key == "player_sensitivity_factor")
    assert _is_none_value(shield, 0)
    assert _is_none_value(shield, 0.0)
    assert not _is_none_value(shield, 5)
    assert _is_none_value(factor, 1.0)
    assert not _is_none_value(factor, 1.5)


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #
def test_rate_limiter_weighted_budget():
    clock = Clock()
    rl = _RateLimiter(budget=2.0, time_fn=clock)
    assert rl.check(WEIGHT_SOFT)  # 0.5 -> 0.5
    assert rl.check(WEIGHT_MEDIUM)  # +1.0 -> 1.5
    assert rl.check(WEIGHT_SOFT)  # +0.5 -> 2.0
    assert not rl.check(WEIGHT_SOFT)  # would be 2.5 > 2.0
    assert rl.current_weight() == pytest.approx(2.0)


def test_rate_limiter_hard_exhausts_budget():
    rl = _RateLimiter(budget=2.0, time_fn=Clock())
    assert rl.check(WEIGHT_HARD)  # 2.0 -> exactly budget
    assert not rl.check(WEIGHT_SOFT)  # nothing left


def test_rate_limiter_window_slide():
    clock = Clock()
    rl = _RateLimiter(budget=2.0, time_fn=clock)
    assert rl.check(WEIGHT_HARD)
    assert not rl.check(WEIGHT_SOFT)
    clock.advance(61.0)  # old event falls out of the 60s window
    assert rl.check(WEIGHT_HARD)


# --------------------------------------------------------------------------- #
# Nonce exactly-once (edge-triggered)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_nonce_same_value_applies_once():
    mgr, events = make_manager(interventions={"eliminate_player": "1:ctrl_a"})
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same nonce -> no second apply
    applied = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert len(applied) == 1
    assert applied[0]["target"] == "ctrl_a"
    assert applied[0]["blocked"] == "false"


@pytest.mark.asyncio
async def test_nonce_change_applies_again():
    mgr, events = make_manager(interventions={"eliminate_player": "1:ctrl_a"})
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        mgr._interventions_client.set("eliminate_player", "2:ctrl_b")  # new nonce
        await mgr.evaluate_all()
    applied = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert len(applied) == 2
    assert applied[1]["target"] == "ctrl_b"


@pytest.mark.asyncio
async def test_nonce_reconnect_reread_no_reapply():
    """Priming the baseline (startup / flagd reconnect) must not re-fire."""
    mgr, events = make_manager(interventions={"eliminate_player": "1:ctrl_a"})
    mgr._prime_baseline()  # simulates startup baseline capture
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # re-read after reconnect, same nonce
    applied = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert applied == []


@pytest.mark.asyncio
async def test_edge_empty_payload_is_noop():
    mgr, events = make_manager(interventions={"eliminate_player": "5:"})
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim == []


@pytest.mark.asyncio
async def test_end_game_nonce_only_shape():
    mgr, events = make_manager(interventions={"end_game": "1"})
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    end = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "end_game"]
    assert len(end) == 1
    assert end[0]["blocked"] == "false"


# --------------------------------------------------------------------------- #
# State-shaped change detection
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_state_flag_applies_on_change_only():
    mgr, events = make_manager(interventions={"music_tempo_override": 1.3})
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # 0 -> 1.3 (change)
        await mgr.evaluate_all()  # 1.3 -> 1.3 (no change)
    tempo = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "adjust_music_tempo"]
    assert len(tempo) == 1


@pytest.mark.asyncio
async def test_state_flag_revert_to_none_not_dispatched():
    mgr, events = make_manager(interventions={"volume_override": 0.3})
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # -1 -> 0.3 (apply)
        mgr._interventions_client.set("volume_override", -1)  # back to none
        await mgr.evaluate_all()  # change, but it's the none-value -> no dispatch
    vol = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "adjust_volume"]
    assert len(vol) == 1  # only the apply, not the revert


# --------------------------------------------------------------------------- #
# Enforcement: allowed-membership
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_allowed_membership_blocks():
    mgr, events = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},
        allowed=["play_audio_cue"],  # eliminate not allowed
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert len(elim) == 1
    assert elim[0]["blocked"] == "true"
    assert elim[0]["block_reason"] == BLOCK_NOT_ALLOWED


@pytest.mark.asyncio
async def test_empty_allowed_blocks_everything():
    mgr, events = make_manager(interventions={"audio_cue": "1:beep"}, allowed=[])
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    cue = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "play_audio_cue"]
    assert cue[0]["block_reason"] == BLOCK_NOT_ALLOWED


# --------------------------------------------------------------------------- #
# Enforcement: weighted rate limit — the GLOBAL BACKSTOP (#919)
#
# The coordinator limiter is the generous process-global backstop, not the
# per-game governor (that is the agent's). These tests pin a LOW backstop to
# prove the safety net still trips on a flood; the "normal traffic never trips"
# and "per-game independence" properties are covered further below.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_backstop_blocks_after_global_budget_exhausted():
    clock = Clock()
    mgr, events = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},  # hard = 2.0
        backstop=2,  # tiny backstop so the flood trips it immediately
        time_fn=clock,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # consumes 2.0 (applied)
        mgr._interventions_client.set("eliminate_player", "2:ctrl_b")  # new trigger
        await mgr.evaluate_all()  # backstop exhausted -> blocked
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "false"
    assert elim[1]["blocked"] == "true"
    assert elim[1]["block_reason"] == BLOCK_RATE_LIMITED


@pytest.mark.asyncio
async def test_backstop_window_slide_allows_again():
    clock = Clock()
    mgr, events = make_manager(interventions={"eliminate_player": "1:ctrl_a"}, backstop=2, time_fn=clock)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        clock.advance(61.0)
        mgr._interventions_client.set("eliminate_player", "2:ctrl_b")
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[1]["blocked"] == "false"


@pytest.mark.asyncio
async def test_blocked_intervention_does_not_consume_backstop_budget():
    """A type blocked by membership must not eat backstop budget."""
    clock = Clock()
    mgr, _ = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},
        allowed=["eliminate_player"],
        backstop=2,
        time_fn=clock,
    )
    # block eliminate via membership by removing it AFTER constructing? Instead
    # verify the rate limiter directly: a not-allowed type returns before check.
    mgr._agent_client.set("interventions_allowed", [])  # now nothing allowed
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # blocked (not allowed) -> no reservation
    assert mgr._rate_limiter.current_weight() == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Defense-in-depth budget contract (#919): the coordinator limiter is a GENEROUS
# GLOBAL BACKSTOP, not the per-game governor. These tests pin the contract:
#   - the default backstop is generous and is read from the dedicated
#     coordinator flag (NOT the agent's per-game budget flag — drift guard);
#   - normal multi-session traffic stays well under the backstop and is never
#     throttled by the coordinator (per-game pacing is the agent's job).
# --------------------------------------------------------------------------- #
def test_default_backstop_is_generous():
    """A freshly-constructed manager starts with the generous global backstop,
    far above the agent's per-game budget — so it never governs normal pacing."""
    from services.game_coordinator.interventions import (
        DEFAULT_COORDINATOR_BACKSTOP_BUDGET,
    )

    mgr, _ = make_manager()
    # Constructed budget is the generous default, not the agent per-game value.
    assert mgr._rate_limiter._budget == pytest.approx(DEFAULT_COORDINATOR_BACKSTOP_BUDGET)
    assert DEFAULT_COORDINATOR_BACKSTOP_BUDGET >= 60.0


def test_refresh_budget_reads_coordinator_flag_not_per_game_flag():
    """Drift guard (#919): _refresh_budget must read
    policy.coordinator_backstop_per_minute and IGNORE the agent's
    policy.max_interventions_per_minute, so the two layers can never silently
    converge on one shared per-game value."""
    mgr, _ = make_manager(backstop=42)
    # Agent's per-game budget set to a small, different value.
    mgr._agent_client.set("policy.max_interventions_per_minute", 1)
    mgr._refresh_budget()
    # Coordinator backstop tracks ONLY its own flag, never the per-game one.
    assert mgr._rate_limiter._budget == pytest.approx(42.0)

    mgr._agent_client.set("policy.coordinator_backstop_per_minute", 99)
    mgr._refresh_budget()
    assert mgr._rate_limiter._budget == pytest.approx(99.0)


def test_refresh_budget_falls_back_to_generous_default_when_flag_absent():
    """When the coordinator flag is unreachable, the backstop falls back to the
    generous default — it must NOT collapse to the small per-game budget."""
    from services.game_coordinator.interventions import (
        DEFAULT_COORDINATOR_BACKSTOP_BUDGET,
    )

    mgr, _ = make_manager()
    # Remove the coordinator flag entirely; leave a small per-game value present.
    mgr._agent_client.values.pop("policy.coordinator_backstop_per_minute", None)
    mgr._agent_client.set("policy.max_interventions_per_minute", 1)
    mgr._refresh_budget()
    assert mgr._rate_limiter._budget == pytest.approx(DEFAULT_COORDINATOR_BACKSTOP_BUDGET)


@pytest.mark.asyncio
async def test_normal_multi_session_traffic_does_not_trip_backstop():
    """Normal traffic across several concurrent games — each well within its own
    authoritative per-game budget — stays far under the generous global backstop,
    so the coordinator never throttles it. Here we simulate many distinct
    interventions (one per game's worth of activity) inside one window and assert
    none are blocked.

    Each game at the default agent budget (2.0/min) emitting a single hard (2.0)
    intervention => 8 concurrent games = 16.0 weight, still < 60 default backstop.
    """
    clock = Clock()
    # Default generous backstop (60). Fire 8 distinct hard interventions (16.0).
    mgr, events = make_manager(time_fn=clock)  # backstop defaults to 60
    with patch("services.game_coordinator.metrics.interventions_total"):
        for i in range(8):
            mgr._interventions_client.set("eliminate_player", f"{i}:ctrl_{i}")
            await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert len(elim) == 8
    assert all(d["blocked"] == "false" for d in elim)
    # 8 hard interventions = 16.0 weight, comfortably under the 60 backstop.
    assert mgr._rate_limiter.current_weight() == pytest.approx(16.0)


# --------------------------------------------------------------------------- #
# Enforcement: battery guard
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_battery_guard_blocks_low():
    mgr, events = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},
        battery={"ctrl_a": 10},  # below default threshold 20
        threshold=20,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "true"
    assert elim[0]["block_reason"] == BLOCK_LOW_BATTERY


@pytest.mark.asyncio
async def test_battery_guard_passes_when_above_threshold():
    mgr, events = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},
        battery={"ctrl_a": 90},
        threshold=20,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "false"


@pytest.mark.asyncio
async def test_battery_guard_missing_does_not_block():
    mgr, events = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},
        battery={},  # no battery reading for ctrl_a
        threshold=20,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "false"


# --------------------------------------------------------------------------- #
# Enforcement: mode-capability matrix
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mode_matrix_blocks_revive_in_ffa():
    mgr, events = make_manager(
        interventions={"revive_player": "1:ctrl_a"},
        game=FakeGame("FFA"),
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    rev = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "revive_player"]
    assert rev[0]["blocked"] == "true"
    assert rev[0]["block_reason"] == BLOCK_MODE_UNSUPPORTED


@pytest.mark.asyncio
async def test_mode_matrix_blocks_led_effect_in_werewolf():
    mgr, events = make_manager(
        interventions={"controller_effect": "1:ctrl_a:pulse"},
        game=FakeGame("Werewolf"),
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    eff = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "send_controller_effect"]
    assert eff[0]["block_reason"] == BLOCK_MODE_UNSUPPORTED


@pytest.mark.asyncio
async def test_mode_matrix_allows_revive_in_nonstop():
    mgr, events = make_manager(
        interventions={"revive_player": "1:ctrl_a"},
        game=FakeGame("Nonstop Joust"),
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    rev = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "revive_player"]
    assert rev[0]["blocked"] == "false"


# --------------------------------------------------------------------------- #
# Registry dispatch
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_registry_dispatch_calls_registered_handler():
    mgr, _ = make_manager(interventions={"audio_cue": "1:beep"})
    handler = AsyncMock()
    mgr.register_handler("audio_cue", handler)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    handler.assert_awaited_once()
    ctx = handler.await_args.args[0]
    assert ctx.spec.type_id == "play_audio_cue"
    assert ctx.payload == "beep"


@pytest.mark.asyncio
async def test_blocked_intervention_skips_handler():
    mgr, _ = make_manager(interventions={"audio_cue": "1:beep"}, allowed=[])
    handler = AsyncMock()
    mgr.register_handler("audio_cue", handler)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_handler_unknown_key_raises():
    mgr, _ = make_manager()
    with pytest.raises(KeyError):
        mgr.register_handler("not_a_flag", AsyncMock())


@pytest.mark.asyncio
async def test_handler_error_records_blocked():
    mgr, events = make_manager(interventions={"audio_cue": "1:beep"})

    async def boom(_ctx):
        raise RuntimeError("handler failed")

    mgr.register_handler("audio_cue", boom)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    cue = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "play_audio_cue"]
    assert cue[0]["blocked"] == "true"
    assert cue[0]["block_reason"] == "handler_error"


# --------------------------------------------------------------------------- #
# Metric emission
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_metric_recorded_with_labels():
    mgr, _ = make_manager(
        interventions={"audio_cue": "1:beep"},
        objectives={"endurance": 0.7, "balanced": 0.1, "accelerate": 0.1, "chaos": 0.1},
    )
    with patch("services.game_coordinator.metrics.interventions_total") as metric:
        await mgr.evaluate_all()
    metric.labels.assert_any_call(type="play_audio_cue", objective="endurance", blocked="false", block_reason="")
    metric.labels.return_value.inc.assert_called()


@pytest.mark.asyncio
async def test_dominant_objective_default_balanced():
    mgr, _ = make_manager(interventions={"audio_cue": "1:beep"})
    assert mgr._dominant_objective() == "balanced"


# --------------------------------------------------------------------------- #
# Multiple flags in one evaluation pass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_multiple_flags_evaluated_in_one_pass():
    mgr, events = make_manager(
        interventions={
            "music_tempo_override": 1.15,
            "audio_cue": "1:beep",
            "shield_seconds": 5,
        },
        # shield_seconds is player-targeted: it resolves per active serial, so a
        # live game with at least one player is required for it to apply.
        game=FakeGame("Nonstop Joust", players={"p1": FakePlayer("p1")}),
        backstop=10,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    applied_types = {d["type"] for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["blocked"] == "false"}
    assert {"adjust_music_tempo", "play_audio_cue", "grant_shield"} <= applied_types


# --------------------------------------------------------------------------- #
# Player-targeted state-flag change detection (#730, PR G)
#
# These flags are driven via flagd's per-serial targeting if-ladder; the agent
# writer leaves the global defaultVariant on neutral, so change detection keys on
# the per-serial RESOLVED map, not the global evaluated value. The FakeFlagClient
# ignores targeting_key (returns the global value for every serial), which is
# enough to exercise the resolved-map change-detection branch.
# --------------------------------------------------------------------------- #
def _applied(events, type_id):
    return [
        d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id and d["blocked"] == "false"
    ]


@pytest.mark.asyncio
async def test_player_targeted_state_dispatches_on_resolved_change():
    """A player-targeted state flag dispatches when its per-serial resolved value
    moves off neutral, even though the global defaultVariant never changes."""
    game = FakeGame("Nonstop Joust", players={"p1": FakePlayer("p1")})
    mgr, events = make_manager(interventions={"player_sensitivity_factor": 1.0}, game=game, backstop=10)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # neutral 1.0 -> no dispatch
    assert _applied(events, "adjust_player_sensitivity") == []

    with patch("services.game_coordinator.metrics.interventions_total"):
        mgr._interventions_client.set("player_sensitivity_factor", 1.5)
        await mgr.evaluate_all()  # resolved 1.5 -> dispatch
    assert len(_applied(events, "adjust_player_sensitivity")) == 1


@pytest.mark.asyncio
async def test_player_targeted_state_no_redispatch_when_unchanged():
    """Re-evaluating an unchanged resolved map does not re-fire (no spurious
    metric/event or rate-limit consumption)."""
    game = FakeGame("Nonstop Joust", players={"p1": FakePlayer("p1")})
    mgr, events = make_manager(interventions={"player_sensitivity_factor": 1.5}, game=game, backstop=10)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same resolved map -> no second dispatch
    assert len(_applied(events, "adjust_player_sensitivity")) == 1


@pytest.mark.asyncio
async def test_player_targeted_state_no_game_is_noop():
    """No live game means no serials to target: no dispatch, no crash."""
    mgr, events = make_manager(interventions={"shield_seconds": 5}, game=None, backstop=10)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    assert _applied(events, "grant_shield") == []


@pytest.mark.asyncio
async def test_player_targeted_state_revert_to_neutral_does_not_fire():
    """Reverting all serials back to neutral collapses the change key to empty
    and dispatches nothing (shields/factors simply expire/reset)."""
    game = FakeGame("Nonstop Joust", players={"p1": FakePlayer("p1")})
    mgr, events = make_manager(interventions={"shield_seconds": 5}, game=game, backstop=10)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # 5 -> applied
        mgr._interventions_client.set("shield_seconds", 0)
        await mgr.evaluate_all()  # back to neutral -> no enforced intervention
    assert len(_applied(events, "grant_shield")) == 1
