"""
Unit tests for GameCoordinatorServicer.

Tests the game lifecycle management:
- StartGame validation (player count, duplicate starts)
- ForceEndGame (running and idle states)
- GetGameState (state queries)
- State transitions and thread safety
- Error handling

Issue #209: Improve test coverage for critical game flow
Issue #775: multi-session refactor — the game loop lifecycle moved to
GameSession (see test_game_session.py); these tests now drive the servicer's
session registry. State is advanced via the session's EventBus (publish
GAME_STARTED) rather than poking servicer globals, since the servicer exposes
primary-session state through read-only properties.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.servicer import GameCoordinatorServicer

# Stops the real game thread from spinning while still registering the session.
_NO_THREAD = patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None)


class MockGrpcContext:
    """Mock gRPC context for testing."""

    def __init__(self):
        self._cancelled = False
        self._metadata = []

    def cancelled(self):
        return self._cancelled

    def invocation_metadata(self):
        return self._metadata


class MockSpan:
    """Mock OpenTelemetry span."""

    def __init__(self):
        self.attributes = {}
        self.events = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _config(game_name="FFA", serials=("p1", "p2"), sensitivity=2, origin=None, **kwargs):
    # Default to MENU origin (#837): these characterization tests assert the
    # legacy "first start becomes primary" behavior, which is now reserved for
    # the real (menu-origin) game. Pass origin explicitly to exercise shadows.
    if origin is None:
        origin = game_coordinator_pb2.GAME_ORIGIN_MENU
    return game_coordinator_pb2.StartGameConfig(
        game_name=game_name,
        players=[game_coordinator_pb2.Player(serial=s) for s in serials],
        sensitivity=sensitivity,
        origin=origin,
        **kwargs,
    )


async def _advance_to_running(servicer, game_id):
    """Publish GAME_STARTED on the session bus to transition it to RUNNING."""
    session = servicer.sessions[game_id]
    await session.event_bus.publish(GameEvent.GAME_STARTED, {"game_id": game_id})


class TestGameCoordinatorInit:
    """Tests for GameCoordinatorServicer initialization."""

    def test_initial_state_is_idle(self):
        """Servicer should start in IDLE state (no primary session)."""
        servicer = GameCoordinatorServicer()
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE

    def test_no_current_game_on_init(self):
        """No game should be running on init."""
        servicer = GameCoordinatorServicer()
        assert servicer.current_game is None
        assert servicer.game_id is None

    def test_empty_sessions_on_init(self):
        """Session registry should be empty on init."""
        servicer = GameCoordinatorServicer()
        assert len(servicer.sessions) == 0


class TestStartGameValidation:
    """Tests for _start_game_from_config validation."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_rejects_less_than_two_players(self, servicer):
        """Should reject game start with less than 2 players."""
        config = _config(serials=("p1",))
        success, error = await servicer._start_game_from_config(config, MockSpan())
        assert success is False
        assert "at least 2 players" in error.lower()

    @pytest.mark.asyncio
    async def test_rejects_zero_players(self, servicer):
        """Should reject game start with zero players."""
        config = game_coordinator_pb2.StartGameConfig(game_name="FFA", players=[], sensitivity=2)
        success, error = await servicer._start_game_from_config(config, MockSpan())
        assert success is False
        assert "at least 2 players" in error.lower()

    @pytest.mark.asyncio
    async def test_accepts_two_players(self, servicer):
        """Should accept game start with exactly 2 players."""
        with _NO_THREAD:
            success, game_id = await servicer._start_game_from_config(_config(), MockSpan())
        assert success is True
        assert game_id.startswith("game_")

    @pytest.mark.asyncio
    async def test_accepts_many_players(self, servicer):
        """Should accept game start with many players."""
        config = _config(serials=tuple(f"p{i}" for i in range(8)))
        with _NO_THREAD:
            success, game_id = await servicer._start_game_from_config(config, MockSpan())
        assert success is True
        assert len(servicer.sessions[game_id].players) == 8

    @pytest.mark.asyncio
    async def test_rejects_start_when_already_running(self, servicer):
        """Should reject second game start when game already running (cap=1)."""
        with _NO_THREAD:
            success1, gid1 = await servicer._start_game_from_config(_config(), MockSpan())
            assert success1 is True
            await _advance_to_running(servicer, gid1)

            success2, error = await servicer._start_game_from_config(
                _config(game_name="Teams", serials=("p3", "p4")), MockSpan()
            )
        assert success2 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_rejects_start_when_starting(self, servicer):
        """Should reject game start when another game occupies the only slot."""
        with _NO_THREAD:
            success1, _ = await servicer._start_game_from_config(_config(), MockSpan())
            assert success1 is True
            # Session sits in STARTING (no GAME_STARTED published yet).
            success2, error = await servicer._start_game_from_config(_config(serials=("p3", "p4")), MockSpan())
        assert success2 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_generates_uuid_game_id(self, servicer):
        """Should generate a uuid-based game ID (collision-safe)."""
        with _NO_THREAD:
            success, game_id = await servicer._start_game_from_config(_config(), MockSpan())
        assert success is True
        assert game_id.startswith("game_")
        # uuid hex[:12] -> 12 hex chars after the prefix.
        suffix = game_id[len("game_") :]
        assert len(suffix) == 12
        assert servicer.game_id == game_id

    @pytest.mark.asyncio
    async def test_stores_game_config(self, servicer):
        """Should store game configuration on start."""
        config = _config(
            game_name="JoustTeams",
            sensitivity=3,
            teams_config=game_coordinator_pb2.TeamsConfig(num_teams=2, random_assignment=True),
        )
        with _NO_THREAD:
            _, game_id = await servicer._start_game_from_config(config, MockSpan())

        session = servicer.sessions[game_id]
        assert session.game_name == "JoustTeams"
        assert len(session.players) == 2
        assert session.game_config.sensitivity == 3
        assert session.game_config.teams_config.num_teams == 2


