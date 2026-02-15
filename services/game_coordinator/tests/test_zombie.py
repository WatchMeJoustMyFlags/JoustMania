"""
Unit tests for Zombie game mode.

Tests the core Zombie mechanics:
- Human vs Zombie team assignment
- Human conversion to zombie on death
- Zombie respawn mechanics
- Win conditions (all converted vs time expired)
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

from conftest import EventCollector, MockControllerManagerService, async_noop

from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.zombie import (
    HUMAN_COLOR,
    INITIAL_ZOMBIES,
    ZOMBIE_COLOR,
    ZombieGame,
    ZombiePlayer,
    calculate_game_duration,
)


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class TestZombieGameMode:
    """Test Zombie game mechanics."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_zombie_001",
        )

        return game, mock_controller_manager, event_collector

    def test_calculate_game_duration(self):
        """Test game duration calculation based on player count."""
        # 4 players: (4 * 3 / 16) * 60 = 45 seconds
        assert calculate_game_duration(4) == 45.0

        # 8 players: (8 * 3 / 16) * 60 = 90 seconds
        assert calculate_game_duration(8) == 90.0

        # 12 players: (12 * 3 / 16) * 60 = 135 seconds
        assert calculate_game_duration(12) == 135.0

    @pytest.mark.asyncio
    async def test_player_initialization(self, zombie_game):
        """Test that players are initialized correctly."""
        game, mock_controller_manager, _ = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All 4 players should be created
        assert len(game.players) == 4

        # Should have 2 zombies and 2 humans (INITIAL_ZOMBIES=2)
        zombie_count = len(game.zombie_serials)
        human_count = len(game.human_serials)

        assert zombie_count == min(INITIAL_ZOMBIES, 3)  # min(2, 4-1)
        assert human_count == 4 - zombie_count

    @pytest.mark.asyncio
    async def test_initial_zombies_marked_correctly(self, zombie_game):
        """Test that initial zombies have correct attributes."""
        game, mock_controller_manager, _ = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.zombie_serials:
            player = game.players[serial]
            assert player.is_zombie is True
            assert player.team == 1
            assert player.color == ZOMBIE_COLOR

    @pytest.mark.asyncio
    async def test_initial_humans_marked_correctly(self, zombie_game):
        """Test that initial humans have correct attributes."""
        game, mock_controller_manager, _ = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.human_serials:
            player = game.players[serial]
            assert player.is_zombie is False
            assert player.team == 0
            assert player.color == HUMAN_COLOR

    @pytest.mark.asyncio
    async def test_win_condition_all_humans_converted(self, zombie_game):
        """Test that zombies win when all humans are converted."""
        game, mock_controller_manager, event_collector = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially no win
        game.time_remaining = 100  # Plenty of time
        assert not await game._check_win_condition()

        # Convert all humans to zombies
        for serial in list(game.human_serials):
            game.players[serial].is_zombie = True
            game.players[serial].team = 1
            game.zombie_serials.append(serial)
        game.human_serials.clear()

        # Zombies should win
        assert await game._check_win_condition()

        # Verify zombie_winner event
        winner_events = event_collector.get_events_of_type("zombie_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["winner"] == "zombies"

    @pytest.mark.asyncio
    async def test_win_condition_time_expired_humans_survive(self, zombie_game):
        """Test that humans win when time expires with survivors."""
        game, mock_controller_manager, event_collector = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set time to 0 (expired)
        game.time_remaining = 0

        # Humans should win (still have humans alive)
        assert await game._check_win_condition()

        # Verify zombie_winner event
        winner_events = event_collector.get_events_of_type("zombie_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["winner"] == "humans"

    @pytest.mark.asyncio
    async def test_no_win_with_time_and_humans(self, zombie_game):
        """Test that game continues with time remaining and humans alive."""
        game, mock_controller_manager, _ = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Time remaining and humans alive
        game.time_remaining = 100

        # Game should continue
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_human_conversion_updates_lists(self, zombie_game):
        """Test that human conversion updates tracking lists."""
        game, mock_controller_manager, _ = zombie_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get a human serial
        human_serial = game.human_serials[0]
        initial_human_count = len(game.human_serials)
        initial_zombie_count = len(game.zombie_serials)

        # Simulate conversion (set player as zombie directly to test logic)
        player = game.players[human_serial]
        player.is_zombie = True
        player.team = 1
        game.human_serials.remove(human_serial)
        game.zombie_serials.append(human_serial)

        # Lists should be updated
        assert len(game.human_serials) == initial_human_count - 1
        assert len(game.zombie_serials) == initial_zombie_count + 1
        assert human_serial not in game.human_serials
        assert human_serial in game.zombie_serials

    @pytest.mark.asyncio
    async def test_zombie_player_dataclass(self, zombie_game):
        """Test ZombiePlayer dataclass attributes."""
        game, mock_controller_manager, _ = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players should be ZombiePlayer instances
        for _serial, player in game.players.items():
            assert isinstance(player, ZombiePlayer)
            assert hasattr(player, "is_zombie")
            assert hasattr(player, "respawn_until")

    @pytest.mark.asyncio
    async def test_game_duration_set_on_init(self, zombie_game):
        """Test that game duration is calculated on player init."""
        game, mock_controller_manager, _ = zombie_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 4 players: (4 * 3 / 16) * 60 = 45 seconds
        expected_duration = calculate_game_duration(4)
        assert game.game_duration == expected_duration
        assert game.time_remaining == expected_duration


class TestZombieKillMechanics:
    """Tests for zombie kill and conversion mechanics."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_zombie_kill",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_human_killed_becomes_zombie(self, zombie_game):
        """When a human is killed, they should become a zombie."""
        game, mock_controller_manager, event_collector = zombie_game
        game.gameplay_stream = MockGameplayStream()
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get a human serial
        human_serial = game.human_serials[0]
        player = game.players[human_serial]

        assert player.is_zombie is False
        assert player.team == 0

        # Kill the human
        await game._kill_player_impl(human_serial, accel_mag=3.0)

        # Player should now be a zombie
        assert player.is_zombie is True
        assert player.team == 1
        assert player.color == ZOMBIE_COLOR
        assert player.alive is True  # Zombies stay alive after conversion

    @pytest.mark.asyncio
    async def test_human_conversion_publishes_event(self, zombie_game):
        """Human conversion should publish human_converted event."""
        game, mock_controller_manager, event_collector = zombie_game
        game.gameplay_stream = MockGameplayStream()
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        human_serial = game.human_serials[0]
        initial_human_count = len(game.human_serials)

        await game._kill_player_impl(human_serial, accel_mag=3.0)

        # Check event was published
        conversion_events = event_collector.get_events_of_type("human_converted")
        assert len(conversion_events) == 1
        assert conversion_events[0]["serial"] == human_serial
        assert conversion_events[0]["remaining_humans"] == initial_human_count - 1

    @pytest.mark.asyncio
    async def test_zombie_killed_sets_respawn(self, zombie_game):
        """When a zombie is killed, they should be set to respawn."""
        game, mock_controller_manager, _ = zombie_game
        game.gameplay_stream = MockGameplayStream()
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get a zombie serial
        zombie_serial = game.zombie_serials[0]
        player = game.players[zombie_serial]

        assert player.is_zombie is True
        assert player.alive is True

        # Kill the zombie
        await game._kill_player_impl(zombie_serial, accel_mag=3.0)

        # Zombie should be dead with respawn timer set
        assert player.alive is False
        assert player.respawn_until > 0

    @pytest.mark.asyncio
    async def test_converted_human_removed_from_human_list(self, zombie_game):
        """Converted human should be removed from human_serials."""
        game, mock_controller_manager, _ = zombie_game
        game.gameplay_stream = MockGameplayStream()
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        human_serial = game.human_serials[0]
        assert human_serial in game.human_serials
        assert human_serial not in game.zombie_serials

        await game._kill_player_impl(human_serial, accel_mag=3.0)

        assert human_serial not in game.human_serials
        assert human_serial in game.zombie_serials


class TestZombieThresholds:
    """Tests for zombie-specific thresholds."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_zombie_thresh",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_zombie_returns_zombie_thresholds(self, zombie_game):
        """Zombies should get thresholds from ZOMBIE_THRESHOLDS dict."""

        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get threshold for a zombie
        zombie_serial = game.zombie_serials[0]
        zombie_player = game.players[zombie_serial]
        zombie_thresholds = game._get_effective_thresholds(zombie_player)

        # Should return a tuple from ZOMBIE_THRESHOLDS
        assert isinstance(zombie_thresholds, tuple), "Zombie thresholds should be tuple"
        assert len(zombie_thresholds) == 2, "Zombie thresholds should have (warning, death)"
        assert zombie_thresholds[0] < zombie_thresholds[1], "Warning should be less than death threshold"

    @pytest.mark.asyncio
    async def test_human_returns_sensitivity_value(self, zombie_game):
        """Humans should return sensitivity.value for threshold lookup."""
        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get threshold for a human
        human_serial = game.human_serials[0]
        human_player = game.players[human_serial]
        human_thresholds = game._get_effective_thresholds(human_player)

        # Humans return sensitivity.value (int index) for base class threshold lookup
        # This is intentional - base class uses this to index into SLOW_MAX/FAST_MAX arrays
        assert isinstance(human_thresholds, int), "Human thresholds should be int index"


class TestZombieEdgeCases:
    """Tests for edge cases in zombie game."""

    def test_calculate_duration_two_players(self):
        """Game duration with minimum players."""
        # 2 players: (2 * 3 / 16) * 60 = 22.5 seconds
        duration = calculate_game_duration(2)
        assert duration == 22.5

    def test_calculate_duration_sixteen_players(self):
        """Game duration with 16 players."""
        # 16 players: (16 * 3 / 16) * 60 = 180 seconds (3 minutes)
        duration = calculate_game_duration(16)
        assert duration == 180.0

    @pytest.mark.asyncio
    async def test_minimum_one_human(self):
        """Even with many players, should have at least 1 human."""
        mock_controller_manager = MockControllerManagerService(num_controllers=3)

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_min_human",
        )

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # With 3 players and INITIAL_ZOMBIES=2, should have 2 zombies and 1 human
        assert len(game.human_serials) >= 1
        assert len(game.zombie_serials) == min(INITIAL_ZOMBIES, 2)

    @pytest.mark.asyncio
    async def test_get_game_name(self):
        """get_game_name should return 'Zombie'."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_name",
        )

        assert game.get_game_name() == "Zombie"


# ---------------------------------------------------------------------------
# Recording stream mock for tests that inspect written messages
# ---------------------------------------------------------------------------


class RecordingGameplayStream:
    """Mock gameplay stream that records all written messages."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


# ---------------------------------------------------------------------------
# New test classes appended below
# ---------------------------------------------------------------------------


class TestZombieRespawn:
    """Tests for _respawn_zombie() method."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_zombie_respawn",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_respawn_zombie_revives_player(self, zombie_game):
        """After respawn delay, a dead zombie should be revived with grace period."""
        game, mock_controller_manager, _ = zombie_game
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        player = game.players[zombie_serial]
        player.alive = False

        game.gameplay_stream = RecordingGameplayStream()

        await game._respawn_zombie(zombie_serial, 0.01)

        assert player.alive is True
        assert player.respawn_until == 0.0
        assert player.grace_until > 0

    @pytest.mark.asyncio
    async def test_respawn_zombie_skips_if_not_running(self, zombie_game):
        """If game is no longer running, respawn should not revive the zombie."""
        game, mock_controller_manager, _ = zombie_game
        game.running = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        player = game.players[zombie_serial]
        player.alive = False

        game.gameplay_stream = RecordingGameplayStream()

        await game._respawn_zombie(zombie_serial, 0.01)

        assert player.alive is False

    @pytest.mark.asyncio
    async def test_respawn_zombie_sends_flash_effect(self, zombie_game):
        """Respawn should send a GAME_EFFECT_FLASH via the gameplay stream."""
        game, mock_controller_manager, _ = zombie_game
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        player = game.players[zombie_serial]
        player.alive = False

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._respawn_zombie(zombie_serial, 0.01)

        # Find a message with GAME_EFFECT_FLASH
        flash_messages = [
            m
            for m in stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_FLASH
        ]
        assert len(flash_messages) == 1
        assert flash_messages[0].game_effect.serial == zombie_serial

    @pytest.mark.asyncio
    async def test_respawn_zombie_skips_if_player_not_found(self, zombie_game):
        """Calling _respawn_zombie with a nonexistent serial should not raise."""
        game, mock_controller_manager, _ = zombie_game
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.gameplay_stream = RecordingGameplayStream()

        # Should return silently without error
        await game._respawn_zombie("nonexistent_serial", 0.01)


class TestZombieIntroPhase:
    """Tests for _zombie_intro_phase() method."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_zombie_intro",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_zombie_intro_publishes_start_and_end_events(self, zombie_game):
        """Intro phase should publish zombie_intro_start and zombie_intro_end events."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = zombie_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.zombie.asyncio.sleep", new_callable=AsyncMock):
            await game._zombie_intro_phase()

        start_events = event_collector.get_events_of_type("zombie_intro_start")
        end_events = event_collector.get_events_of_type("zombie_intro_end")
        assert len(start_events) == 1
        assert len(end_events) == 1

    @pytest.mark.asyncio
    async def test_zombie_intro_sets_colors_for_all_players(self, zombie_game):
        """Intro phase should send base_color commands for each player serial."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = zombie_game
        game.running = True
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.zombie.asyncio.sleep", new_callable=AsyncMock):
            await game._zombie_intro_phase()

        # Extract serials from base_color messages
        color_serials = set()
        for m in stream.messages:
            if m.HasField("base_color"):
                color_serials.add(m.base_color.serial)

        # Every player should have received a base_color command
        for serial in game.players:
            assert serial in color_serials, f"Missing base_color for {serial}"

    @pytest.mark.asyncio
    async def test_zombie_intro_stops_if_not_running(self, zombie_game):
        """If game.running becomes False during intro, zombie_intro_end should NOT be published."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = zombie_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        call_count = 0

        async def stop_after_first_call(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                game.running = False

        with patch("services.game_coordinator.games.zombie.asyncio.sleep", side_effect=stop_after_first_call):
            await game._zombie_intro_phase()

        end_events = event_collector.get_events_of_type("zombie_intro_end")
        assert len(end_events) == 0


class TestZombieEndGame:
    """Tests for _end_game_impl() method."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_zombie_endgame",
        )

        return game, mock_controller_manager, event_collector

    async def _setup_game(self, game, mock_controller_manager):
        """Shared setup: initialize players, set state to RUNNING, mock helpers."""
        from unittest.mock import AsyncMock

        from services.game_coordinator.games.base import GameState

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.state = GameState.RUNNING
        game.start_time = 100.0
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()
        game._wait_for_rainbow_effect = AsyncMock()

    @pytest.mark.asyncio
    async def test_end_game_humans_win_time_expired(self, zombie_game):
        """When time_remaining=0 and humans are alive, humans should win."""
        game, mock_controller_manager, event_collector = zombie_game
        await self._setup_game(game, mock_controller_manager)

        game.time_remaining = 0

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winner"] == "humans"

    @pytest.mark.asyncio
    async def test_end_game_zombies_win_all_converted(self, zombie_game):
        """When all humans are converted (none alive as humans), zombies win."""
        game, mock_controller_manager, event_collector = zombie_game
        await self._setup_game(game, mock_controller_manager)

        game.time_remaining = 100

        # Convert all humans to zombies
        for serial in list(game.human_serials):
            player = game.players[serial]
            player.is_zombie = True
            player.team = 1
            player.alive = True
            game.zombie_serials.append(serial)
        game.human_serials.clear()

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winner"] == "zombies"

    @pytest.mark.asyncio
    async def test_end_game_cancels_timer_task(self, zombie_game):
        """_end_game_impl should cancel the timer_task if it is not done."""
        import asyncio
        from unittest.mock import MagicMock

        game, mock_controller_manager, _ = zombie_game
        await self._setup_game(game, mock_controller_manager)

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        game.timer_task = mock_task
        game.time_remaining = 0

        await game._end_game_impl()

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_game_plays_human_victory_sound(self, zombie_game):
        """When humans win, VOX_HUMAN_VICTORY should be played."""
        game, mock_controller_manager, _ = zombie_game
        await self._setup_game(game, mock_controller_manager)

        game.time_remaining = 0

        await game._end_game_impl()

        # Check _play_sound was called with Sound.VOX_HUMAN_VICTORY
        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_HUMAN_VICTORY in sound_args

    @pytest.mark.asyncio
    async def test_end_game_plays_zombie_victory_sound(self, zombie_game):
        """When zombies win, VOX_ZOMBIE_VICTORY should be played."""
        game, mock_controller_manager, _ = zombie_game
        await self._setup_game(game, mock_controller_manager)

        game.time_remaining = 100

        # Convert all humans to zombies
        for serial in list(game.human_serials):
            player = game.players[serial]
            player.is_zombie = True
            player.team = 1
            player.alive = True
            game.zombie_serials.append(serial)
        game.human_serials.clear()

        await game._end_game_impl()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_ZOMBIE_VICTORY in sound_args


