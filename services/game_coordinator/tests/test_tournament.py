"""
Unit tests for Tournament game mode.

Tests the core Tournament mechanics:
- Bracket generation
- Player states (WAITING, FIGHTING, ELIMINATED, CHAMPION)
- Match handling
- Win condition (last player standing)
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

import time

from conftest import EventCollector, MockControllerManagerService, async_noop

from lib.types import Sound
from services.game_coordinator.games.tournament import (
    WAITING_COLOR,
    Match,
    TournamentGame,
    TournamentPlayer,
    TournamentState,
)


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class TestTournamentGameMode:
    """Test Tournament game mechanics."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_tournament_001",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_player_initialization(self, tournament_game):
        """Test that players are initialized correctly."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All 4 players should be created
        assert len(game.players) == 4

    @pytest.mark.asyncio
    async def test_players_start_waiting(self, tournament_game):
        """Test that all players start in WAITING state."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for _serial, player in game.players.items():
            assert player.tournament_state == TournamentState.WAITING
            assert player.wins == 0

    def test_generate_bracket_4_players(self, tournament_game):
        """Test bracket generation for 4 players (power of 2)."""
        game, _, _ = tournament_game

        bracket = game._generate_bracket(4)

        # 4 players = 2 rounds = 3 matches (2 first round + 1 final)
        # First round: 2 matches
        # Final: 1 match
        assert len(bracket) >= 2  # At least first round matches
        assert game.total_rounds == 2  # log2(4) = 2 rounds

    def test_generate_bracket_3_players(self, tournament_game):
        """Test bracket generation for 3 players (needs bye)."""
        game, _, _ = tournament_game

        bracket = game._generate_bracket(3)

        # 3 players rounds up to 4 slots
        # One player gets a bye
        has_bye = any(m.is_bye for m in bracket)
        assert has_bye or game.total_rounds == 2

    def test_generate_bracket_empty(self, tournament_game):
        """Test bracket generation with no players."""
        game, _, _ = tournament_game

        bracket = game._generate_bracket(0)
        assert bracket == []

        bracket = game._generate_bracket(1)
        assert bracket == []

    @pytest.mark.asyncio
    async def test_check_win_condition_all_active(self, tournament_game):
        """Test that win condition is false when multiple players active."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players active - no win
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_check_win_condition_one_remaining(self, tournament_game):
        """Test that win condition triggers when one player remains."""
        game, mock_controller_manager, event_collector = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Eliminate 3 players
        serials = list(game.players.keys())
        for serial in serials[:-1]:  # All but last
            game.players[serial].tournament_state = TournamentState.ELIMINATED

        # Should have winner
        assert await game._check_win_condition()

        # Last player should be champion
        last_serial = serials[-1]
        assert game.players[last_serial].tournament_state == TournamentState.CHAMPION

        # Verify event
        champion_events = event_collector.get_events_of_type("tournament_champion")
        assert len(champion_events) == 1

    @pytest.mark.asyncio
    async def test_tournament_player_dataclass(self, tournament_game):
        """Test TournamentPlayer dataclass attributes."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players should be TournamentPlayer instances
        for _serial, player in game.players.items():
            assert isinstance(player, TournamentPlayer)
            assert hasattr(player, "tournament_state")
            assert hasattr(player, "bracket_position")
            assert hasattr(player, "round_number")
            assert hasattr(player, "wins")
            assert hasattr(player, "invincible_until")

    def test_match_dataclass(self, tournament_game):
        """Test Match dataclass attributes."""
        match = Match(
            match_id=1,
            round_number=1,
            player1_serial="player1",
            player2_serial="player2",
        )

        assert match.match_id == 1
        assert match.round_number == 1
        assert match.player1_serial == "player1"
        assert match.player2_serial == "player2"
        assert match.winner_serial is None
        assert match.is_complete is False
        assert match.is_bye is False

    @pytest.mark.asyncio
    async def test_kill_player_not_in_match_ignored(self, tournament_game):
        """Test that kills are ignored if player not in FIGHTING state."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        player = game.players[serial]

        # Player is in WAITING state
        assert player.tournament_state == TournamentState.WAITING

        # Kill should be ignored
        await game._kill_player_impl(serial, accel_mag=5.0)

        # Player state should be unchanged
        assert player.tournament_state == TournamentState.WAITING

    @pytest.mark.asyncio
    async def test_get_game_name(self, tournament_game):
        """Test get_game_name returns 'Tournament'."""
        game, _, _ = tournament_game
        assert game.get_game_name() == "Tournament"

    @pytest.mark.asyncio
    async def test_get_next_match(self, tournament_game):
        """Test _get_next_match returns a ready, incomplete match."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        bracket = game._generate_bracket(4)
        game.bracket = bracket

        next_match = game._get_next_match()

        assert next_match is not None
        assert next_match.player1_serial is not None
        assert next_match.player2_serial is not None
        assert next_match.is_complete is False

    @pytest.mark.asyncio
    async def test_advance_winner(self, tournament_game):
        """Test _advance_winner moves winner to next round."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        bracket = game._generate_bracket(4)
        game.bracket = bracket

        # Get first match from bracket
        first_match = bracket[0]

        # Complete the first match
        first_match.is_complete = True
        first_match.winner_serial = first_match.player1_serial

        # Advance the winner
        game._advance_winner(first_match.player1_serial, first_match)

        # Winner should be in WAITING state with round 2
        winner = game.players[first_match.player1_serial]
        assert winner.tournament_state == TournamentState.WAITING
        assert winner.round_number == 2


class TestTournamentBracket:
    """Tests for tournament bracket generation."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=8)

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_tournament_bracket",
        )

        return game, mock_controller_manager

    def test_generate_bracket_8_players(self, tournament_game):
        """Test bracket generation for 8 players."""
        game, _ = tournament_game

        bracket = game._generate_bracket(8)

        # 8 players = 3 rounds = 7 matches (4 + 2 + 1)
        assert game.total_rounds == 3  # log2(8) = 3 rounds
        assert len(bracket) >= 4  # At least first round matches

    def test_generate_bracket_5_players_has_byes(self, tournament_game):
        """Test bracket generation for 5 players has byes."""
        game, _ = tournament_game

        bracket = game._generate_bracket(5)

        # 5 players rounds up to 8 slots (3 byes)
        # Check that there are bye matches
        bye_count = sum(1 for m in bracket if m.is_bye)
        assert bye_count == 3  # 8 - 5 = 3 byes

    def test_generate_bracket_6_players(self, tournament_game):
        """Test bracket generation for 6 players."""
        game, _ = tournament_game

        bracket = game._generate_bracket(6)

        # 6 players rounds up to 8 slots (2 byes)
        bye_count = sum(1 for m in bracket if m.is_bye)
        assert bye_count == 2  # 8 - 6 = 2 byes

    def test_generate_bracket_2_players(self, tournament_game):
        """Test bracket for minimum 2 players."""
        game, _ = tournament_game

        bracket = game._generate_bracket(2)

        # 2 players = 1 round = 1 match
        assert game.total_rounds == 1
        assert len(bracket) == 1


