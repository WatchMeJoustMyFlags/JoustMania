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


class FakeGame:
    def __init__(self, name):
        self._name = name

    def get_game_name(self):
        return self._name


def make_manager(
    *,
    interventions=None,
    allowed=None,
    battery=None,
    game=None,
    budget=2,
    threshold=20,
    objectives=None,
    time_fn=None,
):
    """Build a manager wired with fake clients, bypassing start()."""
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    agent_values = {
        "interventions_allowed": list(ALL_TYPES) if allowed is None else allowed,
        "policy.max_interventions_per_minute": budget,
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
    mgr._rate_limiter.set_budget(float(budget))
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
# Enforcement: weighted rate limit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rate_limit_blocks_after_budget_exhausted():
    clock = Clock()
    mgr, events = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},  # hard = 2.0
        budget=2,
        time_fn=clock,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # consumes 2.0 (applied)
        mgr._interventions_client.set("eliminate_player", "2:ctrl_b")  # new trigger
        await mgr.evaluate_all()  # budget exhausted -> blocked
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[0]["blocked"] == "false"
    assert elim[1]["blocked"] == "true"
    assert elim[1]["block_reason"] == BLOCK_RATE_LIMITED


@pytest.mark.asyncio
async def test_rate_limit_window_slide_allows_again():
    clock = Clock()
    mgr, events = make_manager(interventions={"eliminate_player": "1:ctrl_a"}, budget=2, time_fn=clock)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        clock.advance(61.0)
        mgr._interventions_client.set("eliminate_player", "2:ctrl_b")
        await mgr.evaluate_all()
    elim = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == "eliminate_player"]
    assert elim[1]["blocked"] == "false"


@pytest.mark.asyncio
async def test_blocked_intervention_does_not_consume_budget():
    """A type blocked by membership must not eat rate-limit budget."""
    clock = Clock()
    mgr, _ = make_manager(
        interventions={"eliminate_player": "1:ctrl_a"},
        allowed=["eliminate_player"],
        budget=2,
        time_fn=clock,
    )
    # block eliminate via membership by removing it AFTER constructing? Instead
    # verify the rate limiter directly: a not-allowed type returns before check.
    mgr._agent_client.set("interventions_allowed", [])  # now nothing allowed
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()  # blocked (not allowed) -> no reservation
    assert mgr._rate_limiter.current_weight() == pytest.approx(0.0)


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
    metric.labels.assert_any_call(type="play_audio_cue", objective="endurance", blocked="false")
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
        budget=10,
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
    applied_types = {d["type"] for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["blocked"] == "false"}
    assert {"adjust_music_tempo", "play_audio_cue", "grant_shield"} <= applied_types