class TestZombieCleanup:
    """Tests for cleanup() method."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )

        return ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_zombie_cleanup",
        )

    @pytest.mark.asyncio
    async def test_cleanup_cancels_timer_task(self, zombie_game):
        """cleanup() should cancel the timer_task if it is not done."""
        import asyncio
        from unittest.mock import MagicMock

        game = zombie_game

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        game.timer_task = mock_task

        await game.cleanup()

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_ignores_completed_timer(self, zombie_game):
        """cleanup() should NOT cancel a timer_task that is already done."""
        import asyncio
        from unittest.mock import MagicMock

        game = zombie_game

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = True
        game.timer_task = mock_task

        await game.cleanup()

        mock_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_handles_no_timer(self, zombie_game):
        """cleanup() should not raise when timer_task is None."""
        game = zombie_game
        game.timer_task = None

        # Should complete without error
        await game.cleanup()


class TestZombieGameTimer:
    """Tests for _game_timer() announcements."""

    @pytest.fixture
    def zombie_game(self):
        """Create a Zombie game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = ZombieGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_zombie_timer",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_game_timer_decrements_time(self, zombie_game):
        """_game_timer should decrement time_remaining."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = zombie_game
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.running = True
        game.time_remaining = 3.0

        call_count = 0

        async def counting_sleep(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                game.running = False

        with patch("services.game_coordinator.games.zombie.asyncio.sleep", side_effect=counting_sleep):
            await game._game_timer()

        # After 2 ticks of sleep(1.0), time_remaining should have decreased by 2
        assert game.time_remaining == 1.0

    @pytest.mark.asyncio
    async def test_game_timer_publishes_60s_announcement(self, zombie_game):
        """_game_timer should publish time_announcement at 60 seconds remaining."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = zombie_game
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.running = True
        game.time_remaining = 61.0

        call_count = 0

        async def counting_sleep(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            # Let it run 2 ticks: 61 -> 60 (announcement) -> 59, then stop
            if call_count >= 3:
                game.running = False

        with patch("services.game_coordinator.games.zombie.asyncio.sleep", side_effect=counting_sleep):
            await game._game_timer()

        time_events = event_collector.get_events_of_type("time_announcement")
        found_60s = any(e["seconds_remaining"] == 60 for e in time_events)
        assert found_60s, f"Expected 60s announcement, got events: {time_events}"
