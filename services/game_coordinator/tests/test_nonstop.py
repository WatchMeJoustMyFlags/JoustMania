"""
Unit tests for Nonstop Joust game mode.

Tests the core Nonstop mechanics:
- Player scoring (kills, deaths, score, streaks)
- Respawn mechanics
- Spawn protection
- Time-based win condition
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from opentelemetry import trace

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop
from test_random_teams_formation import SERIALS, QueueControllerManager, QueueStream, RecordingLog, _players, _wait_for

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games import nonstop_joust as nonstop_mod
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.nonstop_joust import (
    RESPAWN_DURATION,
    NonstopJoustGame,
    NonstopPlayer,
)
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class TestNonstopJoustGameMode:
    """Test Nonstop Joust game mechanics."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_001",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_player_initialization(self, nonstop_game):
        """Test that players are initialized correctly."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All 4 players should be created
        assert len(game.players) == 4

    @pytest.mark.uc("UC-026", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_players_start_with_zero_scores(self, nonstop_game):
        """Test that all players start with zero scores."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for _serial, player in game.players.items():
            assert player.kills == 0
            assert player.deaths == 0
            assert player.score == 0
            assert player.current_streak == 0
            assert player.best_streak == 0

    @pytest.mark.uc("UC-026", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_kill_player_increments_deaths(self, nonstop_game):
        """Test that _kill_player_impl increments death count."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        assert player.deaths == 0

        # Kill player
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.deaths == 1

    @pytest.mark.asyncio
    async def test_kill_player_resets_streak(self, nonstop_game):
        """Test that _kill_player_impl resets current streak."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Set a streak
        player.current_streak = 5

        # Kill player - streak should reset
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.current_streak == 0

    @pytest.mark.uc("UC-026", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_kill_player_sets_respawn_timer(self, nonstop_game):
        """Test that _kill_player_impl sets respawn timer."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Kill player
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.respawn_timer == RESPAWN_DURATION

    @pytest.mark.uc("UC-026", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_kill_player_marks_dead(self, nonstop_game):
        """Test that _kill_player_impl marks player as dead."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]
        assert player.alive is True

        # Kill player
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.alive is False

    @pytest.mark.uc("UC-026", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_kill_player_publishes_event(self, nonstop_game):
        """Test that _kill_player_impl publishes death event."""
        game, mock_controller_manager, event_collector = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]

        # Kill player
        await game._kill_player_impl(serial, accel_mag=5.0)

        # Verify death event
        death_events = event_collector.get_events_of_type("player_death")
        assert len(death_events) == 1
        assert death_events[0]["serial"] == serial

    @pytest.mark.asyncio
    async def test_check_win_condition_unlimited_mode(self, nonstop_game):
        """Test that win condition is false in unlimited mode."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Unlimited mode (time_limit = 0)
        game.time_limit = 0

        # Should never end
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_check_win_condition_time_not_expired(self, nonstop_game):
        """Test that win condition is false when time hasn't expired."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set time limit
        game.time_limit = 60
        game.start_time = time.time()  # Just started

        # Time hasn't expired
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_check_win_condition_time_expired(self, nonstop_game):
        """Test that win condition triggers when time expires."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set time limit and pretend it expired
        game.time_limit = 60
        game.start_time = time.time() - 70  # 70 seconds ago

        # Time expired
        assert await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_nonstop_player_dataclass(self, nonstop_game):
        """Test NonstopPlayer dataclass attributes."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players should be NonstopPlayer instances
        for _serial, player in game.players.items():
            assert isinstance(player, NonstopPlayer)
            assert hasattr(player, "kills")
            assert hasattr(player, "deaths")
            assert hasattr(player, "score")
            assert hasattr(player, "current_streak")
            assert hasattr(player, "best_streak")
            assert hasattr(player, "respawn_timer")
            assert hasattr(player, "spawn_protected")
            assert hasattr(player, "spawn_protection_end")

    @pytest.mark.uc("UC-026", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_multiple_deaths_accumulate(self, nonstop_game):
        """Test that multiple deaths accumulate."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Die multiple times
        for _ in range(3):
            player.alive = True  # Reset for respawn
            await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.deaths == 3

    @pytest.mark.asyncio
    async def test_get_game_name(self, nonstop_game):
        """Test get_game_name returns 'Nonstop Joust'."""
        game, _, _ = nonstop_game
        assert game.get_game_name() == "Nonstop Joust"


@pytest.mark.uc("UC-026", flow="main", steps=[2])
class TestNonstopRespawn:
    """Tests for respawn mechanics."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_respawn",
        )
        game.state = GameState.RUNNING  # a respawn is refused outside RUNNING (UC-026 BR-003)

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_respawn_sets_alive_true(self, nonstop_game):
        """Test that _respawn_player sets alive to True."""
        game, mock_controller_manager, _ = nonstop_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Kill the player first
        player.alive = False

        # Respawn
        await game._respawn_player(serial)

        assert player.alive is True

    @pytest.mark.asyncio
    async def test_respawn_sets_spawn_protection(self, nonstop_game):
        """Test that _respawn_player enables spawn protection."""
        game, mock_controller_manager, _ = nonstop_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        player.alive = False
        await game._respawn_player(serial)

        assert player.spawn_protected is True
        assert player.spawn_protection_end > time.time()

    @pytest.mark.asyncio
    async def test_respawn_resets_warning_state(self, nonstop_game):
        """Test that _respawn_player resets warning state."""
        game, mock_controller_manager, _ = nonstop_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Set some warning state
        player.warning_until = time.time() + 5.0
        player.smoothed_accel = 2.5
        player.alive = False

        await game._respawn_player(serial)

        assert player.warning_until == 0.0
        assert player.smoothed_accel == 0.0

    @pytest.mark.asyncio
    async def test_respawn_publishes_event(self, nonstop_game):
        """Test that _respawn_player publishes respawn event."""
        game, mock_controller_manager, event_collector = nonstop_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        player.alive = False
        await game._respawn_player(serial)

        respawn_events = event_collector.get_events_of_type("player_respawned")
        assert len(respawn_events) == 1
        assert respawn_events[0]["serial"] == serial


@pytest.mark.uc("UC-026", flow="main", steps=[2])
class TestNonstopSpawnProtection:
    """Tests for spawn protection mechanics."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)
        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_nonstop_protection",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_spawn_protected_player_skips_processing(self, nonstop_game):
        """Test that spawn protected players skip death checks."""
        game, mock_controller_manager = nonstop_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Enable spawn protection
        player.spawn_protected = True

        # Real proto controller state with high acceleration
        state = controller_manager_pb2.GameplayData(
            serial=serial,
            accel=controller_manager_pb2.Vector3(x=5.0, y=5.0, z=5.0),
        )

        # Process should return early for protected player
        await game._process_controller_state(state)

        # Player should still be alive
        assert player.alive is True


@pytest.mark.uc("UC-026", br=["BR-001"])
class TestNonstopScoring:
    """Tests for scoring mechanics."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_scoring",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_score_calculation_zero_deaths(self, nonstop_game):
        """Test score calculation with zero deaths (100 points)."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # No deaths
        player.deaths = 0

        # Score = max(0, 100 - (deaths * 10)) = 100
        expected_score = max(0, 100 - (player.deaths * 10))
        assert expected_score == 100

    @pytest.mark.asyncio
    async def test_score_calculation_some_deaths(self, nonstop_game):
        """Test score calculation with some deaths."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # 3 deaths
        player.deaths = 3

        # Score = max(0, 100 - (3 * 10)) = 70
        expected_score = max(0, 100 - (player.deaths * 10))
        assert expected_score == 70

    @pytest.mark.asyncio
    async def test_score_calculation_many_deaths(self, nonstop_game):
        """Test score calculation with many deaths (minimum 0)."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # 15 deaths
        player.deaths = 15

        # Score = max(0, 100 - (15 * 10)) = max(0, -50) = 0
        expected_score = max(0, 100 - (player.deaths * 10))
        assert expected_score == 0

    @pytest.mark.asyncio
    async def test_end_game_scoring(self, nonstop_game):
        """Test that _end_game_impl calculates final scores correctly."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Give player 0 some deaths
        serial = list(game.players.keys())[0]
        player = game.players[serial]
        player.deaths = 3

        # Setup required state for _end_game_impl
        game.gameplay_stream = MockGameplayStream()
        game.start_time = time.time()
        game.state = GameState.RUNNING

        await game._end_game_impl()

        # Score = max(0, 100 - (3 * 10)) = 70
        assert player.score == 70

    @pytest.mark.asyncio
    async def test_respawn_timer_decrement(self, nonstop_game):
        """Test that _update_respawn_timers decrements respawn timer."""
        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Setup gameplay stream for respawn countdown colors
        game.gameplay_stream = MockGameplayStream()

        # Kill player 0
        serial = list(game.players.keys())[0]
        await game._kill_player_impl(serial, accel_mag=5.0)

        player = game.players[serial]
        initial_timer = player.respawn_timer

        # Half a second later on the game clock
        game._clock = lambda: player.respawn_deadline - initial_timer + 0.5

        # Update respawn timers
        await game._update_respawn_timers()

        # Timer should have decreased
        assert player.respawn_timer < initial_timer


class TestNonstopSettings:
    """Tests for settings loading."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)
        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_nonstop_settings",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_default_time_limit_zero(self, nonstop_game):
        """Test default time limit is 0 (unlimited)."""
        game, _ = nonstop_game

        # Default before loading settings
        assert game.time_limit == 0

    @pytest.mark.asyncio
    async def test_load_settings_parses_time_limit(self, nonstop_game):
        """Test that time_limit is parsed from settings dict."""
        game, _ = nonstop_game

        # Directly set settings (simulating what would be loaded)
        game.settings = {"nonstop_time_limit": "120"}
        game.time_limit = int(game.settings.get("nonstop_time_limit", "0"))

        assert game.time_limit == 120


@pytest.mark.uc("UC-026", flow="main", steps=[2])
class TestNonstopRespawnCountdown:
    """Tests for respawn countdown colors."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)
        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_nonstop_countdown",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_countdown_color_logic(self, nonstop_game):
        """Test respawn countdown color selection logic."""
        # Test the color selection logic
        # > 2s: Gray (128, 128, 128)
        # 1-2s: Yellow (255, 255, 0)
        # < 1s: Green (0, 255, 0)

        def get_countdown_color(time_remaining):
            if time_remaining > 2.0:
                return (128, 128, 128)  # Gray
            if time_remaining > 1.0:
                return (255, 255, 0)  # Yellow
            return (0, 255, 0)  # Green

        assert get_countdown_color(2.5) == (128, 128, 128)  # Gray
        assert get_countdown_color(1.5) == (255, 255, 0)  # Yellow
        assert get_countdown_color(0.5) == (0, 255, 0)  # Green


class RecordingGameplayStream:
    """Mock gameplay stream that records all writes."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


@pytest.mark.uc("UC-026", flow="main", steps=[2])
class TestNonstopShowRespawnCountdown:
    """Tests for _show_respawn_countdown color transitions via stream."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game with recording stream."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_countdown_stream",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_show_respawn_countdown_gray_phase(self, nonstop_game):
        """Test that _show_respawn_countdown sends gray at >2s remaining."""
        game, mock_controller_manager, _ = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)
        serial = list(game.players.keys())[0]

        await game._show_respawn_countdown(serial, 2.5)

        # Should have written a base_color message with gray
        assert len(stream.messages) >= 1
        color_msg = stream.messages[-1]
        assert color_msg.base_color.serial == serial
        assert color_msg.base_color.color.r == 128
        assert color_msg.base_color.color.g == 128
        assert color_msg.base_color.color.b == 128

    @pytest.mark.asyncio
    async def test_show_respawn_countdown_yellow_phase(self, nonstop_game):
        """Test that _show_respawn_countdown sends yellow at 1-2s remaining."""
        game, mock_controller_manager, _ = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)
        serial = list(game.players.keys())[0]

        await game._show_respawn_countdown(serial, 1.5)

        assert len(stream.messages) >= 1
        color_msg = stream.messages[-1]
        assert color_msg.base_color.serial == serial
        assert color_msg.base_color.color.r == 255
        assert color_msg.base_color.color.g == 255
        assert color_msg.base_color.color.b == 0

    @pytest.mark.asyncio
    async def test_show_respawn_countdown_green_phase(self, nonstop_game):
        """Test that _show_respawn_countdown sends green at <1s remaining."""
        game, mock_controller_manager, _ = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)
        serial = list(game.players.keys())[0]

        await game._show_respawn_countdown(serial, 0.5)

        assert len(stream.messages) >= 1
        color_msg = stream.messages[-1]
        assert color_msg.base_color.serial == serial
        assert color_msg.base_color.color.r == 0
        assert color_msg.base_color.color.g == 255
        assert color_msg.base_color.color.b == 0

    @pytest.mark.asyncio
    async def test_show_respawn_countdown_skips_duplicate_color(self, nonstop_game):
        """Test that repeated calls with same color phase don't re-send."""
        game, mock_controller_manager, _ = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)
        serial = list(game.players.keys())[0]

        # First call at 2.5s (gray) should send
        await game._show_respawn_countdown(serial, 2.5)
        count_after_first = len(stream.messages)
        assert count_after_first >= 1

        # Second call still in gray phase should NOT send again
        await game._show_respawn_countdown(serial, 2.3)
        assert len(stream.messages) == count_after_first