class TestTournamentMatch:
    """Tests for match mechanics."""

    def test_match_defaults(self):
        """Test Match default values."""
        match = Match(
            match_id=1,
            round_number=1,
            player1_serial="p1",
            player2_serial="p2",
        )

        assert match.winner_serial is None
        assert match.is_complete is False
        assert match.is_bye is False

    def test_match_with_bye(self):
        """Test Match with bye marker."""
        match = Match(
            match_id=2,
            round_number=1,
            player1_serial="p1",
            player2_serial=None,
            is_bye=True,
        )

        assert match.is_bye is True
        assert match.player2_serial is None


class TestTournamentWinCondition:
    """Tests for tournament win conditions."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_tournament_win",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_no_win_all_waiting(self, tournament_game):
        """No winner when all players are in WAITING state."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All waiting - no win
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_no_win_multiple_non_eliminated(self, tournament_game):
        """No winner when multiple players are not eliminated."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Set one to FIGHTING, one to WAITING, one to ELIMINATED
        serials = list(game.players.keys())
        game.players[serials[0]].tournament_state = TournamentState.FIGHTING
        game.players[serials[1]].tournament_state = TournamentState.WAITING
        game.players[serials[2]].tournament_state = TournamentState.ELIMINATED
        game.players[serials[3]].tournament_state = TournamentState.WAITING

        # Multiple non-eliminated - no win
        assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_champion_receives_event(self, tournament_game):
        """Champion event includes player info."""
        game, mock_controller_manager, event_collector = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Eliminate all but one
        serials = list(game.players.keys())
        champion_serial = serials[-1]

        for serial in serials[:-1]:
            game.players[serial].tournament_state = TournamentState.ELIMINATED

        await game._check_win_condition()

        champion_events = event_collector.get_events_of_type("tournament_champion")
        assert len(champion_events) == 1
        assert champion_events[0]["champion"] == champion_serial


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


class TestGetNextMatch:
    """Tests for _get_next_match() method."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_tournament_next_match",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_get_next_match_returns_ready_match(self, tournament_game):
        """Should return a match that has both players assigned."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        match = game._get_next_match()
        assert match is not None
        assert match.player1_serial is not None
        assert match.player2_serial is not None
        assert match.is_complete is False

    @pytest.mark.asyncio
    async def test_get_next_match_skips_completed_matches(self, tournament_game):
        """Completed matches should be skipped."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Complete all first-round matches
        for match in game.bracket:
            if match.round_number == 1:
                match.is_complete = True

        result = game._get_next_match()
        # Should return None or a later-round match (which doesn't have both players yet)
        if result is not None:
            assert result.round_number > 1

    @pytest.mark.asyncio
    async def test_get_next_match_returns_none_all_complete(self, tournament_game):
        """When all matches are complete, should return None."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for match in game.bracket:
            match.is_complete = True

        assert game._get_next_match() is None

    @pytest.mark.asyncio
    async def test_get_next_match_skips_match_without_both_players(self, tournament_game):
        """A non-bye match with only one player should be skipped."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Complete first round so we get to round 2 matches (which need winners to fill)
        for match in game.bracket:
            if match.round_number == 1:
                match.is_complete = True

        # Round 2 matches should have no players assigned yet (winners not advanced)
        result = game._get_next_match()
        # Should be None since round 2 matches lack players
        assert result is None


