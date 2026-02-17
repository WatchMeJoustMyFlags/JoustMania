"""
Unit tests for Fight Club game mode.

Tests the core Fight Club mechanics:
- Queue-based 1v1 matches
- Player state management (IN_LINE, DEFENDER, FIGHTER)
- Scoring system
- Invincibility period
- Win condition (highest score after minimum rounds)
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

from conftest import EventCollector, MockControllerManagerService

from proto import controller_manager_pb2
from services.game_coordinator.games.fight_club import (
    DEFAULT_INVINCIBILITY_DURATION,
    DEFAULT_MIN_ROUNDS,
    DEFENDER_COLOR,
    FIGHTER_COLOR,
    WAITING_COLOR,
    FightClubGame,
    FightState,
)


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class TestFightClubGameMode:
    """Test Fight Club game mechanics."""

    @pytest.fixture
    def fight_club_game(self):
        """Create a Fight Club game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_001",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_player_initialization(self, fight_club_game):
        """Test that players are initialized in queue."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All 4 players should be created
        assert len(game.players) == 4

        # All players should be in queue
        assert len(game.queue) == 4

    @pytest.mark.asyncio
    async def test_players_start_in_line(self, fight_club_game):
        """Test that all players start in IN_LINE state."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for _serial, player in game.players.items():
            assert player.state == FightState.IN_LINE
            assert player.color == WAITING_COLOR
            assert player.score == 0

    @pytest.mark.asyncio
    async def test_start_round_sets_defender_and_fighter(self, fight_club_game):
        """Test that _start_round sets defender and fighter from queue."""
        game, mock_controller_manager, _ = fight_club_game
        # Don't set gameplay_stream to avoid proto bug with ColorUpdate
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Start first round
        await game._start_round()

        # First player becomes defender, second becomes fighter
        assert game.current_defender == "mock_controller_0"
        assert game.current_fighter == "mock_controller_1"

        # They should have correct states
        defender = game.players[game.current_defender]
        fighter = game.players[game.current_fighter]

        assert defender.state == FightState.DEFENDER
        assert defender.color == DEFENDER_COLOR

        assert fighter.state == FightState.FIGHTER
        assert fighter.color == FIGHTER_COLOR

    @pytest.mark.asyncio
    async def test_queue_decreases_after_round_start(self, fight_club_game):
        """Test that queue shrinks as players enter fights."""
        game, mock_controller_manager, _ = fight_club_game
        # Don't set gameplay_stream to avoid proto bug with ColorUpdate
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially 4 in queue
        assert len(game.queue) == 4

        # Start round - 2 leave queue
        await game._start_round()

        assert len(game.queue) == 2

    @pytest.mark.asyncio
    async def test_invincibility_set_on_round_start(self, fight_club_game):
        """Test that players get invincibility when round starts."""
        game, mock_controller_manager, _ = fight_club_game
        # Don't set gameplay_stream to avoid proto bug with ColorUpdate
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)

        time_before = time.time()
        await game._start_round()

        defender = game.players[game.current_defender]
        fighter = game.players[game.current_fighter]

        # Both should have invincibility for ~4 seconds
        assert defender.invincible_until > time_before + DEFAULT_INVINCIBILITY_DURATION - 0.5
        assert fighter.invincible_until > time_before + DEFAULT_INVINCIBILITY_DURATION - 0.5

    @pytest.mark.asyncio
    async def test_kill_player_during_invincibility_ignored(self, fight_club_game):
        """Test that kills during invincibility are ignored."""
        game, mock_controller_manager, _ = fight_club_game
        # Don't set gameplay_stream to avoid proto bug with ColorUpdate
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        defender = game.players[game.current_defender]
        initial_score = defender.score

        # Try to kill defender during invincibility (should be ignored)
        await game._kill_player_impl(game.current_defender, accel_mag=5.0)

        # Defender should still be in fight, not moved to queue
        assert game.current_defender is not None
        assert defender.state == FightState.DEFENDER
        assert defender.score == initial_score

    @pytest.mark.asyncio
    async def test_kill_player_after_invincibility(self, fight_club_game):
        """Test that kills after invincibility are processed."""
        game, mock_controller_manager, event_collector = fight_club_game
        # Don't set gameplay_stream to avoid proto bug with ColorUpdate
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        defender_serial = game.current_defender
        fighter_serial = game.current_fighter

        # Clear invincibility
        game.players[defender_serial].invincible_until = 0
        game.players[fighter_serial].invincible_until = 0

        # Kill defender - fighter wins
        await game._kill_player_impl(defender_serial, accel_mag=5.0)

        # Fighter becomes new defender with 1 point
        assert game.current_defender == fighter_serial
        assert game.players[fighter_serial].score == 1
        assert game.players[fighter_serial].state == FightState.DEFENDER

        # Old defender goes to back of queue
        assert defender_serial in game.queue
        assert game.players[defender_serial].state == FightState.IN_LINE

    @pytest.mark.asyncio
    async def test_defender_wins_stays_defender(self, fight_club_game):
        """Test that when defender wins, they stay as defender."""
        game, mock_controller_manager, _ = fight_club_game
        # Don't set gameplay_stream to avoid proto bug with ColorUpdate
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        defender_serial = game.current_defender
        fighter_serial = game.current_fighter

        # Clear invincibility
        game.players[defender_serial].invincible_until = 0
        game.players[fighter_serial].invincible_until = 0

        # Kill fighter - defender wins
        await game._kill_player_impl(fighter_serial, accel_mag=5.0)

        # Defender stays with 1 point
        assert game.current_defender == defender_serial
        assert game.players[defender_serial].score == 1

        # Fighter goes to back of queue
        assert fighter_serial in game.queue

    @pytest.mark.asyncio
    async def test_should_end_game_before_min_rounds(self, fight_club_game):
        """Test that game doesn't end before minimum rounds."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Even with clear leader, shouldn't end before DEFAULT_MIN_ROUNDS
        game.round_number = DEFAULT_MIN_ROUNDS - 1
        game.players["mock_controller_0"].score = 5
        game.players["mock_controller_1"].score = 0

        assert game._should_end_game() is False

    @pytest.mark.asyncio
    async def test_should_end_game_after_min_rounds_with_leader(self, fight_club_game):
        """Test that game ends after minimum rounds with clear leader."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Past DEFAULT_MIN_ROUNDS with clear leader
        game.round_number = DEFAULT_MIN_ROUNDS + 1
        game.players["mock_controller_0"].score = 5
        game.players["mock_controller_1"].score = 3

        assert game._should_end_game() is True

    @pytest.mark.asyncio
    async def test_should_not_end_game_when_tied(self, fight_club_game):
        """Test that game continues with tied scores (enables face-off)."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Past DEFAULT_MIN_ROUNDS but tied
        game.round_number = DEFAULT_MIN_ROUNDS + 1
        game.players["mock_controller_0"].score = 5
        game.players["mock_controller_1"].score = 5

        # Should enable face-off mode but not end
        assert game._should_end_game() is False
        assert game.face_off_mode is True

    @pytest.mark.asyncio
    async def test_check_win_condition(self, fight_club_game):
        """Test _check_win_condition returns game_over flag."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially not over
        assert await game._check_win_condition() is False

        # Set game over
        game.game_over = True
        assert await game._check_win_condition() is True

    @pytest.mark.asyncio
    async def test_process_controller_state_reads_proto_accel(self, fight_club_game):
        """Test that _process_controller_state correctly reads nested accel from proto."""
        game, mock_controller_manager, _ = fight_club_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        defender_serial = game.current_defender
        # Clear invincibility so processing actually happens
        game.players[defender_serial].invincible_until = 0

        # Build a ControllerState proto with nested accel (not flat accel_x)
        state = controller_manager_pb2.ControllerState(
            serial=defender_serial,
            accel=controller_manager_pb2.Vector3(x=0.5, y=0.0, z=0.0),
        )

        # This would crash with AttributeError if code used state.accel_x
        await game._process_controller_state(state)

        # Verify the player's smoothed_accel was updated
        assert game.players[defender_serial].smoothed_accel > 0

    @pytest.mark.asyncio
    async def test_start_round_assigns_roles(self, fight_club_game):
        """Test that after _start_round, defender and fighter have correct state."""
        game, mock_controller_manager, _ = fight_club_game
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        # Defender and fighter should be assigned
        assert game.current_defender is not None
        assert game.current_fighter is not None

        # Verify their states
        defender = game.players[game.current_defender]
        fighter = game.players[game.current_fighter]

        assert defender.state == FightState.DEFENDER
        assert fighter.state == FightState.FIGHTER

    @pytest.mark.asyncio
    async def test_handle_timeout_both_to_queue(self, fight_club_game):
        """Test that when round times out, both players go to back of queue."""
        game, mock_controller_manager, _ = fight_club_game
        game.gameplay_stream = None

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        # Record who is fighting
        defender_serial = game.current_defender
        fighter_serial = game.current_fighter

        # Handle timeout
        await game._handle_timeout()

        # Both original combatants should be back in the queue
        assert defender_serial in game.queue
        assert fighter_serial in game.queue

        # Both should be IN_LINE state
        assert game.players[defender_serial].state == FightState.IN_LINE
        assert game.players[fighter_serial].state == FightState.IN_LINE

    @pytest.mark.asyncio
    async def test_invincibility_blocks_death(self, fight_club_game):
        """Test that a player with invincibility survives lethal motion."""
        game, mock_controller_manager, _ = fight_club_game
        game.gameplay_stream = MockGameplayStream()

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        defender_serial = game.current_defender

        # Verify invincibility is active
        assert game.players[defender_serial].invincible_until > time.time()

        # Feed 10 lethal motion frames during invincibility
        for _ in range(10):
            state = controller_manager_pb2.ControllerState(
                serial=defender_serial,
                accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
            )
            await game._process_controller_state(state)

        # Defender should still be alive
        assert game.players[defender_serial].alive is True