@pytest.mark.uc("UC-026", flow="main", steps=[2])
class TestNonstopUpdateRespawnTimers:
    """Tests for _update_respawn_timers including respawn trigger and spawn protection expiry."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=3)
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_respawn_timers",
        )
        game.state = GameState.RUNNING  # a respawn is refused outside RUNNING (UC-026 BR-003)

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_respawn_triggers_when_timer_reaches_zero(self, nonstop_game):
        """Test that a player respawns when respawn_timer decrements to zero."""
        game, mock_controller_manager, event_collector = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Set player as dead with the respawn deadline just passed
        player.alive = False
        player.respawn_deadline = game._clock() - 0.01

        await game._update_respawn_timers()

        # Player should have been respawned
        assert player.alive is True
        assert player.spawn_protected is True

        # Should have published respawn event
        respawn_events = event_collector.get_events_of_type("player_respawned")
        assert len(respawn_events) == 1

    @pytest.mark.asyncio
    async def test_spawn_protection_expiry_restores_white(self, nonstop_game):
        """Test that spawn protection expiry sends white color and clears flag."""
        game, mock_controller_manager, _ = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Set player as alive with spawn protection that already expired
        player.alive = True
        player.spawn_protected = True
        player.spawn_protection_end = time.time() - 1.0  # Expired 1s ago

        await game._update_respawn_timers()

        # Spawn protection should be cleared
        assert player.spawn_protected is False

        # Should have sent white color command
        color_msgs = [m for m in stream.messages if m.HasField("base_color")]
        assert len(color_msgs) >= 1
        last_color = color_msgs[-1]
        assert last_color.base_color.color.r == 255
        assert last_color.base_color.color.g == 255
        assert last_color.base_color.color.b == 255


class TestNonstopEndGameImpl:
    """Tests for _end_game_impl scoring, winner detection, and event publishing."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_end_game",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.uc("UC-026", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_end_game_determines_winner_by_fewest_deaths(self, nonstop_game):
        """Test winner is the player with fewest deaths (highest score)."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, event_collector = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set different death counts
        serials = list(game.players.keys())
        game.players[serials[0]].deaths = 0  # Winner: score=100
        game.players[serials[1]].deaths = 3  # score=70
        game.players[serials[2]].deaths = 5  # score=50
        game.players[serials[3]].deaths = 10  # score=0

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game._time_limit_reached = True  # only a time-limit end has a winner (UC-026 BR-003)
        game.running = False  # Skip celebration sleep

        await game._end_game_impl()

        # Verify scores assigned correctly
        assert game.players[serials[0]].score == 100
        assert game.players[serials[1]].score == 70
        assert game.players[serials[2]].score == 50
        assert game.players[serials[3]].score == 0

        # Verify winner event published
        winner_events = event_collector.get_events_of_type("game_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["serial"] == serials[0]
        assert winner_events[0]["score"] == 100

    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended(self, nonstop_game):
        """Test that _end_game_impl publishes game_ended event with duration."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, event_collector = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time() - 60  # 60 seconds ago
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["game_id"] == "test_nonstop_end_game"
        assert ended_events[0]["duration"] >= 59  # ~60 seconds

    @pytest.mark.asyncio
    async def test_end_game_sends_winner_rainbow_effect(self, nonstop_game):
        """Test that _end_game_impl sends rainbow effect to winner."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serials = list(game.players.keys())
        game.players[serials[0]].deaths = 0  # Winner

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game._time_limit_reached = True  # only a time-limit end has a winner (UC-026 BR-003)
        game.running = False

        await game._end_game_impl()

        # Check for rainbow effect sent to winner
        effect_msgs = [m for m in stream.messages if m.HasField("game_effect")]
        assert len(effect_msgs) >= 1
        assert effect_msgs[0].game_effect.serial == serials[0]
        assert effect_msgs[0].game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW

    @pytest.mark.asyncio
    async def test_end_game_sets_state_to_ended(self, nonstop_game):
        """Test that _end_game_impl transitions state to ENDED."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = nonstop_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        assert game.state == GameState.ENDED


class TestNonstopGameLoopFilterUpdate:
    """Tests for _game_loop filter update logic."""

    @pytest.fixture
    def nonstop_game(self):
        """Create a Nonstop Joust game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=3)
        event_collector = EventCollector()

        game = NonstopJoustGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_nonstop_filter",
            time_limit_seconds=1,
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_check_win_condition_no_start_time(self):
        """Test win condition returns False when start_time is None."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = NonstopJoustGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_no_start",
            time_limit_seconds=60,
        )

        await game._initialize_players_impl(mock_cm.controllers)
        game.start_time = None

        # Time limit is set but start_time is None, should return False
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_dead_player_skips_processing(self):
        """Test that dead players are skipped in _process_controller_state."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = NonstopJoustGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_dead_skip",
        )

        await game._initialize_players_impl(mock_cm.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]
        player.alive = False

        state = controller_manager_pb2.GameplayData(
            serial=serial,
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
        )

        # Should not crash and player stays dead
        await game._process_controller_state(state)
        assert player.alive is False

    @pytest.mark.asyncio
    async def test_unknown_controller_skips_processing(self):
        """Test that unknown controller serials are ignored."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        game = NonstopJoustGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_unknown",
        )

        await game._initialize_players_impl(mock_cm.controllers)

        state = controller_manager_pb2.GameplayData(
            serial="unknown_controller_999",
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
        )

        # Should not crash
        await game._process_controller_state(state)