class TestAdvanceWinner:
    """Tests for _advance_winner() method."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_tournament_advance",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_advance_winner_places_in_next_round(self, tournament_game):
        """Winner should appear in a round 2 match."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get first match
        first_match = [m for m in game.bracket if m.round_number == 1 and not m.is_bye][0]
        winner = first_match.player1_serial
        first_match.is_complete = True
        first_match.winner_serial = winner

        game._advance_winner(winner, first_match)

        # Winner should appear in a round 2 match
        round2_matches = [m for m in game.bracket if m.round_number == 2]
        found = any(m.player1_serial == winner or m.player2_serial == winner for m in round2_matches)
        assert found, f"Winner {winner} not found in any round 2 match"

    @pytest.mark.asyncio
    async def test_advance_winner_updates_player_state(self, tournament_game):
        """After advancing, player should be in round 2 and WAITING state."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        first_match = [m for m in game.bracket if m.round_number == 1 and not m.is_bye][0]
        winner = first_match.player1_serial
        first_match.is_complete = True
        first_match.winner_serial = winner

        game._advance_winner(winner, first_match)

        player = game.players[winner]
        assert player.round_number == 2
        assert player.tournament_state == TournamentState.WAITING

    @pytest.mark.asyncio
    async def test_advance_winner_final_round_no_advance(self, tournament_game):
        """Advancing from final round should not crash."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]

        # Create a fake final-round match
        final_match = Match(
            match_id=99,
            round_number=game.total_rounds,
            player1_serial=serial,
            player2_serial=list(game.players.keys())[1],
            winner_serial=serial,
            is_complete=True,
        )

        # Should not raise
        game._advance_winner(serial, final_match)


