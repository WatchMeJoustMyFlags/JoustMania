"""
Unit tests for the #766 F6 intervention levers.

Two new state-shaped interventions:

- global_difficulty_factor (adjust_global_difficulty): combined with each
  player's sensitivity_factor in the threshold division; live-read per frame;
  revert restores the neutral 1.0.
- pacing_profile (set_pacing_profile): atomically swaps the live music windows
  to a named preset (resolved via F3's resolve_music_windows); revert restores
  the game's init-resolved windows; unknown presets fall back safely.

Also covers registry well-formedness, enforcement (weight Medium charged, mode
gates inherited from adjust_global_sensitivity / adjust_music_tempo).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop

from services.game_coordinator import difficulty_handlers as dh
from services.game_coordinator.difficulty_handlers import (
    GLOBAL_DIFFICULTY_FACTOR_DEFAULT,
    handle_global_difficulty_factor,
    handle_global_difficulty_factor_revert,
    handle_pacing_profile,
    handle_pacing_profile_revert,
    register_difficulty_handlers,
)
from services.game_coordinator.games.base import MusicWindows, Player, resolve_music_windows
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    MODE_CAPABILITY_DENY,
    WEIGHT_MEDIUM,
    InterventionContext,
    InterventionManager,
    SessionView,
)


def _primary_view(game, mgr):
    """Synthesize the primary SessionView for white-box _enforce_and_dispatch
    calls (#838: the method is now per-session)."""
    return SessionView(game_id="", game=game, publish=mgr._publish)


# Preset window dicts mirroring services/flagd/game.json variants.
_CALM = {
    "min_music_fast_time": 3,
    "max_music_fast_time": 6,
    "min_music_slow_time": 13,
    "max_music_slow_time": 30,
    "end_min_music_fast_time": 4,
    "end_max_music_fast_time": 7,
    "end_min_music_slow_time": 10,
    "end_max_music_slow_time": 16,
}
_FRANTIC = {
    "min_music_fast_time": 6,
    "max_music_fast_time": 11,
    "min_music_slow_time": 6,
    "max_music_slow_time": 14,
    "end_min_music_fast_time": 8,
    "end_max_music_fast_time": 14,
    "end_min_music_slow_time": 5,
    "end_max_music_slow_time": 8,
}
_PRESETS = {"calm": _CALM, "frantic": _FRANTIC}


def make_game(sensitivity=2, num_players=2):
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=AsyncMock(),
        game_id="test_f6",
        sensitivity=sensitivity,
    )
    for i in range(num_players):
        s = f"p{i + 1}"
        game.players[s] = Player(serial=s, alive=True, color=(255, 0, 0))
    return game


def _spec(flag_key):
    return next(s for s in INTERVENTION_SPECS if s.flag_key == flag_key)


def make_ctx(flag_key, value, game):
    return InterventionContext(
        spec=_spec(flag_key),
        value=value,
        payload="",
        target_serial=None,
        game=game,
        objective="balanced",
    )


def _patch_windows_resolution():
    """Patch read_object_flag_variant to resolve presets like flagd targeting:
    known name -> its preset dict; unknown -> the 'default' (empty -> defaults)."""

    def fake(_domain, _flag, targeting_key, _default):
        return _PRESETS.get(targeting_key, {})

    return patch.object(dh, "read_object_flag_variant", side_effect=fake)