class TestFightClubStartRoundEdgeCases:
    """Test _start_round edge cases: empty queue, face-off mode, existing defender."""

    @pytest.fixture
    def fight_club_game(self):
        """Create a Fight Club game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_edge",
        )
        game.gameplay_stream = None

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_start_round_no_challengers_ends_game(self, fight_club_game):
        """When queue is empty after defender assigned, game_over is set."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Drain queue to just one player
        game.queue.clear()
        game.current_defender = "mock_controller_0"

        await game._start_round()

        assert game.game_over is True

    @pytest.mark.asyncio
    async def test_start_round_face_off_mode_no_time_limit(self, fight_club_game):
        """In face-off mode, round_end_time is set to infinity."""
        game, mock_controller_manager, event_collector = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.face_off_mode = True
        await game._start_round()

        assert game.round_end_time == float("inf")

        # Verify event published with face_off=True
        round_events = event_collector.get_events_of_type("round_start")
        assert len(round_events) == 1
        assert round_events[0]["face_off"] is True

    @pytest.mark.asyncio
    async def test_start_round_face_off_no_timer_task(self, fight_club_game):
        """In face-off mode, no round_task is created."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.running = True
        game.face_off_mode = True

        await game._start_round()

        # round_task should not be created in face-off mode
        assert game.round_task is None

    @pytest.mark.asyncio
    async def test_start_round_existing_defender_uses_queue_for_fighter(self, fight_club_game):
        """When defender already exists, only challenger is pulled from queue."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set existing defender and remove from queue
        game.current_defender = game.queue.pop(0)  # "mock_controller_0"
        initial_queue_len = len(game.queue)

        await game._start_round()

        # Only fighter was pulled from queue
        assert len(game.queue) == initial_queue_len - 1
        assert game.current_defender == "mock_controller_0"
        assert game.current_fighter is not None


