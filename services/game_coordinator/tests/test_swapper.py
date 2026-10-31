"""
Unit tests for Swapper game mode.

Tests the core swapper mechanics:
- Team swapping on death
- Grace period after swap
- Win condition detection (all players on same team)
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
import pytest_asyncio
from opentelemetry import trace

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.swapper import SwapperGame
from services.game_coordinator.intervention_errors import InterventionNotApplied
from services.game_coordinator.interventions import BLOCK_ELIMINATE_REFUSED, INTERVENTION_SPECS, InterventionContext
from services.game_coordinator.lifecycle_handlers import handle_eliminate_player


class TestSwapperTeamSwapping:
    """Test Swapper's team swapping mechanics."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players (2 per team)."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},  # No auto-deaths, we'll trigger manually
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_001",
        )
        game.running = True

        return game, mock_controller_manager, event_collector

    @pytest.mark.uc("UC-024", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_initial_team_assignment(self, swapper_game):
        """Test that players are assigned to teams round-robin (0, 1, 0, 1)."""
        game, mock_controller_manager, _ = swapper_game

        # Manually initialize players (normally done by game loop)
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Verify round-robin assignment: 0, 1, 0, 1
        assert game.players["mock_controller_0"].team == 0
        assert game.players["mock_controller_1"].team == 1
        assert game.players["mock_controller_2"].team == 0
        assert game.players["mock_controller_3"].team == 1

    @pytest.mark.uc("UC-024", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_kill_player_impl_swaps_team(self, swapper_game):
        """Test that _kill_player_impl swaps player to the other team."""
        game, mock_controller_manager, event_collector = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get initial team for player 0
        player = game.players["mock_controller_0"]
        initial_team = player.team
        assert initial_team == 0

        # Mock the gameplay stream (needed for color updates in _kill_player_impl)
        game.gameplay_stream = MockGameplayStream()

        # Assign team colors (needed for swap)
        await game._assign_team_colors()

        # Call _kill_player_impl directly
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Verify team swapped
        assert player.team == 1, f"Expected team 1, got {player.team}"
        assert player.team != initial_team

        # Verify swap event was published
        swap_events = event_collector.get_events_of_type("player_swapped")
        assert len(swap_events) == 1
        assert swap_events[0]["serial"] == "mock_controller_0"
        assert swap_events[0]["old_team"] == 0
        assert swap_events[0]["new_team"] == 1

    @pytest.mark.asyncio
    async def test_double_swap_returns_to_original_team(self, swapper_game):
        """Test that killing a player twice swaps them back to original team."""
        game, mock_controller_manager, event_collector = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        player = game.players["mock_controller_0"]
        initial_team = player.team
        assert initial_team == 0

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # First swap: 0 -> 1
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.team == 1

        # Clear grace period for immediate second swap
        player.grace_until = 0

        # Second swap: 1 -> 0
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.team == 0, f"Expected team 0 after double swap, got {player.team}"

        # Verify two swap events
        swap_events = event_collector.get_events_of_type("player_swapped")
        assert len(swap_events) == 2

    @pytest.mark.uc("UC-024", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_grace_period_set_after_swap(self, swapper_game):
        """Test that player gets grace period after swapping."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        player = game.players["mock_controller_0"]

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Record time before swap
        time_before = game_clock.now()

        # Swap player
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Verify grace period is ~2 seconds in the future
        assert player.grace_until > time_before + 1.5
        assert player.grace_until < time_before + 3.0

    @pytest.mark.asyncio
    async def test_win_condition_all_on_same_team(self, swapper_game):
        """Test that win condition triggers when all players on same team."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players (2 per team initially)
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initial state: team 0 has [0, 2], team 1 has [1, 3]
        assert not await game._check_win_condition()

        # Move all players to team 1
        for serial in game.players:
            game.players[serial].team = 1

        # Now should trigger win condition
        assert await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_get_alive_teams(self, swapper_game):
        """Test _get_alive_teams returns teams with players."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initial: both teams have players
        teams = game._get_alive_teams()
        assert teams == {0, 1}

        # Move all to team 0
        for serial in game.players:
            game.players[serial].team = 0

        teams = game._get_alive_teams()
        assert teams == {0}

    @pytest.mark.asyncio
    async def test_last_death_serial_tracked(self, swapper_game):
        """Test that last_death_serial tracks the last player to swap."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Initially no last death
        assert game.last_death_serial is None

        # Swap player 0
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert game.last_death_serial == "mock_controller_0"

        # Swap player 1 (clear grace first)
        game.players["mock_controller_1"].grace_until = 0
        await game._kill_player_impl("mock_controller_1", accel_mag=5.0)
        assert game.last_death_serial == "mock_controller_1"

    @pytest.mark.asyncio
    async def test_swap_count_tracked(self, swapper_game):
        """Test that swap_count is tracked per player."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        player = game.players["mock_controller_0"]

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Initially no swaps
        assert getattr(player, "swap_count", 0) == 0

        # First swap
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.swap_count == 1

        # Clear grace and swap again
        player.grace_until = 0
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.swap_count == 2

    @pytest.mark.asyncio
    async def test_swap_changes_color(self, swapper_game):
        """After swap, player color should match their new team's color."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Setup gameplay stream and assign team colors
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Record initial color for player 0
        player = game.players["mock_controller_0"]
        initial_color = player.color

        # Swap player 0 via kill
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Color should have changed to new team's color
        assert player.color != initial_color
        new_team = player.team
        expected_color = game.team_colors[new_team]["rgb"]
        assert player.color == expected_color

    @pytest.mark.asyncio
    async def test_end_game_all_same_team(self, swapper_game):
        """When all players are on the same team, game ends."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Move all players to team 0
        for serial in game.players:
            game.players[serial].team = 0

        # Win condition should trigger
        assert await game._check_win_condition()


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class RecordingGameplayStream:
    """Mock gameplay stream that records all writes."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class TestSwapperWinConditionExclusion:
    """Tests for last-swapper exclusion from winners."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_exclusion",
        )
        game.running = True

        return game, mock_controller_manager, event_collector

    @pytest.mark.uc("UC-024", flow="main", steps=[3], br=["BR-001"])
    @pytest.mark.asyncio
    async def test_last_swapper_excluded_from_winners(self, swapper_game):
        """Test that the player who caused the final swap is NOT a winner."""
        game, mock_controller_manager, event_collector = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Move all players to team 0
        for serial in game.players:
            game.players[serial].team = 0

        # Set last_death_serial (the final swapper)
        game.last_death_serial = "mock_controller_0"

        result = await game._check_win_condition()
        assert result is True

        # Verify the swapper_winner event excludes the last swapper
        winner_events = event_collector.get_events_of_type("swapper_winner")
        assert len(winner_events) == 1
        assert "mock_controller_0" not in winner_events[0]["winners"]
        assert winner_events[0]["excluded_serial"] == "mock_controller_0"
        # Other players should be winners
        assert "mock_controller_1" in winner_events[0]["winners"]
        assert "mock_controller_2" in winner_events[0]["winners"]
        assert "mock_controller_3" in winner_events[0]["winners"]

    @pytest.mark.asyncio
    async def test_win_condition_includes_team_info(self, swapper_game):
        """Test that win event includes correct team number and name."""
        game, mock_controller_manager, event_collector = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Move all players to team 1
        for serial in game.players:
            game.players[serial].team = 1

        game.last_death_serial = "mock_controller_2"

        await game._check_win_condition()

        winner_events = event_collector.get_events_of_type("swapper_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["team"] == 1
        assert winner_events[0]["team_name"] == game.team_colors[1]["name"]


class TestSwapperGracePeriod:
    """Tests for grace period during team swap."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_grace",
        )
        game.running = True

        return game, mock_controller_manager, event_collector

    @pytest.mark.uc("UC-024", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_player_alive_after_swap(self, swapper_game):
        """Test that player is alive=True after completing a swap."""
        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        player = game.players["mock_controller_0"]
        assert player.alive is True

        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Player should be alive again after swap completes
        assert player.alive is True

    @pytest.mark.asyncio
    async def test_grace_period_is_approximately_2_seconds(self, swapper_game):
        """Test that grace period is approximately 2 seconds from swap time."""
        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        player = game.players["mock_controller_1"]
        before = game_clock.now()

        await game._kill_player_impl("mock_controller_1", accel_mag=5.0)

        # Grace period should be ~2 seconds from when swap was initiated
        assert player.grace_until >= before + 1.5
        assert player.grace_until <= before + 3.5  # Allow for asyncio.sleep(0.5) in swap


class TestSwapperEndGameImpl:
    """Tests for _end_game_impl winner effects and team sound."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_endgame",
        )
        game.running = True

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_end_game_sends_rainbow_to_winners(self, swapper_game):
        """Test that _end_game_impl sends rainbow effect to winning players."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All on team 0
        for serial in game.players:
            game.players[serial].team = 0

        game.last_death_serial = "mock_controller_3"  # Excluded

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        assert await game._check_win_condition()  # latches the win (UC-024 BR-004)
        game.running = False

        await game._end_game_impl()

        # Rainbow effects should be sent to winners (not the excluded player)
        effect_msgs = [m for m in stream.messages if m.HasField("game_effect")]
        effect_serials = {m.game_effect.serial for m in effect_msgs}

        assert "mock_controller_0" in effect_serials
        assert "mock_controller_1" in effect_serials
        assert "mock_controller_2" in effect_serials
        assert "mock_controller_3" not in effect_serials

    @pytest.mark.asyncio
    async def test_end_game_dims_excluded_player(self, swapper_game):
        """Test that _end_game_impl sets dim gray on the excluded last-swapper."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.players:
            game.players[serial].team = 0

        game.last_death_serial = "mock_controller_2"

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        assert await game._check_win_condition()  # latches the win (UC-024 BR-004)
        game.running = False

        await game._end_game_impl()

        # Look for dim gray color command for excluded player
        color_msgs = [m for m in stream.messages if m.HasField("base_color")]
        excluded_colors = [m for m in color_msgs if m.base_color.serial == "mock_controller_2"]
        assert len(excluded_colors) >= 1
        assert excluded_colors[0].base_color.color.r == 50
        assert excluded_colors[0].base_color.color.g == 50
        assert excluded_colors[0].base_color.color.b == 50

    @pytest.mark.uc("UC-024", flow="main", steps=[4])
    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended_event(self, swapper_game):
        """Test that _end_game_impl publishes game_ended with winning_team."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, event_collector = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.players:
            game.players[serial].team = 1

        game.last_death_serial = None
        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = game_clock.now() - 30
        game.state = GameState.RUNNING
        assert await game._check_win_condition()  # latches the win (UC-024 BR-004)
        game.running = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winning_team"] == 1
        assert ended_events[0]["game_id"] == "test_swapper_endgame"

    @pytest.mark.uc("UC-024", flow="main", steps=[4])
    @pytest.mark.asyncio
    async def test_end_game_sets_state_ended(self, swapper_game):
        """Test that _end_game_impl transitions to ENDED state."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.players:
            game.players[serial].team = 0

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        assert await game._check_win_condition()  # latches the win (UC-024 BR-004)
        game.running = False

        await game._end_game_impl()

        assert game.state == GameState.ENDED


class TestSwapperSpanManagement:
    """Tests for hierarchical span recreation on team swap."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_spans",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.uc("UC-024", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_get_team_counts(self, swapper_game):
        """Test _get_team_counts returns correct per-team player counts."""
        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initial round-robin: 2 on team 0, 2 on team 1
        counts = game._get_team_counts()
        assert counts[0] == 2
        assert counts[1] == 2

        # Move one player to team 1
        game.players["mock_controller_0"].team = 1
        counts = game._get_team_counts()
        assert counts[0] == 1
        assert counts[1] == 3

    @pytest.mark.asyncio
    async def test_get_game_name(self, swapper_game):
        """Test get_game_name returns 'Swapper'."""
        game, _, _ = swapper_game
        assert game.get_game_name() == "Swapper"


class TestSwapperLedAfterSwap:
    """#1342: a swapped player's LED ends on the new team colour, never on the death effect."""

    @pytest_asyncio.fixture
    async def game(self):
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        g = SwapperGame(
            controller_manager_client=mock_cm,
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="test_swapper_led",
        )
        await g._initialize_players_impl(mock_cm.controllers)
        await g._assign_team_colors()
        g.gameplay_stream = RecordingGameplayStream()
        g.running = True

        async def no_sound(*_args, **_kwargs):
            return None

        g._play_sound = no_sound
        yield g
        await g.cleanup()

    @staticmethod
    def _fast_pause(game):
        async def instant():
            return None

        game._swap_pause = instant

    @staticmethod
    def _writes_for(stream, serial):
        out = []
        for m in stream.messages:
            if m.HasField("game_effect") and m.game_effect.serial == serial:
                out.append(("effect", m.game_effect.effect))
            elif m.HasField("base_color") and m.base_color.serial == serial:
                c = m.base_color.color
                out.append(("base", (c.r, c.g, c.b)))
        return out

    @pytest.mark.uc("UC-024", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_swap_sends_one_flash_no_death_and_ends_on_new_team_colour(self, game):
        self._fast_pause(game)
        serial = "mock_controller_0"
        with patch("services.game_coordinator.games.swapper.metrics") as sm:
            await game._kill_player(serial, 3.0)

        writes = self._writes_for(game.gameplay_stream, serial)
        new_rgb = tuple(game.team_colors[1]["rgb"])
        assert ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) not in writes
        assert writes.count(("effect", controller_manager_pb2.GAME_EFFECT_FLASH)) == 1
        assert writes[-1] == ("base", new_rgb)
        assert game.players[serial].alive is True
        sm.player_alive.labels.assert_called_with(serial=serial, game_id=game.game_id)
        sm.player_alive.labels.return_value.set.assert_called_with(1)

    @pytest.mark.uc("UC-024", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_swap_after_force_end_does_not_recreate_the_gauge(self, game):
        async def end_game_during_pause():
            game.running = False

        game._swap_pause = end_game_during_pause
        with patch("services.game_coordinator.games.swapper.metrics") as sm:
            await game._kill_player("mock_controller_0", 3.0)

        sm.player_alive.labels.return_value.set.assert_not_called()


STILL = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)


def _frame(*serials: str, disconnects: tuple[str, ...] = ()):
    """A gameplay update: still samples for ``serials`` plus disconnect events."""
    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[controller_manager_pb2.GameplayData(serial=s, accel=STILL) for s in serials],
        disconnects=[controller_manager_pb2.DisconnectEvent(serial=s) for s in disconnects],
        timestamp=int(time.time() * 1000),
    )


def _record_sounds(game) -> list:
    """Replace the game's sound player with a recorder; returns the list of played sounds."""
    sounds: list = []

    async def record_sound(sound, *_args, **_kwargs):
        sounds.append(sound)

    game._play_sound = record_sound
    return sounds


class TestSwapperDisconnect:
    """UC-024 A1 (#1344): a disconnected player is eliminated, never swapped, never a winner."""

    @staticmethod
    async def _game(num_players: int):
        events = EventCollector()
        mock_cm = MockControllerManagerService(num_controllers=num_players, death_schedule={}, max_duration=10.0)
        g = SwapperGame(
            controller_manager_client=mock_cm,
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_swapper_disconnect",
        )
        await g._initialize_players_impl(mock_cm.controllers)
        await g._assign_team_colors()
        g.gameplay_stream = RecordingGameplayStream()
        g.running = True
        g.state = GameState.RUNNING
        g.start_time = game_clock.now() - 5.0
        events.clear()

        async def no_sound(*_args, **_kwargs):
            return None

        async def instant():
            return None

        g._play_sound = no_sound
        g._swap_pause = instant
        return g, events

    @staticmethod
    def _writes_for(stream, serial):
        return TestSwapperLedAfterSwap._writes_for(stream, serial)

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-001"])
    @pytest.mark.asyncio
    async def test_mid_loop_disconnect_is_not_swapped_and_never_wins(self):
        # Teams: c0, c2 on team 0; c1, c3 on team 1.
        game, events = await self._game(4)
        gone = "mock_controller_1"

        assert not await game._process_gameplay_update(
            _frame("mock_controller_0", "mock_controller_2", "mock_controller_3", disconnects=(gone,))
        )

        player = game.players[gone]
        assert gone in game.players  # stays in the session's player list
        assert player.team == 1 and player.alive is False and player.span is None
        assert events.count_events_of_type("player_swapped") == 0
        writes = self._writes_for(game.gameplay_stream, gone)
        assert ("effect", controller_manager_pb2.GAME_EFFECT_FLASH) not in writes
        assert writes[-1] == ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert game._get_team_counts() == {0: 2, 1: 1}

        # c3 swaps to team 0: team 1 has only the eliminated c1 left, so team 0 wins.
        await game._kill_player("mock_controller_3", 3.0)
        assert await game._process_gameplay_update(_frame("mock_controller_0"))

        winner = events.get_events_of_type("swapper_winner")
        assert len(winner) == 1
        assert winner[0]["team"] == 0
        assert sorted(winner[0]["winners"]) == ["mock_controller_0", "mock_controller_2"]
        assert winner[0]["excluded_serial"] == "mock_controller_3"
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1")
    @pytest.mark.asyncio
    async def test_warmup_disconnect_eliminates(self):
        game, events = await self._game(4)
        gone = "mock_controller_0"
        game._warmup_disconnects = [gone]
        span = create_autospec(trace.Span, instance=True)
        span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
        game.players[gone].span = span

        assert not await game._handle_warmup_disconnects()

        assert game.players[gone].team == 0 and game.players[gone].alive is False
        assert events.count_events_of_type("player_swapped") == 0
        writes = self._writes_for(game.gameplay_stream, gone)
        assert writes[-1] == ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert ("effect", controller_manager_pb2.GAME_EFFECT_FLASH) not in writes
        eliminated = [c for c in span.add_event.call_args_list if c.args[0] == "player_eliminated"]
        assert [c.kwargs["attributes"]["reason"] for c in eliminated] == ["controller_disconnect"]
        assert game._get_alive_teams() == {0, 1}
        assert game._get_team_counts() == {0: 1, 1: 2}
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-001"])
    @pytest.mark.asyncio
    async def test_two_players_one_disconnects_the_other_wins_nobody_excluded(self):
        game, events = await self._game(2)
        # c0 swapped once and came back (team 0 -> 1 -> 0): it is the last swapped player.
        await game._kill_player("mock_controller_0", 3.0)
        await game._kill_player("mock_controller_0", 3.0)
        assert game.last_death_serial == "mock_controller_0"

        assert await game._process_gameplay_update(_frame(disconnects=("mock_controller_1",)))

        winner = events.get_events_of_type("swapper_winner")
        assert winner == [
            {
                "team": 0,
                "team_name": game.team_colors[0]["name"],
                "winners": ["mock_controller_0"],
                "excluded_serial": "",
            }
        ]

        game.gameplay_stream = RecordingGameplayStream()
        game.running = False  # the celebration loop ends before its first sleep
        await game._end_game_impl()
        rainbow = [
            m.game_effect.serial
            for m in game.gameplay_stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        ]
        assert rainbow == ["mock_controller_0"]
        assert not any(m.HasField("base_color") for m in game.gameplay_stream.messages)  # nobody greyed
        assert events.get_events_of_type("game_ended")[0]["winning_team"] == 0
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-001", "BR-002"])
    @pytest.mark.asyncio
    async def test_disconnect_during_eliminate_swap_pause_keeps_player_out(self):
        game, events = await self._game(2)
        target = "mock_controller_0"
        paused, release = asyncio.Event(), asyncio.Event()

        async def gated_pause():
            paused.set()
            await release.wait()

        game._swap_pause = gated_pause
        sounds = _record_sounds(game)
        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(paused.wait(), 2.0)
        assert game.players[target].team == 1  # mid-swap

        # The disconnect lands while the swap sleeps; the win check runs at once.
        assert await game._process_gameplay_update(_frame(disconnects=(target,)))
        winner = events.get_events_of_type("swapper_winner")
        assert winner[0]["winners"] == ["mock_controller_1"]
        assert winner[0]["excluded_serial"] == ""

        release.set()
        await asyncio.wait_for(kill, 2.0)

        player = game.players[target]
        assert player.alive is False and player.span is None
        assert game._excluded_serial() is None
        assert events.count_events_of_type("player_swapped") == 0
        writes = self._writes_for(game.gameplay_stream, target)
        assert not any(kind == "base" for kind, _ in writes)  # no new team colour
        assert writes[-1] == ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert Sound.SFX_BEEP not in sounds  # A1 step 3: no swap sound
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-002"])
    @pytest.mark.asyncio
    async def test_disconnect_during_eliminate_kill_sound_eliminates_instead_of_swapping(self):
        game, events = await self._game(2)
        target = "mock_controller_0"
        in_sound, release = asyncio.Event(), asyncio.Event()

        sounds: list = []

        async def gated_sound(sound, *_args, **_kwargs):
            sounds.append(sound)
            if not in_sound.is_set():
                in_sound.set()
                await release.wait()

        game._play_sound = gated_sound
        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(in_sound.wait(), 2.0)

        assert await game._process_gameplay_update(_frame(disconnects=(target,)))
        assert events.get_events_of_type("swapper_winner")[0]["winners"] == ["mock_controller_1"]

        release.set()
        await asyncio.wait_for(kill, 2.0)

        player = game.players[target]
        assert player.team == 0 and player.alive is False
        assert events.count_events_of_type("player_swapped") == 0
        writes = self._writes_for(game.gameplay_stream, target)
        assert ("effect", controller_manager_pb2.GAME_EFFECT_FLASH) not in writes
        assert writes[-1] == ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        assert Sound.SFX_BEEP not in sounds  # A1 step 3: no swap sound
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1")
    @pytest.mark.asyncio
    async def test_every_player_eliminated_gives_no_winner(self):
        game, events = await self._game(2)
        game._eliminated.update(game.players)

        assert await game._check_win_condition()
        assert events.get_events_of_type("swapper_winner") == []

        game.running = False  # the celebration loop ends before its first sleep
        await game._end_game_impl()
        assert events.get_events_of_type("game_ended")[0]["winning_team"] == "none"
        assert not any(
            m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
            for m in game.gameplay_stream.messages
        )
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="main", steps=[3])
    @pytest.mark.uc("UC-024", flow="A1")
    @pytest.mark.asyncio
    async def test_win_check_during_swap_pauses_counts_the_swapping_players(self):
        """Team counts leave out eliminated players only, not players whose swap is still pausing."""
        # Teams: c0, c2 on team 0; c1 on team 1.
        game, events = await self._game(3)
        paused, release = asyncio.Event(), asyncio.Event()
        pausing = 0

        async def gated_pause():
            nonlocal pausing
            pausing += 1
            if pausing == 2:
                paused.set()
            await release.wait()

        game._swap_pause = gated_pause
        swaps = [
            asyncio.create_task(game._kill_player("mock_controller_0", 3.0)),
            asyncio.create_task(game._kill_player("mock_controller_1", 3.0)),
        ]
        await asyncio.wait_for(paused.wait(), 2.0)

        # c0 now on team 1, c1 on team 0, both not alive during the pause: still two teams.
        assert game._get_alive_teams() == {0, 1}
        assert not await game._check_win_condition()
        assert events.get_events_of_type("swapper_winner") == []

        release.set()
        await asyncio.wait_for(asyncio.gather(*swaps), 2.0)
        await game.cleanup()

    @staticmethod
    def _rainbow(stream):
        return [
            m.game_effect.serial
            for m in stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        ]

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-001"])
    @pytest.mark.asyncio
    async def test_eliminated_player_on_the_winning_team_never_wins(self):
        # Teams: c0, c2 on team 0; c1 on team 1. c2 disconnects, then c1 swaps to team 0.
        game, events = await self._game(3)
        assert not await game._process_gameplay_update(
            _frame("mock_controller_0", "mock_controller_1", disconnects=("mock_controller_2",))
        )
        assert game.players["mock_controller_2"].team == 0

        await game._kill_player("mock_controller_1", 3.0)
        assert await game._process_gameplay_update(_frame("mock_controller_0"))

        winner = events.get_events_of_type("swapper_winner")
        assert len(winner) == 1 and winner[0]["team"] == 0
        assert winner[0]["winners"] == ["mock_controller_0"]
        assert winner[0]["excluded_serial"] == "mock_controller_1"

        game.gameplay_stream = RecordingGameplayStream()
        game.running = False  # the celebration loop ends before its first sleep
        await game._end_game_impl()
        assert self._rainbow(game.gameplay_stream) == ["mock_controller_0"]
        assert events.get_events_of_type("game_ended")[0]["winning_team"] == 0
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-001", "BR-002"])
    @pytest.mark.asyncio
    async def test_disconnect_after_a_converging_agent_swap_keeps_the_swapper_excluded(self):
        # Teams: c0, c2 on team 0; c1 on team 1. The agent swaps c1 to team 0: converged.
        game, events = await self._game(3)
        await game._kill_player("mock_controller_1", 0.0, reason="agent_intervention")
        assert game._get_alive_teams() == {0}
        assert events.get_events_of_type("swapper_winner") == []

        # The next update carries c2's disconnect before any win check ran.
        assert await game._process_gameplay_update(_frame("mock_controller_0", disconnects=("mock_controller_2",)))

        winner = events.get_events_of_type("swapper_winner")
        assert len(winner) == 1 and winner[0]["team"] == 0
        assert winner[0]["winners"] == ["mock_controller_0"]
        assert winner[0]["excluded_serial"] == "mock_controller_1"
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="A1", br=["BR-001", "BR-002"])
    @pytest.mark.asyncio
    async def test_converged_swapper_left_alone_on_its_team_wins(self):
        # D2: the agent swaps c1 to team 0 (converged), then c0 disconnects: c1 is the
        # only player left, so it is not excluded and the winners list is not empty.
        game, events = await self._game(2)
        await game._kill_player("mock_controller_1", 0.0, reason="agent_intervention")
        assert game._get_alive_teams() == {0}

        assert await game._process_gameplay_update(_frame("mock_controller_1", disconnects=("mock_controller_0",)))

        winner = events.get_events_of_type("swapper_winner")
        assert len(winner) == 1 and winner[0]["team"] == 0
        assert winner[0]["winners"] == ["mock_controller_1"]
        assert winner[0]["excluded_serial"] == ""
        await game.cleanup()


class TestSwapperForceEndWinner:
    """UC-024 BR-003 / BR-004 (#1343): a force-end declares no winner; the first win is latched."""

    @staticmethod
    async def _game(num_players: int):
        game, events = await TestSwapperDisconnect._game(num_players)
        game.sounds = []

        async def record_sound(sound, *_args, **_kwargs):
            game.sounds.append(sound)

        game._play_sound = record_sound
        return game, events

    @staticmethod
    def _effects(stream, effect):
        return [
            m.game_effect.serial
            for m in stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == effect
        ]

    @staticmethod
    def _greys(stream):
        return [
            m.base_color.serial
            for m in stream.messages
            if m.HasField("base_color")
            and (m.base_color.color.r, m.base_color.color.g, m.base_color.color.b) == (50, 50, 50)
        ]

    @staticmethod
    def _spans(game):
        spans = {}
        for serial, player in game.players.items():
            span = create_autospec(trace.Span, instance=True)
            # A real (invalid) context, so trace propagation on the kill path works.
            span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
            player.span = span
            spans[serial] = span
        return spans

    @staticmethod
    def _span_winner_flags(spans):
        return {
            serial: [
                c.kwargs["attributes"]["winner"] for c in span.add_event.call_args_list if c.args[0] == "game_ended"
            ]
            for serial, span in spans.items()
        }

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_force_end_with_two_teams_declares_no_winner(self):
        game, events = await self._game(4)
        spans = self._spans(game)
        game.last_death_serial = "mock_controller_3"
        game.force_end("operator")

        await game._end_game_impl()

        stream = game.gameplay_stream
        assert self._effects(stream, controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == []
        assert self._greys(stream) == []
        assert game.sounds == []
        assert all(flags == [False] for flags in self._span_winner_flags(spans).values())
        assert events.get_events_of_type("swapper_winner") == []
        ended = events.get_events_of_type("game_ended")
        assert len(ended) == 1
        assert ended[0]["winning_team"] == "none"
        assert ended[0]["outcome"] == "force_ended"
        assert ended[0]["reason"] == "operator"
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_stream_end_without_a_win_declares_no_winner(self):
        game, events = await self._game(4)
        game.running = False  # the stream ended; not force-ended, nothing latched

        await game._end_game_impl()

        assert self._effects(game.gameplay_stream, controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == []
        assert game.sounds == []
        assert events.get_events_of_type("game_ended")[0]["winning_team"] == "none"
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_force_end_during_swap_pause_publishes_no_winner_and_no_swap(self):
        game, events = await self._game(2)
        target = "mock_controller_0"
        paused, release = asyncio.Event(), asyncio.Event()

        async def gated_pause():
            paused.set()
            await release.wait()

        game._swap_pause = gated_pause
        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(paused.wait(), 2.0)
        assert game.players[target].team == 1  # both players on team 1 now: converged

        game.force_end("operator")
        assert await game._check_win_condition() is False
        assert events.get_events_of_type("swapper_winner") == []

        release.set()
        await asyncio.wait_for(kill, 2.0)

        assert events.count_events_of_type("player_swapped") == 0
        assert Sound.SFX_BEEP not in game.sounds  # no swap beep (the base kill sound is not ours)
        writes = TestSwapperLedAfterSwap._writes_for(game.gameplay_stream, target)
        assert not any(kind == "base" for kind, _ in writes)  # no colour over the end
        assert ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) not in writes

        await game._end_game_impl()
        assert self._effects(game.gameplay_stream, controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW) == []
        assert events.get_events_of_type("game_ended")[0]["winning_team"] == "none"
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-004"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_latched_eliminate_is_refused_before_any_bookkeeping(self):
        """#1383: once latched, the kill gate declines an agent eliminate: no swap, no counters."""
        game, events = await self._game(4)
        for player in game.players.values():
            player.team = 0
        assert await game._check_win_condition()  # latches the win

        ctx = InterventionContext(
            spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player"),
            value="mock_controller_1",
            payload="mock_controller_1",
            target_serial="mock_controller_1",
            game=game,
            objective="balanced",
        )
        with (
            patch("services.game_coordinator.games.base.metrics") as m,
            pytest.raises(InterventionNotApplied) as exc,
        ):
            await handle_eliminate_player(ctx)
        assert exc.value.reason == BLOCK_ELIMINATE_REFUSED
        assert game.players["mock_controller_1"].team == 0
        assert game.dead_count == 0
        m.player_deaths_total.labels.assert_not_called()
        assert game.sounds == []
        assert events.count_events_of_type("player_swapped") == 0
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003", "BR-004"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_force_end_during_the_hold_keeps_the_winner(self):
        game, events = await self._game(4)
        spans = self._spans(game)
        for player in game.players.values():
            player.team = 0
        game.last_death_serial = "mock_controller_3"

        assert await game._check_win_condition()
        assert await game._check_win_condition()  # latched: not published again
        assert events.count_events_of_type("swapper_winner") == 1
        game._latch_game_outcome()  # what run() does when the loop returns
        game.force_end("operator")

        # The hold: an agent kill and a disconnect no longer change the winners (BR-004).
        await game._kill_player("mock_controller_1", 0.0, reason="agent_intervention")
        await game._handle_disconnect("mock_controller_2")
        assert game.players["mock_controller_1"].team == 0 and game._eliminated == set()
        assert events.count_events_of_type("player_swapped") == 0

        await game._end_game_impl()

        stream = game.gameplay_stream
        assert sorted(self._effects(stream, controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW)) == [
            "mock_controller_0",
            "mock_controller_1",
            "mock_controller_2",
        ]
        assert self._greys(stream) == ["mock_controller_3"]
        assert len(game.sounds) == 1
        flags = self._span_winner_flags(spans)
        assert flags["mock_controller_0"] == [True] and flags["mock_controller_3"] == [False]
        ended = events.get_events_of_type("game_ended")[0]
        assert ended["winning_team"] == 0 and ended["outcome"] == "completed"
        assert "reason" not in ended
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_late_wake_up_after_the_latch_keeps_the_end_effects(self):
        # Teams: c0, c2 on team 0; c1 on team 1. The agent swaps c1 to team 0.
        game, events = await self._game(3)
        target = "mock_controller_1"
        paused, release = asyncio.Event(), asyncio.Event()

        async def gated_pause():
            paused.set()
            await release.wait()

        game._swap_pause = gated_pause
        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(paused.wait(), 2.0)

        # The gameplay loop sees the convergence while the swap sleeps and latches it.
        assert await game._process_gameplay_update(_frame("mock_controller_0"))
        winner = events.get_events_of_type("swapper_winner")
        assert len(winner) == 1 and winner[0]["excluded_serial"] == target
        game._latch_game_outcome()

        release.set()
        await asyncio.wait_for(kill, 2.0)

        writes = TestSwapperLedAfterSwap._writes_for(game.gameplay_stream, target)
        assert not any(kind == "base" for kind, _ in writes)  # the swap did not finish
        assert ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) not in writes
        assert events.count_events_of_type("player_swapped") == 0
        assert Sound.SFX_BEEP not in game.sounds

        game.running = False  # the celebration wait ends at once
        await game._end_game_impl()
        assert self._greys(game.gameplay_stream) == [target]
        await game.cleanup()

    @staticmethod
    def _hold_explosion(game):
        """Hold the base kill path at its explosion sound, before the swap impl runs."""
        held, release = asyncio.Event(), asyncio.Event()

        async def gated_sound(sound, *_args, **_kwargs):
            game.sounds.append(sound)
            if sound == Sound.SFX_EXPLOSION:
                held.set()
                await release.wait()

        game._play_sound = gated_sound
        return held, release

    @pytest.mark.uc("UC-024", br=["BR-004"])
    @pytest.mark.asyncio
    async def test_kill_held_at_the_explosion_does_not_swap_a_latched_winner(self):
        game, events = await self._game(3)
        for player in game.players.values():
            player.team = 0
        game.last_death_serial = "mock_controller_2"
        target = "mock_controller_1"
        held, release = self._hold_explosion(game)

        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(held.wait(), 2.0)
        assert await game._check_win_condition()  # the loop latches the win meanwhile
        winners = events.get_events_of_type("swapper_winner")[0]["winners"]

        release.set()
        await asyncio.wait_for(kill, 2.0)

        assert game.players[target].team == 0 and game.players[target].alive is True
        assert game.last_death_serial == "mock_controller_2"
        assert game._latched_winners == winners
        writes = TestSwapperLedAfterSwap._writes_for(game.gameplay_stream, target)
        assert ("effect", controller_manager_pb2.GAME_EFFECT_FLASH) not in writes
        assert ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH) not in writes
        assert events.count_events_of_type("player_swapped") == 0
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_kill_held_at_the_explosion_does_not_swap_after_a_force_end(self):
        game, events = await self._game(4)
        target = "mock_controller_0"
        old_team = game.players[target].team
        held, release = self._hold_explosion(game)

        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(held.wait(), 2.0)
        game.force_end("operator")

        release.set()
        await asyncio.wait_for(kill, 2.0)

        assert game.players[target].team == old_team and game.players[target].alive is True
        assert game.last_death_serial is None
        writes = TestSwapperLedAfterSwap._writes_for(game.gameplay_stream, target)
        assert writes == []  # no flash, no colour, no death effect onto the ending stream
        assert events.count_events_of_type("player_swapped") == 0
        await game.cleanup()


