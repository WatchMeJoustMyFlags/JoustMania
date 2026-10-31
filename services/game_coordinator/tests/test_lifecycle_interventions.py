"""
Unit tests for the shield + lifecycle intervention handlers (#730, PR D).

Covers the three shield/lifecycle interventions and the BaseGameMode primitives
they drive:

- grant_shield (BaseGameMode): sets grace_until, sends the shield LED effect,
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
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature.flag_evaluation import FlagEvaluationDetails

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import BaseGameMode, GameState, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.zombie import ZombieGame
from services.game_coordinator.intervention_errors import InterventionNotApplied
from services.game_coordinator.interventions import (
    BLOCK_ELIMINATE_REFUSED,
    BLOCK_MODE_UNSUPPORTED,
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
    SessionView,
)
from services.game_coordinator.lifecycle_handlers import (
    ELIMINATE_REASON,
    handle_auto_rubberband,
    handle_eliminate_player,
    handle_revive_player,
    handle_shield_seconds,
    handle_soft_penalty,
    handle_toughness_boost,
    parse_auto_rubberband_value,
    parse_soft_penalty_value,
    parse_toughness_boost_value,
    register_lifecycle_handlers,
)


@pytest.fixture(autouse=True)
def _no_shield_limits():
    """Shield limits off (#2044, UC-040 BR-022): these tests are not about the cap or the cooldown."""
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


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

    def get_string_details(self, _key, default, ctx=None):
        return FlagEvaluationDetails(flag_key=_key, value=self.get_string_value(_key, default, ctx))


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
        before = game_clock.now()
        granted = await game.grant_shield("p1", 5.0)
        assert granted is True
        # grace_until pushed roughly 5s into the future.
        assert game.players["p1"].grace_until >= before + 4.5
        # The shield LED effect was sent (#1301; was a generic PULSE).
        assert controller_manager_pb2.GAME_EFFECT_SHIELD in game.gameplay_stream.effects()

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
    @pytest.mark.uc("UC-020", flow="A3")
    async def test_shields_only_targeted_serials(self):
        game = make_ffa_game(num_players=3)
        mgr, _ = make_manager(game=game)
        # p1 gets a 5s shield; p2/p3 resolve to the neutral default (0).
        mgr._interventions_client = TargetingFlagClient(default=0, per_serial={"p1": 5})

        before = game_clock.now()
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
# toughness_boost: value parser (#1129)
# --------------------------------------------------------------------------- #
class TestParseToughnessBoostValue:
    def test_seconds_only_defaults_boost(self):
        assert parse_toughness_boost_value("5") == (5.0, 2.0)

    def test_seconds_and_boost(self):
        assert parse_toughness_boost_value("8:1.8") == (8.0, 1.8)

    def test_seconds_capped(self):
        secs, _ = parse_toughness_boost_value("9999")
        assert secs == 30.0

    def test_boost_clamped(self):
        assert parse_toughness_boost_value("5:9")[1] == 2.0
        assert parse_toughness_boost_value("5:0.1")[1] == 1.0

    @pytest.mark.parametrize(
        "bad",
        ["", "0", "none", "off", "-3", "abc", "5:abc", "5:2:1", "  ", "x:y"],
    )
    def test_malformed_or_neutral_is_none(self, bad):
        assert parse_toughness_boost_value(bad) is None


# --------------------------------------------------------------------------- #
# toughness_boost handler (#1129)
# --------------------------------------------------------------------------- #
class TestToughnessBoostHandler:
    @pytest.mark.asyncio
    async def test_arms_only_targeted_serials(self):
        game = make_ffa_game(num_players=3)
        mgr, _ = make_manager(game=game)
        # p1 gets an 8s/1.8 boost; p2/p3 resolve to neutral "0".
        mgr._interventions_client = TargetingFlagClient(default="0", per_serial={"p1": "8:1.8"})

        await handle_toughness_boost(make_ctx("toughness_boost_seconds", "8:1.8", game), mgr)

        assert game.players["p1"].toughness_boost_until > game_clock.now()
        assert game.players["p1"].toughness_boost_factor == pytest.approx(1.8)
        assert game.players["p2"].toughness_boost_until == 0.0
        assert game.players["p3"].toughness_boost_until == 0.0

    @pytest.mark.asyncio
    async def test_malformed_value_is_safe_noop(self):
        game = make_ffa_game(num_players=1)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default="0", per_serial={"p1": "garbage"})

        await handle_toughness_boost(make_ctx("toughness_boost_seconds", "garbage", game), mgr)
        assert game.players["p1"].toughness_boost_until == 0.0

    @pytest.mark.asyncio
    async def test_battery_gated_serial_skipped(self):
        game = make_ffa_game(num_players=2)
        mgr, _ = make_manager(game=game, battery={"p1": 90, "p2": 5}, threshold=20)
        mgr._interventions_client = TargetingFlagClient(default="0", per_serial={"p1": "5", "p2": "5"})

        await handle_toughness_boost(make_ctx("toughness_boost_seconds", "5", game), mgr)
        assert game.players["p1"].toughness_boost_until > 0.0
        assert game.players["p2"].toughness_boost_until == 0.0

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        mgr, _ = make_manager(game=None)
        mgr._interventions_client = TargetingFlagClient(default="0")
        await handle_toughness_boost(make_ctx("toughness_boost_seconds", "5", None), mgr)  # no raise


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
        assert game.players["p1"].warning_until > game_clock.now()
        assert before == pytest.approx(game._compute_effective_thresholds(game.players["p1"]))
        assert game.players["p2"].warning_until == 0.0

    @pytest.mark.asyncio
    async def test_tighten_arms_only_targeted_serials(self):
        game = make_ffa_game(num_players=3)
        mgr, _ = make_manager(game=game)
        mgr._interventions_client = TargetingFlagClient(default="none", per_serial={"p1": "tighten:8:0.6"})

        await handle_soft_penalty(make_ctx("soft_penalty_action", "tighten:8:0.6", game), mgr)

        assert game.players["p1"].soft_penalty_until > game_clock.now()
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
        assert game.players["p1"].warning_until > game_clock.now()

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
    @pytest.mark.uc("UC-020", flow="A3")
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

        await handle_eliminate_player(make_ctx("eliminate_player", "p1", game, payload="p1", target="p1"))

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
            return await orig(serial, accel_mag, reason)

        game._kill_player = counting_kill

        # Empty serial -> no kill, no raise.
        await handle_eliminate_player(make_ctx("eliminate_player", "", game, payload=""))
        # No game -> no raise.
        await handle_eliminate_player(make_ctx("eliminate_player", "", None, payload="p1"))

        assert calls == []

    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_unknown_or_dead_target_is_refused(self):
        """#1383: an eliminate that kills nobody is not applied (mirrors revive_refused, #1969)."""
        game = make_ffa_game(num_players=1)
        with pytest.raises(InterventionNotApplied) as unknown:
            await handle_eliminate_player(make_ctx("eliminate_player", "", game, payload="ghost"))
        assert unknown.value.reason == BLOCK_ELIMINATE_REFUSED

        game.players["p1"].alive = False
        with pytest.raises(InterventionNotApplied) as dead:
            await handle_eliminate_player(make_ctx("eliminate_player", "", game, payload="p1"))
        assert dead.value.reason == BLOCK_ELIMINATE_REFUSED
        assert game.dead_count == 0

    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_kill_declined_by_the_mode_is_refused(self):
        """#1383: the mode's kill gate declines the kill, so nothing was applied."""
        game = make_ffa_game(num_players=2)
        game._accepts_kill = lambda _serial: False

        with pytest.raises(InterventionNotApplied) as exc:
            await handle_eliminate_player(make_ctx("eliminate_player", "p1", game, payload="p1", target="p1"))
        assert exc.value.reason == BLOCK_ELIMINATE_REFUSED
        assert game.players["p1"].alive is True
        assert game.dead_count == 0

    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_legacy_kill_returning_none_counts_as_applied(self):
        """Only an explicit False is a refusal: an override returning None still applied."""
        game = make_ffa_game(num_players=2)
        killed = []

        async def legacy_kill(serial, accel_mag, reason="motion"):  # noqa: ARG001
            killed.append(serial)

        game._kill_player = legacy_kill

        await handle_eliminate_player(make_ctx("eliminate_player", "p1", game, payload="p1"))
        assert killed == ["p1"]