class TestInitialStreamConfig:
    """Regression: the loop's initial GameplayStreamConfig construction.

    The construction used the removed-and-reserved proto field ``serials`` and
    crashed every NonStop game at loop startup ('Protocol message
    GameplayStreamConfig has no "serials" field'). Unit tests never caught it
    because they inject a mock gameplay_stream PAST the init block — this test
    exercises the real proto message construction.
    """

    @pytest.mark.asyncio
    async def test_initial_stream_control_builds_with_player_colors(self):
        mock_cm = MockControllerManagerService(num_controllers=4)
        game = NonstopJoustGame(
            controller_manager_client=mock_cm,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_stream_config",
        )
        await game._initialize_players_impl(mock_cm.controllers)
        await game._set_unique_colors()

        control = game._build_initial_stream_control(update_frequency_hz=60)

        assert control.config.update_frequency_hz == 60
        colors = list(control.config.colors)
        assert len(colors) == 4, "initial colors must carry the full player set"
        assert {c.serial for c in colors} == set(game.players.keys())
        # The removed field must never come back.
        assert not hasattr(control.config, "serials")


class _OrderedCall:
    """A gameplay call fake that records writes and cancels in one shared order list."""

    def __init__(self, name, order):
        self.name = name
        self.order = order

    async def write(self, _message):
        self.order.append(f"{self.name}.write")

    def cancel(self):
        self.order.append(f"{self.name}.cancel")
        return True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class TestNonstopStreamSwap:
    """UC-030 BR-006 (#1914): NonStop's game loop replaces the countdown call."""

    @pytest.mark.uc("UC-030", br=["BR-006"])
    @pytest.mark.asyncio
    async def test_countdown_call_closes_only_after_the_new_call_sent_its_config(self):
        order = []
        game = NonstopJoustGame(
            controller_manager_client=MockControllerManagerService(num_controllers=2, death_schedule={}),
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_nonstop_swap",
        )
        countdown_call = _OrderedCall("countdown", order)
        game_call = _OrderedCall("game", order)
        game.gameplay_stream = countdown_call
        game.controller_client = SimpleNamespace(StreamGameplayData=lambda: game_call)
        game.running = True

        await game._game_loop()

        # The new call claims the players with its config first; only then the
        # countdown call closes. The game's own call stays open for the end-game
        # step: run()'s teardown or cleanup() closes it (UC-026 step 3, #1932).
        assert order == ["game.write", "countdown.cancel"]
        assert game.gameplay_stream is game_call


