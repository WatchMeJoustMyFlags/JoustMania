"""
Unit tests for Traitor game mode.

Tests the core Traitor mechanics:
- Traitor assignment based on player count
- Visible team vs secret team tracking
- Win condition based on secret teams
- Player death handling
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop

from lib.types import GameEvent
from proto import controller_manager_pb2, game_coordinator_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import GameState, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.traitor import TraitorGame, TraitorPlayer
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager
from services.game_coordinator.tests.test_death_detection_uc030 import LETHAL, STILL, _frame
from services.game_coordinator.tests.test_ffa_uc020 import RecordingAudio, RecordingControllerManager


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

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
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
    @pytest.mark.uc("UC-022", flow="main", steps=[2])
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
    @pytest.mark.uc("UC-022", flow="main", steps=[4])
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

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
    def test_traitor_count_2_players(self, traitor_game):
        """A 2-player game has no traitor; from 3 players there is one."""
        game, _ = traitor_game
        assert game._get_traitor_count(2) == 0
        assert game._get_traitor_count(3) == 1

    @staticmethod
    def _make_game(num_players, tiers=None):
        manager = MockControllerManagerService(num_controllers=num_players)
        game = TraitorGame(
            controller_manager_client=manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id=f"test_traitor_cap_{num_players}",
        )
        game.random_teams = False
        if tiers is not None:
            game._count_tiers = tiers
        return game, manager

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
    def test_tier_count_not_below_players_does_not_hang(self):
        """A tier asking for >= players traitors finishes with 1 traitor (the loop has no await)."""
        game, manager = self._make_game(4, tiers=[{"max_players": 10, "count": 10}])
        errors = []

        def run_init():
            try:
                asyncio.run(game._initialize_players_impl(manager.controllers))
            except Exception as exc:  # pragma: no cover - surfaced by assertion below
                errors.append(exc)

        worker = threading.Thread(target=run_init, daemon=True)
        worker.start()
        worker.join(2.0)
        assert not worker.is_alive(), "traitor selection loop did not terminate"
        assert not errors
        assert len(game.traitor_serials) == 1

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
    @pytest.mark.asyncio
    async def test_two_players_no_traitor_no_instant_win(self):
        """With 2 players nobody is a traitor and the first sample does not end the game."""
        game, manager = self._make_game(2)
        await game._initialize_players_impl(manager.controllers)
        assert game.traitor_serials == []
        assert not await game._check_win_condition()

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("num_players", range(2, 17))
    async def test_default_tiers_loyal_outnumber_traitors(self, num_players):
        """Default tiers: traitors < loyal players and at least 2 non-empty secret teams."""
        game, manager = self._make_game(num_players)
        await game._initialize_players_impl(manager.controllers)
        traitors = len(game.traitor_serials)
        assert traitors < num_players - traitors
        secret_teams = {p.secret_team for p in game.players.values()}
        assert len(secret_teams) >= 2

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

    @pytest.mark.uc("UC-022", flow="A2")
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
        assert len(event_collector.get_events_of_type("game_tie")) == 1
        assert event_collector.get_events_of_type("traitor_winner") == []

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

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
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

    @pytest.mark.uc("UC-022", flow="main", steps=[1])
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
        """Test that traitor signal sends rumble effects to traitor controllers.

        The injected stream matches production: run() opens the gameplay stream
        before the traitor_signal phase (UC-022 BR-003); the full-run check is in
        test_traitor_signal_stream.py.
        """
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
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        game.running = True
        assert await game._check_win_condition()  # latches the result (UC-022 BR-005)
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
        game.start_time = game_clock.now() - 45
        game.state = GameState.RUNNING
        game.running = True
        assert await game._check_win_condition()  # latches the result (UC-022 BR-005)
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
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        game.running = True
        assert await game._check_win_condition()  # latches the result (UC-022 BR-005)
        game.running = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winning_team"] == "none"  # No winner (was -1 before #2003)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", cond=["Post-S-1"])
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
        game.start_time = game_clock.now()
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
    @pytest.mark.uc("UC-022", flow="main", steps=[4])
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


# --------------------------------------------------------------------------- #
# UC-022 gaps closed by #1361: main steps 3 and 4, Post-F-1
# --------------------------------------------------------------------------- #
def _uc022_players(count: int) -> list:
    return [game_coordinator_pb2.Player(serial=f"mock_controller_{i}") for i in range(count)]


class TestUC022Step3BaseThresholds:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", flow="main", steps=[3])
    async def test_traitor_and_loyal_die_at_the_base_thresholds(self):
        """UC-022 step 3: Traitor adds no role threshold; a lethal sample kills a traitor and a loyal player alike."""
        events = EventCollector()
        manager = MockControllerManagerService(num_controllers=6)
        game = TraitorGame(
            controller_manager_client=manager,
            event_publisher=events.publish,
            audio_client=None,
            game_id="uc022_step3",
            initial_players=_uc022_players(6),
            rng_seed=11,
        )
        await game._initialize_players()
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        traitor = game.players[game.traitor_serials[0]]
        loyal_serials = [s for s in game.players if s not in game.traitor_serials]
        loyal = game.players[loyal_serials[0]]
        assert traitor.is_traitor and not loyal.is_traitor

        reference = FFAGame(
            controller_manager_client=MockControllerManagerService(num_controllers=2),
            event_publisher=EventCollector().publish,
            game_id="uc022_step3_ffa",
        )
        base = reference._compute_effective_thresholds(Player(serial="x", team=0))
        assert game._compute_effective_thresholds(traitor) == base
        assert game._compute_effective_thresholds(loyal) == base

        for _ in range(10):
            await game._process_gameplay_update(_frame((traitor.serial, LETHAL), (loyal.serial, STILL)))
            if not traitor.alive:
                break
        assert traitor.alive is False
        assert loyal.alive is True

        for _ in range(10):
            await game._process_gameplay_update(_frame((loyal.serial, LETHAL)))
            if not loyal.alive:
                break
        assert loyal.alive is False


def _uc022_fast_config():
    return GamePerformanceConfig(countdown_phase_duration_ms=50, winner_rainbow_duration_ms=200)


class TestUC022Step4EndToEnd:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", flow="main", steps=[4], cond=["Post-S-1"])
    async def test_run_ends_with_traitor_winner_then_game_ended(self):
        """UC-022 step 4: run() with deaths leaving one secret team publishes traitor_winner, then game_ended."""
        seed = 5
        # A throwaway game with the same seed shows which player sits on which secret team.
        probe = TraitorGame(
            controller_manager_client=MockControllerManagerService(num_controllers=4),
            event_publisher=EventCollector().publish,
            game_id="uc022_probe",
            initial_players=_uc022_players(4),
            rng_seed=seed,
        )
        await probe._initialize_players()
        serials = [f"mock_controller_{i}" for i in range(4)]
        winning_team = probe.players[serials[0]].secret_team
        expected_winners = [s for s in serials if probe.players[s].secret_team == winning_team]
        losers = [i for i, s in enumerate(serials) if s not in expected_winners]
        assert losers, "the seed must leave a second secret team to eliminate"

        manager = RecordingControllerManager(
            num_controllers=4,
            death_schedule={1.0 + 0.2 * n: idx for n, idx in enumerate(losers)},
            max_duration=30.0,
        )
        events = EventCollector()
        game = TraitorGame(
            controller_manager_client=manager,
            event_publisher=events.publish,
            audio_client=create_autospec(RecordingAudio, instance=True),
            game_id="uc022_step4",
            initial_players=_uc022_players(4),
            rng_seed=seed,
        )
        with (
            patch.object(get_config_manager(), "get_config", return_value=_uc022_fast_config()),
            patch.object(game, "_play_sound", autospec=True),
        ):
            await asyncio.wait_for(game.run(), timeout=40.0)

        types = [t for t, _ in events.events]
        winner = events.get_events_of_type("traitor_winner")
        assert len(winner) == 1
        assert winner[0]["team"] == winning_team
        assert sorted(winner[0]["winners"]) == sorted(expected_winners)
        assert sorted(winner[0]["traitor_winners"] + winner[0]["loyal_winners"]) == sorted(expected_winners)
        assert winner[0]["traitor_winners"] == [s for s in expected_winners if s in game.traitor_serials]
        assert types.index("traitor_winner") < types.index(GameEvent.GAME_ENDED)
        assert types.count(GameEvent.GAME_ENDED) == 1
        assert GameEvent.GAME_FORCE_ENDED not in types
        assert game.state == GameState.ENDED


class TestUC022PostF1:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", cond=["Post-F-1"])
    async def test_traitor_init_failure_publishes_game_error_and_raises(self):
        """UC-022 Post-F-1: a Traitor run() whose player initialisation raises publishes game_error and ends."""
        events = EventCollector()
        game = TraitorGame(
            controller_manager_client=RecordingControllerManager(num_controllers=4, infinite=True),
            event_publisher=events.publish,
            audio_client=create_autospec(RecordingAudio, instance=True),
            game_id="uc022_f1",
            initial_players=_uc022_players(4),
            rng_seed=3,
        )
        with (
            patch.object(get_config_manager(), "get_config", return_value=_uc022_fast_config()),
            patch.object(game, "_initialize_players_impl", autospec=True, side_effect=RuntimeError("init boom")),
            pytest.raises(RuntimeError, match="init boom"),
        ):
            await asyncio.wait_for(game.run(), timeout=15.0)

        assert events.get_events_of_type(GameEvent.GAME_ERROR) == [{"game_id": "uc022_f1", "error": "init boom"}]
        types = [t for t, _ in events.events]
        assert GameEvent.GAME_STARTED not in types
        assert game.state == GameState.ENDED


class TestTraitorBR005ForceEndNoWinner:
    """UC-022 BR-005 (#2003): only a latched result is announced; a force-end before it declares none."""

    @pytest.fixture
    def fast_rainbow(self):
        config = GamePerformanceConfig(winner_rainbow_duration_ms=200)
        with patch.object(get_config_manager(), "get_config", return_value=config):
            yield config

    @staticmethod
    async def _running_game(events_hook=None):
        cm = MockControllerManagerService(num_controllers=4)
        events = EventCollector()

        async def publish(event_type, data):
            await events.publish(event_type, data)
            if events_hook:
                events_hook(event_type)

        game = TraitorGame(
            controller_manager_client=cm,
            event_publisher=publish,
            audio_client=None,
            game_id="uc022_br5",
        )
        game.random_teams = False
        await game._initialize_players_impl(cm.controllers)
        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = game_clock.now()
        game.state = GameState.RUNNING
        game.running = True
        game.sounds = []

        async def record_sound(sound, *_args, **_kwargs):
            game.sounds.append(sound)

        game._play_sound = record_sound
        return game, events

    @staticmethod
    def _eliminate_secret_team(game, team: int) -> list[str]:
        """Mark secret team ``team`` dead (no win check); returns the surviving serials."""
        for player in game.players.values():
            if player.secret_team == team:
                player.alive = False
        return sorted(s for s, p in game.players.items() if p.alive)

    @staticmethod
    def _rainbows(game) -> list[str]:
        return sorted(
            m.game_effect.serial
            for m in game.gameplay_stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        )

    def _assert_no_winner(self, game, events, outcome: str, reason: str | None):
        assert self._rainbows(game) == []
        assert game.sounds == []
        assert events.get_events_of_type("traitor_winner") == []
        ended = events.get_events_of_type("game_ended")
        assert len(ended) == 1
        assert ended[0]["winning_team"] == "none"
        assert ended[0]["outcome"] == outcome
        if reason is None:
            assert "reason" not in ended[0]
        else:
            assert ended[0]["reason"] == reason

    def _assert_winner(self, game, events, team: int, winners: list[str]):
        assert self._rainbows(game) == winners
        assert len(game.sounds) == 1
        assert events.count_events_of_type("traitor_winner") == 1
        ended = events.get_events_of_type("game_ended")
        assert len(ended) == 1
        assert ended[0]["winning_team"] == team
        assert ended[0]["outcome"] == "completed"
        assert "reason" not in ended[0]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    async def test_force_end_while_running_declares_no_winner(self, fast_rainbow):
        """A force-end before any result: no traitor_winner, rainbow, voice or wait; game_ended none + reason."""
        game, events = await self._running_game()
        self._eliminate_secret_team(game, 1)  # one secret team left, but no win check ran yet
        game.force_end("operator")

        assert not await game._check_win_condition()  # a force-ended game latches nothing
        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    async def test_force_end_after_the_decision_keeps_the_winner(self, fast_rainbow):
        """A force-end during the hold after a win keeps the winner, its effects and outcome completed."""
        game, events = await self._running_game()
        winners = self._eliminate_secret_team(game, 1)

        assert await game._check_win_condition()
        game.force_end("operator")
        assert await game._check_win_condition()  # latched: not published again
        await game._end_game_impl()

        self._assert_winner(game, events, 0, winners)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    async def test_same_tick_force_end_during_the_win_publish_keeps_the_winner(self, fast_rainbow):
        """Same tick, decision first: a force-end landing while traitor_winner is published keeps the winner."""
        holder: dict = {}

        def force_end_on_win(event_type):
            if event_type == "traitor_winner":
                holder["game"].force_end("operator")

        game, events = await self._running_game(force_end_on_win)
        holder["game"] = game
        winners = self._eliminate_secret_team(game, 1)

        assert await game._check_win_condition()
        assert game.game_outcome() == "completed"
        await game._end_game_impl()

        self._assert_winner(game, events, 0, winners)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    async def test_same_tick_force_end_before_the_win_check_declares_no_winner(self, fast_rainbow):
        """Same tick, force-end first: the lethal kill's win check then latches and publishes nothing."""
        game, events = await self._running_game()
        losers = [s for s, p in game.players.items() if p.secret_team == 1]
        for serial in losers[:-1]:
            game.players[serial].alive = False
        game.force_end("operator")
        game.players[losers[-1]].alive = False  # the same tick's last lethal sample

        assert not await game._check_win_condition()
        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    @pytest.mark.uc("UC-027", flow="main", steps=[3])
    async def test_force_end_between_the_force_end_check_and_the_latch_declares_no_winner(self, fast_rainbow):
        """A cross-thread force-end lands after the win check's force_ended check, before the latch."""
        game, events = await self._running_game()
        self._eliminate_secret_team(game, 1)
        latch = game._latch_game_outcome

        def force_end_then_latch():
            game.force_end("operator")
            latch()

        game._latch_game_outcome = force_end_then_latch

        assert not await game._check_win_condition()  # outcome force_ended: no result latched
        assert game._latched_team is None
        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    async def test_force_ended_outcome_overrides_a_latched_team(self, fast_rainbow):
        """Defence in depth: a force_ended outcome declares no winner even when a team is latched."""
        game, events = await self._running_game()
        game._win_latched = True
        game._latched_team = 0
        game.force_end("operator")
        game._latch_game_outcome()

        await game._end_game_impl()

        self._assert_no_winner(game, events, "force_ended", "operator")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-022", br=["BR-005"])
    async def test_stream_end_before_a_decision_declares_no_winner(self, fast_rainbow):
        """A gameplay stream that ends before any result: winning_team none, outcome completed, no effects."""
        game, events = await self._running_game()
        self._eliminate_secret_team(game, 1)  # one secret team left, but the stream ended before the win check
        game.running = False

        await game._end_game_impl()

        self._assert_no_winner(game, events, "completed", None)
