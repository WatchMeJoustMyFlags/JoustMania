"""
Motion processing tests for all game modes.

Ensures every game mode correctly handles _process_controller_state with real
protobuf messages. This prevents the FightClub "state.accel_x" bug (PR #501)
where flat attribute access was used instead of nested proto Vector3 fields.

For each game mode, tests:
1. Real proto message doesn't crash (no AttributeError on accel.x)
2. Gentle motion doesn't kill
3. Lethal motion kills (after enough EMA frames)
4. Mode-specific guards (spawn protection, invincibility, etc.)
"""

import sys
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
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.random_teams import RandomTeamsGame
from services.game_coordinator.games.swapper import SwapperGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.games.tournament import TournamentGame, TournamentState
from services.game_coordinator.games.traitor import TraitorGame
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.games.zombie import ZombieGame


class MockGameplayStream:
    """Mock bidirectional stream for testing."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


def _gentle_state(serial: str) -> controller_manager_pb2.GameplayData:
    """Create a gentle (non-lethal) controller state proto."""
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=0.1, y=0.0, z=1.0),
    )


def _lethal_state(serial: str) -> controller_manager_pb2.GameplayData:
    """Create a lethal (high acceleration) controller state proto."""
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
    )


# ─── FFA ───────────────────────────────────────────────────────────────────


class TestFFAMotionProcessing:
    """FFA uses base _process_controller_state — verify real proto works."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_ffa_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill player after EMA builds up."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False


# ─── Nonstop Joust ─────────────────────────────────────────────────────────


class TestNonstopMotionProcessing:
    """Nonstop overrides _process_controller_state for spawn protection."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = NonstopJoustGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_nonstop_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0
        game.players[serial].spawn_protected = False

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0
        game.players[serial].spawn_protected = False

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False

    @pytest.mark.asyncio
    async def test_spawn_protection_blocks_death(self, game):
        """Spawn-protected player should survive lethal motion (real proto)."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].spawn_protected = True

        for _ in range(10):
            await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is True


# ─── Teams ─────────────────────────────────────────────────────────────────


class TestTeamsMotionProcessing:
    """Teams uses base _process_controller_state."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = SimpleTeamsGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_teams_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False


# ─── Random Teams ──────────────────────────────────────────────────────────


class TestRandomTeamsMotionProcessing:
    """Random Teams uses base _process_controller_state."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = RandomTeamsGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_random_teams_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False


# ─── Swapper ───────────────────────────────────────────────────────────────


class TestSwapperMotionProcessing:
    """Swapper uses base _process_controller_state; death triggers team swap."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = SwapperGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_swapper_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_swaps_team(self, game):
        """Lethal motion should trigger team swap (Swapper's death handler)."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        original_team = game.players[serial].team
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            await game._process_controller_state(_lethal_state(serial))

        # Swapper: death swaps team, player stays alive
        assert game.players[serial].team != original_team


# ─── Traitor ───────────────────────────────────────────────────────────────


class TestTraitorMotionProcessing:
    """Traitor uses base _process_controller_state."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = TraitorGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False


# ─── Zombie ────────────────────────────────────────────────────────────────


class TestZombieMotionProcessing:
    """Zombie uses base _process_controller_state; has custom thresholds."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=6, death_schedule={})
        game = ZombieGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_zombie_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill human player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        # Find a human player
        serial = next(s for s, p in game.players.items() if not p.is_zombie)
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False


# ─── Werewolf ──────────────────────────────────────────────────────────────


class TestWerewolfMotionProcessing:
    """Werewolf uses base _process_controller_state; has custom thresholds."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = WerewolfGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_werewolf_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_lethal_motion_causes_death(self, game):
        """Lethal motion with real proto should kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(10):
            if game.players[serial].alive:
                await game._process_controller_state(_lethal_state(serial))

        assert game.players[serial].alive is False


# ─── Tournament ────────────────────────────────────────────────────────────


class TestTournamentMotionProcessing:
    """Tournament uses base _process_controller_state; guards in _kill_player_impl."""

    @pytest.fixture
    def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_tournament_motion",
        )
        return game, mock_cm

    @pytest.mark.asyncio
    async def test_gentle_motion_no_death(self, game):
        """Gentle motion with real proto should not kill player."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0

        for _ in range(5):
            await game._process_controller_state(_gentle_state(serial))

        assert game.players[serial].alive is True

    @pytest.mark.asyncio
    async def test_non_fighting_player_survives_lethal_motion(self, game):
        """Player not in FIGHTING state should survive lethal motion."""
        game, mock_cm = game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_cm.controllers)

        serial = "mock_controller_0"
        game.players[serial].grace_until = 0.0
        # Player is WAITING, not FIGHTING — _kill_player_impl returns early
        game.players[serial].tournament_state = TournamentState.WAITING

        for _ in range(10):
            await game._process_controller_state(_lethal_state(serial))

        # base _process_controller_state calls _kill_player, which delegates
        # to _kill_player_impl, which checks tournament_state != FIGHTING
        assert game.players[serial].alive is True
