"""
Unit tests for the shield + lifecycle intervention handlers (#730, PR D).

Covers the three shield/lifecycle interventions and the BaseGameMode primitives
they drive:

- grant_shield (BaseGameMode): sets grace_until, sends a visible pulse effect,
  and extends-not-shortens an existing shield; defensive on unknown/dead player.
- shield_seconds handler: per-serial targeting (targeted serials shielded,
  value-0 default skipped), battery-gated skip, no-live-game no-op.
- eliminate_player handler: routes through _kill_player with reason
  ``agent_intervention`` + accel 0.0; defensive on unknown / already-dead /
  empty serial / no game.
- revive_player handler + revive_player primitive: native respawn path (Nonstop),
  generic re-entry with spawn grace, defensive on unknown / already-alive.

Telemetry is disabled via conftest; the intervention metric is patched per the
repo metric-testing convention (.claude/rules/development.md).
"""

import sys
import time
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
)
from services.game_coordinator.lifecycle_handlers import (
    ELIMINATE_REASON,
    handle_auto_rubberband,
    handle_eliminate_player,
    handle_partial_shield,
    handle_revive_player,
    handle_shield_seconds,
    handle_soft_penalty,
    parse_auto_rubberband_value,
    parse_partial_shield_value,
    parse_soft_penalty_value,
    register_lifecycle_handlers,
)


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
class RecordingGameplayStream:
    """Gameplay stream mock that records every written GameplayStreamControl."""

    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def effects(self):
        """Return the list of GameEffectCommand effect enums written."""
        return [m.game_effect.effect for m in self.writes if m.HasField("game_effect")]


def make_ffa_game(num_players=2, record_stream=True):
    """Build a real FFAGame with a recording gameplay stream."""
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_lifecycle_ffa",
    )
    if record_stream:
        game.gameplay_stream = RecordingGameplayStream()
    for i in range(num_players):
        s = f"p{i + 1}"
        game.players[s] = Player(serial=s, alive=True, color=(255, 0, 0))
    return game


def make_nonstop_game(num_players=2):
    """Build a real NonstopJoustGame with initialized players + recording stream."""
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    game = NonstopJoustGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_lifecycle_nonstop",
    )
    game.gameplay_stream = RecordingGameplayStream()
    return game, mock_cm


class TargetingFlagClient:
    """Flag client resolving per-serial via EvaluationContext.targeting_key."""

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

    def get_string_value(self, _key, default, ctx=None):
        return str(self._resolve(ctx, default))


class _AgentStub:
    def __init__(self, threshold):
        self._threshold = threshold

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.battery_threshold":
            return self._threshold
        return default

    def get_object_value(self, _key, default, _ctx=None):
        return default


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
    mgr._agent_client = _AgentStub(threshold)
    return mgr, events


def _spec(flag_key):
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
# grant_shield primitive (BaseGameMode)
# --------------------------------------------------------------------------- #
class TestGrantShieldPrimitive:
    @pytest.mark.asyncio
    async def test_sets_grace_and_pulse(self):
        game = make_ffa_game()
        before = time.time()
        granted = await game.grant_shield("p1", 5.0)
        assert granted is True
        # grace_until pushed roughly 5s into the future.
        assert game.players["p1"].grace_until >= before + 4.5
        # A visible pulse effect was sent.
        assert controller_manager_pb2.GAME_EFFECT_PULSE in game.gameplay_stream.effects()

    @pytest.mark.asyncio
    async def test_extends_not_shortens(self):
        game = make_ffa_game()
        await game.grant_shield("p1", 10.0)
        long_grace = game.players["p1"].grace_until
        # A shorter shield must NOT pull the longer grace in.
        await game.grant_shield("p1", 1.0)
        assert game.players["p1"].grace_until == pytest.approx(long_grace)

    @pytest.mark.asyncio
    async def test_extends_when_longer(self):
        game = make_ffa_game()
        await game.grant_shield("p1", 1.0)
        short_grace = game.players["p1"].grace_until
        await game.grant_shield("p1", 10.0)
        assert game.players["p1"].grace_until > short_grace

    @pytest.mark.asyncio
    async def test_unknown_or_dead_or_nonpositive_is_noop(self):
        game = make_ffa_game()
        assert await game.grant_shield("nope", 5.0) is False
        assert await game.grant_shield("p1", 0.0) is False
        game.players["p1"].alive = False
        assert await game.grant_shield("p1", 5.0) is False


