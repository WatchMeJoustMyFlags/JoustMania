"""
Unit tests for the difficulty intervention handlers (#730, PR C).

Covers the three difficulty state-shaped interventions and the reusable
per-player targeting helper they introduce:

- music_tempo_override: override beats/suspends the music loop's schedule; revert
  resumes the natural schedule from the current state; mid-game sensitivity swap
  during an in-flight tempo LERP.
- global_sensitivity_override: live swap changes effective thresholds on the next
  computation; revert restores the game's configured sensitivity.
- player_sensitivity_factor: per-serial targeting (targeted player changed,
  others default), clamping, and battery-gated skip.
- resolve_player_targets: reusable targeting helper, unit-tested directly
  (PR D consumes it for shield_seconds).

Telemetry is disabled via conftest; the intervention metric is patched per the
repo metric-testing convention (.claude/rules/development.md).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop

from lib.types import GameEvent, Sensitivity
from services.game_coordinator.difficulty_handlers import (
    handle_global_sensitivity_override,
    handle_music_tempo_override,
    handle_music_tempo_override_revert,
    handle_player_handicap_factor,
    handle_player_sensitivity_factor,
    register_difficulty_handlers,
)
from services.game_coordinator.games.base import (
    FAST_MUSIC_SPEED,
    SLOW_MUSIC_SPEED,
    Player,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import (
    InterventionContext,
    InterventionManager,
    SessionView,
)


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
def make_game(sensitivity=2, num_players=2):
    """Build a real FFAGame (for live _compute_effective_thresholds) with audio."""
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    mock_audio = AsyncMock()
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=mock_audio,
        game_id="test_difficulty",
        sensitivity=sensitivity,
    )
    game.music_track_id = "track_123"
    game.music_speed = SLOW_MUSIC_SPEED
    game.speed_up = True
    game.gameplay_span = None
    game.gameplay_span_context = None
    for i in range(num_players):
        s = f"p{i + 1}"
        game.players[s] = Player(serial=s, alive=True, color=(255, 0, 0))
    return game


class TargetingFlagClient:
    """Flag client that resolves per-serial via EvaluationContext.targeting_key.

    ``per_serial`` maps serial -> value; serials not present fall back to
    ``default`` (mimicking flagd targeting rules with a default variant).
    """

    def __init__(self, default, per_serial=None):
        self.default = default
        self.per_serial = per_serial or {}

    def _resolve(self, ctx, default):
        key = getattr(ctx, "targeting_key", None) if ctx is not None else None
        if key in self.per_serial:
            return self.per_serial[key]
        return self.default if self.default is not None else default

    def get_float_value(self, _key, default, ctx=None):
        return float(self._resolve(ctx, default))

    def get_integer_value(self, _key, default, ctx=None):
        return int(self._resolve(ctx, default))


def make_manager(game=None, battery=None, threshold=20):
    """Manager bypassing start(); records published events."""
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(
        event_publisher=publisher,
        get_game=lambda: game,
        battery_provider=(lambda s: battery.get(s)) if battery is not None else None,
    )
    # Minimal agent client: only battery threshold is consulted by the helper.
    mgr._agent_client = _AgentStub(threshold)
    return mgr, events


class _AgentStub:
    def __init__(self, threshold):
        self._threshold = threshold

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.battery_threshold":
            return self._threshold
        return default

    def get_object_value(self, _key, default, _ctx=None):
        return default


def _spec(flag_key):
    from services.game_coordinator.interventions import INTERVENTION_SPECS

    return next(s for s in INTERVENTION_SPECS if s.flag_key == flag_key)


def make_ctx(flag_key, value, game, *, payload="", target=None):
    return InterventionContext(
        spec=_spec(flag_key),
        value=value,
        payload=payload,
        target_serial=target,
        game=game,
        objective="balanced",
    )


# --------------------------------------------------------------------------- #
# music_tempo_override
# --------------------------------------------------------------------------- #
class TestMusicTempoOverride:
    @pytest.mark.asyncio
    async def test_override_sets_attribute(self):
        game = make_game()
        await handle_music_tempo_override(make_ctx("music_tempo_override", 1.3, game))
        assert game.tempo_override == pytest.approx(1.3)

    @pytest.mark.asyncio
    async def test_override_beats_and_suspends_schedule(self):
        """While override holds, the music loop adopts it and skips its schedule."""
        game = make_game()
        # Make the natural schedule "due" so without an override it would fire.
        game.change_time = 0.0
        game.tempo_override = 1.15
        await game._check_music_speed()
        # Override tempo applied via _apply_tempo_change path.
        assert game.music_speed == pytest.approx(1.15)
        # A second check holds the override (no further transition since already at tempo).
        game.audio_client.ChangeTempo.reset_mock()
        await game._check_music_speed()
        game.audio_client.ChangeTempo.assert_not_called()

    @pytest.mark.asyncio
    async def test_revert_resumes_schedule(self):
        """Clearing the override (value 0) lets the natural schedule run again."""
        game = make_game()
        game.tempo_override = 1.3
        await game._check_music_speed()
        assert game.music_speed == pytest.approx(1.3)

        # Revert via handler.
        await handle_music_tempo_override(make_ctx("music_tempo_override", 0, game))
        assert game.tempo_override is None

        # Now the schedule resumes: force it due and confirm a scheduled change fires.
        game.audio_client.ChangeTempo.reset_mock()
        game.change_time = 0.0
        await game._check_music_speed()
        game.audio_client.ChangeTempo.assert_called_once()

    @pytest.mark.asyncio
    async def test_revert_handler_clears_override(self):
        """The dedicated revert handler (#922) clears a stuck override."""
        game = make_game()
        game.tempo_override = 1.3
        await handle_music_tempo_override_revert(make_ctx("music_tempo_override", 0, game))
        assert game.tempo_override is None

    @pytest.mark.asyncio
    async def test_revert_handler_no_live_game_is_noop(self):
        await handle_music_tempo_override_revert(make_ctx("music_tempo_override", 0, None))  # no raise

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        await handle_music_tempo_override(make_ctx("music_tempo_override", 1.3, None))  # no raise

    @pytest.mark.asyncio
    async def test_mid_game_sensitivity_swap_during_tempo_lerp(self):
        """Swapping sensitivity while a tempo override LERP is in flight is safe:
        thresholds are recomputed per frame from the live sensitivity and speed.
        """
        game = make_game(sensitivity=2)
        player = game.players["p1"]

        # Override drives music to fast; thresholds at MEDIUM/fast.
        game.tempo_override = FAST_MUSIC_SPEED
        await game._check_music_speed()
        warn_before, death_before = game._compute_effective_thresholds(player)

        # Mid-LERP global sensitivity swap to a harder level.
        await handle_global_sensitivity_override(make_ctx("global_sensitivity_override", 4, game))
        warn_after, death_after = game._compute_effective_thresholds(player)

        # Harder sensitivity (index 4) raises the death threshold; computation
        # stays well-defined despite the active override.
        assert death_after != death_before
        assert game.sensitivity == Sensitivity.ULTRA_FAST


# --------------------------------------------------------------------------- #
# global_sensitivity_override
# --------------------------------------------------------------------------- #
class TestGlobalSensitivityOverride:
    @pytest.mark.asyncio
    async def test_live_swap_changes_effective_thresholds(self):
        game = make_game(sensitivity=0)  # ULTRA_SLOW
        player = game.players["p1"]
        _, death_before = game._compute_effective_thresholds(player)

        await handle_global_sensitivity_override(make_ctx("global_sensitivity_override", 4, game))
        assert game.sensitivity == Sensitivity.ULTRA_FAST
        _, death_after = game._compute_effective_thresholds(player)
        # Different sensitivity index -> different effective death threshold.
        assert death_after != death_before

    @pytest.mark.asyncio
    async def test_revert_restores_configured_sensitivity(self):
        game = make_game(sensitivity=1)  # configured SLOW
        assert game.configured_sensitivity == Sensitivity.SLOW

        await handle_global_sensitivity_override(make_ctx("global_sensitivity_override", 3, game))
        assert game.sensitivity == Sensitivity.FAST

        # Revert with -1 restores the original configured level.
        await handle_global_sensitivity_override(make_ctx("global_sensitivity_override", -1, game))
        assert game.sensitivity == Sensitivity.SLOW

    @pytest.mark.asyncio
    async def test_invalid_index_ignored(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx("global_sensitivity_override", 99, game))
        assert game.sensitivity == Sensitivity.MEDIUM  # unchanged

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        await handle_global_sensitivity_override(make_ctx("global_sensitivity_override", 3, None))


# --------------------------------------------------------------------------- #
# player_sensitivity_factor + targeting
# --------------------------------------------------------------------------- #
class TestPlayerSensitivityFactor:
    @pytest.mark.asyncio
    async def test_targeted_player_changed_others_default(self):
        game = make_game(num_players=3)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p2": 1.5})

        await handle_player_sensitivity_factor(make_ctx("player_sensitivity_factor", 1.5, game), mgr)

        assert game.players["p1"].sensitivity_factor == pytest.approx(1.0)
        assert game.players["p2"].sensitivity_factor == pytest.approx(1.5)
        assert game.players["p3"].sensitivity_factor == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_factor_is_clamped(self):
        game = make_game(num_players=2)
        mgr, _ = make_manager(game=game)
        # Out-of-range factors are clamped to [0.5, 2.0].
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p1": 5.0, "p2": 0.1})

        await handle_player_sensitivity_factor(make_ctx("player_sensitivity_factor", 0, game), mgr)

        assert game.players["p1"].sensitivity_factor == pytest.approx(2.0)
        assert game.players["p2"].sensitivity_factor == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_changed_factor_changes_effective_threshold(self):
        game = make_game(num_players=2)
        player = game.players["p1"]
        _, death_default = game._compute_effective_thresholds(player)

        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p1": 1.5})
        await handle_player_sensitivity_factor(make_ctx("player_sensitivity_factor", 1.5, game), mgr)

        _, death_after = game._compute_effective_thresholds(player)
        # Higher factor -> lower (easier) death threshold.
        assert death_after < death_default

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default=1.0)
        await handle_player_sensitivity_factor(make_ctx("player_sensitivity_factor", 1.5, None), mgr)