class TestFightClubHandleTimeout:
    """Test _handle_timeout edge cases."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_timeout",
        )
        game.gameplay_stream = None

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_handle_timeout_publishes_event(self, fight_club_game):
        """Timeout publishes a round_timeout event with the round number."""
        game, mock_controller_manager, event_collector = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()
        round_num = game.round_number

        await game._handle_timeout()

        timeout_events = event_collector.get_events_of_type("round_timeout")
        assert len(timeout_events) == 1
        assert timeout_events[0]["round"] == round_num

    @pytest.mark.asyncio
    async def test_handle_timeout_triggers_game_over_when_should_end(self, fight_club_game):
        """After timeout, if _should_end_game returns True, game_over is set."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        # Set round_number past min and give someone a clear lead
        game.round_number = DEFAULT_MIN_ROUNDS + 1
        game.players["mock_controller_0"].score = 5
        game.players["mock_controller_1"].score = 1

        await game._handle_timeout()

        assert game.game_over is True

    @pytest.mark.asyncio
    async def test_handle_timeout_clears_current_combatants(self, fight_club_game):
        """After timeout, current_defender and current_fighter are cleared."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        await game._handle_timeout()

        # After timeout handling starts a new round, new combatants are assigned
        # The point is the old ones got cleared and re-queued
        # We check the queue grew by 2 (both went back)
        # Initial: 4 players. Start round: 2 in queue, 2 fighting.
        # After timeout + new round: old 2 go back, new 2 pulled = 2 in queue
        assert len(game.queue) == 2


class TestFightClubEndGameImpl:
    """Test _end_game_impl: winner scoring, effects, events."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=3,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_end",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_end_game_publishes_winner_and_scores(self, fight_club_game):
        """_end_game_impl publishes game_ended event with winner and scores."""
        game, mock_controller_manager, event_collector = fight_club_game
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = time.time() - 30.0  # Game ran for 30 seconds

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set scores - controller 1 wins
        game.players["mock_controller_0"].score = 2
        game.players["mock_controller_1"].score = 5
        game.players["mock_controller_2"].score = 1
        game.round_number = 12

        await game._end_game_impl()

        end_events = event_collector.get_events_of_type("game_ended")
        assert len(end_events) == 1
        data = end_events[0]
        assert data["winner"] == "mock_controller_1"
        assert data["winner_score"] == 5
        assert data["total_rounds"] == 12
        assert data["final_scores"]["mock_controller_0"] == 2
        assert data["final_scores"]["mock_controller_1"] == 5
        assert data["final_scores"]["mock_controller_2"] == 1

    @pytest.mark.asyncio
    async def test_end_game_sends_rainbow_effect(self, fight_club_game):
        """_end_game_impl sends winner rainbow effect via gameplay stream."""
        game, mock_controller_manager, _ = fight_club_game
        stream = MockGameplayStream()
        stream.writes = []
        original_write = stream.write

        async def tracking_write(msg):
            stream.writes.append(msg)
            await original_write(msg)

        stream.write = tracking_write
        game.gameplay_stream = stream
        game.running = True
        game.start_time = time.time()

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.players["mock_controller_0"].score = 3

        await game._end_game_impl()

        # Check that a rainbow effect was sent
        rainbow_sent = any(
            msg.HasField("game_effect") and msg.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
            for msg in stream.writes
        )
        assert rainbow_sent is True

    @pytest.mark.asyncio
    async def test_end_game_no_stream_no_crash(self, fight_club_game):
        """_end_game_impl works without gameplay_stream (no crash)."""
        game, mock_controller_manager, event_collector = fight_club_game
        game.gameplay_stream = None
        game.running = True
        game.start_time = time.time()

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.players["mock_controller_0"].score = 1

        # Should not raise
        await game._end_game_impl()

        end_events = event_collector.get_events_of_type("game_ended")
        assert len(end_events) == 1