# --------------------------------------------------------------------------- #
# shield_seconds handler
# --------------------------------------------------------------------------- #
class TestShieldSecondsHandler:
    @pytest.mark.asyncio
    async def test_shields_only_targeted_serials(self):
        game = make_ffa_game(num_players=3)
        mgr, _ = make_manager(game=game)
        # p1 gets a 5s shield; p2/p3 resolve to the neutral default (0).
        mgr._interventions_client = TargetingFlagClient(default=0, per_serial={"p1": 5})

        before = time.time()
        await handle_shield_seconds(make_ctx("shield_seconds", 5, game), mgr)

        assert game.players["p1"].grace_until >= before + 4.5
        assert game.players["p2"].grace_until == 0.0
        assert game.players["p3"].grace_until == 0.0

    @pytest.mark.asyncio
    async def test_battery_gated_serial_skipped(self):
        game = make_ffa_game(num_players=2)
        # p2 below threshold -> dropped by resolve_player_targets entirely.
        mgr, _ = make_manager(game=game, battery={"p1": 90, "p2": 5}, threshold=20)
        mgr._interventions_client = TargetingFlagClient(default=0, per_serial={"p1": 5, "p2": 5})

        await handle_shield_seconds(make_ctx("shield_seconds", 5, game), mgr)
        assert game.players["p1"].grace_until > 0.0
        assert game.players["p2"].grace_until == 0.0

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default=0)
        await handle_shield_seconds(make_ctx("shield_seconds", 5, None), mgr)  # no raise


# --------------------------------------------------------------------------- #
# partial_shield: value parser (#1129)
# --------------------------------------------------------------------------- #
class TestParsePartialShieldValue:
    def test_seconds_only_defaults_boost(self):
        assert parse_partial_shield_value("5") == (5.0, 2.0)

    def test_seconds_and_boost(self):
        assert parse_partial_shield_value("8:1.8") == (8.0, 1.8)

    def test_seconds_capped(self):
        secs, _ = parse_partial_shield_value("9999")
        assert secs == 30.0

    def test_boost_clamped(self):
        assert parse_partial_shield_value("5:9")[1] == 2.0
        assert parse_partial_shield_value("5:0.1")[1] == 1.0

    @pytest.mark.parametrize(
        "bad",
        ["", "0", "none", "off", "-3", "abc", "5:abc", "5:2:1", "  ", "x:y"],
    )
    def test_malformed_or_neutral_is_none(self, bad):
        assert parse_partial_shield_value(bad) is None


# --------------------------------------------------------------------------- #
# partial_shield handler (#1129)
# --------------------------------------------------------------------------- #
class TestPartialShieldHandler:
    @pytest.mark.asyncio
    async def test_arms_only_targeted_serials(self):
        game = make_ffa_game(num_players=3)
        mgr, _ = make_manager(game=game)
        # p1 gets an 8s/1.8 boost; p2/p3 resolve to neutral "0".
        mgr._interventions_client = TargetingFlagClient(default="0", per_serial={"p1": "8:1.8"})

        await handle_partial_shield(make_ctx("partial_shield_seconds", "8:1.8", game), mgr)

        assert game.players["p1"].partial_shield_until > time.time()
        assert game.players["p1"].partial_shield_boost == pytest.approx(1.8)
        assert game.players["p2"].partial_shield_until == 0.0
        assert game.players["p3"].partial_shield_until == 0.0

    @pytest.mark.asyncio
    async def test_malformed_value_is_safe_noop(self):
        game = make_ffa_game(num_players=1)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default="0", per_serial={"p1": "garbage"})

        await handle_partial_shield(make_ctx("partial_shield_seconds", "garbage", game), mgr)
        assert game.players["p1"].partial_shield_until == 0.0

    @pytest.mark.asyncio
    async def test_battery_gated_serial_skipped(self):
        game = make_ffa_game(num_players=2)
        mgr, _ = make_manager(game=game, battery={"p1": 90, "p2": 5}, threshold=20)
        mgr._interventions_client = TargetingFlagClient(default="0", per_serial={"p1": "5", "p2": "5"})

        await handle_partial_shield(make_ctx("partial_shield_seconds", "5", game), mgr)
        assert game.players["p1"].partial_shield_until > 0.0
        assert game.players["p2"].partial_shield_until == 0.0

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default="0")
        await handle_partial_shield(make_ctx("partial_shield_seconds", "5", None), mgr)  # no raise