# --------------------------------------------------------------------------- #
# revive_player handler + primitive
# --------------------------------------------------------------------------- #
class TestRevivePlayer:
    @pytest.mark.asyncio
    async def test_revive_in_respawn_mode_uses_native_path(self):
        game, mock_cm = make_nonstop_game(num_players=2)
        game.state = GameState.RUNNING  # NonStop refuses a revive outside RUNNING (UC-026 BR-003)
        await game._initialize_players_impl(mock_cm.controllers)
        serial = next(iter(game.players))
        game.players[serial].alive = False

        before = game_clock.now()
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

        before = game_clock.now()
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

    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_refused_nonstop_revive_after_game_end_is_not_applied(self):
        """#1948: NonStop refuses a revive once the game ended; the handler reports not applied."""
        game, mock_cm = make_nonstop_game(num_players=2)
        await game._initialize_players_impl(mock_cm.controllers)
        serial = next(iter(game.players))
        game.players[serial].alive = False
        game.state = GameState.ENDED

        with pytest.raises(InterventionNotApplied) as exc:
            await handle_revive_player(make_ctx("revive_player", "", game, payload=serial))
        assert exc.value.reason == "revive_refused"
        assert game.players[serial].alive is False

    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_refused_zombie_revive_after_the_win_is_not_applied(self):
        """#1948: Zombies refuses a revive once the win is latched; the handler reports not applied."""
        mock_cm = MockControllerManagerService(num_controllers=4)
        game = ZombieGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_lifecycle_zombie",
        )
        await game._initialize_players_impl(mock_cm.controllers)
        game.gameplay_stream = RecordingGameplayStream()
        game.running = True
        game._play_sound = async_noop
        game.zombie_respawn_min = game.zombie_respawn_max = 60.0
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        game._winner_side = "humans"
        try:
            with pytest.raises(InterventionNotApplied) as exc:
                await handle_revive_player(make_ctx("revive_player", "", game, payload=zombie))
            assert exc.value.reason == "revive_refused"
            assert game.players[zombie].alive is False
        finally:
            await game.cleanup()


