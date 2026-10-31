"""
Unit tests for Zombie game mode.

Tests the core Zombie mechanics:
- Human vs Zombie team assignment
- Human conversion to zombie on death
- Zombie respawn mechanics
- Win conditions (all converted vs time expired)
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, MockGameplayStream, async_noop

from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.zombie import (
    HUMAN_COLOR,
    INITIAL_ZOMBIES,
    ZOMBIE_COLOR,
    ZombieGame,
    ZombiePlayer,
    calculate_game_duration,
)


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

    @pytest.mark.uc("UC-023", flow="main", steps=[1])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[1])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[1])
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

    @pytest.mark.uc("UC-023", flow="A1")
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

    @pytest.mark.uc("UC-023", flow="main", steps=[5])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[2])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[3])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[3])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_human_conversion_sets_grace_period(self, zombie_game):
        """Conversion must grant grace so the death spike that converted the
        human can't instantly re-kill the fresh zombie (#757)."""
        game, mock_controller_manager, _ = zombie_game
        game.gameplay_stream = MockGameplayStream()
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        human_serial = game.human_serials[0]
        player = game.players[human_serial]
        assert player.grace_until <= game_clock.now()

        before = game_clock.now()
        await game._kill_player_impl(human_serial, accel_mag=3.0)

        # Grace should extend ~2s past the conversion
        assert player.grace_until >= before + 1.9
        assert player.grace_until <= game_clock.now() + 2.1

    @pytest.mark.uc("UC-023", flow="main", steps=[4])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[4], br=["BR-002"])
    @pytest.mark.asyncio
    async def test_zombie_returns_zombie_thresholds(self, zombie_game):
        """Zombies should get thresholds from ZOMBIE_THRESHOLDS dict."""
        from services.game_coordinator.games.zombie import ZOMBIE_THRESHOLDS

        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get threshold for a zombie
        zombie_serial = game.zombie_serials[0]
        zombie_player = game.players[zombie_serial]
        zombie_thresholds = game._compute_effective_thresholds(zombie_player)

        # Should return a tuple from ZOMBIE_THRESHOLDS
        assert isinstance(zombie_thresholds, tuple), "Zombie thresholds should be tuple"
        assert len(zombie_thresholds) == 2, "Zombie thresholds should have (warning, death)"
        assert zombie_thresholds[0] < zombie_thresholds[1], "Warning should be less than death threshold"

        # Verify it matches ZOMBIE_THRESHOLDS for game's sensitivity
        expected = ZOMBIE_THRESHOLDS.get(game.sensitivity, (2.1, 2.6))
        assert zombie_thresholds == expected

    @pytest.mark.uc("UC-023", br=["BR-002"])
    @pytest.mark.asyncio
    async def test_human_returns_base_thresholds(self, zombie_game):
        """Humans should return base class thresholds (tuple of warn, death)."""
        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get threshold for a human
        human_serial = game.human_serials[0]
        human_player = game.players[human_serial]
        human_thresholds = game._compute_effective_thresholds(human_player)

        # Humans delegate to base class, which returns (warn, death) tuple
        assert isinstance(human_thresholds, tuple), "Human thresholds should be tuple"
        assert len(human_thresholds) == 2, "Human thresholds should have (warning, death)"
        assert human_thresholds[0] < human_thresholds[1], "Warning should be less than death threshold"

    @pytest.mark.asyncio
    async def test_zombie_threshold_higher_than_human(self, zombie_game):
        """Zombie death threshold should be higher than human death threshold."""
        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        human_serial = game.human_serials[0]

        zombie_thresh = game._compute_effective_thresholds(game.players[zombie_serial])
        human_thresh = game._compute_effective_thresholds(game.players[human_serial])

        # Zombie death threshold should be higher (harder to kill)
        assert zombie_thresh[1] > human_thresh[1], (
            f"Zombie death threshold {zombie_thresh[1]} should be > human {human_thresh[1]}"
        )

    @pytest.mark.uc("UC-023", flow="main", steps=[4], br=["BR-002"])
    @pytest.mark.asyncio
    async def test_zombie_survives_accel_that_kills_human(self, zombie_game):
        """Integration test: acceleration between human and zombie death thresholds kills human but not zombie."""
        from proto import controller_manager_pb2

        game, mock_controller_manager = zombie_game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        human_serial = game.human_serials[0]

        zombie_thresh = game._compute_effective_thresholds(game.players[zombie_serial])
        human_thresh = game._compute_effective_thresholds(game.players[human_serial])

        # Pick acceleration between human death and zombie death thresholds
        mid_accel = (human_thresh[1] + zombie_thresh[1]) / 2

        game.players[zombie_serial].grace_until = 0.0
        game.players[human_serial].grace_until = 0.0

        state_human = controller_manager_pb2.GameplayData(
            serial=human_serial,
            accel=controller_manager_pb2.Vector3(x=mid_accel, y=0.0, z=0.0),
        )
        state_zombie = controller_manager_pb2.GameplayData(
            serial=zombie_serial,
            accel=controller_manager_pb2.Vector3(x=mid_accel, y=0.0, z=0.0),
        )

        # Feed enough frames for EMA to build up
        for _ in range(20):
            if game.players[human_serial].alive and not game.players[human_serial].is_zombie:
                await game._process_controller_state(state_human)
            await game._process_controller_state(state_zombie)

        # Human should be dead (converted to zombie), zombie should survive
        assert game.players[human_serial].is_zombie, "Human should have been converted to zombie"
        assert game.players[zombie_serial].alive, "Zombie should survive mid-range acceleration"

    @pytest.mark.uc("UC-023", flow="main", steps=[4], br=["BR-002"])
    @pytest.mark.asyncio
    async def test_zombie_thresholds_all_sensitivities(self, zombie_game):
        """All 5 sensitivity levels should return valid tuples from ZOMBIE_THRESHOLDS."""
        from services.game_coordinator.games.base import Sensitivity
        from services.game_coordinator.games.zombie import ZOMBIE_THRESHOLDS

        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        zombie_player = game.players[zombie_serial]

        for sensitivity in Sensitivity:
            game.sensitivity = sensitivity
            thresh = game._compute_effective_thresholds(zombie_player)
            expected = ZOMBIE_THRESHOLDS[sensitivity]
            assert thresh == expected, f"Sensitivity {sensitivity}: got {thresh}, expected {expected}"

    @pytest.mark.uc("UC-023", flow="main", steps=[4], br=["BR-002"])
    @pytest.mark.asyncio
    async def test_zombie_thresholds_static_across_music_speed(self, zombie_game):
        """Zombie thresholds should not change with music tempo (intentionally static)."""
        game, mock_controller_manager = zombie_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        zombie_serial = game.zombie_serials[0]
        zombie_player = game.players[zombie_serial]

        # Get thresholds at slow music speed
        game.music_speed = 1.0
        thresh_slow = game._compute_effective_thresholds(zombie_player)

        # Get thresholds at fast music speed
        game.music_speed = 1.3
        thresh_fast = game._compute_effective_thresholds(zombie_player)

        # Zombie thresholds come from static dict, not affected by music
        assert thresh_slow == thresh_fast, f"Zombie thresholds should be static: slow={thresh_slow}, fast={thresh_fast}"


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

    @pytest.mark.uc("UC-023", flow="main", steps=[4])
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

    @pytest.mark.uc("UC-023", flow="main", steps=[4])
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
        # #1342: the zombie colour is written before the flash, which restores to it.
        assert _writes_for(stream, zombie_serial) == [GREY, FLASH]

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

        with patch("services.game_coordinator.game_clock.sleep", new_callable=AsyncMock):
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

        with patch("services.game_coordinator.game_clock.sleep", new_callable=AsyncMock):
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
    @pytest.mark.real_durations
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

        with patch("services.game_coordinator.game_clock.sleep", side_effect=stop_after_first_call):
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

    @pytest.mark.uc("UC-023", flow="main", steps=[5], cond=["Post-S-1"])
    @pytest.mark.asyncio
    async def test_end_game_humans_win_time_expired(self, zombie_game):
        """When time_remaining=0 and humans are alive, humans should win."""
        game, mock_controller_manager, event_collector = zombie_game
        await self._setup_game(game, mock_controller_manager)

        game.time_remaining = 0
        assert await game._check_win_condition()

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winner"] == "humans"
        assert game.state.value == "ended"

    @pytest.mark.uc("UC-023", flow="A1")
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
        assert await game._check_win_condition()

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
        assert await game._check_win_condition()

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
        assert await game._check_win_condition()

        await game._end_game_impl()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_ZOMBIE_VICTORY in sound_args