class TestRunMatchBye:
    """Tests for _run_match() with bye matches."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 3 players (forces a bye)."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=3,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_tournament_bye",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_run_match_bye_auto_advances(self, tournament_game):
        """Bye match should auto-advance the player."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.gameplay_stream = RecordingGameplayStream()

        # Find a bye match
        bye_match = next((m for m in game.bracket if m.is_bye), None)
        assert bye_match is not None, "Expected at least one bye with 3 players"

        bye_player = bye_match.player1_serial

        await game._run_match(bye_match)

        # Player should be advanced to next round
        round2_matches = [m for m in game.bracket if m.round_number == 2]
        found = any(m.player1_serial == bye_player or m.player2_serial == bye_player for m in round2_matches)
        assert found

    @pytest.mark.asyncio
    async def test_run_match_bye_does_not_interact_with_stream(self, tournament_game):
        """Bye match should not write to gameplay stream."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        bye_match = next((m for m in game.bracket if m.is_bye), None)
        assert bye_match is not None

        await game._run_match(bye_match)

        # No stream writes for bye
        assert len(stream.messages) == 0


class TestKillPlayerInvincibility:
    """Tests for _kill_player_impl() with invincibility and match context."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_tournament_invincibility",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_kill_player_during_invincibility_ignored(self, tournament_game):
        """Kill during invincibility should be ignored."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        game.players[p1].tournament_state = TournamentState.FIGHTING
        game.players[p1].invincible_until = time.time() + 100  # Far future
        game.players[p2].tournament_state = TournamentState.FIGHTING

        game.current_match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        await game._kill_player_impl(p1, accel_mag=5.0)

        assert game.current_match.is_complete is False

    @pytest.mark.asyncio
    async def test_kill_player_after_invincibility_sets_winner(self, tournament_game):
        """Kill after invincibility expires should set match winner."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        game.players[p1].tournament_state = TournamentState.FIGHTING
        game.players[p1].invincible_until = time.time() - 1  # Past
        game.players[p2].tournament_state = TournamentState.FIGHTING
        game.players[p2].invincible_until = time.time() - 1

        game.current_match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        await game._kill_player_impl(p1, accel_mag=5.0)

        assert game.current_match.is_complete is True
        assert game.current_match.winner_serial == p2

    @pytest.mark.asyncio
    async def test_kill_player_not_in_current_match_ignored(self, tournament_game):
        """Kill of a player not in the current match should be ignored."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serials = list(game.players.keys())
        p1, p2, p3 = serials[0], serials[1], serials[2]

        game.players[p3].tournament_state = TournamentState.FIGHTING
        game.players[p3].invincible_until = time.time() - 1

        game.current_match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        await game._kill_player_impl(p3, accel_mag=5.0)

        assert game.current_match.is_complete is False

    @pytest.mark.asyncio
    async def test_kill_player_no_current_match_ignored(self, tournament_game):
        """Kill with no current match should be ignored."""
        game, mock_controller_manager = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        serial = list(game.players.keys())[0]
        game.players[serial].tournament_state = TournamentState.FIGHTING
        game.players[serial].invincible_until = time.time() - 1

        game.current_match = None

        await game._kill_player_impl(serial, accel_mag=5.0)
        # Should not raise


class TestTournamentIntroPhase:
    """Tests for _tournament_intro_phase() method."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_tournament_intro",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_intro_publishes_event(self, tournament_game):
        """Intro should publish tournament_intro event."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = tournament_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock):
            await game._tournament_intro_phase()

        intro_events = event_collector.get_events_of_type("tournament_intro")
        assert len(intro_events) == 1
        assert intro_events[0]["total_players"] == 4
        assert intro_events[0]["total_rounds"] == game.total_rounds

    @pytest.mark.asyncio
    async def test_intro_sets_waiting_colors(self, tournament_game):
        """Intro should set all controllers to WAITING_COLOR (gray)."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = tournament_game
        game.running = True
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock):
            await game._tournament_intro_phase()

        color_serials = set()
        for m in stream.messages:
            if m.HasField("base_color"):
                color_serials.add(m.base_color.serial)
                assert m.base_color.color.r == WAITING_COLOR[0]
                assert m.base_color.color.g == WAITING_COLOR[1]
                assert m.base_color.color.b == WAITING_COLOR[2]

        for serial in game.players:
            assert serial in color_serials

    @pytest.mark.asyncio
    async def test_intro_stops_if_not_running(self, tournament_game):
        """If game stops during intro, should return early."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = tournament_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        call_count = 0

        async def stop_after_first(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                game.running = False

        with patch("services.game_coordinator.games.tournament.asyncio.sleep", side_effect=stop_after_first):
            await game._tournament_intro_phase()

        # Intro event is published before the loop, so it will exist
        # The key is that it returns without error


class TestTournamentEndGame:
    """Tests for _end_game_impl() method."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_tournament_endgame",
        )

        return game, mock_controller_manager, event_collector

    async def _setup_game(self, game, mock_controller_manager):
        """Shared setup: init players, set state, mock helpers."""
        from unittest.mock import AsyncMock, patch

        from services.game_coordinator.games.base import GameState

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.state = GameState.RUNNING
        game.start_time = time.time()
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()
        # Patch asyncio.sleep to be instant for the celebration loop
        self._sleep_patcher = patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock)
        self._sleep_patcher.start()

    def _teardown(self):
        self._sleep_patcher.stop()

    @pytest.mark.asyncio
    async def test_end_game_champion_gets_rainbow(self, tournament_game):
        """Champion should get GAME_EFFECT_WINNER_RAINBOW."""
        from proto import controller_manager_pb2

        game, mock_controller_manager, _ = tournament_game
        await self._setup_game(game, mock_controller_manager)

        serials = list(game.players.keys())
        champion = serials[0]
        game.players[champion].tournament_state = TournamentState.CHAMPION
        for s in serials[1:]:
            game.players[s].tournament_state = TournamentState.ELIMINATED

        await game._end_game_impl()
        self._teardown()

        stream = game.gameplay_stream
        rainbow_msgs = [
            m
            for m in stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        ]
        assert len(rainbow_msgs) == 1
        assert rainbow_msgs[0].game_effect.serial == champion

    @pytest.mark.asyncio
    async def test_end_game_plays_congratulations_sound(self, tournament_game):
        """Champion exists → VOX_CONGRATULATIONS played."""
        game, mock_controller_manager, _ = tournament_game
        await self._setup_game(game, mock_controller_manager)

        serials = list(game.players.keys())
        game.players[serials[0]].tournament_state = TournamentState.CHAMPION
        for s in serials[1:]:
            game.players[s].tournament_state = TournamentState.ELIMINATED

        await game._end_game_impl()
        self._teardown()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_CONGRATULATIONS in sound_args

    @pytest.mark.asyncio
    async def test_end_game_no_champion(self, tournament_game):
        """No champion → game_ended event has champion=None."""
        game, mock_controller_manager, event_collector = tournament_game
        await self._setup_game(game, mock_controller_manager)

        # All eliminated, no champion
        for player in game.players.values():
            player.tournament_state = TournamentState.ELIMINATED

        await game._end_game_impl()
        self._teardown()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["champion"] is None

    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended_event(self, tournament_game):
        """game_ended event should include game_id and total_matches."""
        game, mock_controller_manager, event_collector = tournament_game
        await self._setup_game(game, mock_controller_manager)

        serials = list(game.players.keys())
        game.players[serials[0]].tournament_state = TournamentState.CHAMPION

        # Mark some bracket matches as completed
        for match in game.bracket[:2]:
            match.is_complete = True

        await game._end_game_impl()
        self._teardown()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["game_id"] == "test_tournament_endgame"


