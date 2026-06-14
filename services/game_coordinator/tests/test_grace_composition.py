"""
Unit tests for the unified grace / shield composition rule (#817, epic #814).

Two actors grant temporary death-immunity through different fields:

- Admin baseline: ``invincible_until`` (from ``invincibility_seconds``), set by
  Tournament / Fight Club at match/round start. The human owns this.
- Agent delta: ``grace_until``, granted mid-game via ``grant_shield`` (also reused
  by respawn / spawn grace). The agent owns this and never persists it.

The ratified composition rule is::

    effective_grace_until = max(grace_until, invincible_until)

i.e. a player is death-immune while *either* window is open. The agent can only
ever EXTEND protection; neither source silently cancels the other.

These tests cover both the unified primitive (``effective_grace_until`` /
``is_in_grace`` / ``grant_shield``) and the mode-level death checks that route
through it (base per-frame gate, Tournament/Fight Club ``_kill_player_impl`` and
the Fight Club ``_process_controller_state`` gate). Telemetry is disabled via
conftest.
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

from conftest import EventCollector, MockControllerManagerService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.base import BaseGameMode, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.fight_club import FightClubGame, FightState
from services.game_coordinator.games.tournament import TournamentPlayer, TournamentState


class MockGameplayStream:
    """Minimal gameplay-stream mock that accepts writes silently."""

    async def write(self, message):
        pass


def _make_ffa_game():
    mock_cm = MockControllerManagerService(num_controllers=2)
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_grace_ffa",
    )
    game.gameplay_stream = MockGameplayStream()
    return game


# --------------------------------------------------------------------------- #
# effective_grace_until — the single composition primitive
# --------------------------------------------------------------------------- #
class TestEffectiveGraceUntil:
    """The static composition rule: max(grace_until, invincible_until)."""

    def test_base_player_has_no_invincible_until(self):
        """Base Player exposes only grace_until; helper reads invincible_until
        defensively, so a base Player's effective grace is just grace_until."""
        p = Player(serial="A")
        p.grace_until = 100.0
        assert not hasattr(p, "invincible_until")
        assert BaseGameMode.effective_grace_until(p) == 100.0

    def test_agent_only_grace(self):
        """Agent shield only (no admin invincibility) -> grace_until wins."""
        p = TournamentPlayer(serial="A")
        p.grace_until = 50.0
        p.invincible_until = 0.0
        assert BaseGameMode.effective_grace_until(p) == 50.0

    def test_admin_only_invincibility(self):
        """Admin invincibility only (no agent shield) -> invincible_until wins."""
        p = TournamentPlayer(serial="A")
        p.grace_until = 0.0
        p.invincible_until = 70.0
        assert BaseGameMode.effective_grace_until(p) == 70.0

    def test_both_active_shield_longer(self):
        """Shield extends beyond invincibility -> shield window wins."""
        p = TournamentPlayer(serial="A")
        p.invincible_until = 60.0
        p.grace_until = 90.0
        assert BaseGameMode.effective_grace_until(p) == 90.0

    def test_both_active_invincibility_longer(self):
        """Shield shorter than invincibility -> admin baseline still honored."""
        p = TournamentPlayer(serial="A")
        p.invincible_until = 80.0
        p.grace_until = 30.0
        assert BaseGameMode.effective_grace_until(p) == 80.0

    def test_neither_active(self):
        p = TournamentPlayer(serial="A")
        p.invincible_until = 0.0
        p.grace_until = 0.0
        assert BaseGameMode.effective_grace_until(p) == 0.0

    def test_is_in_grace_true_when_either_open(self):
        p = TournamentPlayer(serial="A")
        now = time.time()
        # admin window open, agent closed
        p.invincible_until = now + 10.0
        p.grace_until = 0.0
        game = _make_ffa_game()
        assert game.is_in_grace(p, now=now) is True

        # agent window open, admin closed
        p.invincible_until = 0.0
        p.grace_until = now + 10.0
        assert game.is_in_grace(p, now=now) is True

    def test_is_in_grace_false_when_both_expired(self):
        p = TournamentPlayer(serial="A")
        now = time.time()
        p.invincible_until = now - 5.0
        p.grace_until = now - 5.0
        game = _make_ffa_game()
        assert game.is_in_grace(p, now=now) is False