class TestZombieForceEndWinner:
    """A force-end declares no winner; a winner latched earlier is kept (UC-023 BR-003, #1343)."""

    @pytest_asyncio.fixture
    async def setup(self):
        from unittest.mock import AsyncMock

        from services.game_coordinator.games.base import GameState

        manager = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        events = EventCollector()
        game = ZombieGame(
            controller_manager_client=manager,
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_zombie_force_end",
        )
        await game._initialize_players_impl(manager.controllers)
        game.state = GameState.RUNNING
        game.start_time = 100.0
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()
        game._wait_for_rainbow_effect = AsyncMock()
        game.time_remaining = 100
        return game, events

    @staticmethod
    def _rainbow_writes(game):
        return [
            m
            for m in game.gameplay_stream.messages
            if m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        ]

    @staticmethod
    def _convert_all_humans(game):
        for serial in list(game.human_serials):
            player = game.players[serial]
            player.is_zombie = True
            player.team = 1
            player.alive = True
            game.zombie_serials.append(serial)
        game.human_serials.clear()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003"])
    async def test_force_end_while_running_declares_no_winner(self, setup):
        game, events = setup
        game.force_end("operator")
        game._latch_game_outcome()  # the run() latches when the loop exits

        await game._end_game_impl()

        assert events.get_events_of_type("zombie_winner") == []
        assert self._rainbow_writes(game) == []
        sounds = [c[0][0] for c in game._play_sound.call_args_list]
        assert Sound.VOX_HUMAN_VICTORY not in sounds
        assert Sound.VOX_ZOMBIE_VICTORY not in sounds
        game._wait_for_rainbow_effect.assert_not_called()
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winner"] == "none"
        assert ended["outcome"] == "force_ended"
        assert ended["reason"] == "operator"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003"])
    @pytest.mark.parametrize("side", ["zombies", "humans"])
    async def test_force_end_after_win_keeps_the_winner(self, setup, side):
        game, events = setup
        if side == "zombies":
            self._convert_all_humans(game)
        else:
            game.time_remaining = 0
        assert await game._check_win_condition()
        game._latch_game_outcome()  # the loop returns into its 1 s hold
        game.force_end("late")

        await game._end_game_impl()

        (winner_event,) = events.get_events_of_type("zombie_winner")
        assert winner_event["winner"] == side
        assert self._rainbow_writes(game)
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winner"] == side
        assert ended["outcome"] == "completed"
        assert "reason" not in ended

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003"])
    @pytest.mark.parametrize("side", ["zombies", "humans"])
    async def test_force_end_during_the_winner_publish_keeps_the_winner(self, setup, side):
        """#1988: a force-end landing while zombie_winner is published keeps that winner."""
        game, events = setup
        game.running = True
        if side == "zombies":
            self._convert_all_humans(game)
        else:
            game.time_remaining = 0

        async def publish(kind, data):
            if kind == "zombie_winner":
                game.force_end("operator")  # the servicer thread, mid-publish
            await events.publish(kind, data)

        game.event_publisher = publish
        assert await game._check_win_condition()
        game._latch_game_outcome()  # the run() latches when the loop exits

        await game._end_game_impl()

        (winner_event,) = events.get_events_of_type("zombie_winner")
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winner"] == winner_event["winner"] == side
        assert ended["outcome"] == "completed" and "reason" not in ended
        assert self._rainbow_writes(game)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003"])
    @pytest.mark.parametrize("side", ["zombies", "humans"])
    async def test_force_end_racing_the_outcome_latch_publishes_no_winner(self, setup, side):
        """#1988: a force-end landing right before the outcome latch wins the race: no winner anywhere."""
        game, events = setup
        game.running = True
        if side == "zombies":
            self._convert_all_humans(game)
        else:
            game.time_remaining = 0
        latch = game._latch_game_outcome

        def force_end_then_latch():
            game.force_end("operator")
            latch()

        game._latch_game_outcome = force_end_then_latch

        assert await game._check_win_condition() is False
        await game._end_game_impl()

        assert events.get_events_of_type("zombie_winner") == []
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winner"] == "none" and ended["outcome"] == "force_ended"
        assert self._rainbow_writes(game) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003"])
    @pytest.mark.parametrize("side", ["zombies", "humans"])
    async def test_force_end_before_the_win_check_publishes_no_winner(self, setup, side):
        """#1988: a force-end that lands first declares no winner, even with the win condition met."""
        game, events = setup
        game.running = True
        if side == "zombies":
            self._convert_all_humans(game)
        else:
            game.time_remaining = 0
        game.force_end("operator")

        assert await game._check_win_condition() is False
        game._latch_game_outcome()
        await game._end_game_impl()

        assert events.get_events_of_type("zombie_winner") == []
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winner"] == "none" and ended["outcome"] == "force_ended"
        assert self._rainbow_writes(game) == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003", "BR-005"])
    async def test_win_is_latched_and_published_once(self, setup):
        game, events = setup
        self._convert_all_humans(game)
        assert await game._check_win_condition()
        game.time_remaining = 0  # a later time-up must not change or repeat the win
        assert await game._check_win_condition()

        assert len(events.get_events_of_type("zombie_winner")) == 1
        assert game._winner_side == "zombies"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.uc("UC-023", br=["BR-003"])
    async def test_stream_end_without_a_win_declares_no_winner(self, setup):
        game, events = setup
        game._latch_game_outcome()  # the loop returned (stream closed), no win

        await game._end_game_impl()

        assert self._rainbow_writes(game) == []
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winner"] == "none"
        assert ended["outcome"] == "completed"


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

        with patch("services.game_coordinator.game_clock.sleep", side_effect=counting_sleep):
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

        with patch("services.game_coordinator.game_clock.sleep", side_effect=counting_sleep):
            await game._game_timer()

        time_events = event_collector.get_events_of_type("time_announcement")
        found_60s = any(e["seconds_remaining"] == 60 for e in time_events)
        assert found_60s, f"Expected 60s announcement, got events: {time_events}"