class TestForceEndGame:
    """Tests for ForceEndGame RPC."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_force_end_no_game_running(self, servicer):
        """ForceEndGame should fail gracefully when no game running."""
        request = game_coordinator_pb2.ForceEndGameRequest(reason="test")
        response = await servicer.ForceEndGame(request, MockGrpcContext())
        assert response.success is False
        assert "no game in progress" in response.error.lower()

    @pytest.mark.asyncio
    async def test_force_end_idle_state(self, servicer):
        """ForceEndGame should fail when no primary session exists."""
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE
        response = await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="test"), MockGrpcContext()
        )
        assert response.success is False

    @pytest.mark.asyncio
    async def test_force_end_running_game(self, servicer):
        """ForceEndGame should succeed when game is running."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(), MockSpan())
            await _advance_to_running(servicer, gid)

        # Attach a mock live game so force_end() is invoked on it.
        mock_game = MagicMock()
        servicer.sessions[gid].current_game = mock_game

        response = await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="user requested"), MockGrpcContext()
        )
        assert response.success is True
        assert response.error == ""
        mock_game.force_end.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_end_starting_game(self, servicer):
        """ForceEndGame should succeed when game is starting."""
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), MockSpan())

        response = await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="cancelled"), MockGrpcContext()
        )
        assert response.success is True
        # Primary slot freed after end -> IDLE.
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE

    @pytest.mark.asyncio
    async def test_force_end_frees_primary_slot(self, servicer):
        """After force-end, a new primary game can start."""
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), MockSpan())
            await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="test"), MockGrpcContext())
            # A new game can now claim the primary slot.
            success, _ = await servicer._start_game_from_config(_config(serials=("p3", "p4")), MockSpan())
        assert success is True