# --------------------------------------------------------------------------- #
# Coordinator-side block in a real FFA game (UC-040 A22)
# --------------------------------------------------------------------------- #
class _AllowingAgentStub(_AgentStub):
    """Agent-domain stub whose interventions_allowed lists ``allowed``."""

    def __init__(self, allowed, threshold=20):
        super().__init__(threshold)
        self._allowed = allowed

    def get_string_value(self, key, default, _ctx=None):
        if key == "interventions_allowed":
            return ",".join(self._allowed)
        return default


class TestCoordinatorBlocksInFFA:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A22", br=["BR-004"])
    @pytest.mark.uc("UC-020", flow="A3")
    async def test_revive_player_in_real_ffa_is_mode_unsupported(self):
        """UC-040 A22: revive_player passes the allow-list (the `full` variant offers
        it), but the capability matrix denies it in a real FFA game: the dead player
        stays dead, agent_intervention is published with blocked=true and
        mode_unsupported, no backstop budget is used and no agent cue is sent.
        Unlike test_interventions.py::test_mode_matrix_blocks_revive_in_ffa (a FakeGame
        named "FFA"), this drives the real FFAGame and the registered revive handler."""
        game = make_ffa_game(num_players=2)
        game.players["p1"].alive = False
        mgr, events = make_manager(game=game)
        mgr._agent_client = _AllowingAgentStub(["revive_player"])
        register_lifecycle_handlers(mgr)
        assert "revive_player" in mgr._allowed_types()  # the block is not membership

        session = SessionView(game_id="", game=game, publish=mgr._publish)
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr._enforce_and_dispatch(_spec("revive_player"), "p1", "p1", "p1", session)

        assert game.players["p1"].alive is False
        assert game.gameplay_stream.writes == []  # no respawn effect, no magenta cue
        published = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION]
        assert len(published) == 1
        assert published[0]["type"] == "revive_player"
        assert published[0]["blocked"] == "true"
        assert published[0]["block_reason"] == BLOCK_MODE_UNSUPPORTED
        assert mgr._rate_limiter.current_weight() == pytest.approx(0.0)


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

        assert game.players["p2"].rubberband_until > game_clock.now()
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
            "toughness_boost_seconds",
            "soft_penalty_action",
            "auto_rubberband",
            "eliminate_player",
            "revive_player",
        ):
            handler = mgr._handlers[flag]
            assert handler is not mgr._noop_handler