# ---------------------------------------------------------------------------
# #1342: the zombie LED after conversion, respawn and revive (UC-023 steps 3/4, BR-004)
# ---------------------------------------------------------------------------


def _writes_for(stream, serial):
    """The stream writes for one serial, as ("effect", enum) or ("base", rgb) tuples."""
    out = []
    for m in stream.messages:
        if m.HasField("game_effect") and m.game_effect.serial == serial:
            out.append(("effect", m.game_effect.effect))
        elif m.HasField("base_color") and m.base_color.serial == serial:
            c = m.base_color.color
            out.append(("base", (c.r, c.g, c.b)))
    return out


DEATH = ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
FLASH = ("effect", controller_manager_pb2.GAME_EFFECT_FLASH)
RESPAWN = ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_RESPAWN)
GREY = ("base", tuple(ZOMBIE_COLOR))


class BlockingGameplayStream(RecordingGameplayStream):
    """Records writes; a write of the zombie colour waits on ``gate`` (O2 race test)."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.blocked = asyncio.Event()

    async def write(self, message):
        self.messages.append(message)
        if message.HasField("base_color"):
            self.blocked.set()
            await self.gate.wait()


class TestZombieLedAfterKill:
    """#1342: a converted or respawned zombie keeps the zombie colour; revive stays a zombie."""

    @pytest_asyncio.fixture
    async def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        g = ZombieGame(
            controller_manager_client=mock_cm,
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="test_zombie_led",
        )
        await g._initialize_players_impl(mock_cm.controllers)
        g.gameplay_stream = RecordingGameplayStream()
        g.running = True
        g._play_sound = async_noop
        yield g
        await g.cleanup()

    @staticmethod
    def _fast_sleep(monkeypatch):
        real_sleep = asyncio.sleep

        async def fast(_delay, *_args, **_kwargs):
            await real_sleep(0)

        monkeypatch.setattr("services.game_coordinator.game_clock.sleep", fast)

    @pytest.mark.uc("UC-023", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_conversion_sends_one_death_then_the_zombie_colour(self, game, monkeypatch):
        self._fast_sleep(monkeypatch)
        human = game.human_serials[0]
        with patch("services.game_coordinator.games.zombie.metrics") as zm:
            await game._kill_player(human, 3.0)
            # The zombie colour follows from a task, off the game loop (#2023).
            await game._conversion_colour_tasks[human]

        writes = _writes_for(game.gameplay_stream, human)
        assert writes.count(DEATH) == 1
        assert writes[-1] == GREY
        assert game.players[human].alive is True
        zm.player_alive.labels.assert_called_with(serial=human, game_id=game.game_id)
        zm.player_alive.labels.return_value.set.assert_called_with(1)

    @pytest.mark.uc("UC-023", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_conversion_after_force_end_does_not_recreate_the_gauge(self, game):
        class EndGameOnWrite(RecordingGameplayStream):
            async def write(self, message):
                await super().write(message)
                game.running = False

        # The force end lands during the death-flash write, the kill path's await (#2023).
        game.gameplay_stream = EndGameOnWrite()
        human = game.human_serials[0]
        with patch("services.game_coordinator.games.zombie.metrics") as zm:
            await game._kill_player(human, 3.0)

        zm.player_alive.labels.return_value.set.assert_not_called()

    @pytest.mark.uc("UC-023", flow="main", steps=[4])
    @pytest.mark.asyncio
    async def test_zombie_death_then_timer_respawn_ends_grey(self, game, monkeypatch):
        self._fast_sleep(monkeypatch)
        zombie = game.zombie_serials[0]
        with patch("services.game_coordinator.games.zombie.metrics") as zm:
            await game._kill_player(zombie, 5.0)
            assert game.players[zombie].alive is False
            task = game._respawn_tasks[zombie]
            await task

        assert _writes_for(game.gameplay_stream, zombie) == [DEATH, GREY, FLASH]
        assert game.players[zombie].alive is True
        assert zombie not in game._respawn_tasks
        zm.player_alive.labels.return_value.set.assert_called_with(1)

    @pytest.mark.uc("UC-023", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_revive_dead_zombie_respawns_grey_and_cancels_timer(self, game):
        game.zombie_respawn_min = game.zombie_respawn_max = 60.0
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        task = game._respawn_tasks[zombie]

        before = game_clock.now()
        assert await game.revive_player(zombie) is True
        await asyncio.gather(task, return_exceptions=True)

        writes = _writes_for(game.gameplay_stream, zombie)
        assert RESPAWN not in writes
        assert writes[-2:] == [GREY, FLASH]
        assert task.cancelled()
        assert game._respawn_tasks == {}
        player = game.players[zombie]
        assert player.alive is True and player.is_zombie is True
        assert player.grace_until >= before + 1.5

    @pytest.mark.uc("UC-023", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_revive_on_human_is_a_noop(self, game):
        human = game.human_serials[0]
        assert await game.revive_player(human) is False
        assert _writes_for(game.gameplay_stream, human) == []

    @pytest.mark.uc("UC-023", flow="main", steps=[4], br=["BR-003", "BR-004"])
    @pytest.mark.parametrize("decided", ["winner_latched", "not_running"])
    @pytest.mark.asyncio
    async def test_respawn_and_revive_refused_once_decided(self, game, decided):
        game.zombie_respawn_min = game.zombie_respawn_max = 60.0
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        before = list(game.gameplay_stream.messages)
        if decided == "winner_latched":
            game._winner_side = "humans"
        else:
            game.running = False
        player = game.players[zombie]
        grace_before = player.grace_until

        assert await game._respawn_player(zombie) is False
        assert await game.revive_player(zombie) is False

        assert game.gameplay_stream.messages == before
        assert player.alive is False
        assert player.grace_until == grace_before

    @pytest.mark.uc("UC-023", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_racing_revive_and_timer_respawn_once(self, game):
        """O2: the state flips before the first write, so a racing revive or timer is a no-op."""
        game.zombie_respawn_min = game.zombie_respawn_max = 60.0
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        stream = BlockingGameplayStream()
        game.gameplay_stream = stream

        first = asyncio.create_task(game.revive_player(zombie))
        await asyncio.wait_for(stream.blocked.wait(), timeout=1.0)
        # First revive is suspended on its grey write: a second revive and the timer path refuse.
        assert await asyncio.wait_for(game.revive_player(zombie), timeout=1.0) is False
        assert await asyncio.wait_for(game._respawn_player(zombie), timeout=1.0) is False
        stream.gate.set()
        assert await first is True

        assert _writes_for(stream, zombie) == [GREY, FLASH]

    @pytest.mark.uc("UC-023", flow="main", steps=[3], br=["BR-003"])
    @pytest.mark.asyncio
    async def test_eliminate_during_conversion_sleep_with_win_latched_sends_no_death_after_grey(
        self, game, monkeypatch
    ):
        """O3: eliminate_player runs off the game loop; the loop may latch the win meanwhile."""
        gate = asyncio.Event()
        sleeping = asyncio.Event()

        async def gated_sleep(_delay, *_args, **_kwargs):
            sleeping.set()
            await gate.wait()

        monkeypatch.setattr("services.game_coordinator.game_clock.sleep", gated_sleep)
        human = game.human_serials[0]
        kill = asyncio.create_task(game._kill_player(human, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(sleeping.wait(), timeout=1.0)
        game._winner_side = "zombies"
        gate.set()
        await kill
        await game._conversion_colour_tasks[human]

        writes = _writes_for(game.gameplay_stream, human)
        assert writes[-1] == GREY
        assert DEATH not in writes[writes.index(GREY) :]


class TestZombieSpecUnits:
    """UC-023 steps 1 and 4, BR-002 and BR-004 (capability matrix): values the spec states."""

    @staticmethod
    def _game(num_controllers=4, seed=0, **kwargs):
        mock_cm = MockControllerManagerService(num_controllers=num_controllers, death_schedule={}, max_duration=10.0)
        game = ZombieGame(
            controller_manager_client=mock_cm,
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="test_zombie_units",
            rng_seed=seed,
            **kwargs,
        )
        return game, mock_cm

    @pytest.mark.uc("UC-023", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_initial_count_is_capped_at_players_minus_one(self):
        with patch("services.game_coordinator.games.zombie.read_int_flag", return_value=5):
            game, mock_cm = self._game(num_controllers=3)
        assert game.initial_zombies == 5
        await game._initialize_players_impl(mock_cm.controllers)
        assert len(game.zombie_serials) == 2
        assert len(game.human_serials) == 1

    @pytest.mark.uc("UC-023", flow="main", steps=[4])
    @pytest.mark.asyncio
    async def test_respawn_delay_is_drawn_from_the_seeded_rng_inside_the_window(self):
        import random

        with (
            patch(
                "services.game_coordinator.games.zombie.read_float_flag",
                side_effect=lambda _n, key, d, **_: {
                    "zombie.respawn_min_seconds": 3.0,
                    "zombie.respawn_max_seconds": 7.0,
                }.get(key, d),
            ),
        ):
            game, mock_cm = self._game(num_controllers=4, seed=42)
        await game._initialize_players_impl(mock_cm.controllers)
        game.running = True
        game.gameplay_stream = MockGameplayStream()
        game._play_sound = async_noop

        serials = sorted(c.serial for c in mock_cm.controllers)
        expected_rng = random.Random(42)
        expected_rng.sample(serials, len(game.zombie_serials))
        expected_delay = expected_rng.uniform(3.0, 7.0)

        zombie = game.zombie_serials[0]
        before = game_clock.now()
        await game._kill_player_impl(zombie, 5.0)
        delay = game.players[zombie].respawn_until - before

        assert 3.0 <= delay <= 7.0 + 0.5
        assert delay == pytest.approx(expected_delay, abs=0.5)
        assert not game.players[zombie].alive
        await game.cleanup()

    @pytest.mark.uc("UC-023", br=["BR-002"])
    @pytest.mark.asyncio
    async def test_zombie_thresholds_ignore_sensitivity_and_difficulty_factors(self):
        game, mock_cm = self._game()
        await game._initialize_players_impl(mock_cm.controllers)
        zombie = game.players[game.zombie_serials[0]]
        baseline = game._compute_effective_thresholds(zombie)

        zombie.sensitivity_factor = 2.0
        game.global_difficulty_factor = 2.0

        assert game._compute_effective_thresholds(zombie) == baseline
        human = game.players[game.human_serials[0]]
        assert game._compute_effective_thresholds(human) != game.zombie_thresholds[game.sensitivity]

    @pytest.mark.uc("UC-023", br=["BR-004"])
    def test_capability_matrix_key_is_the_game_name_and_blocks_global_tuning(self):
        from services.game_coordinator.interventions import MODE_CAPABILITY_DENY

        game, _ = self._game()
        assert game.get_game_name() == "Zombie"
        denied = MODE_CAPABILITY_DENY[game.get_game_name()]
        assert {"adjust_global_sensitivity", "adjust_global_difficulty"} <= denied
        assert "revive_player" not in denied


class TestZombieRespawnSpanAndGrace:
    """BR-004: the ``zombie_respawned`` span event cause and the revive grace rule."""

    @pytest_asyncio.fixture
    async def game(self, monkeypatch):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        # conftest disables the SDK via OTEL_SDK_DISABLED, which a TracerProvider reads at construction.
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        tracer = provider.get_tracer("test_zombie_respawn")

        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        g = ZombieGame(
            controller_manager_client=mock_cm,
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="test_zombie_respawn_span",
        )
        await g._initialize_players_impl(mock_cm.controllers)
        for serial, player in g.players.items():
            player.span = tracer.start_span(f"player_{serial}")
        g.gameplay_stream = RecordingGameplayStream()
        g.running = True
        g._play_sound = async_noop
        g.zombie_respawn_min = g.zombie_respawn_max = 60.0
        yield g
        await g.cleanup()

    def _respawn_causes(self, game, serial):
        game.players[serial].span.end()
        (span,) = [s for s in self.exporter.get_finished_spans() if s.name == f"player_{serial}"]
        return [e.attributes["respawn.cause"] for e in span.events if e.name == "zombie_respawned"]

    @pytest.mark.uc("UC-023", flow="main", steps=[4], br=["BR-004"])
    @pytest.mark.asyncio
    async def test_timer_respawn_records_cause_timer(self, game, monkeypatch):
        real_sleep = asyncio.sleep

        async def fast(_delay, *_a, **_k):
            await real_sleep(0)

        monkeypatch.setattr("services.game_coordinator.game_clock.sleep", fast)
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        await game._respawn_tasks[zombie]

        assert self._respawn_causes(game, zombie) == ["timer"]

    @pytest.mark.uc("UC-023", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_revive_records_cause_revive(self, game):
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        assert await game.revive_player(zombie) is True

        assert self._respawn_causes(game, zombie) == ["revive"]

    @pytest.mark.uc("UC-023", br=["BR-004"])
    @pytest.mark.parametrize(
        ("death_grace", "expected_grace"),
        [(1.0, 2.0), (3.5, 3.5)],
        ids=["revive_grace_larger", "death_grace_larger"],
    )
    @pytest.mark.asyncio
    async def test_revive_grace_is_the_larger_of_death_grace_and_revive_spawn_grace(
        self, game, monkeypatch, death_grace, expected_grace
    ):
        game.death_grace_period = death_grace
        assert game.REVIVE_SPAWN_GRACE == 2.0
        zombie = game.zombie_serials[0]
        await game._kill_player(zombie, 5.0)
        monkeypatch.setattr(game_clock, "now", lambda: 1000.0)

        assert await game.revive_player(zombie) is True

        assert game.players[zombie].grace_until == 1000.0 + expected_grace


class TestZombieLoopWinCheckAndDisconnect:
    """BR-005 (win check after every sample and disconnect) and main step 3 (disconnects mid-loop)."""

    @pytest_asyncio.fixture
    async def setup(self, monkeypatch):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        events = EventCollector()
        g = ZombieGame(
            controller_manager_client=mock_cm,
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_zombie_loop_disconnect",
        )
        await g._initialize_players_impl(mock_cm.controllers)
        g.gameplay_stream = RecordingGameplayStream()
        g.running = True
        g._play_sound = async_noop
        g.time_remaining = 100.0
        real_sleep = asyncio.sleep

        async def fast(_delay, *_a, **_k):
            await real_sleep(0)

        monkeypatch.setattr("services.game_coordinator.game_clock.sleep", fast)
        yield g, events
        await g.cleanup()

    @staticmethod
    def _update(disconnects=(), controllers=()):
        return controller_manager_pb2.GameplayDataUpdate(
            controllers=[
                controller_manager_pb2.GameplayData(serial=s, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0))
                for s in controllers
            ],
            disconnects=[controller_manager_pb2.DisconnectEvent(serial=s) for s in disconnects],
            timestamp=1,
        )

    @pytest.mark.uc("UC-023", flow="main", steps=[3], br=["BR-005"])
    @pytest.mark.asyncio
    async def test_human_disconnect_mid_loop_converts_and_the_game_goes_on(self, setup):
        game, events = setup
        human = game.human_serials[0]

        assert await game._process_gameplay_update(self._update(disconnects=[human])) is False

        assert game.players[human].is_zombie and human in game.zombie_serials
        assert human not in game.human_serials
        assert events.get_events_of_type("zombie_winner") == []

    @pytest.mark.uc("UC-023", flow="A1", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_last_human_disconnecting_mid_loop_gives_the_zombies_the_win(self, setup):
        game, events = setup
        # One human left: the other converted already.
        await game._kill_player(game.human_serials[0], 5.0)
        (last_human,) = game.human_serials

        assert await game._process_gameplay_update(self._update(disconnects=[last_human])) is True

        (winner,) = events.get_events_of_type("zombie_winner")
        assert winner["winner"] == "zombies"
        assert game._winner_side == "zombies"

    @pytest.mark.uc("UC-023", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_disconnected_zombie_dies_stays_in_the_game_and_respawns_after_its_delay(self, setup):
        game, _ = setup
        zombie = game.zombie_serials[0]

        assert await game._process_gameplay_update(self._update(disconnects=[zombie])) is False

        player = game.players[zombie]
        assert player.alive is False and zombie in game.players
        assert zombie in game.zombie_serials and player.is_zombie
        task = game._respawn_tasks[zombie]
        await task
        assert game.players[zombie].alive is True
        assert game.players[zombie].is_zombie is True

    @pytest.mark.uc("UC-023", flow="main", steps=[5], br=["BR-005"])
    @pytest.mark.asyncio
    async def test_time_up_takes_effect_at_the_next_check_not_instantly(self, setup):
        game, events = setup
        game.time_remaining = 2.0
        humans = list(game.human_serials)

        await game._game_timer()

        # The timer only decrements: it reached 0 but decided nothing.
        assert game.time_remaining <= 0
        assert events.get_events_of_type("zombie_winner") == []
        assert game._winner_side is None

        # The first win check after that (here a motion sample) latches the humans' win.
        assert await game._process_gameplay_update(self._update(controllers=humans[:1])) is True
        (winner,) = events.get_events_of_type("zombie_winner")
        assert winner["winner"] == "humans"