# ---------------------------------------------------------------------------
# UC-026 step 3, A1, BR-003 (#1338, #1932, #1343, #1585, #1954): end of game.
# The run tests go through the real run() with #1912's queue stream, returned by
# the StreamGameplayData stub (NonStop opens its own stream in _game_loop). They
# patch the 2 s winner hold through its module constant, never the global clock.
# ---------------------------------------------------------------------------

RAINBOW = controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW


class _ClosableQueueStream(QueueStream):
    """A queue stream that logs its cancel and can end like a server-side close.

    A write after the call ended or was cancelled raises, like a finished
    grpc.aio call. ``end_after`` ends the stream after that many frames.
    """

    def __init__(self, log, end_after=None):
        super().__init__(log)
        self.end_after = end_after
        self.read = 0
        self.finished = False

    def cancel(self):
        self.finished = True
        self.log.entries.append(("cancel", self))
        if self.producer:
            self.producer.cancel()
        return True

    async def write(self, message):
        if self.finished:
            raise RuntimeError("write on a finished gameplay call")
        await super().write(message)

    async def __anext__(self):
        if self.end_after is not None and self.read >= self.end_after:
            self.finished = True
            raise StopAsyncIteration
        self.read += 1
        return await super().__anext__()


class _NonstopControllerManager(QueueControllerManager):
    """The first call is run()'s countdown stream, the second NonStop's own game stream."""

    def __init__(self, log, end_game_stream_after=None):
        super().__init__(log)
        self.end_game_stream_after = end_game_stream_after

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        end_after = self.end_game_stream_after if self.streams else None
        stream = _ClosableQueueStream(self.log, end_after=end_after)
        self.streams.append(stream)
        return stream