# --------------------------------------------------------------------------- #
# soft_penalty: value parser (#1134)
# --------------------------------------------------------------------------- #
class TestParseSoftPenaltyValue:
    def test_warn_default(self):
        assert parse_soft_penalty_value("warn") == ("warn", 0.0, 1.0)

    def test_bare_tighten_defaults(self):
        action, seconds, factor = parse_soft_penalty_value("tighten")
        assert action == "tighten"
        assert seconds == 5.0
        assert factor == 0.6

    def test_tighten_with_params(self):
        assert parse_soft_penalty_value("tighten:8:0.6") == ("tighten", 8.0, 0.6)

    def test_tighten_seconds_capped(self):
        _, secs, _ = parse_soft_penalty_value("tighten:9999:0.6")
        assert secs == 30.0

    def test_tighten_factor_clamped(self):
        assert parse_soft_penalty_value("tighten:5:0.1")[2] == 0.5
        assert parse_soft_penalty_value("tighten:5:1.5")[2] == 1.0

    @pytest.mark.parametrize("neutral", ["", "none", "off", "0", "  "])
    def test_neutral_is_none(self, neutral):
        assert parse_soft_penalty_value(neutral) is None

    @pytest.mark.parametrize(
        "bad",
        ["abc", "loosen", "tighten:5", "tighten:a:b", "tighten:0:0.6", "tighten:-3:0.6", "tighten:5:0.6:1"],
    )
    def test_malformed_degrades_to_warn(self, bad):
        """ANY malformed/unknown value degrades to the safe default warn — never a
        garbage dispatch, and crucially never an accidental tighten."""
        result = parse_soft_penalty_value(bad)
        # "tighten:5" / bad-param forms keep the tighten action with safe defaults;
        # unknown actions degrade to warn. Either way it is never garbage.
        assert result is not None
        action, seconds, factor = result
        if bad.startswith("tighten:"):
            assert action == "tighten"
            assert 0 < seconds <= 30.0
            assert 0.5 <= factor <= 1.0
        else:
            assert action == "warn"


