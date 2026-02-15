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