class TestPlayerHandicapFactor:
    """#1107 (#1103 MVP action 1): per-player MULTIPLICATIVE handicap that
    COMPOSES with sensitivity_factor (>1 harder to die, <1 easier), clamped
    [0.5, 2.0]."""

    @pytest.mark.asyncio
    async def test_targeted_player_changed_others_default(self):
        game = make_game(num_players=3)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p2": 1.5})

        await handle_player_handicap_factor(make_ctx("player_handicap_factor", 1.5, game), mgr)

        assert game.players["p1"].handicap_factor == pytest.approx(1.0)
        assert game.players["p2"].handicap_factor == pytest.approx(1.5)
        assert game.players["p3"].handicap_factor == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_factor_is_clamped(self):
        game = make_game(num_players=2)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p1": 5.0, "p2": 0.1})

        await handle_player_handicap_factor(make_ctx("player_handicap_factor", 0, game), mgr)

        assert game.players["p1"].handicap_factor == pytest.approx(2.0)
        assert game.players["p2"].handicap_factor == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_help_raises_threshold_rein_in_lowers(self):
        """>1 handicap = higher (harder) threshold; <1 = lower (easier)."""
        game = make_game(num_players=2)
        p1 = game.players["p1"]
        _, death_neutral = game._compute_effective_thresholds(p1)

        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p1": 1.5})
        await handle_player_handicap_factor(make_ctx("player_handicap_factor", 1.5, game), mgr)
        _, death_help = game._compute_effective_thresholds(p1)
        assert death_help > death_neutral  # harder to die

        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p1": 0.5})
        await handle_player_handicap_factor(make_ctx("player_handicap_factor", 0.5, game), mgr)
        _, death_rein = game._compute_effective_thresholds(p1)
        assert death_rein < death_neutral  # easier to die

    @pytest.mark.asyncio
    async def test_composes_with_sensitivity_not_replaces(self):
        """Handicap and sensitivity are SEPARATE knobs that both apply."""
        game = make_game(num_players=1)
        p1 = game.players["p1"]
        _, death_neutral = game._compute_effective_thresholds(p1)

        # sensitivity_factor 2.0 alone halves the threshold (easier).
        p1.sensitivity_factor = 2.0
        _, death_sens_only = game._compute_effective_thresholds(p1)
        assert death_sens_only == pytest.approx(death_neutral / 2.0)

        # Add a 2.0 handicap (harder): it MULTIPLIES the already-divided threshold,
        # composing back to the neutral threshold (0.5 * 2.0 == 1.0 net).
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p1": 2.0})
        await handle_player_handicap_factor(make_ctx("player_handicap_factor", 2.0, game), mgr)
        _, death_both = game._compute_effective_thresholds(p1)
        assert p1.sensitivity_factor == pytest.approx(2.0)  # sensitivity untouched
        assert p1.handicap_factor == pytest.approx(2.0)
        assert death_both == pytest.approx(death_neutral)

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default=1.0)
        await handle_player_handicap_factor(make_ctx("player_handicap_factor", 1.5, None), mgr)