# --------------------------------------------------------------------------- #
# soft_penalty handler (#1134)
# --------------------------------------------------------------------------- #
class TestSoftPenaltyHandler:
    @pytest.mark.asyncio
    async def test_warn_fires_no_threshold_change(self):
        game = make_ffa_game(num_players=2)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default="none", per_serial={"p1": "warn"})

        before = game._compute_effective_thresholds(game.players["p1"])
        await handle_soft_penalty(make_ctx("soft_penalty_action", "warn", game), mgr)

        # warn: visible cue only, no tighten armed, thresholds unchanged.
        assert game.players["p1"].soft_penalty_until == 0.0
        assert game.players["p1"].warning_until > time.time()
        assert before == pytest.approx(game._compute_effective_thresholds(game.players["p1"]))
        assert game.players["p2"].warning_until == 0.0

    @pytest.mark.asyncio
    async def test_tighten_arms_only_targeted_serials(self):
        game = make_ffa_game(num_players=3)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default="none", per_serial={"p1": "tighten:8:0.6"})

        await handle_soft_penalty(make_ctx("soft_penalty_action", "tighten:8:0.6", game), mgr)

        assert game.players["p1"].soft_penalty_until > time.time()
        assert game.players["p1"].soft_penalty_factor == pytest.approx(0.6)
        assert game.players["p2"].soft_penalty_until == 0.0
        assert game.players["p3"].soft_penalty_until == 0.0

    @pytest.mark.asyncio
    async def test_malformed_value_degrades_to_warn(self):
        game = make_ffa_game(num_players=1)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default="none", per_serial={"p1": "garbage"})

        await handle_soft_penalty(make_ctx("soft_penalty_action", "garbage", game), mgr)
        # Degrades to warn: a cue fires, but NO tighten is armed.
        assert game.players["p1"].soft_penalty_until == 0.0
        assert game.players["p1"].warning_until > time.time()

    @pytest.mark.asyncio
    async def test_battery_gated_serial_skipped(self):
        game = make_ffa_game(num_players=2)
        mgr, _ = make_manager(game=game, battery={"p1": 90, "p2": 5}, threshold=20)
        mgr._interventions_client = TargetingFlagClient(default="none", per_serial={"p1": "tighten", "p2": "tighten"})

        await handle_soft_penalty(make_ctx("soft_penalty_action", "tighten", game), mgr)
        assert game.players["p1"].soft_penalty_until > 0.0
        assert game.players["p2"].soft_penalty_until == 0.0

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default="none")
        await handle_soft_penalty(make_ctx("soft_penalty_action", "warn", None), mgr)  # no raise


# --------------------------------------------------------------------------- #
# eliminate_player handler
# --------------------------------------------------------------------------- #
class TestEliminatePlayerHandler:
    @pytest.mark.asyncio
    async def test_kills_via_kill_player_with_agent_reason(self):
        game = make_ffa_game(num_players=2)
        captured = {}
        orig_kill = game._kill_player

        async def spy_kill(serial, accel_mag, reason="motion"):
            captured["serial"] = serial
            captured["accel_mag"] = accel_mag
            captured["reason"] = reason
            await orig_kill(serial, accel_mag, reason)

        game._kill_player = spy_kill

        await handle_eliminate_player(make_ctx("eliminate_player", "n1:p1", game, payload="p1", target="p1"))

        assert captured["serial"] == "p1"
        assert captured["accel_mag"] == 0.0
        assert captured["reason"] == ELIMINATE_REASON
        assert game.players["p1"].alive is False

    @pytest.mark.asyncio
    async def test_defensive_paths(self):
        game = make_ffa_game(num_players=1)
        calls = []
        orig = game._kill_player

        async def counting_kill(serial, accel_mag, reason="motion"):
            calls.append(serial)
            await orig(serial, accel_mag, reason)

        game._kill_player = counting_kill

        # Unknown serial -> no kill.
        await handle_eliminate_player(make_ctx("eliminate_player", "", game, payload="ghost"))
        # Empty serial -> no kill.
        await handle_eliminate_player(make_ctx("eliminate_player", "", game, payload=""))
        # Already dead -> no kill.
        game.players["p1"].alive = False
        await handle_eliminate_player(make_ctx("eliminate_player", "", game, payload="p1"))
        # No game -> no raise.
        await handle_eliminate_player(make_ctx("eliminate_player", "", None, payload="p1"))

        assert calls == []