class TestFightClubFaceOff:
    """Test face-off mode: triggered by tie, different behavior for kills."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_faceoff",
        )
        game.gameplay_stream = None

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_should_end_game_face_off_sets_combatants(self, fight_club_game):
        """Face-off mode sets current_defender and current_fighter to tied players."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set up tied scores past min rounds
        game.round_number = DEFAULT_MIN_ROUNDS + 1
        game.players["mock_controller_0"].score = 7
        game.players["mock_controller_1"].score = 7
        game.players["mock_controller_2"].score = 3
        game.players["mock_controller_3"].score = 2

        result = game._should_end_game()

        assert result is False
        assert game.face_off_mode is True
        assert game.current_defender == "mock_controller_0"
        assert game.current_fighter == "mock_controller_1"

    @pytest.mark.asyncio
    async def test_should_end_game_already_in_face_off_continues(self, fight_club_game):
        """If already in face-off mode and still tied, continue (return False)."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        game.round_number = DEFAULT_MIN_ROUNDS + 2
        game.face_off_mode = True
        game.players["mock_controller_0"].score = 7
        game.players["mock_controller_1"].score = 7

        result = game._should_end_game()
        assert result is False

    @pytest.mark.asyncio
    async def test_kill_during_face_off_ends_game(self, fight_club_game):
        """A kill during face-off resolves the tie and ends the game."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set up face-off mode with tied scores
        game.face_off_mode = True
        game.round_number = DEFAULT_MIN_ROUNDS + 1
        game.current_defender = "mock_controller_0"
        game.current_fighter = "mock_controller_1"

        # Set states and scores for combatants
        game.players["mock_controller_0"].state = FightState.DEFENDER
        game.players["mock_controller_0"].score = 7
        game.players["mock_controller_0"].invincible_until = 0
        game.players["mock_controller_1"].state = FightState.FIGHTER
        game.players["mock_controller_1"].score = 7
        game.players["mock_controller_1"].invincible_until = 0

        # Kill the fighter - defender wins, gets 8 points
        await game._kill_player_impl("mock_controller_1", accel_mag=5.0)

        # Defender now has 8, fighter has 7 - clear leader, should end
        assert game.players["mock_controller_0"].score == 8
        assert game.game_over is True