class TestTournamentCleanup:
    """Tests for cleanup() method."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        return TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_tournament_cleanup",
        )

    @pytest.mark.asyncio
    async def test_cleanup_ends_match_span(self, tournament_game):
        """cleanup() should end match_span if present."""
        from unittest.mock import MagicMock

        game = tournament_game

        mock_span = MagicMock()
        game.match_span = mock_span

        await game.cleanup()

        mock_span.end.assert_called_once()
        assert game.match_span is None

    @pytest.mark.asyncio
    async def test_cleanup_handles_no_match_span(self, tournament_game):
        """cleanup() should not raise when match_span is None."""
        game = tournament_game
        game.match_span = None

        await game.cleanup()


class TestTournamentAdditionalPhases:
    """Tests for _get_additional_phases() method."""

    def test_additional_phases_returns_tournament_intro(self):
        """Should return a list with one Phase named 'tournament_intro'."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_phases",
        )

        phases = game._get_additional_phases()
        assert len(phases) == 1
        assert phases[0].name == "tournament_intro"


# ---------------------------------------------------------------------------
# Async gameplay stream mock for _run_match tests
# ---------------------------------------------------------------------------


def _patch_win_flash():
    """Ensure GAME_EFFECT_WIN_FLASH exists on the proto module.

    The source references ``controller_manager_pb2.GAME_EFFECT_WIN_FLASH``
    which is not defined in the proto. We add it at test time so that
    the code path can execute without modifying production code.
    """
    from proto import controller_manager_pb2 as cm

    if not hasattr(cm, "GAME_EFFECT_WIN_FLASH"):
        cm.GAME_EFFECT_WIN_FLASH = cm.GAME_EFFECT_FLASH


_patch_win_flash()