# --------------------------------------------------------------------------- #
# grant_shield extend-not-shorten against the *effective* grace
# --------------------------------------------------------------------------- #
class TestGrantShieldComposition:
    """grant_shield writes only grace_until and never shortens effective grace."""

    @pytest.mark.asyncio
    async def test_shield_writes_grace_not_invincible(self):
        """Agent shield writes grace_until only; admin invincible_until untouched."""
        game = _make_ffa_game()
        p = TournamentPlayer(serial="A", alive=True)
        p.invincible_until = 0.0
        game.players["A"] = p

        before = time.time()
        ok = await game.grant_shield("A", 5.0)
        assert ok is True
        assert p.grace_until >= before + 5.0
        # The agent must NEVER write the admin baseline field.
        assert p.invincible_until == 0.0

    @pytest.mark.asyncio
    async def test_shorter_shield_during_longer_admin_invincibility_is_noop(self):
        """Admin invincibility longer than the requested shield: shield must not
        shorten the effective grace (returns True, grace_until unchanged-low)."""
        game = _make_ffa_game()
        now = time.time()
        p = TournamentPlayer(serial="A", alive=True)
        p.invincible_until = now + 100.0  # long admin baseline
        p.grace_until = 0.0
        game.players["A"] = p

        ok = await game.grant_shield("A", 2.0)  # much shorter
        assert ok is True
        # grace_until must not be bumped to a value that would *shorten* protection
        # (it stays below the admin baseline; effective grace is unchanged).
        assert game.effective_grace_until(p) == pytest.approx(now + 100.0, abs=1.0)
        assert p.grace_until < p.invincible_until

    @pytest.mark.asyncio
    async def test_longer_shield_extends_beyond_admin_invincibility(self):
        """Shield longer than admin invincibility extends the effective grace."""
        game = _make_ffa_game()
        now = time.time()
        p = TournamentPlayer(serial="A", alive=True)
        p.invincible_until = now + 3.0
        p.grace_until = 0.0
        game.players["A"] = p

        ok = await game.grant_shield("A", 30.0)
        assert ok is True
        assert p.grace_until >= now + 30.0
        assert game.effective_grace_until(p) >= now + 30.0

    @pytest.mark.asyncio
    async def test_repeated_shields_only_extend(self):
        """Repeated shields never shorten; a shorter follow-up is a no-op."""
        game = _make_ffa_game()
        p = Player(serial="A", alive=True)
        game.players["A"] = p

        await game.grant_shield("A", 30.0)
        first = p.grace_until
        await game.grant_shield("A", 2.0)  # shorter, must not pull in
        assert p.grace_until == first


# --------------------------------------------------------------------------- #
# Base per-frame gate routes through the unified rule
# --------------------------------------------------------------------------- #
class TestBaseGateComposition:
    """The base _process_controller_state grace gate honors admin invincibility."""

    def _frame(self, x, y, z):
        return controller_manager_pb2.GameplayData(
            serial="A",
            accel=controller_manager_pb2.Vector3(x=x, y=y, z=z),
        )

    @pytest.mark.asyncio
    async def test_admin_invincibility_blocks_base_gate(self):
        """A player with only admin invincibility (grace_until=0) is protected by
        the base per-frame gate — proving the gate uses effective grace, not just
        grace_until."""
        game = _make_ffa_game()
        # TournamentPlayer carries invincible_until but FFA's base gate is mode-agnostic.
        p = TournamentPlayer(serial="A", alive=True)
        p.grace_until = 0.0
        p.invincible_until = time.time() + 60.0
        game.players["A"] = p

        kills = []

        async def mock_kill(serial, _accel_mag):
            kills.append(serial)
            p.alive = False

        game._kill_player = mock_kill

        for _ in range(20):
            await game._process_controller_state(self._frame(5.0, 3.0, 4.0))
        assert not kills, "Admin invincibility must block the base gate via effective grace"


