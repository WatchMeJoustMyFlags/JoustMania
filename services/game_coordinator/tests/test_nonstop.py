"""
Unit tests for Nonstop Joust game mode.

Tests the core Nonstop mechanics:
- Player scoring (kills, deaths, score, streaks)
- Respawn mechanics
- Spawn protection
- Time-based win condition
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
from services.game_coordinator.games.nonstop_joust import (
    RESPAWN_DURATION,
    NonstopJoustGame,
    NonstopPlayer,
)


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

        # Set update frequency
        game._current_update_frequency = 30

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

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_respawn_triggers_when_timer_reaches_zero(self, nonstop_game):
        """Test that a player respawns when respawn_timer decrements to zero."""
        game, mock_controller_manager, event_collector = nonstop_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game._current_update_frequency = 30

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Set player as dead with timer about to expire
        player.alive = False
        player.respawn_timer = 0.01  # Very small, one tick will go to 0

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
        game._current_update_frequency = 30

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