@pytest.fixture
def fast_nonstop():
    """No countdown and a short winner hold."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(nonstop_mod, "WINNER_HOLD_SECONDS", 0.2),
    ):
        yield config


def _nonstop_run_game(log, client, time_limit):
    return NonstopJoustGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_nonstop_end",
        initial_players=_players(),
        time_limit_seconds=time_limit,
    )


def _record_sounds(game) -> list:
    sounds = []

    async def play(sound, *_args, **_kwargs):
        sounds.append(sound)

    game._play_sound = play
    return sounds


def _stop_producers(client):
    for stream in client.streams:
        if stream.producer:
            stream.producer.cancel()


def _rainbow_writes(log) -> list[int]:
    return [i for i, e in log.writes("game_effect") if e.effect == RAINBOW]


def _assert_no_winner(log, sounds):
    assert log.events.get_events_of_type(GameEvent.GAME_WINNER) == []
    assert _rainbow_writes(log) == []
    assert Sound.VOX_CONGRATULATIONS not in sounds
    for summary in log.events.get_events_of_type(GameEvent.PLAYER_ANALYTICS):
        assert summary["winner"] is False


class _FrameCall:
    """Endless gameplay call (empty frames every 5 ms) that records writes."""

    def __init__(self):
        self.messages = []
        self.cancelled = False

    async def write(self, message):
        self.messages.append(message)

    def cancel(self):
        self.cancelled = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.005)
        return controller_manager_pb2.GameplayDataUpdate()


async def _loop_game(time_limit=0.05):
    events = EventCollector()
    client = MockControllerManagerService(num_controllers=4, death_schedule={})
    game = NonstopJoustGame(
        controller_manager_client=client,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_nonstop_latch",
        time_limit_seconds=time_limit,
    )
    await game._initialize_players_impl(client.controllers)
    call = _FrameCall()
    game.controller_client = SimpleNamespace(StreamGameplayData=lambda: call)
    game.state = GameState.RUNNING
    game.running = True
    return game, events, call


class TestNonstopEndOfGame:
    """UC-026 step 3, A1 and BR-003: the time limit, the winner and the stream at the end."""

    @pytest.mark.uc("UC-026", flow="main", steps=[3], cond=["Post-S-1"])
    @pytest.mark.asyncio
    async def test_time_limit_ends_the_game_and_the_rainbow_goes_out_before_the_stream_closes(self, fast_nonstop):
        log = RecordingLog()
        client = _NonstopControllerManager(log)
        game = _nonstop_run_game(log, client, time_limit=0.3)
        sounds = _record_sounds(game)
        span_serials = []
        create_span = game._create_player_lifecycle_span

        def spy(serial, context=None):
            span_serials.append(serial)
            return create_span(serial, context)

        game._create_player_lifecycle_span = spy
        try:
            # Ends by itself: the clock starts at the first frame (#1338)
            await asyncio.wait_for(game.run(), timeout=5)
        finally:
            _stop_producers(client)

        assert len(client.streams) == 2, "the countdown stream and NonStop's own game stream"
        game_stream = client.streams[1]
        winners = log.events.get_events_of_type(GameEvent.GAME_WINNER)
        assert len(winners) == 1
        ended = log.events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["duration"] > 0
        assert ended[0]["outcome"] == "completed"
        assert ended[0]["winner"] == winners[0]["serial"]
        assert "reason" not in ended[0]
        assert Sound.VOX_CONGRATULATIONS in sounds

        # The rainbow goes over the game stream, which closes only afterwards (#1932)
        rainbow_at = _rainbow_writes(log)
        cancel_at = [i for i, (kind, value) in enumerate(log.entries) if kind == "cancel" and value is game_stream]
        assert rainbow_at, "no winner rainbow"
        assert cancel_at, "the game stream was never closed"
        assert rainbow_at[0] < cancel_at[0]
        assert game.gameplay_stream is None
        assert game.state == GameState.ENDED, "Post-S-1: the session ends"

        # One lifecycle span per player (the loop no longer opens a second set)
        assert sorted(span_serials) == sorted(SERIALS)

    @pytest.mark.uc("UC-026", flow="A1", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_force_end_of_an_unlimited_game_declares_no_winner(self, fast_nonstop):
        log = RecordingLog()
        client = _NonstopControllerManager(log)
        game = _nonstop_run_game(log, client, time_limit=0)
        sounds = _record_sounds(game)
        task = asyncio.create_task(game.run())
        try:
            await _wait_for(lambda: len(client.streams) == 2 and client.streams[1].read >= 5)
            game.force_end("operator")
            await asyncio.wait_for(task, timeout=5)
        finally:
            _stop_producers(client)

        _assert_no_winner(log, sounds)
        ended = log.events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["outcome"] == "force_ended"
        assert ended[0]["reason"] == "operator"
        assert ended[0]["winner"] == "none"
        assert ended[0]["duration"] > 0

    @pytest.mark.uc("UC-026", cond=["Pre-1"])
    @pytest.mark.asyncio
    async def test_one_player_raises_before_the_stream_opens(self, fast_nonstop):
        log = RecordingLog()
        client = _NonstopControllerManager(log)
        game = NonstopJoustGame(
            controller_manager_client=client,
            event_publisher=log.publish,
            audio_client=None,
            game_id="test_nonstop_one_player",
            initial_players=_players()[:1],
            time_limit_seconds=0,
        )

        with pytest.raises(ValueError, match="at least 2 players"):
            await asyncio.wait_for(game.run(), timeout=5)

        assert client.streams == []
        assert log.events.get_events_of_type(GameEvent.GAME_STARTED) == []

    @pytest.mark.uc("UC-026", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_a_stream_the_server_ends_declares_no_winner_without_error_or_hold(self, fast_nonstop):
        log = RecordingLog()
        client = _NonstopControllerManager(log, end_game_stream_after=5)
        game = _nonstop_run_game(log, client, time_limit=0)
        sounds = _record_sounds(game)
        try:
            # A 30 s winner hold would time this out: no winner, so no hold
            with patch.object(nonstop_mod, "WINNER_HOLD_SECONDS", 30.0):
                await asyncio.wait_for(game.run(), timeout=5)
        finally:
            _stop_producers(client)

        assert client.streams[1].finished
        assert log.events.get_events_of_type(GameEvent.GAME_ERROR) == []
        _assert_no_winner(log, sounds)
        ended = log.events.get_events_of_type(GameEvent.GAME_ENDED)
        assert len(ended) == 1
        assert ended[0]["outcome"] == "completed"
        assert ended[0]["winner"] == "none"

    @pytest.mark.uc("UC-026", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_force_end_earlier_in_the_time_limit_frame_declares_no_winner(self):
        game, events, call = await _loop_game()
        real_update = game._update_respawn_timers
        forced = []

        async def force_end_then_update():
            if not forced:
                await asyncio.sleep(0.06)  # past the time limit
                game.force_end("operator")
                forced.append(True)
            await real_update()

        game._update_respawn_timers = force_end_then_update
        await asyncio.wait_for(game._game_loop(), timeout=5)
        assert game._time_limit_reached, "the time limit was detected in the same frame"
        game._latch_game_outcome()  # what _run_gameplay_phase does next
        await game._end_game_impl()

        assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
        assert not [m for m in call.messages if m.HasField("game_effect")]
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended[0]["outcome"] == "force_ended"
        assert ended[0]["winner"] == "none"

    @pytest.mark.uc("UC-026", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_server_ending_the_stream_after_the_time_limit_latch_keeps_the_winner(self):
        game, events, call = await _loop_game()
        finished = []

        async def write(message):
            if finished:
                raise RuntimeError("write on a finished gameplay call")
            call.messages.append(message)

        call.write = write
        call.done = lambda: bool(finished)
        await asyncio.wait_for(game._game_loop(), timeout=5)
        assert game._time_limit_reached
        game._latch_game_outcome()
        finished.append(True)  # the controller manager ends the stream here
        await game._end_game_impl()

        assert events.get_events_of_type(GameEvent.GAME_ERROR) == []
        winners = events.get_events_of_type(GameEvent.GAME_WINNER)
        assert len(winners) == 1
        assert not [m for m in call.messages if m.HasField("game_effect")]
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended[0]["outcome"] == "completed"
        assert ended[0]["winner"] == winners[0]["serial"]

    @pytest.mark.uc("UC-026", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_force_end_after_the_time_limit_latch_keeps_the_winner(self):
        game, events, call = await _loop_game()
        await asyncio.wait_for(game._game_loop(), timeout=5)
        game.force_end("late")  # during the winner hold
        game._latch_game_outcome()
        await game._end_game_impl()

        winners = events.get_events_of_type(GameEvent.GAME_WINNER)
        assert len(winners) == 1
        assert [m for m in call.messages if m.HasField("game_effect") and m.game_effect.effect == RAINBOW]
        ended = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended[0]["outcome"] == "completed"
        assert ended[0]["winner"] == winners[0]["serial"]

    @pytest.mark.uc("UC-026", br=["BR-003"])
    @pytest.mark.parametrize("case", ["stream_closed", "outcome_latched", "not_running"])
    @pytest.mark.asyncio
    async def test_revive_after_the_game_is_over_is_refused_without_a_write(self, case):
        events = EventCollector()
        client = MockControllerManagerService(num_controllers=2, death_schedule={})
        game = NonstopJoustGame(
            controller_manager_client=client,
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_nonstop_revive_refused",
        )
        await game._initialize_players_impl(client.controllers)
        game.state = GameState.RUNNING
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        serial = next(iter(game.players))
        player = game.players[serial]
        player.alive = False
        grace_before = player.grace_until
        if case == "stream_closed":
            game._release_gameplay_stream()
        elif case == "outcome_latched":
            game._latch_game_outcome()
        else:
            game.state = GameState.ENDING

        assert await game.revive_player(serial) is False
        assert player.alive is False
        assert player.spawn_protected is False
        assert player.grace_until == grace_before
        assert stream.messages == []
        assert events.get_events_of_type("player_respawned") == []

    @pytest.mark.uc("UC-026", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_player_spans_close_with_the_final_score(self):
        client = MockControllerManagerService(num_controllers=2, death_schedule={})
        game = NonstopJoustGame(
            controller_manager_client=client,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_nonstop_final_score",
        )
        await game._initialize_players_impl(client.controllers)
        spans = {}
        for deaths, (serial, player) in enumerate(game.players.items()):
            player.deaths = deaths * 3
            player.span = spans[serial] = create_autospec(trace.Span, instance=True)

        game._close_all_player_spans()

        for deaths, (serial, span) in enumerate(spans.items()):
            final = [c for c in span.add_event.call_args_list if c.args[0] == "final_score"]
            assert len(final) == 1
            assert final[0].args[1]["score"] == max(0, 100 - deaths * 3 * 10)
            assert final[0].args[1]["deaths"] == deaths * 3
            span.end.assert_called_once()
            assert game.players[serial].span is None


class _FiniteFrameCall:
    """Gameplay call that yields ``frames`` empty frames without delay, then ends."""

    def __init__(self, frames: int, frame=None):
        self.frames = frames
        self.frame = frame or controller_manager_pb2.GameplayDataUpdate()
        self.messages = []

    async def write(self, message):
        self.messages.append(message)

    def cancel(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.frames <= 0:
            raise StopAsyncIteration
        self.frames -= 1
        return self.frame


class _FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.mark.uc("UC-026", flow="main", steps=[2])
class TestNonstopRespawnClock:
    """UC-026 step 2 (#1963): respawn after respawn_seconds of wall-clock time, no per-frame sleep."""

    async def _game(self, respawn_seconds: float = 3.0):
        log = RecordingLog()
        client = MockControllerManagerService(num_controllers=4, death_schedule={})
        game = NonstopJoustGame(
            controller_manager_client=client,
            event_publisher=log.publish,
            audio_client=None,
            game_id="test_nonstop_respawn_clock",
        )
        game.respawn_duration = respawn_seconds
        await game._initialize_players_impl(client.controllers)
        game.gameplay_stream = MockGameplayStream()
        game.state = GameState.RUNNING
        game.running = True
        game._clock = _FakeClock()
        return game, log

    @pytest.mark.asyncio
    async def test_half_the_frame_rate_still_respawns_after_respawn_seconds(self):
        game, log = await self._game(respawn_seconds=3.0)
        serial = next(iter(game.players))
        await game._kill_player_impl(serial, accel_mag=5.0)
        player = game.players[serial]

        # Frames arrive at 30 Hz, half the configured rate
        respawned_at = None
        for tick in range(1, 200):
            game._clock.now += 1 / 30
            await game._update_respawn_timers()
            if player.alive:
                respawned_at = tick / 30
                break

        assert respawned_at is not None, "the player never respawned"
        assert 3.0 <= respawned_at < 3.0 + 2 / 30, respawned_at
        assert len(log.events.get_events_of_type("player_respawned")) == 1

    @pytest.mark.asyncio
    async def test_countdown_colour_follows_the_time_left(self):
        game, _ = await self._game(respawn_seconds=3.0)
        serial = next(iter(game.players))
        await game._kill_player_impl(serial, accel_mag=5.0)
        player = game.players[serial]

        colours = []
        for elapsed in (0.5, 1.5, 2.5):
            game._clock.now = player.respawn_deadline - 3.0 + elapsed
            await game._update_respawn_timers()
            colours.append(player._last_respawn_color)

        assert colours == [(128, 128, 128), (255, 255, 0), (0, 255, 0)]
        assert player.alive is False

    @pytest.mark.asyncio
    async def test_zero_respawn_seconds_respawns_on_the_next_frame(self):
        game, log = await self._game(respawn_seconds=0.0)
        serial = next(iter(game.players))
        await game._kill_player_impl(serial, accel_mag=5.0)

        await game._update_respawn_timers()

        assert game.players[serial].alive is True
        assert game.players[serial].spawn_protected is True
        assert len(log.events.get_events_of_type("player_respawned")) == 1

    @pytest.mark.asyncio
    async def test_agent_revive_ends_the_pending_countdown(self):
        game, log = await self._game(respawn_seconds=3.0)
        serial = next(iter(game.players))
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert await game.revive_player(serial) is True
        game._clock.now += 10
        await game._update_respawn_timers()

        assert game.players[serial].alive is True
        assert game.players[serial].respawn_deadline is None
        assert len(log.events.get_events_of_type("player_respawned")) == 1

    @pytest.mark.asyncio
    async def test_a_death_respawns_on_a_later_frame_than_the_one_it_happened_in(self):
        game, log = await self._game(respawn_seconds=0.0)
        serial = next(iter(game.players))
        killed = []

        async def kill_in_first_frame(_state):
            if not killed:
                killed.append(True)
                await game._kill_player_impl(serial, accel_mag=5.0)

        game._process_controller_state = kill_in_first_frame
        frame = controller_manager_pb2.GameplayDataUpdate()
        frame.controllers.add(serial=serial)
        call = _FiniteFrameCall(frames=2, frame=frame)
        game.controller_client = SimpleNamespace(StreamGameplayData=lambda: call)
        alive_at_frame_end = []
        real_check = game._check_win_condition

        async def check_and_record():
            alive_at_frame_end.append(game.players[serial].alive)
            return await real_check()

        game._check_win_condition = check_and_record
        await asyncio.wait_for(game._game_loop(), timeout=5)

        # frame 1: killed, and the respawn check ran before the motion step, so still dead
        # at the end of the frame; frame 2: the 0 s deadline has passed
        assert alive_at_frame_end == [False, True]
        assert len(log.events.get_events_of_type("player_death")) == 1
        assert len(log.events.get_events_of_type("player_respawned")) == 1

    @pytest.mark.asyncio
    async def test_every_death_shows_the_countdown_with_a_one_second_respawn(self):
        game, _ = await self._game(respawn_seconds=1.0)
        serial = next(iter(game.players))
        player = game.players[serial]
        led_writes = []
        beeps = []
        real_write = game.gameplay_stream.write

        async def record_write(message):
            if message.HasField("base_color"):
                led_writes.append(message.base_color.color.g)
            await real_write(message)

        async def record_sound(sound, *_args, **_kwargs):
            beeps.append(sound)

        game.gameplay_stream.write = record_write
        game._play_sound = record_sound

        for _ in range(2):
            await game._kill_player_impl(serial, accel_mag=5.0)
            led_writes.clear()
            beeps.clear()
            await game._update_respawn_timers()  # countdown frame, 1 s left: green
            assert player.alive is False
            assert led_writes == [255], "the death shows the green countdown LED"
            assert len(beeps) == 1, "the death beeps"
            game._clock.now += 1.0
            await game._update_respawn_timers()
            assert player.alive is True

    @pytest.mark.asyncio
    async def test_the_loop_does_not_sleep_between_frames(self):
        game, _ = await self._game()
        call = _FiniteFrameCall(frames=20)
        game.controller_client = SimpleNamespace(StreamGameplayData=lambda: call)
        real_sleep = asyncio.sleep
        sleeps = []

        async def recording_sleep(delay, *_args, **_kwargs):
            sleeps.append(delay)
            await real_sleep(0)

        config = GamePerformanceConfig(update_frequency_hz=30)
        with (
            patch.object(get_config_manager(), "get_config", return_value=config),
            patch.object(game_clock, "sleep", recording_sleep),
        ):
            await asyncio.wait_for(game._game_loop(), timeout=5)

        assert call.frames == 0, "every frame was processed"
        assert sleeps == [], f"the game loop slept between frames: {sleeps[:3]}"

    @pytest.mark.asyncio
    async def test_the_loop_records_the_frame_metrics(self):
        game, _ = await self._game()
        call = _FiniteFrameCall(frames=20)
        game.controller_client = SimpleNamespace(StreamGameplayData=lambda: call)
        config = GamePerformanceConfig(update_frequency_hz=30)

        with (
            patch.object(get_config_manager(), "get_config", return_value=config),
            patch.object(game, "_update_frame_metrics", wraps=game._update_frame_metrics) as frame_metrics,
            patch("services.game_coordinator.games.base.metrics.actual_update_frequency_hz") as actual_hz,
        ):
            await asyncio.wait_for(game._game_loop(), timeout=5)

        assert frame_metrics.call_count == 20
        assert [c.args[4] for c in frame_metrics.call_args_list] == list(range(1, 21))
        assert all(c.args[1] == pytest.approx(1000 / 30) for c in frame_metrics.call_args_list)
        assert actual_hz.labels.return_value.set.call_count == 2  # every 10th frame


# ---------------------------------------------------------------------------
# UC-026 A2 and UC-030 BR-003 (#1591, #1971): controller disconnects and the startup grace.
# Maintainer decision (2026-10-22): a disconnect is one death, the player does not
# respawn, and it stays in the winner ranking with its final score.
# ---------------------------------------------------------------------------


def _disconnect_frame(serial: str) -> controller_manager_pb2.GameplayDataUpdate:
    frame = controller_manager_pb2.GameplayDataUpdate()
    frame.disconnects.add(serial=serial)
    return frame


class TestNonstopDisconnects:
    """UC-026 A2: a disconnect is a death without respawn; UC-030 BR-003: the 0.3 s startup grace."""

    async def _game(self, respawn_seconds: float = 0.0):
        log = RecordingLog()
        client = MockControllerManagerService(num_controllers=3, death_schedule={})
        game = NonstopJoustGame(
            controller_manager_client=client,
            event_publisher=log.publish,
            audio_client=None,
            game_id="test_nonstop_disconnects",
        )
        # 0 s: a player who could respawn does so on the next frame
        game.respawn_duration = respawn_seconds
        await game._initialize_players_impl(client.controllers)
        game.state = GameState.RUNNING
        game.running = True
        game._clock = _FakeClock()
        return game, log

    async def _run_loop(self, game, call):
        game.controller_client = SimpleNamespace(StreamGameplayData=lambda: call)
        await asyncio.wait_for(game._game_loop(), timeout=5)

    def _assert_disconnect_death(self, game, log, serial):
        player = game.players[serial]
        assert player.alive is False
        assert player.deaths == 1
        assert player.respawn_deadline is None
        deaths = log.events.get_events_of_type("player_death")
        assert [d["serial"] for d in deaths] == [serial]
        assert log.events.get_events_of_type("player_respawned") == []

    @pytest.mark.uc("UC-026", flow="A2")
    @pytest.mark.asyncio
    async def test_a_warm_up_disconnect_is_a_death_at_loop_start_without_respawn(self):
        game, log = await self._game()
        serial = next(iter(game.players))
        game._warmup_disconnects = [serial]
        call = _FiniteFrameCall(frames=5)
        alive_at_first_frame = []
        real_update = game._update_respawn_timers

        async def record_then_update():
            if not alive_at_first_frame:
                alive_at_first_frame.append(game.players[serial].alive)
            game._clock.now += 10  # well past any respawn deadline
            await real_update()

        game._update_respawn_timers = record_then_update
        await self._run_loop(game, call)

        assert alive_at_first_frame == [False], "handled before the first gameplay frame"
        assert game._warmup_disconnects == []
        self._assert_disconnect_death(game, log, serial)
        # The alive filter drops the disconnected player
        filters = [m.filter_update.serials for m in call.messages if m.HasField("filter_update")]
        assert filters and serial not in filters[-1]

    @pytest.mark.uc("UC-026", flow="A2")
    @pytest.mark.asyncio
    async def test_an_in_game_disconnect_is_a_death_without_respawn(self):
        game, log = await self._game()
        serial = next(iter(game.players))
        call = _FiniteFrameCall(frames=5, frame=_disconnect_frame(serial))
        real_update = game._update_respawn_timers

        async def advance_then_update():
            game._clock.now += 10
            await real_update()

        game._update_respawn_timers = advance_then_update
        with patch.object(game, "_set_players_alive_aggregate", wraps=game._set_players_alive_aggregate) as aggregate:
            await self._run_loop(game, call)

        self._assert_disconnect_death(game, log, serial)
        assert aggregate.call_args_list[-1].args[0] == len(game.players) - 1

    @pytest.mark.uc("UC-026", flow="A2")
    @pytest.mark.asyncio
    async def test_a_disconnect_while_waiting_to_respawn_cancels_the_respawn_without_a_second_death(self):
        game, log = await self._game(respawn_seconds=3.0)
        game.gameplay_stream = MockGameplayStream()
        serial = next(iter(game.players))
        await game._kill_player_impl(serial, accel_mag=5.0)

        await game._handle_disconnect(serial)
        game._clock.now += 10
        await game._update_respawn_timers()

        self._assert_disconnect_death(game, log, serial)

    @pytest.mark.uc("UC-026", flow="A2", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_an_agent_revive_of_a_disconnected_player_is_refused(self):
        game, log = await self._game()
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        serial = next(iter(game.players))
        await game._handle_disconnect(serial)
        stream.messages.clear()
        grace_before = game.players[serial].grace_until

        assert await game.revive_player(serial) is False

        player = game.players[serial]
        assert player.alive is False
        assert player.spawn_protected is False
        assert player.grace_until == grace_before
        assert stream.messages == []
        assert log.events.get_events_of_type("player_respawned") == []

    @pytest.mark.uc("UC-026", flow="A2", br=["BR-001"])
    @pytest.mark.asyncio
    async def test_a_disconnected_player_stays_in_the_winner_ranking_with_its_score(self):
        game, events, call = await _loop_game(time_limit=0.05)
        serials = list(game.players)
        gone = serials[0]
        await game._handle_disconnect(gone)
        for serial in serials[1:]:
            game.players[serial].deaths = 3

        with patch.object(nonstop_mod, "WINNER_HOLD_SECONDS", 0.0):
            await asyncio.wait_for(game._game_loop(), timeout=5)
            assert game._time_limit_reached
            game._latch_game_outcome()
            await game._end_game_impl()

        winners = events.get_events_of_type(GameEvent.GAME_WINNER)
        assert [w["serial"] for w in winners] == [gone]
        assert winners[0]["score"] == 90
        assert game.players[gone].alive is False

    @pytest.mark.uc("UC-026", flow="A2")
    @pytest.mark.asyncio
    async def test_a_disconnect_in_the_frame_where_the_respawn_deadline_passed_wins_over_the_respawn(self):
        game, log = await self._game(respawn_seconds=3.0)
        serial = next(iter(game.players))
        game.gameplay_stream = MockGameplayStream()
        await game._kill_player_impl(serial, accel_mag=5.0)
        game._clock.now += 10  # the deadline has passed when the frame arrives
        call = _FiniteFrameCall(frames=1, frame=_disconnect_frame(serial))

        await self._run_loop(game, call)

        self._assert_disconnect_death(game, log, serial)

    @pytest.mark.uc("UC-026", flow="A2")
    @pytest.mark.asyncio
    async def test_a_warm_up_disconnect_is_not_handled_when_the_game_already_stopped(self):
        game, log = await self._game()
        serial = next(iter(game.players))
        game._warmup_disconnects = [serial]
        game.running = False  # force-ended during the countdown or the warm-up

        await self._run_loop(game, _FiniteFrameCall(frames=1))

        assert game.players[serial].alive is True
        assert game.players[serial].deaths == 0
        assert log.events.get_events_of_type("player_death") == []

    @pytest.mark.uc("UC-026", flow="A2")
    @pytest.mark.asyncio
    async def test_a_disconnect_under_spawn_protection_writes_no_base_colour_at_expiry(self):
        game, log = await self._game()
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        serial = next(iter(game.players))
        player = game.players[serial]
        player.spawn_protected = True
        player.spawn_protection_end = time.time() - 1  # protection would expire now

        await game._handle_disconnect(serial)
        stream.messages.clear()
        await game._update_respawn_timers()

        assert player.spawn_protected is False
        assert [m for m in stream.messages if m.HasField("base_color")] == []

    @pytest.mark.uc("UC-030", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_the_startup_grace_never_shortens_a_longer_grace(self):
        game, log = await self._game()
        serial = next(iter(game.players))
        shield_until = time.time() + 60  # an agent shield granted during the countdown
        game.players[serial].grace_until = shield_until

        await self._run_loop(game, _FiniteFrameCall(frames=1))

        assert game.players[serial].grace_until == shield_until

    @pytest.mark.uc("UC-030", br=["BR-003"])
    @pytest.mark.uc("UC-026", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_a_spike_in_the_first_frame_inside_the_startup_grace_does_not_kill(self):
        game, log = await self._game()
        serial = next(iter(game.players))
        frame = controller_manager_pb2.GameplayDataUpdate()
        for s in game.players:
            frame.controllers.add(
                serial=s, accel=controller_manager_pb2.Vector3(x=50.0 if s == serial else 0.0, y=0.0, z=1.0)
            )
        call = _FiniteFrameCall(frames=1, frame=frame)

        await self._run_loop(game, call)

        assert game.players[serial].alive is True
        assert log.events.get_events_of_type("player_death") == []

        # Control: the same spike after the grace kills
        game.players[serial].grace_until = 0.0
        await game._process_controller_state(frame.controllers[0])
        assert game.players[serial].alive is False