# --------------------------------------------------------------------------- #
# Fight Club composition (its own gate previously ignored grace_until)
# --------------------------------------------------------------------------- #
class TestFightClubComposition:
    """Fight Club overrides _process_controller_state; agent shields must work."""

    def _fc_game(self):
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = FightClubGame(
            controller_manager_client=mock_cm,
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="test_grace_fc",
        )
        game.gameplay_stream = MockGameplayStream()
        return game

    def _frame(self, serial, x, y, z):
        return controller_manager_pb2.GameplayData(
            serial=serial,
            accel=controller_manager_pb2.Vector3(x=x, y=y, z=z),
        )

    @pytest.mark.asyncio
    async def test_agent_shield_protects_fight_club_fighter(self):
        """Before #817 Fight Club's gate only checked invincible_until and ignored
        grace_until, so an agent shield did nothing. Now the unified gate honors it."""
        game = self._fc_game()
        from services.game_coordinator.games.fight_club import FightClubPlayer

        defender = FightClubPlayer(serial="D", alive=True)
        defender.state = FightState.DEFENDER
        defender.invincible_until = 0.0  # admin invincibility expired
        defender.grace_until = time.time() + 60.0  # agent shield active
        game.players["D"] = defender
        game.current_defender = "D"

        # Lethal motion under an active agent shield must not kill.
        for _ in range(10):
            await game._process_controller_state(self._frame("D", 8.0, 8.0, 8.0))
        assert defender.alive, "Agent shield must protect a Fight Club fighter (#817)"

    @pytest.mark.asyncio
    async def test_kill_impl_blocked_by_agent_shield(self):
        """Fight Club _kill_player_impl honors an agent shield (grace_until)."""
        game = self._fc_game()
        from services.game_coordinator.games.fight_club import FightClubPlayer

        p = FightClubPlayer(serial="D", alive=True)
        p.state = FightState.DEFENDER
        p.invincible_until = 0.0
        p.grace_until = time.time() + 60.0
        game.players["D"] = p

        await game._kill_player_impl("D", 9.0)
        assert p.alive, "Agent shield must block Fight Club _kill_player_impl (#817)"

    @pytest.mark.asyncio
    async def test_kill_impl_blocked_by_admin_invincibility(self):
        """Admin invincibility still blocks Fight Club deaths (characterization)."""
        game = self._fc_game()
        from services.game_coordinator.games.fight_club import FightClubPlayer

        p = FightClubPlayer(serial="D", alive=True)
        p.state = FightState.DEFENDER
        p.invincible_until = time.time() + 60.0
        p.grace_until = 0.0
        game.players["D"] = p

        await game._kill_player_impl("D", 9.0)
        assert p.alive, "Admin invincibility must still block death"


# --------------------------------------------------------------------------- #
# Tournament composition
# --------------------------------------------------------------------------- #
class TestTournamentComposition:
    """Tournament _kill_player_impl honors both admin invincibility and shield."""

    def _t_game(self):
        mock_cm = MockControllerManagerService(num_controllers=2)
        from services.game_coordinator.games.tournament import TournamentGame

        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_grace_t",
        )
        game.gameplay_stream = MockGameplayStream()
        return game

    @pytest.mark.asyncio
    async def test_admin_invincibility_blocks_death(self):
        """Characterization: admin invincibility blocks Tournament death."""
        game = self._t_game()
        p = TournamentPlayer(serial="A", alive=True)
        p.tournament_state = TournamentState.FIGHTING
        p.invincible_until = time.time() + 60.0
        p.grace_until = 0.0
        game.players["A"] = p

        await game._kill_player_impl("A", 9.0)
        assert p.alive, "Admin invincibility must block Tournament death"

    @pytest.mark.asyncio
    async def test_agent_shield_blocks_death(self):
        """Agent shield (grace_until) blocks Tournament death even with admin
        invincibility expired (#817)."""
        game = self._t_game()
        p = TournamentPlayer(serial="A", alive=True)
        p.tournament_state = TournamentState.FIGHTING
        p.invincible_until = 0.0
        p.grace_until = time.time() + 60.0
        game.players["A"] = p

        await game._kill_player_impl("A", 9.0)
        assert p.alive, "Agent shield must block Tournament death (#817)"