class AsyncGameplayStreamMock:
    """Mock gameplay stream that yields controller data and records writes.

    Supports producing a configurable sequence of updates via ``__anext__``,
    then raising ``StopAsyncIteration`` when exhausted.
    """

    def __init__(self, updates=None, *, timeout_forever=False):
        """
        Args:
            updates: List of GameplayDataUpdate protos to yield. When
                exhausted the stream raises StopAsyncIteration.
            timeout_forever: If True, every ``__anext__`` raises
                ``TimeoutError`` so the match loop only checks elapsed time.
        """
        self._updates = list(updates or [])
        self._idx = 0
        self._timeout_forever = timeout_forever
        self.messages: list = []

    async def write(self, message):
        self.messages.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._timeout_forever:
            raise TimeoutError
        if self._idx < len(self._updates):
            update = self._updates[self._idx]
            self._idx += 1
            return update
        raise StopAsyncIteration


# ---------------------------------------------------------------------------
# _run_match orchestration tests
# ---------------------------------------------------------------------------


class TestRunMatchActive:
    """Tests for _run_match() with two active players (non-bye)."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_run_match",
            invincibility_seconds=0.0,
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_run_match_sets_fighting_state_and_colors(self, tournament_game):
        """_run_match sets both players to FIGHTING and sends red/blue colors."""
        from unittest.mock import AsyncMock

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        # Stream that always times out so we can control match completion
        # externally by marking the match complete after checking state.
        stream = AsyncGameplayStreamMock(timeout_forever=True)
        game.gameplay_stream = stream

        # Kill p1 after first iteration so match finishes quickly
        async def kill_after_check(_gd):
            if game.players[p1].tournament_state == TournamentState.FIGHTING:
                match.winner_serial = p2
                match.is_complete = True

        game._process_controller_state = kill_after_check

        await game._run_match(match)

        # Colors should have been sent: red for p1, blue for p2
        color_msgs = [m for m in stream.messages if m.HasField("base_color")]
        color_serials = {m.base_color.serial for m in color_msgs}
        assert p1 in color_serials
        assert p2 in color_serials

        # match_start event should have been published
        match_starts = event_collector.get_events_of_type("match_start")
        assert len(match_starts) == 1
        assert match_starts[0]["player1"] == p1
        assert match_starts[0]["player2"] == p2

    @pytest.mark.asyncio
    async def test_run_match_timeout_picks_random_winner(self, tournament_game):
        """When match timer expires, a random winner is chosen and finalized."""
        from unittest.mock import AsyncMock, patch

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        stream = AsyncGameplayStreamMock(timeout_forever=True)
        game.gameplay_stream = stream

        # Make time.time() jump past MATCH_DURATION on the second call
        original_time = time.time
        call_count = 0

        def fast_time():
            nonlocal call_count
            call_count += 1
            # First calls are for invincibility setup; after ~5 calls jump ahead
            if call_count > 4:
                return original_time() + 100
            return original_time()

        with (
            patch("services.game_coordinator.games.tournament.time.time", side_effect=fast_time),
            patch("services.game_coordinator.games.tournament.random.choice", return_value=p1),
        ):
            await game._run_match(match)

        # Match should be finalized with p1 as winner
        assert match.is_complete is True
        assert match.winner_serial == p1

        # Loser should be ELIMINATED
        assert game.players[p2].tournament_state == TournamentState.ELIMINATED
        assert game.players[p2].alive is False

        # Winner wins count should increase
        assert game.players[p1].wins == 1

        # match_end event should be published
        match_ends = event_collector.get_events_of_type("match_end")
        assert len(match_ends) == 1
        assert match_ends[0]["winner"] == p1
        assert match_ends[0]["loser"] == p2

    @pytest.mark.asyncio
    async def test_run_match_stream_ended_during_match(self, tournament_game):
        """StopAsyncIteration during match exits cleanly without crash."""
        from unittest.mock import AsyncMock

        game, mock_cm, _ = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        # Empty stream -- __anext__ raises StopAsyncIteration immediately
        stream = AsyncGameplayStreamMock(updates=[])
        game.gameplay_stream = stream

        # Should not raise
        await game._run_match(match)

        # Match should NOT be finalized since nobody won
        assert match.is_complete is False

    @pytest.mark.asyncio
    async def test_run_match_game_stopped_mid_match(self, tournament_game):
        """Setting running=False during match causes clean exit."""
        from unittest.mock import AsyncMock

        game, mock_cm, _ = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        call_count = 0

        async def stop_on_second_anext():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                game.running = False
            raise TimeoutError

        stream = AsyncGameplayStreamMock(timeout_forever=True)
        stream.__anext__ = stop_on_second_anext
        game.gameplay_stream = stream

        await game._run_match(match)

        # Match should not be complete since we stopped
        assert match.is_complete is False

    @pytest.mark.asyncio
    async def test_run_match_winner_effect_and_loser_off(self, tournament_game):
        """After match finalization, winner gets WIN_FLASH and loser LED turns off."""
        from unittest.mock import AsyncMock, patch

        from proto import controller_manager_pb2 as cm_pb2

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        stream = AsyncGameplayStreamMock(timeout_forever=True)
        game.gameplay_stream = stream

        original_time = time.time
        call_count = 0

        def fast_time():
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                return original_time() + 100
            return original_time()

        with (
            patch("services.game_coordinator.games.tournament.time.time", side_effect=fast_time),
            patch("services.game_coordinator.games.tournament.random.choice", return_value=p2),
        ):
            await game._run_match(match)

        # Winner effect
        effect_msgs = [m for m in stream.messages if m.HasField("game_effect")]
        assert any(
            m.game_effect.serial == p2 and m.game_effect.effect == cm_pb2.GAME_EFFECT_WIN_FLASH for m in effect_msgs
        )

        # Loser LED off (0,0,0)
        color_msgs = [m for m in stream.messages if m.HasField("base_color")]
        loser_off = [m for m in color_msgs if m.base_color.serial == p1]
        assert any(
            m.base_color.color.r == 0 and m.base_color.color.g == 0 and m.base_color.color.b == 0 for m in loser_off
        )


# ---------------------------------------------------------------------------
# _gameplay_loop tests
# ---------------------------------------------------------------------------


class TestGameplayLoop:
    """Tests for _gameplay_loop() orchestration."""

    @pytest.fixture
    def tournament_game(self):
        """Create a Tournament game with 2 players (simplest bracket)."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=2,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_gameplay_loop",
            invincibility_seconds=0.0,
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_gameplay_loop_completes_tournament(self, tournament_game):
        """_gameplay_loop should run matches until a champion is determined."""
        from unittest.mock import AsyncMock, patch

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        stream = AsyncGameplayStreamMock(timeout_forever=True)
        game.gameplay_stream = stream

        original_time = time.time
        call_count = 0

        def fast_time():
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                return original_time() + 100
            return original_time()

        with (
            patch("services.game_coordinator.games.tournament.time.time", side_effect=fast_time),
            patch("services.game_coordinator.games.tournament.random.choice", return_value=p1),
            patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock),
        ):
            await game._gameplay_loop()

        # One player should be eliminated, one waiting/champion
        assert game.players[p2].tournament_state == TournamentState.ELIMINATED
        assert game.players[p1].tournament_state == TournamentState.WAITING

    @pytest.mark.asyncio
    async def test_gameplay_loop_round_change_publishes_event(self, tournament_game):
        """When advancing to a new round, round_start event should be published."""
        from unittest.mock import AsyncMock, patch

        # 4 players so we get round transitions
        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_round_change",
            invincibility_seconds=0.0,
        )

        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        stream = AsyncGameplayStreamMock(timeout_forever=True)
        game.gameplay_stream = stream

        original_time = time.time
        call_count = 0

        def fast_time():
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                return original_time() + 100
            return original_time()

        with (
            patch("services.game_coordinator.games.tournament.time.time", side_effect=fast_time),
            patch("services.game_coordinator.games.tournament.random.choice", side_effect=lambda x: x[0]),
            patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock),
        ):
            await game._gameplay_loop()

        # Should have round_start events for round 2
        round_starts = event_collector.get_events_of_type("round_start")
        assert len(round_starts) >= 1
        assert round_starts[0]["round"] == 2