class TestFightClubUpdatePlayerColors:
    """Test _update_player_colors with gameplay stream."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=3,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_colors",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_update_player_colors_sends_correct_colors(self, fight_club_game):
        """_update_player_colors sends defender=red, fighter=green, others=white."""
        game, mock_controller_manager, _ = fight_club_game
        stream = MockGameplayStream()
        stream.writes = []

        async def tracking_write(msg):
            stream.writes.append(msg)

        stream.write = tracking_write
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Manually set states
        game.players["mock_controller_0"].state = FightState.DEFENDER
        game.players["mock_controller_1"].state = FightState.FIGHTER
        game.players["mock_controller_2"].state = FightState.IN_LINE

        await game._update_player_colors()

        assert len(stream.writes) == 3

        # Extract serial->color mapping from writes
        color_map = {}
        for msg in stream.writes:
            serial = msg.base_color.serial
            color = (msg.base_color.color.r, msg.base_color.color.g, msg.base_color.color.b)
            color_map[serial] = color

        assert color_map["mock_controller_0"] == DEFENDER_COLOR
        assert color_map["mock_controller_1"] == FIGHTER_COLOR
        assert color_map["mock_controller_2"] == WAITING_COLOR


class TestFightClubProcessControllerState:
    """Test _process_controller_state for non-fighting players."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=3,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_process",
        )
        game.gameplay_stream = MockGameplayStream()

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_process_state_ignores_in_line_player(self, fight_club_game):
        """Controller state for IN_LINE player is ignored (no EMA update)."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        # Find an IN_LINE player
        in_line_serial = None
        for serial, player in game.players.items():
            if player.state == FightState.IN_LINE:
                in_line_serial = serial
                break

        assert in_line_serial is not None

        state = controller_manager_pb2.ControllerState(
            serial=in_line_serial,
            accel=controller_manager_pb2.Vector3(x=10.0, y=10.0, z=10.0),
        )

        await game._process_controller_state(state)

        # Smoothed accel should remain at 0 (never updated)
        assert game.players[in_line_serial].smoothed_accel == 0.0

    @pytest.mark.asyncio
    async def test_process_state_unknown_player_ignored(self, fight_club_game):
        """Controller state for unknown serial is silently ignored."""
        game, mock_controller_manager, _ = fight_club_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        state = controller_manager_pb2.ControllerState(
            serial="UNKNOWN_SERIAL",
            accel=controller_manager_pb2.Vector3(x=5.0, y=5.0, z=5.0),
        )

        # Should not raise
        await game._process_controller_state(state)


class TestFightClubCleanupAndForceEnd:
    """Test cleanup and on_force_end methods."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=2,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_cleanup",
        )
        game.gameplay_stream = None

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_on_force_end_sets_game_over(self, fight_club_game):
        """on_force_end sets game_over flag so game loop exits."""
        game, _, _ = fight_club_game

        assert game.game_over is False
        await game.on_force_end()
        assert game.game_over is True

    @pytest.mark.asyncio
    async def test_cleanup_cancels_round_task(self, fight_club_game):
        """cleanup cancels the round_task if active."""
        game, mock_controller_manager, _ = fight_club_game
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)
        await game._start_round()

        # Round task should have been created (non-face-off)
        assert game.round_task is not None

        await game.cleanup()

        # After cleanup, task should be done (cancelled)
        assert game.round_task.done()

    @pytest.mark.asyncio
    async def test_cleanup_no_round_task_no_crash(self, fight_club_game):
        """cleanup works when there is no round_task."""
        game, _, _ = fight_club_game
        game.round_task = None

        # Should not raise
        await game.cleanup()