# --------------------------------------------------------------------------- #
# global_difficulty_factor
# --------------------------------------------------------------------------- #
class TestGlobalDifficultyFactor:
    @pytest.mark.asyncio
    async def test_neutral_one_preserves_baseline(self):
        game = make_game()
        player = game.players["p1"]
        base_warn, base_death = game._compute_effective_thresholds(player)
        # Applying 1.0 explicitly leaves thresholds unchanged.
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 1.0, game))
        w, d = game._compute_effective_thresholds(player)
        assert w == pytest.approx(base_warn)
        assert d == pytest.approx(base_death)

    @pytest.mark.asyncio
    async def test_factor_combines_with_player_factor(self):
        game = make_game()
        player = game.players["p1"]
        player.sensitivity_factor = 1.0
        base_warn, base_death = game._compute_effective_thresholds(player)

        # 2.0 global -> thresholds divided by 2.0 (easier to die).
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 2.0, game))
        w, d = game._compute_effective_thresholds(player)
        assert w == pytest.approx(base_warn / 2.0)
        assert d == pytest.approx(base_death / 2.0)

        # 0.5 global -> thresholds divided by 0.5 (harder to die).
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 0.5, game))
        w, d = game._compute_effective_thresholds(player)
        assert w == pytest.approx(base_warn / 0.5)
        assert d == pytest.approx(base_death / 0.5)

    @pytest.mark.asyncio
    async def test_combined_factor_is_clamped(self):
        game = make_game()
        player = game.players["p1"]
        player.sensitivity_factor = 2.0  # already at max
        base_warn, _ = game._compute_effective_thresholds(player)  # uses combined clamp
        # Without global factor, combined = 2.0 (clamped to 2.0).
        clamped_warn_no_global = base_warn

        # global 2.0 -> combined 4.0 -> clamped back to 2.0: same thresholds.
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 2.0, game))
        w, _ = game._compute_effective_thresholds(player)
        assert w == pytest.approx(clamped_warn_no_global)

    @pytest.mark.asyncio
    async def test_handler_clamps_stored_value(self):
        game = make_game()
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 5.0, game))
        assert game.global_difficulty_factor == pytest.approx(2.0)
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 0.1, game))
        assert game.global_difficulty_factor == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_revert_restores_neutral(self):
        game = make_game()
        game.global_difficulty_factor = 1.8
        await handle_global_difficulty_factor_revert(make_ctx("global_difficulty_factor", 1.0, game))
        assert game.global_difficulty_factor == pytest.approx(GLOBAL_DIFFICULTY_FACTOR_DEFAULT)

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        await handle_global_difficulty_factor(make_ctx("global_difficulty_factor", 1.5, None))
        await handle_global_difficulty_factor_revert(make_ctx("global_difficulty_factor", 1.0, None))


# --------------------------------------------------------------------------- #
# pacing_profile
# --------------------------------------------------------------------------- #
class TestPacingProfile:
    @pytest.mark.asyncio
    async def test_swap_is_atomic_and_correct(self):
        game = make_game()
        init = game.music_windows
        with _patch_windows_resolution():
            await handle_pacing_profile(make_ctx("pacing_profile", "frantic", game))
        # New instance assigned (atomic store; original untouched).
        assert game.music_windows is not init
        assert game.music_windows == resolve_music_windows(_FRANTIC)
        # init reference preserved unchanged.
        assert game.init_music_windows == init

    @pytest.mark.asyncio
    async def test_revert_restores_init_windows(self):
        game = make_game()
        init = game.init_music_windows
        with _patch_windows_resolution():
            await handle_pacing_profile(make_ctx("pacing_profile", "calm", game))
        assert game.music_windows != init
        await handle_pacing_profile_revert(make_ctx("pacing_profile", "none", game))
        assert game.music_windows is init

    @pytest.mark.asyncio
    async def test_neutral_profile_restores_init(self):
        game = make_game()
        init = game.init_music_windows
        with _patch_windows_resolution():
            await handle_pacing_profile(make_ctx("pacing_profile", "frantic", game))
            assert game.music_windows != init
            # "default"/"none"/"" all mean neutral.
            await handle_pacing_profile(make_ctx("pacing_profile", "default", game))
        assert game.music_windows is init

    @pytest.mark.asyncio
    async def test_unknown_preset_falls_back_to_init(self):
        game = make_game()
        # Start the game with non-default (custom) windows so fallback is observable.
        custom = resolve_music_windows(_CALM)
        game.music_windows = custom
        game.init_music_windows = custom
        with _patch_windows_resolution():
            # 'bogus' resolves to {} -> resolve_music_windows -> module defaults;
            # handler prefers the game's init windows over bare defaults.
            await handle_pacing_profile(make_ctx("pacing_profile", "bogus", game))
        assert game.music_windows is custom

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        with _patch_windows_resolution():
            await handle_pacing_profile(make_ctx("pacing_profile", "calm", None))
        await handle_pacing_profile_revert(make_ctx("pacing_profile", "none", None))