# --------------------------------------------------------------------------- #
# #1295: the span event recording an applied targeted intervention names the player
# --------------------------------------------------------------------------- #
def _named_game_with_spans(num_players=2):
    """FFA game whose players carry display names and autospec'd lifecycle spans.

    Telemetry is disabled in tests (the SDK hands out non-recording spans), so the
    span is an autospec of ``trace.Span`` with an invalid span context (what the
    trace-context injection reads) and its ``add_event`` calls are asserted.
    """
    from unittest.mock import create_autospec

    from opentelemetry import trace

    game = make_ffa_game(num_players=num_players)
    names = ["Calm Lion", "Swift Fox", "Bold Bear"]
    for i, player in enumerate(game.players.values()):
        player.name = names[i]
        span = create_autospec(trace.Span, instance=True)
        span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
        player.span = span
    return game


def _span_event_attrs(span, event_name):
    """Attributes of the single ``add_event(event_name, ...)`` call on ``span``."""
    calls = [c for c in span.add_event.call_args_list if c.args and c.args[0] == event_name]
    assert len(calls) == 1, f"{event_name}: {span.add_event.call_args_list}"
    return calls[0].kwargs["attributes"]


def _event_attrs(player, event_name):
    return _span_event_attrs(player.span, event_name)


class TestAppliedInterventionEventsNamePlayer:
    """UC-040 step 13: the coordinator's record of an applied intervention names the player."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="main", steps=[13])
    async def test_shield_granted_carries_serial_and_name(self):
        """UC-040 main: shield_granted carries player.serial and player.name."""
        game = _named_game_with_spans()
        assert await game.grant_shield("p1", 5.0) is True
        attrs = _event_attrs(game.players["p1"], "shield_granted")
        assert attrs["player.serial"] == "p1"
        assert attrs["player.name"] == "Calm Lion"
        assert attrs["shield_seconds"] == 5.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="main", steps=[13])
    async def test_toughness_boost_and_soft_penalty_carry_identity(self):
        """UC-040 main: toughness_boost_armed / soft_penalty_tighten name the player."""
        game = _named_game_with_spans()
        assert await game.grant_toughness_boost("p1", 5.0, 1.5) is True
        assert await game.apply_soft_penalty("p2", 5.0, 0.6) is True
        shield = _event_attrs(game.players["p1"], "toughness_boost_armed")
        penalty = _event_attrs(game.players["p2"], "soft_penalty_tighten")
        assert (shield["player.serial"], shield["player.name"]) == ("p1", "Calm Lion")
        assert (penalty["player.serial"], penalty["player.name"]) == ("p2", "Swift Fox")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="main", steps=[13])
    async def test_revive_and_eliminate_carry_identity(self):
        """UC-040 main: player_revived and the agent-forced player_death name the player."""
        game = _named_game_with_spans()
        game.players["p1"].alive = False
        assert await game.revive_player("p1") is True
        revived = _event_attrs(game.players["p1"], "player_revived")
        assert (revived["player.serial"], revived["player.name"]) == ("p1", "Calm Lion")

        p2 = game.players["p2"]
        span = p2.span  # _kill_player may close and clear the span
        await handle_eliminate_player(make_ctx("eliminate_player", "p2", game, payload="p2", target="p2"))
        # Exactly one player_death per death (#1327): the base kill path's, which
        # names the player and carries death.reason.
        deaths = [
            c.kwargs["attributes"] for c in span.add_event.call_args_list if c.args and c.args[0] == "player_death"
        ]
        assert len(deaths) == 1
        attrs = deaths[0]
        assert attrs["player.serial"] == "p2"
        assert attrs["player.name"] == "Swift Fox"
        assert attrs["death.reason"] == ELIMINATE_REASON

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="main", steps=[13])
    async def test_rubberband_boost_carries_identity(self):
        """UC-040 main: auto_rubberband_boost names each boosted player."""
        game = _named_game_with_spans()
        game.players["p1"].smoothed_accel = 0.2
        game.players["p2"].smoothed_accel = 1.5
        assert await game.apply_auto_rubberband("strong") >= 1
        attrs = _event_attrs(game.players["p2"], "auto_rubberband_boost")
        assert (attrs["player.serial"], attrs["player.name"]) == ("p2", "Swift Fox")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="main", steps=[13])
    async def test_unnamed_player_carries_serial_only(self):
        """UC-040 main: before the controller reported a name, only player.serial is set."""
        game = _named_game_with_spans()
        game.players["p1"].name = ""
        assert await game.grant_shield("p1", 5.0) is True
        attrs = _event_attrs(game.players["p1"], "shield_granted")
        assert attrs["player.serial"] == "p1"
        assert "player.name" not in attrs