# --------------------------------------------------------------------------- #
# resolve_player_targets (reusable helper) — PR D consumes this
# --------------------------------------------------------------------------- #
class TestResolvePlayerTargets:
    def test_resolves_per_serial_with_default(self):
        game = make_game(num_players=3)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p2": 1.5})

        result = mgr.resolve_player_targets("player_sensitivity_factor", 1.0, game)
        assert result == {"p1": 1.0, "p2": 1.5, "p3": 1.0}

    def test_battery_gate_drops_low_battery_serial(self):
        game = make_game(num_players=3)
        mgr, _ = make_manager(game=game, battery={"p1": 50.0, "p2": 5.0, "p3": 80.0}, threshold=20)
        mgr._interventions_client = TargetingFlagClient(default=1.0, per_serial={"p2": 1.5})

        result = mgr.resolve_player_targets("player_sensitivity_factor", 1.0, game)
        # p2 is below the 20% threshold -> excluded from the result entirely.
        assert "p2" not in result
        assert set(result) == {"p1", "p3"}

    def test_battery_gate_disabled_keeps_all(self):
        game = make_game(num_players=2)
        mgr, _ = make_manager(game=game, battery={"p1": 5.0, "p2": 5.0}, threshold=20)
        mgr._interventions_client = TargetingFlagClient(default=1.0)

        result = mgr.resolve_player_targets("player_sensitivity_factor", 1.0, game, battery_gate=False)
        assert set(result) == {"p1", "p2"}

    def test_only_alive_players_resolved(self):
        game = make_game(num_players=3)
        game.players["p2"].alive = False
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=1.0)

        result = mgr.resolve_player_targets("player_sensitivity_factor", 1.0, game)
        assert set(result) == {"p1", "p3"}

    def test_int_value_kind(self):
        game = make_game(num_players=1)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default=0, per_serial={"p1": 5})

        result = mgr.resolve_player_targets("shield_seconds", 0, game, value_kind="int")
        assert result == {"p1": 5.0}

    def test_no_game_returns_empty(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default=1.0)
        assert mgr.resolve_player_targets("player_sensitivity_factor", 1.0, None) == {}