# --------------------------------------------------------------------------- #
# Registry / policy wiring
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_rows_well_formed(self):
        diff = _spec("global_difficulty_factor")
        assert diff.type_id == "adjust_global_difficulty"
        assert diff.weight == WEIGHT_MEDIUM
        assert diff.edge_triggered is False
        assert diff.player_targeted is False
        assert diff.value_kind == "float"
        assert diff.none_value == pytest.approx(1.0)

        pacing = _spec("pacing_profile")
        assert pacing.type_id == "set_pacing_profile"
        assert pacing.weight == WEIGHT_MEDIUM
        assert pacing.edge_triggered is False
        assert pacing.player_targeted is False
        assert pacing.value_kind == "string"
        assert pacing.none_value == "none"

    def test_mode_gates_inherited(self):
        # adjust_global_difficulty inherits the adjust_global_sensitivity deny
        # rows (role-threshold modes); set_pacing_profile inherits adjust_music_tempo
        # (allowed everywhere — never denied).
        for mode, denied in MODE_CAPABILITY_DENY.items():
            if "adjust_global_sensitivity" in denied:
                assert "adjust_global_difficulty" in denied, mode
            assert "set_pacing_profile" not in denied, mode
        # Specifically: Werewolf/Traitor/Zombie deny global difficulty.
        for mode in ("Werewolf", "Traitor", "Zombie"):
            assert "adjust_global_difficulty" in MODE_CAPABILITY_DENY[mode]


# --------------------------------------------------------------------------- #
# Enforcement (weight charged, mode gate)
# --------------------------------------------------------------------------- #
def make_manager(game=None, allowed=None, budget=10):
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(
        event_publisher=publisher,
        get_game=lambda: game,
    )
    mgr._agent_client = _AgentStub(allowed or [], budget)
    mgr._rate_limiter.set_budget(budget)
    return mgr, events


class _AgentStub:
    def __init__(self, allowed, budget):
        self._allowed = allowed
        self._budget = budget

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.max_interventions_per_minute":
            return self._budget
        if key == "policy.battery_threshold":
            return 20
        return default

    def get_string_value(self, key, default, _ctx=None):
        # interventions_allowed is a STRING flag: comma-separated ids (#1127).
        # Call sites still pass a list; join it here to match the flag format.
        if key == "interventions_allowed":
            return ",".join(self._allowed)
        return default

    def get_object_value(self, key, default, _ctx=None):
        return default


class TestEnforcement:
    @pytest.mark.asyncio
    async def test_weight_medium_charged(self):
        game = make_game()
        mgr, _ = make_manager(game=game, allowed=["adjust_global_difficulty"], budget=10)
        register_difficulty_handlers(mgr)
        before = mgr._rate_limiter.current_weight()
        await mgr._enforce_and_dispatch(_spec("global_difficulty_factor"), 1.5, "", None, _primary_view(game, mgr))
        after = mgr._rate_limiter.current_weight()
        assert after - before == pytest.approx(WEIGHT_MEDIUM)
        assert game.global_difficulty_factor == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_mode_gate_blocks_difficulty_in_werewolf(self):
        game = make_game()
        game.get_game_name = lambda: "Werewolf"
        mgr, events = make_manager(game=game, allowed=["adjust_global_difficulty"], budget=10)
        register_difficulty_handlers(mgr)
        await mgr._enforce_and_dispatch(_spec("global_difficulty_factor"), 1.5, "", None, _primary_view(game, mgr))
        # Blocked: factor never applied; a blocked event was published.
        assert game.global_difficulty_factor == pytest.approx(1.0)
        assert any(d["blocked"] == "true" and d["block_reason"] == "mode_unsupported" for _, d in events)

    @pytest.mark.asyncio
    async def test_pacing_allowed_everywhere(self):
        game = make_game()
        game.get_game_name = lambda: "Werewolf"
        mgr, _ = make_manager(game=game, allowed=["set_pacing_profile"], budget=10)
        register_difficulty_handlers(mgr)
        init = game.init_music_windows
        with _patch_windows_resolution():
            await mgr._enforce_and_dispatch(_spec("pacing_profile"), "frantic", "", None, _primary_view(game, mgr))
        # Not mode-gated; swap applied.
        assert game.music_windows != init
        assert isinstance(game.music_windows, MusicWindows)