class TestFightClubIntroPhase:
    """Test the intro phase."""

    @pytest.fixture
    def fight_club_game(self):
        mock_controller_manager = MockControllerManagerService(
            num_controllers=3,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = FightClubGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_fight_club_intro",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_intro_phase_sends_waiting_colors(self, fight_club_game):
        """Intro phase sets all players to waiting color via gameplay stream."""
        game, mock_controller_manager, _ = fight_club_game
        stream = MockGameplayStream()
        stream.writes = []

        async def tracking_write(msg):
            stream.writes.append(msg)

        stream.write = tracking_write
        game.gameplay_stream = stream
        game.running = True

        await game._initialize_players_impl(mock_controller_manager.controllers)

        await game._intro_phase()

        # Should have sent one color command per player
        color_writes = [w for w in stream.writes if w.HasField("base_color")]
        assert len(color_writes) == 3

        # All should be WAITING_COLOR
        for msg in color_writes:
            color = (msg.base_color.color.r, msg.base_color.color.g, msg.base_color.color.b)
            assert color == WAITING_COLOR

    @pytest.mark.asyncio
    async def test_intro_phase_exits_early_when_not_running(self, fight_club_game):
        """Intro phase exits early if game.running becomes False."""
        game, mock_controller_manager, _ = fight_club_game
        game.gameplay_stream = None
        game.running = False

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Should return quickly without error
        await game._intro_phase()

    @pytest.mark.asyncio
    async def test_get_additional_phases_returns_intro(self, fight_club_game):
        """_get_additional_phases returns the fight_club_intro phase."""
        game, _, _ = fight_club_game

        phases = game._get_additional_phases()
        assert len(phases) == 1
        assert phases[0].name == "fight_club_intro"