# --------------------------------------------------------------------------- #
# Registration / end-to-end dispatch through the manager
# --------------------------------------------------------------------------- #
class TestRegistration:
    def test_register_difficulty_handlers_wires_all_three(self):
        mgr, _ = make_manager(game=None)
        register_difficulty_handlers(mgr)
        assert mgr._handlers["music_tempo_override"] is handle_music_tempo_override
        assert mgr._handlers["global_sensitivity_override"] is handle_global_sensitivity_override
        # player_sensitivity_factor is bound via closure (not identity-equal).
        assert callable(mgr._handlers["player_sensitivity_factor"])
        # #1107: player_handicap_factor is also closure-bound (mirrors sensitivity).
        assert callable(mgr._handlers["player_handicap_factor"])
        # #922: music_tempo_override needs an explicit revert handler (the manager
        # routes reverts to _revert_handlers, not back to the apply-handler).
        assert mgr._revert_handlers["music_tempo_override"] is handle_music_tempo_override_revert

    @pytest.mark.asyncio
    @patch("services.game_coordinator.metrics.interventions_total")
    async def test_dispatch_applies_tempo_override_via_chain(self, _metric):
        """End-to-end: a tempo-override flag change flows through the enforcement
        chain and the real handler sets game.tempo_override."""
        game = make_game()
        events = []

        async def publisher(event_type, data):
            events.append((event_type, data))

        mgr = InterventionManager(event_publisher=publisher, get_game=lambda: game)
        mgr._interventions_client = _ScalarFlagClient({"music_tempo_override": 0})
        mgr._agent_client = _AllowAllAgent()
        register_difficulty_handlers(mgr)
        # Baseline: override currently none (so the later change is detected).
        mgr._prime_baseline()

        # Now the agent sets the override; evaluate the single flag.
        mgr._interventions_client.values["music_tempo_override"] = 1.15
        # #838: _evaluate_one is now per-session; pass the synthesized primary view.
        primary = SessionView(game_id="", game=game, publish=publisher)
        await mgr._evaluate_one(_spec("music_tempo_override"), primary)

        assert game.tempo_override == pytest.approx(1.15)
        assert any(e[0] == GameEvent.AGENT_INTERVENTION and e[1]["blocked"] == "false" for e in events)

    @pytest.mark.asyncio
    @patch("services.game_coordinator.metrics.interventions_total")
    async def test_revert_to_neutral_restores_natural_tempo_via_chain(self, _metric):
        """Acceptance (#922): apply music_tempo_override, then revert the flag to
        neutral (0) through the manager, and assert the override is cleared so the
        natural music schedule resumes.

        This exercises the real revert path: the manager routes a revert-to-neutral
        to ``_revert_handlers``, NOT back to the apply-handler, so without the
        registered revert handler ``game.tempo_override`` would stay pinned at the
        override and the music loop would be stuck at that tempo.
        """
        game = make_game()
        events = []

        async def publisher(event_type, data):
            events.append((event_type, data))

        mgr = InterventionManager(event_publisher=publisher, get_game=lambda: game)
        mgr._interventions_client = _ScalarFlagClient({"music_tempo_override": 0})
        mgr._agent_client = _AllowAllAgent()
        register_difficulty_handlers(mgr)
        mgr._prime_baseline()

        primary = SessionView(game_id="", game=game, publish=publisher)

        # Apply the override.
        mgr._interventions_client.values["music_tempo_override"] = 1.15
        await mgr._evaluate_one(_spec("music_tempo_override"), primary)
        assert game.tempo_override == pytest.approx(1.15)

        # Revert the flag to its neutral value (0).
        mgr._interventions_client.values["music_tempo_override"] = 0
        await mgr._evaluate_one(_spec("music_tempo_override"), primary)

        # Override cleared: the music loop resumes its natural schedule next tick.
        assert game.tempo_override is None

        # Confirm the schedule actually runs again: force a scheduled change due.
        game.audio_client.ChangeTempo.reset_mock()
        game.change_time = 0.0
        await game._check_music_speed()
        game.audio_client.ChangeTempo.assert_called_once()


class _ScalarFlagClient:
    def __init__(self, values):
        self.values = values

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_string_value(self, key, default, _ctx=None):
        return str(self.values.get(key, default))


class _AllowAllAgent:
    def get_string_value(self, key, default, _ctx=None):
        # interventions_allowed is a STRING flag: comma-separated ids (#1127).
        if key == "interventions_allowed":
            return "adjust_music_tempo,adjust_global_sensitivity,adjust_player_sensitivity"
        return default

    def get_object_value(self, key, default, _ctx=None):
        return default

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.max_interventions_per_minute":
            return 100
        if key == "policy.battery_threshold":
            return 20
        return default