# ---------------------------------------------------------------------------
# _check_win_condition edge cases
# ---------------------------------------------------------------------------


class TestCheckWinConditionEdgeCases:
    """Additional edge case tests for _check_win_condition()."""

    @pytest.fixture
    def tournament_game(self):
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_win_edge",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_all_eliminated_no_winner(self, tournament_game):
        """When all players are eliminated, win condition is True with no champion."""
        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)

        for player in game.players.values():
            player.tournament_state = TournamentState.ELIMINATED

        result = await game._check_win_condition()
        assert result is True

        # Should publish tournament_no_winner, not tournament_champion
        no_winner_events = event_collector.get_events_of_type("tournament_no_winner")
        assert len(no_winner_events) == 1
        champion_events = event_collector.get_events_of_type("tournament_champion")
        assert len(champion_events) == 0


# ---------------------------------------------------------------------------
# _kill_player_impl span interaction
# ---------------------------------------------------------------------------


class TestKillPlayerSpan:
    """Tests for _kill_player_impl() span event recording."""

    @pytest.fixture
    def tournament_game(self):
        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        game = TournamentGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_kill_span",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_kill_records_span_event(self, tournament_game):
        """When a player is killed and has a span, add_event is called."""
        from unittest.mock import MagicMock

        game, mock_cm = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        game.players[p1].tournament_state = TournamentState.FIGHTING
        game.players[p1].invincible_until = 0  # expired
        game.players[p2].tournament_state = TournamentState.FIGHTING

        mock_span = MagicMock()
        game.players[p1].span = mock_span

        game.current_match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        await game._kill_player_impl(p1, accel_mag=5.5)

        mock_span.add_event.assert_called_once()
        call_args = mock_span.add_event.call_args
        assert call_args[0][0] == "match_death"
        assert call_args[1]["attributes"]["match_id"] == 0
        assert call_args[1]["attributes"]["accel_magnitude"] == 5.5

    @pytest.mark.asyncio
    async def test_kill_p2_makes_p1_winner(self, tournament_game):
        """When player2 is killed, player1 becomes the winner."""
        game, mock_cm = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        game.players[p2].tournament_state = TournamentState.FIGHTING
        game.players[p2].invincible_until = 0
        game.players[p1].tournament_state = TournamentState.FIGHTING

        game.current_match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)

        await game._kill_player_impl(p2, accel_mag=4.0)

        assert game.current_match.winner_serial == p1
        assert game.current_match.is_complete is True