# --------------------------------------------------------------------------- #
# revive_player handler + primitive
# --------------------------------------------------------------------------- #
class TestRevivePlayer:
    @pytest.mark.asyncio
    async def test_revive_in_respawn_mode_uses_native_path(self):
        game, mock_cm = make_nonstop_game(num_players=2)
        await game._initialize_players_impl(mock_cm.controllers)
        serial = next(iter(game.players))
        game.players[serial].alive = False

        before = time.time()
        await handle_revive_player(make_ctx("revive_player", "", game, payload=serial))

        player = game.players[serial]
        assert player.alive is True
        # Native respawn applies spawn protection; revive ensures spawn grace too.
        assert player.grace_until >= before

    @pytest.mark.asyncio
    async def test_generic_reentry_grants_spawn_grace(self):
        # FFA has no _respawn_player -> generic re-entry path.
        game = make_ffa_game(num_players=2)
        assert not hasattr(game, "_respawn_player")
        game.players["p1"].alive = False

        before = time.time()
        revived = await game.revive_player("p1")
        assert revived is True
        player = game.players["p1"]
        assert player.alive is True
        assert player.grace_until >= before + 1.5  # REVIVE_SPAWN_GRACE ~2.0
        # LED restored to base color + respawn effect emitted.
        effects = game.gameplay_stream.effects()
        assert controller_manager_pb2.GAME_EFFECT_PLAYER_RESPAWN in effects
        assert any(m.HasField("base_color") for m in game.gameplay_stream.writes)

    @pytest.mark.asyncio
    async def test_defensive_unknown_and_already_alive(self):
        game = make_ffa_game(num_players=1)
        # Unknown serial.
        assert await game.revive_player("ghost") is False
        # Already alive.
        assert game.players["p1"].alive is True
        assert await game.revive_player("p1") is False
        # Handler no-ops with no game.
        await handle_revive_player(make_ctx("revive_player", "", None, payload="p1"))  # no raise
        # Handler no-ops with empty serial.
        await handle_revive_player(make_ctx("revive_player", "", game, payload=""))  # no raise


# --------------------------------------------------------------------------- #
# registration wiring
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# auto_rubberband parser + handler (#1143)
# --------------------------------------------------------------------------- #
class TestParseAutoRubberbandValue:
    def test_recognized_strengths(self):
        assert parse_auto_rubberband_value("gentle") == "gentle"
        assert parse_auto_rubberband_value("strong") == "strong"
        assert parse_auto_rubberband_value(" STRONG ") == "strong"

    @pytest.mark.parametrize("neutral", ["", "off", "none", "0"])
    def test_neutral_is_none(self, neutral):
        assert parse_auto_rubberband_value(neutral) is None

    @pytest.mark.parametrize("bad", ["gentle:5", "aggressive", "1.4", "gentlexx"])
    def test_unknown_is_none_noop(self, bad):
        # An unknown/malformed value is a safe no-op (None), never garbage.
        assert parse_auto_rubberband_value(bad) is None


class TestAutoRubberbandHandler:
    @pytest.mark.asyncio
    async def test_expands_boost_to_laggard(self):
        game = make_ffa_game(num_players=2)
        # Build a clear skill gap: p1 leading (low intensity), p2 lagging (high).
        game.players["p1"].smoothed_accel = 0.2
        game.players["p2"].smoothed_accel = 1.5

        await handle_auto_rubberband(make_ctx("auto_rubberband", "strong", game))

        assert game.players["p2"].rubberband_until > time.time()
        assert game.players["p2"].rubberband_boost > 1.0

    @pytest.mark.asyncio
    async def test_neutral_value_noop(self):
        game = make_ffa_game(num_players=2)
        game.players["p1"].smoothed_accel = 0.2
        game.players["p2"].smoothed_accel = 1.5
        await handle_auto_rubberband(make_ctx("auto_rubberband", "off", game))
        for p in game.players.values():
            assert p.rubberband_until == 0.0

    @pytest.mark.asyncio
    async def test_malformed_value_noop(self):
        game = make_ffa_game(num_players=2)
        game.players["p1"].smoothed_accel = 0.2
        game.players["p2"].smoothed_accel = 1.5
        await handle_auto_rubberband(make_ctx("auto_rubberband", "garbage", game))
        for p in game.players.values():
            assert p.rubberband_until == 0.0

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        await handle_auto_rubberband(make_ctx("auto_rubberband", "strong", None))  # no raise


class TestRegistration:
    def test_register_lifecycle_handlers_wires_all(self):
        mgr, _ = make_manager()
        register_lifecycle_handlers(mgr)
        for flag in (
            "shield_seconds",
            "partial_shield_seconds",
            "soft_penalty_action",
            "auto_rubberband",
            "eliminate_player",
            "revive_player",
        ):
            handler = mgr._handlers[flag]
            assert handler is not mgr._noop_handler