LETHAL = controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0)


def _lethal_frame(*lethal: str, still: tuple[str, ...] = ()):
    """A gameplay update with lethal samples for ``lethal`` and still ones for ``still``."""
    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[controller_manager_pb2.GameplayData(serial=s, accel=LETHAL) for s in lethal]
        + [controller_manager_pb2.GameplayData(serial=s, accel=STILL) for s in still],
        timestamp=int(time.time() * 1000),
    )


class TestSwapperMotionLoop:
    """UC-024 steps 2 to 4 and FR-170 driven through the real motion path (no direct impl calls)."""

    @staticmethod
    async def _game(num_players: int):
        game, events = await TestSwapperDisconnect._game(num_players)
        for player in game.players.values():
            player.grace_until = 0.0
        return game, events

    @pytest.mark.real_game_time
    @pytest.mark.uc("UC-024", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_lethal_motion_inside_the_grace_does_not_swap_again(self):
        game, events = await self._game(4)
        serial = "mock_controller_0"  # team 0; the others stay on their teams

        for _ in range(30):
            await game._process_gameplay_update(_lethal_frame(serial, still=("mock_controller_1",)))
            if events.count_events_of_type("player_swapped"):
                break
        assert game.players[serial].team == 1
        assert events.count_events_of_type("player_swapped") == 1
        grace = game.death_grace_period  # the configured death_grace_period_seconds (FR-170)
        assert grace > 0
        remaining = game.players[serial].grace_until - game_clock.now()
        assert grace - 1.0 < remaining <= grace

        # Keep moving hard inside the grace: no second swap, the player stays alive.
        for _ in range(30):
            await game._process_gameplay_update(_lethal_frame(serial, still=("mock_controller_1",)))
        assert events.count_events_of_type("player_swapped") == 1
        assert game.players[serial].team == 1 and game.players[serial].alive is True
        await game.cleanup()

    @pytest.mark.uc("UC-024", flow="main", steps=[3, 4], cond=["Post-S-1"])
    @pytest.mark.asyncio
    async def test_lethal_motion_converges_to_one_winner_and_the_game_ends(self):
        game, events = await self._game(2)
        mover, other = "mock_controller_0", "mock_controller_1"

        finished = False
        for _ in range(30):
            if await game._process_gameplay_update(_lethal_frame(mover, still=(other,))):
                finished = True
                break
        assert finished, "the swap never converged the teams"

        winner = events.get_events_of_type("swapper_winner")
        assert len(winner) == 1
        assert winner[0]["team"] == 1
        assert winner[0]["winners"] == [other]
        assert winner[0]["excluded_serial"] == mover  # the last swapped player does not win

        game.gameplay_stream = RecordingGameplayStream()
        game.running = False  # the celebration loop ends before its first sleep
        await game._end_game_impl()

        ended = events.get_events_of_type("game_ended")
        assert len(ended) == 1 and ended[0]["winning_team"] == 1
        # The unit harness has no servicer session: the game reaching ENDED is the session end
        # (same as UC-026 Post-S-1); the winners are in the event stream above.
        assert game.state == GameState.ENDED
        assert len(events.get_events_of_type("swapper_winner")) == 1  # still published once
        await game.cleanup()


class TestSwapperStart:
    """UC-024 Pre-1: the two-player minimum is enforced by the base run()."""

    @pytest.mark.uc("UC-024", cond=["Pre-1"])
    @pytest.mark.asyncio
    async def test_one_player_raises_before_the_game_starts(self):
        events = EventCollector()
        mock_cm = MockControllerManagerService(num_controllers=1, death_schedule={}, max_duration=10.0)
        game = SwapperGame(
            controller_manager_client=mock_cm,
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_swapper_one_player",
            initial_players=[type("P", (), {"serial": "mock_controller_0"})()],
        )

        with pytest.raises(ValueError, match="at least 2 players"):
            await asyncio.wait_for(game.run(), timeout=10)

        assert events.get_events_of_type("swapper_winner") == []
        await game.cleanup()


class TestSwapperEliminationSpan:
    """UC-024 A1 step 1 and BR-002: the elimination span event, and the revive denial."""

    @pytest.mark.uc("UC-024", flow="A1")
    @pytest.mark.asyncio
    async def test_disconnect_ends_the_player_span_with_player_eliminated(self):
        game, _ = await TestSwapperDisconnect._game(4)
        gone = "mock_controller_1"
        span = create_autospec(trace.Span, instance=True)
        span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
        game.players[gone].span = span

        await game._process_gameplay_update(_frame("mock_controller_0", disconnects=(gone,)))

        events = {c.args[0]: c.kwargs.get("attributes") for c in span.add_event.call_args_list}
        assert events["player_eliminated"] == {"reason": "controller_disconnect"}
        span.end.assert_called_once()
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-002"])
    def test_revive_player_is_denied_for_swapper(self):
        from services.game_coordinator.interventions import MODE_CAPABILITY_DENY

        game = SwapperGame(
            controller_manager_client=MockControllerManagerService(num_controllers=2, death_schedule={}),
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="test_swapper_revive",
        )
        denied = MODE_CAPABILITY_DENY[game.get_game_name()]
        assert "revive_player" in denied
        assert "eliminate_player" not in denied


class TestSwapperWinOutcomeLatch:
    """UC-024 BR-003 / BR-004 (#1988): the winner event and game_ended always agree on a force-end."""

    @staticmethod
    def _force_end_on(game, events, event_type):
        """Force-end the game while ``event_type`` is being published (the servicer thread)."""

        async def publish(kind, data):
            if kind == event_type:
                game.force_end("operator")
            await events.publish(kind, data)

        game.event_publisher = publish

    @pytest.mark.uc("UC-024", br=["BR-003", "BR-004"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_force_end_during_the_winner_publish_keeps_the_winner(self):
        game, events = await TestSwapperForceEndWinner._game(4)
        for player in game.players.values():
            player.team = 0
        game.last_death_serial = "mock_controller_3"
        self._force_end_on(game, events, "swapper_winner")

        assert await game._check_win_condition()
        game._latch_game_outcome()  # what run() does when the loop returns
        await game._end_game_impl()

        (winner,) = events.get_events_of_type("swapper_winner")
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winning_team"] == winner["team"] == 0
        assert ended["outcome"] == "completed" and "reason" not in ended
        rainbow = TestSwapperForceEndWinner._effects(
            game.gameplay_stream, controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        )
        assert sorted(rainbow) == sorted(winner["winners"])
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003", "BR-004"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_force_end_racing_the_outcome_latch_publishes_no_winner(self):
        """A force-end landing right before the outcome latch wins the race: no winner anywhere."""
        game, events = await TestSwapperForceEndWinner._game(4)
        for player in game.players.values():
            player.team = 0
        latch = game._latch_game_outcome

        def force_end_then_latch():
            game.force_end("operator")
            latch()

        game._latch_game_outcome = force_end_then_latch

        assert await game._check_win_condition() is False
        await game._end_game_impl()

        assert events.get_events_of_type("swapper_winner") == []
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winning_team"] == "none" and ended["outcome"] == "force_ended"
        assert ended["reason"] == "operator"
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_force_end_during_the_swap_publish_plays_no_beep(self):
        """The swap event is out, but a force-end landing during its publish silences the beep."""
        game, events = await TestSwapperForceEndWinner._game(3)
        target = "mock_controller_1"

        async def publish(kind, data):
            if kind == "player_swapped":
                game.force_end("operator")
            await events.publish(kind, data)

        game.event_publisher = publish
        await game._kill_player(target, 0.0, reason="agent_intervention")

        assert events.count_events_of_type("player_swapped") == 1
        assert Sound.SFX_BEEP not in game.sounds
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    @pytest.mark.asyncio
    async def test_force_end_before_the_win_check_publishes_no_winner(self):
        game, events = await TestSwapperForceEndWinner._game(4)
        for player in game.players.values():
            player.team = 0
        game.force_end("operator")

        assert await game._check_win_condition() is False
        game._latch_game_outcome()
        await game._end_game_impl()

        assert events.get_events_of_type("swapper_winner") == []
        (ended,) = events.get_events_of_type("game_ended")
        assert ended["winning_team"] == "none" and ended["outcome"] == "force_ended"
        await game.cleanup()

    @staticmethod
    def _player_death_events(span):
        return [c for c in span.add_event.call_args_list if c.args[0] == "player_death"]

    @pytest.mark.uc("UC-024", br=["BR-004"])
    @pytest.mark.uc("UC-030", flow="main", steps=[9])
    @pytest.mark.asyncio
    async def test_kill_held_while_the_win_latches_counts_no_death(self):
        game, events = await TestSwapperForceEndWinner._game(3)
        spans = TestSwapperForceEndWinner._spans(game)
        for player in game.players.values():
            player.team = 0
        target = "mock_controller_1"
        held, release = TestSwapperForceEndWinner._hold_explosion(game)

        with patch("services.game_coordinator.games.base.metrics") as m:
            kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
            await asyncio.wait_for(held.wait(), 2.0)
            assert await game._check_win_condition()  # the loop latches the win meanwhile
            release.set()
            assert await asyncio.wait_for(kill, 2.0) is False

        assert game.dead_count == 0
        m.player_deaths_total.labels.assert_not_called()
        m.player_alive.labels.assert_not_called()
        m.player_elimination_order.labels.assert_not_called()
        assert self._player_death_events(spans[target]) == []
        assert game.players[target].alive is True
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003"])
    @pytest.mark.uc("UC-030", flow="main", steps=[9])
    @pytest.mark.asyncio
    async def test_kill_held_while_force_ended_counts_no_death(self):
        game, events = await TestSwapperForceEndWinner._game(4)
        spans = TestSwapperForceEndWinner._spans(game)
        target = "mock_controller_0"
        held, release = TestSwapperForceEndWinner._hold_explosion(game)

        with patch("services.game_coordinator.games.base.metrics") as m:
            kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
            await asyncio.wait_for(held.wait(), 2.0)
            game.force_end("operator")
            release.set()
            assert await asyncio.wait_for(kill, 2.0) is False

        assert game.dead_count == 0
        m.player_deaths_total.labels.assert_not_called()
        assert self._player_death_events(spans[target]) == []
        await game.cleanup()

    @pytest.mark.uc("UC-024", br=["BR-003", "BR-004"])
    @pytest.mark.parametrize("ending", ["win_latched", "force_ended"])
    @pytest.mark.asyncio
    async def test_end_during_the_colour_write_publishes_no_swap(self, ending):
        # Teams: c0, c2 on team 0; c1 on team 1. The agent swaps c1 to team 0.
        game, events = await TestSwapperForceEndWinner._game(3)
        target = "mock_controller_1"
        writing, release = asyncio.Event(), asyncio.Event()
        real_set_colour = game._set_player_color

        async def gated_set_colour(serial, color):
            await real_set_colour(serial, color)
            writing.set()
            await release.wait()

        game._set_player_color = gated_set_colour
        kill = asyncio.create_task(game._kill_player(target, 0.0, reason="agent_intervention"))
        await asyncio.wait_for(writing.wait(), 2.0)
        if ending == "win_latched":
            assert await game._check_win_condition()
        else:
            game.force_end("operator")
        release.set()
        await asyncio.wait_for(kill, 2.0)

        assert game.players[target].team == 0 and game.players[target].alive is True
        assert events.count_events_of_type("player_swapped") == 0
        assert Sound.SFX_BEEP not in game.sounds
        await game.cleanup()