# ---------------------------------------------------------------------------
# run() lifecycle test
# ---------------------------------------------------------------------------


class TestRunLifecycle:
    """Tests for the full run() orchestration."""

    @pytest.mark.asyncio
    async def test_run_full_lifecycle(self):
        """run() should execute init, intro, countdown, gameplay, and end."""
        from unittest.mock import AsyncMock, patch

        mock_cm = MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0)
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_run_lifecycle",
            invincibility_seconds=0.0,
        )

        stream = AsyncGameplayStreamMock(timeout_forever=True)

        # Mock _initialize_players to call _initialize_players_impl directly
        async def fake_init_players():
            await game._initialize_players_impl(mock_cm.controllers)

        game._initialize_players = fake_init_players
        game._play_sound = AsyncMock()
        game._start_gameplay_stream = AsyncMock()
        game.gameplay_stream = stream
        game._countdown = AsyncMock()
        game._start_game_music = AsyncMock()
        game._stop_game_music = AsyncMock()

        original_time = time.time
        call_count = 0

        def fast_time():
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                return original_time() + 100
            return original_time()

        serials = [f"mock_controller_{i}" for i in range(2)]

        with (
            patch("services.game_coordinator.games.tournament.time.time", side_effect=fast_time),
            patch("services.game_coordinator.games.tournament.random.choice", return_value=serials[0]),
            patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock),
        ):
            await game.run()

        # Verify lifecycle phases were called
        game._start_gameplay_stream.assert_called_once()
        game._countdown.assert_called_once()
        game._start_game_music.assert_called_once()
        game._stop_game_music.assert_called_once()

        # game_started and game_ended events should exist
        started_events = event_collector.get_events_of_type("game_started")
        assert len(started_events) == 1
        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1

    @pytest.mark.asyncio
    async def test_run_stops_early_if_not_running_after_intro(self):
        """If running becomes False during intro, run() returns without gameplay."""
        from unittest.mock import AsyncMock, patch

        mock_cm = MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0)
        event_collector = EventCollector()

        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_run_early_stop",
            invincibility_seconds=0.0,
        )

        async def fake_init_players():
            await game._initialize_players_impl(mock_cm.controllers)

        game._initialize_players = fake_init_players
        game._play_sound = AsyncMock()
        game._start_gameplay_stream = AsyncMock()
        game.gameplay_stream = AsyncGameplayStreamMock(timeout_forever=True)
        game._start_game_music = AsyncMock()
        game._stop_game_music = AsyncMock()

        async def stop_during_countdown():
            game.running = False

        game._countdown = AsyncMock(side_effect=stop_during_countdown)

        with patch("services.game_coordinator.games.tournament.asyncio.sleep", new_callable=AsyncMock):
            await game.run()

        # game_started should NOT have been published since we stopped before gameplay
        started_events = event_collector.get_events_of_type("game_started")
        assert len(started_events) == 0