class TestGetGameState:
    """Tests for GetGameState RPC."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_get_state_idle(self, servicer):
        """GetGameState should return IDLE state when no game."""
        response = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert response.success is True
        assert response.game_info.state == game_coordinator_pb2.GameState.IDLE
        assert response.game_info.game_mode == ""

    @pytest.mark.asyncio
    async def test_get_state_running(self, servicer):
        """GetGameState should return game info when running."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(game_name="FFA"), MockSpan())
            await _advance_to_running(servicer, gid)

        response = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert response.success is True
        assert response.game_info.state == game_coordinator_pb2.GameState.RUNNING
        assert response.game_info.game_mode == "FFA"
        assert response.game_info.game_id == gid

    @pytest.mark.asyncio
    async def test_get_state_with_players(self, servicer):
        """GetGameState should return player info when game has players."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(game_name="Teams"), MockSpan())
            await _advance_to_running(servicer, gid)

        mock_game = MagicMock()
        mock_player1 = MagicMock()
        mock_player1.team = 0
        mock_player1.color = (255, 0, 0)
        mock_player1.alive = True
        mock_player1.sensitivity_factor = 1.0

        mock_player2 = MagicMock()
        mock_player2.team = 1
        mock_player2.color = (0, 0, 255)
        mock_player2.alive = False
        mock_player2.sensitivity_factor = 1.5

        mock_game.players = {"serial_1": mock_player1, "serial_2": mock_player2}
        servicer.sessions[gid].current_game = mock_game

        response = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert response.success is True
        assert len(response.game_info.players) == 2
        player_serials = {p.serial for p in response.game_info.players}
        assert player_serials == {"serial_1", "serial_2"}


class TestEventStateSync:
    """Tests for event-driven state synchronization (primary session)."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_game_started_transitions_to_running(self, servicer):
        """GAME_STARTED on the primary bus transitions the primary to RUNNING."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(), MockSpan())
        servicer._on_primary_event_state_sync(GameEvent.GAME_STARTED)
        assert servicer.game_state == game_coordinator_pb2.GameState.RUNNING

    @pytest.mark.asyncio
    async def test_game_ended_transitions_to_ended(self, servicer):
        """Game ending events transition the primary to ENDED."""
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), MockSpan())
        servicer._on_primary_event_state_sync(GameEvent.GAME_ENDED)
        assert servicer.game_state == game_coordinator_pb2.GameState.ENDED

    @pytest.mark.asyncio
    async def test_game_force_ended_transitions_to_ended(self, servicer):
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), MockSpan())
        servicer._on_primary_event_state_sync(GameEvent.GAME_FORCE_ENDED)
        assert servicer.game_state == game_coordinator_pb2.GameState.ENDED

    @pytest.mark.asyncio
    async def test_game_error_transitions_to_ended(self, servicer):
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), MockSpan())
        servicer._on_primary_event_state_sync(GameEvent.GAME_ERROR)
        assert servicer.game_state == game_coordinator_pb2.GameState.ENDED

    def test_state_sync_noop_without_primary(self, servicer):
        """State-sync callback no-ops when no primary session is live."""
        # Should not raise.
        servicer._on_primary_event_state_sync(GameEvent.GAME_STARTED)
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE


class TestShutdown:
    """Tests for servicer shutdown."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_shutdown_no_sessions(self, servicer):
        """Shutdown with no sessions should complete cleanly."""
        await servicer.shutdown()
        assert len(servicer.sessions) == 0

    @pytest.mark.asyncio
    async def test_shutdown_joins_sessions(self, servicer):
        """Shutdown should join each registered session's thread."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(), MockSpan())

        # Replace join_thread with an awaitable spy.
        from unittest.mock import AsyncMock

        servicer.sessions[gid].join_thread = AsyncMock()

        await servicer.shutdown()

        servicer.sessions[gid].join_thread.assert_awaited_once()


class TestGameNameHandling:
    """Tests for game name handling."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_valid_game_names_accepted(self, servicer):
        """Known game types should pass initial validation."""
        valid_names = ["FFA", "Teams", "Zombie", "Werewolf", "Tournament"]
        for name in valid_names:
            with _NO_THREAD:
                success, gid = await servicer._start_game_from_config(_config(game_name=name), MockSpan())
            assert success is True, f"Game type {name} should be accepted"
            # Free the slot for the next iteration.
            await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="next"), MockGrpcContext())

    @pytest.mark.asyncio
    async def test_game_name_stored_correctly(self, servicer):
        """Game name should be stored on the session."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(game_name="FFA"), MockSpan())
        assert servicer.sessions[gid].game_name == "FFA"


class TestStateTransitionRobustness:
    """Tests for state transition edge cases."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_transition_from_idle_to_starting(self, servicer):
        """Servicer should transition from IDLE to STARTING on game start."""
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), MockSpan())
        assert servicer.game_state in [
            game_coordinator_pb2.GameState.STARTING,
            game_coordinator_pb2.GameState.RUNNING,
        ]

    @pytest.mark.asyncio
    async def test_get_state_during_transition(self, servicer):
        """GetGameState should return valid state during transitions."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(game_name="FFA"), MockSpan())
        response = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert response.success is True
        assert response.game_info.state == game_coordinator_pb2.GameState.STARTING


class TestDuplicatePlayerHandling:
    """Tests for handling duplicate players."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_duplicate_serial_in_players(self, servicer):
        """StartGame with duplicate serials should be handled (no crash)."""
        config = _config(serials=("same", "same", "different"))
        with _NO_THREAD:
            success, _ = await servicer._start_game_from_config(config, MockSpan())
        assert isinstance(success, bool)


class TestConcurrentOperations:
    """Tests for concurrent operation handling."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_get_state_concurrent_safe(self, servicer):
        """GetGameState should be safe under concurrent access."""
        with _NO_THREAD:
            _, gid = await servicer._start_game_from_config(_config(game_name="FFA"), MockSpan())
            await _advance_to_running(servicer, gid)

        responses = []
        for _ in range(10):
            responses.append(await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext()))
        for response in responses:
            assert response.success is True
            assert response.game_info.state == game_coordinator_pb2.GameState.RUNNING
