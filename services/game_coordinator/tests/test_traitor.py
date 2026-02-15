"""
Unit tests for Traitor game mode.

Tests the core Traitor mechanics:
- Traitor assignment based on player count
- Visible team vs secret team tracking
- Win condition based on secret teams
- Player death handling
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
from services.game_coordinator.games.traitor import TraitorGame, TraitorPlayer


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class TestTraitorGameMode:
    """Test Traitor game mechanics."""

    @pytest.fixture
    def traitor_game(self):
        """Create a Traitor game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_001",
        )

        return game, mock_controller_manager, event_collector

    def test_get_traitor_count(self, traitor_game):
        """Test traitor count calculation based on player count."""
        game, _, _ = traitor_game

        # 4-5 players: 1 traitor
        assert game._get_traitor_count(4) == 1
        assert game._get_traitor_count(5) == 1

        # 6-8 players: 2 traitors
        assert game._get_traitor_count(6) == 2
        assert game._get_traitor_count(8) == 2

        # 9-11 players: 3 traitors
        assert game._get_traitor_count(9) == 3
        assert game._get_traitor_count(11) == 3

        # 12+ players: num_players // 3
        assert game._get_traitor_count(12) == 4
        assert game._get_traitor_count(15) == 5

    @pytest.mark.asyncio
    async def test_player_initialization(self, traitor_game):
        """Test that players are initialized correctly."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All 4 players should be created
        assert len(game.players) == 4

        # Should have 1 traitor for 4 players
        assert len(game.traitor_serials) == 1

    @pytest.mark.asyncio
    async def test_traitor_has_different_secret_team(self, traitor_game):
        """Test that traitors have different secret team than visible team."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.traitor_serials:
            player = game.players[serial]
            assert player.is_traitor is True
            # Secret team should be different from visible team
            assert player.secret_team != player.team

    @pytest.mark.asyncio
    async def test_non_traitors_have_matching_teams(self, traitor_game):
        """Test that non-traitors have matching visible and secret teams."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial, player in game.players.items():
            if serial not in game.traitor_serials:
                assert player.is_traitor is False
                assert player.secret_team == player.team

    @pytest.mark.asyncio
    async def test_get_alive_teams_uses_secret_team(self, traitor_game):
        """Test that _get_alive_teams returns secret teams, not visible teams."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get alive teams (should be based on secret_team)
        alive_teams = game._get_alive_teams()

        # Should have both teams (0 and 1) alive
        assert 0 in alive_teams
        assert 1 in alive_teams

    @pytest.mark.asyncio
    async def test_win_condition_based_on_secret_teams(self, traitor_game):
        """Test that win condition uses secret teams."""
        game, mock_controller_manager, event_collector = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially no win
        assert not await game._check_win_condition()

        # Kill all players with secret_team == 1
        for _serial, player in game.players.items():
            if player.secret_team == 1:
                player.alive = False

        # Team 0 should win
        assert await game._check_win_condition()

        # Verify winner event
        winner_events = event_collector.get_events_of_type("traitor_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["team"] == 0

    @pytest.mark.asyncio
    async def test_traitor_wins_with_secret_team(self, traitor_game):
        """Test that traitors are counted with their secret team for win condition."""
        game, mock_controller_manager, event_collector = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get the traitor
        traitor_serial = game.traitor_serials[0]
        traitor = game.players[traitor_serial]
        traitor_secret_team = traitor.secret_team

        # Kill all players EXCEPT those with the traitor's secret team
        for _serial, player in game.players.items():
            if player.secret_team != traitor_secret_team:
                player.alive = False

        # Traitor's secret team wins
        assert await game._check_win_condition()

        winner_events = event_collector.get_events_of_type("traitor_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["team"] == traitor_secret_team

    @pytest.mark.asyncio
    async def test_kill_player_marks_dead(self, traitor_game):
        """Test that _kill_player_impl marks player as dead."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get first player
        serial = list(game.players.keys())[0]
        player = game.players[serial]
        assert player.alive is True

        # Kill the player
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.alive is False

    @pytest.mark.asyncio
    async def test_traitor_player_dataclass(self, traitor_game):
        """Test TraitorPlayer dataclass attributes."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players should be TraitorPlayer instances
        for _serial, player in game.players.items():
            assert isinstance(player, TraitorPlayer)
            assert hasattr(player, "is_traitor")
            assert hasattr(player, "secret_team")

    @pytest.mark.asyncio
    async def test_no_win_with_both_secret_teams_alive(self, traitor_game):
        """Test that game continues with both secret teams having alive players."""
        game, mock_controller_manager, _ = traitor_game
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All alive - no win
        assert not await game._check_win_condition()

        # Kill one player, but keep both secret teams alive
        # Find players from each secret team
        team0_players = [s for s, p in game.players.items() if p.secret_team == 0]

        # Only kill if we have multiple players on each team
        if len(team0_players) > 1:
            game.players[team0_players[0]].alive = False

        # Still both teams alive - no win
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_get_game_name(self, traitor_game):
        """Test get_game_name returns 'Traitor'."""
        game, _, _ = traitor_game
        assert game.get_game_name() == "Traitor"


class TestTraitorCount:
    """Tests for traitor count calculation."""

    @pytest.fixture
    def traitor_game(self):
        """Create a Traitor game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_count",
        )

        return game, mock_controller_manager

    def test_traitor_count_2_players(self, traitor_game):
        """Test traitor count for 2 players."""
        game, _ = traitor_game
        # 2-3 players: 1 traitor
        assert game._get_traitor_count(2) == 1
        assert game._get_traitor_count(3) == 1

    def test_traitor_count_scales_with_players(self, traitor_game):
        """Test traitor count scales appropriately."""
        game, _ = traitor_game

        # Verify count increases with players
        counts = [game._get_traitor_count(n) for n in range(4, 17)]

        # Each count should be >= previous count
        for i in range(1, len(counts)):
            assert counts[i] >= counts[i - 1]


class TestTraitorLargeGame:
    """Tests for larger player count games."""

    @pytest.mark.asyncio
    async def test_8_players_has_2_traitors(self):
        """Test 8 players has 2 traitors."""
        mock_controller_manager = MockControllerManagerService(num_controllers=8)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_8",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        assert len(game.traitor_serials) == 2

    @pytest.mark.asyncio
    async def test_10_players_has_3_traitors(self):
        """Test 10 players has 3 traitors."""
        mock_controller_manager = MockControllerManagerService(num_controllers=10)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_10",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        assert len(game.traitor_serials) == 3


class TestTraitorEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_all_dead_triggers_win_condition(self):
        """Test that all players dead triggers win condition."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_draw",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill all players
        for player in game.players.values():
            player.alive = False

        # Should trigger win (draw scenario - all dead)
        # Note: Traitor checks alive teams, if none alive, might return True
        result = await game._check_win_condition()
        assert result is True  # Game ends when no alive teams

    @pytest.mark.asyncio
    async def test_traitors_distributed_across_visible_teams(self):
        """Test traitors can be on different visible teams."""
        mock_controller_manager = MockControllerManagerService(num_controllers=6)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_dist",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 6 players = 2 traitors
        assert len(game.traitor_serials) == 2

        # Get visible teams of traitors
        traitor_visible_teams = {game.players[s].team for s in game.traitor_serials}

        # Traitors exist and have visible teams assigned
        assert len(traitor_visible_teams) >= 1


class RecordingGameplayStream:
    """Mock gameplay stream that records all writes."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class TestTraitorInitializationRotation:
    """Tests for traitor rotation across teams during initialization."""

    @pytest.mark.asyncio
    async def test_traitors_rotate_across_teams(self):
        """Test that traitors are assigned from different teams in rotation."""
        mock_controller_manager = MockControllerManagerService(num_controllers=6)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_rotation",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 6 players, 2 teams, 2 traitors
        assert len(game.traitor_serials) == 2

        # The rotation algorithm picks from team 0 first, then team 1
        # So traitors should come from different visible teams
        traitor_teams = [game.players[s].team for s in game.traitor_serials]
        assert set(traitor_teams) == {0, 1}

    @pytest.mark.asyncio
    async def test_initialization_publishes_event_with_traitor_count(self):
        """Test that player initialization publishes event with traitor count."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_init_event",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        init_events = event_collector.get_events_of_type("players_initialized")
        assert len(init_events) == 1
        assert init_events[0]["traitor_count"] == 1
        assert init_events[0]["player_count"] == 4
        assert init_events[0]["num_teams"] == 2

    @pytest.mark.asyncio
    async def test_three_teams_traitor_gets_different_secret_team(self):
        """Test that with 3 teams, traitor's secret team differs from visible team."""
        mock_controller_manager = MockControllerManagerService(num_controllers=9)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_3teams",
            num_teams=3,
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 9 players = 3 traitors
        assert len(game.traitor_serials) == 3

        for serial in game.traitor_serials:
            player = game.players[serial]
            assert player.is_traitor is True
            # Secret team must differ from visible team
            assert player.secret_team != player.team
            # Secret team must be a valid team number
            assert 0 <= player.secret_team < 3


class TestTraitorSignalPhase:
    """Tests for the _traitor_signal_phase rumble signaling."""

    @pytest.mark.asyncio
    async def test_traitor_signal_publishes_start_event(self):
        """Test that traitor signal phase publishes start event and respects interruption."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_signal",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set up gameplay stream for rumble commands
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.running = False  # Will cause early return (interrupted)

        await game._traitor_signal_phase()

        # Start event is published before the wait loop
        start_events = event_collector.get_events_of_type("traitor_signal_start")
        assert len(start_events) == 1
        assert start_events[0]["traitor_count"] == 1

        # End event is NOT published when interrupted (running=False)
        end_events = event_collector.get_events_of_type("traitor_signal_end")
        assert len(end_events) == 0

    @pytest.mark.asyncio
    async def test_traitor_signal_sends_rumble_commands(self):
        """Test that traitor signal sends rumble effects to traitor controllers."""
        mock_controller_manager = MockControllerManagerService(num_controllers=6)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_rumble",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.running = False

        await game._traitor_signal_phase()

        # Filter rumble effect messages
        rumble_msgs = [
            m
            for m in stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_RUMBLE
        ]

        # Should have sent rumble to each traitor
        assert len(rumble_msgs) == len(game.traitor_serials)
        rumble_serials = {m.game_effect.serial for m in rumble_msgs}
        assert rumble_serials == set(game.traitor_serials)


class TestTraitorEndGameImpl:
    """Tests for _end_game_impl traitor reveal and winner determination."""

    @pytest.mark.asyncio
    async def test_end_game_sends_rainbow_to_secret_team_winners(self):
        """Test that _end_game_impl sends rainbow to winning secret team members."""
        from services.game_coordinator.games.base import GameState

        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_endgame",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill all players with secret_team == 1
        for _serial, player in game.players.items():
            if player.secret_team == 1:
                player.alive = False

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        # Rainbow effects should only go to alive players on winning secret team
        effect_msgs = [m for m in stream.messages if m.HasField("game_effect")]
        effect_serials = {m.game_effect.serial for m in effect_msgs}

        alive_winners = [s for s, p in game.players.items() if p.alive and p.secret_team == 0]
        for s in alive_winners:
            assert s in effect_serials

    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended_with_winning_team(self):
        """Test that _end_game_impl publishes game_ended with the winning team."""
        from services.game_coordinator.games.base import GameState

        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_ended",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill all with secret_team == 1
        for player in game.players.values():
            if player.secret_team == 1:
                player.alive = False

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time() - 45
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winning_team"] == 0
        assert ended_events[0]["traitor_count"] == len(game.traitor_serials)
        assert ended_events[0]["game_id"] == "test_traitor_ended"

    @pytest.mark.asyncio
    async def test_end_game_no_winner_all_dead(self):
        """Test _end_game_impl when no team has surviving players."""
        from services.game_coordinator.games.base import GameState

        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_draw",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill everyone
        for player in game.players.values():
            player.alive = False

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winning_team"] == -1  # No winner

    @pytest.mark.asyncio
    async def test_end_game_transitions_to_ended_state(self):
        """Test that _end_game_impl sets state to ENDED."""
        from services.game_coordinator.games.base import GameState

        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_traitor_state",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        assert game.state == GameState.ENDED


class TestTraitorInstantWin:
    """Test edge case where all players happen to be on one secret team."""

    @pytest.mark.asyncio
    async def test_all_on_same_secret_team_instant_win(self):
        """Test that if all alive players share a secret team, game ends immediately."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_instant",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Manually force all players to secret_team=0
        for player in game.players.values():
            player.secret_team = 0
            player.alive = True

        result = await game._check_win_condition()
        assert result is True

        winner_events = event_collector.get_events_of_type("traitor_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["team"] == 0
        assert len(winner_events[0]["winners"]) == 4

    @pytest.mark.asyncio
    async def test_win_identifies_traitor_winners_vs_loyal(self):
        """Test that win event separates traitor winners from loyal winners."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TraitorGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_traitor_classify",
        )
        game.random_teams = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill all players with secret_team == 1
        for player in game.players.values():
            if player.secret_team == 1:
                player.alive = False

        result = await game._check_win_condition()
        assert result is True

        winner_events = event_collector.get_events_of_type("traitor_winner")
        assert len(winner_events) == 1

        event = winner_events[0]
        # All winners should have secret_team == 0
        for serial in event["winners"]:
            assert game.players[serial].secret_team == 0

        # Traitor winners are those in traitor_serials AND winners
        for serial in event["traitor_winners"]:
            assert serial in game.traitor_serials
            assert serial in event["winners"]

        # Loyal winners are NOT in traitor_serials
        for serial in event["loyal_winners"]:
            assert serial not in game.traitor_serials
            assert serial in event["winners"]
