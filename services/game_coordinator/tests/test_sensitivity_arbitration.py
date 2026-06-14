"""
Sensitivity arbitration tests (#816, epic #814 / M9 ownership model).

These exercise the admin-baseline vs. agent-override arbitration for global
sensitivity. The ratified ownership model (epic #814):

- Human owns PRE-GAME intent: the lobby baseline sensitivity.
- Agent owns bounded IN-GAME deltas: ``global_sensitivity_override`` is a
  temporary override layer applied ON TOP of the baseline.
- Agent NEVER persists into game.json (it mutates only the live override layer).
- A human baseline change INVALIDATES any active agent override for that
  parameter, so the agent's override never clobbers fresh admin intent.

Concretely the game exposes:
- ``baseline_sensitivity``  — admin source of truth (game-start value, updatable
  mid-game via the admin setter, which invalidates the override).
- ``sensitivity_override``  — the agent override layer (``None`` = no override).
- ``sensitivity``           — read-only EFFECTIVE level = override if active else
  baseline; what ``_compute_effective_thresholds`` and metrics read.

The first class below is the characterization suite (current public behavior
that must be preserved). The later classes are the new edge cases the fix adds.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, async_noop  # noqa: E402

from lib.types import Sensitivity  # noqa: E402
from services.game_coordinator.difficulty_handlers import (  # noqa: E402
    handle_global_sensitivity_override,
)
from services.game_coordinator.games.base import Player  # noqa: E402
from services.game_coordinator.games.ffa import FFAGame  # noqa: E402
from services.game_coordinator.interventions import (  # noqa: E402
    INTERVENTION_SPECS,
    InterventionContext,
)


def make_game(sensitivity=2, num_players=2):
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=AsyncMock(),
        game_id="test_sensitivity_arbitration",
        sensitivity=sensitivity,
    )
    for i in range(num_players):
        s = f"p{i + 1}"
        game.players[s] = Player(serial=s, alive=True, color=(255, 0, 0))
    return game


def _spec(flag_key):
    return next(s for s in INTERVENTION_SPECS if s.flag_key == flag_key)


def make_ctx(value, game):
    return InterventionContext(
        spec=_spec("global_sensitivity_override"),
        value=value,
        payload="",
        target_serial=None,
        game=game,
        objective="balanced",
    )


# --------------------------------------------------------------------------- #
# Characterization: behavior that must be preserved across the fix.
# --------------------------------------------------------------------------- #
class TestCharacterizationPreserved:
    def test_effective_sensitivity_equals_baseline_with_no_override(self):
        game = make_game(sensitivity=1)
        assert game.sensitivity == Sensitivity.SLOW

    @pytest.mark.asyncio
    async def test_agent_override_changes_effective_sensitivity(self):
        game = make_game(sensitivity=0)
        await handle_global_sensitivity_override(make_ctx(4, game))
        assert game.sensitivity == Sensitivity.ULTRA_FAST

    @pytest.mark.asyncio
    async def test_override_clear_returns_to_baseline(self):
        game = make_game(sensitivity=1)  # SLOW baseline
        await handle_global_sensitivity_override(make_ctx(3, game))
        assert game.sensitivity == Sensitivity.FAST
        await handle_global_sensitivity_override(make_ctx(-1, game))
        assert game.sensitivity == Sensitivity.SLOW

    @pytest.mark.asyncio
    async def test_invalid_index_ignored(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(99, game))
        assert game.sensitivity == Sensitivity.MEDIUM

    @pytest.mark.asyncio
    async def test_no_live_game_is_noop(self):
        await handle_global_sensitivity_override(make_ctx(3, None))  # no raise

    @pytest.mark.asyncio
    async def test_override_changes_effective_thresholds(self):
        game = make_game(sensitivity=0)
        player = game.players["p1"]
        _, death_before = game._compute_effective_thresholds(player)
        await handle_global_sensitivity_override(make_ctx(4, game))
        _, death_after = game._compute_effective_thresholds(player)
        assert death_after != death_before

    def test_admin_setter_via_sensitivity_attribute_sets_effective(self):
        """Legacy/admin path: assigning game.sensitivity sets the effective level.

        Existing fixtures and the lobby path assign ``game.sensitivity = X``; that
        must keep setting the effective sensitivity (it sets the baseline).
        """
        game = make_game(sensitivity=2)
        game.sensitivity = Sensitivity.ULTRA_FAST
        assert game.sensitivity == Sensitivity.ULTRA_FAST


# --------------------------------------------------------------------------- #
# Agent never persists into game.json (the writers only touch live state).
# --------------------------------------------------------------------------- #
class TestAgentDoesNotPersist:
    @pytest.mark.asyncio
    async def test_override_does_not_mutate_baseline(self):
        game = make_game(sensitivity=2)  # MEDIUM baseline
        await handle_global_sensitivity_override(make_ctx(4, game))
        # Effective is the override, but the admin baseline is untouched.
        assert game.sensitivity == Sensitivity.ULTRA_FAST
        assert game.baseline_sensitivity == Sensitivity.MEDIUM

    @pytest.mark.asyncio
    async def test_override_clear_leaves_baseline_intact(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(0, game))
        await handle_global_sensitivity_override(make_ctx(-1, game))
        assert game.baseline_sensitivity == Sensitivity.MEDIUM
        assert game.sensitivity_override is None


# --------------------------------------------------------------------------- #
# Restore semantics: clear restores the CURRENT baseline, not a game-start snap.
# --------------------------------------------------------------------------- #
class TestRestoreToCurrentBaseline:
    @pytest.mark.asyncio
    async def test_clear_with_no_admin_change_restores_baseline(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(4, game))
        await handle_global_sensitivity_override(make_ctx(-1, game))
        assert game.sensitivity == Sensitivity.MEDIUM


# --------------------------------------------------------------------------- #
# Admin baseline change INVALIDATES an active agent override.
# --------------------------------------------------------------------------- #
class TestAdminBaselineChangeInvalidatesOverride:
    @pytest.mark.asyncio
    async def test_admin_change_during_active_override_takes_effect(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(4, game))
        assert game.sensitivity == Sensitivity.ULTRA_FAST

        # Admin changes the baseline mid-game -> override invalidated, admin wins.
        game.set_baseline_sensitivity(Sensitivity.SLOW)
        assert game.sensitivity == Sensitivity.SLOW
        assert game.sensitivity_override is None

    @pytest.mark.asyncio
    async def test_admin_setter_attribute_also_invalidates_override(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(4, game))
        game.sensitivity = Sensitivity.SLOW  # admin via attribute
        assert game.sensitivity == Sensitivity.SLOW
        assert game.sensitivity_override is None

    @pytest.mark.asyncio
    async def test_clear_after_admin_change_restores_new_baseline(self):
        """After admin changes baseline, a stale override clear must NOT clobber
        the new admin intent with the old game-start value."""
        game = make_game(sensitivity=2)  # game-start MEDIUM
        await handle_global_sensitivity_override(make_ctx(0, game))  # agent -> ULTRA_SLOW
        game.set_baseline_sensitivity(Sensitivity.FAST)  # admin mid-game change
        # Override already invalidated; a late clear is a no-op on the baseline.
        await handle_global_sensitivity_override(make_ctx(-1, game))
        assert game.sensitivity == Sensitivity.FAST

    @pytest.mark.asyncio
    async def test_new_override_after_admin_change_layers_on_new_baseline(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(0, game))
        game.set_baseline_sensitivity(Sensitivity.SLOW)
        # Agent issues a fresh override against the new baseline.
        await handle_global_sensitivity_override(make_ctx(3, game))
        assert game.sensitivity == Sensitivity.FAST
        await handle_global_sensitivity_override(make_ctx(-1, game))
        assert game.sensitivity == Sensitivity.SLOW  # the NEW baseline


# --------------------------------------------------------------------------- #
# Multiple sequential overrides without an intervening clear.
# --------------------------------------------------------------------------- #
class TestMultipleOverrides:
    @pytest.mark.asyncio
    async def test_second_override_replaces_first(self):
        game = make_game(sensitivity=2)
        await handle_global_sensitivity_override(make_ctx(0, game))
        assert game.sensitivity == Sensitivity.ULTRA_SLOW
        await handle_global_sensitivity_override(make_ctx(4, game))
        assert game.sensitivity == Sensitivity.ULTRA_FAST
        # Clearing returns to the untouched baseline, not the first override.
        await handle_global_sensitivity_override(make_ctx(-1, game))
        assert game.sensitivity == Sensitivity.MEDIUM

    @pytest.mark.asyncio
    async def test_no_admin_change_baseline_constant_across_overrides(self):
        game = make_game(sensitivity=3)
        for idx in (0, 1, 4, 2):
            await handle_global_sensitivity_override(make_ctx(idx, game))
            assert game.baseline_sensitivity == Sensitivity.FAST
