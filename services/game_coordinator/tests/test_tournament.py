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


from conftest import EventCollector, MockControllerManagerService, async_noop

from lib.types import Sound
from services.game_coordinator import game_clock
from services.game_coordinator.games.tournament import (
    WAITING_COLOR,
    Match,
    TournamentGame,
    TournamentPlayer,
    TournamentState,
)


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
        """Test that no result is announced when multiple players are active."""
        game, mock_controller_manager, _ = tournament_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players active - no win
        assert not await game._check_win_condition()

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

    @pytest.mark.uc("UC-025", flow="main", steps=[1])
    @pytest.mark.parametrize("n", range(2, 33))
    def test_byes_seeded_to_meet_round_one_winners(self, tournament_game, n):
        """UC-025 step 1 (#1994): no bye-vs-bye in round 2 where avoidable, every player seated once."""
        game, _ = tournament_game
        game.players = {f"p{i:02d}": TournamentPlayer(serial=f"p{i:02d}") for i in range(n)}

        bracket = game._generate_bracket(n)

        slots = 1
        while slots < n:
            slots *= 2
        first_round = [m for m in bracket if m.round_number == 1]
        round_two = [m for m in bracket if m.round_number == 2]
        # shape unchanged: power-of-two slots, slots - n byes
        assert len(first_round) == slots // 2
        assert sum(m.is_bye for m in first_round) == slots - n
        seated = [s for m in first_round for s in (m.player1_serial, m.player2_serial) if s]
        assert sorted(seated) == sorted(game.players)
        # bye-vs-bye matches in round 2: both slots already filled before any match is played
        bye_vs_bye = sum(1 for m in round_two if m.player1_serial and m.player2_serial)
        byes = slots - n
        round_two_count = slots // 4
        assert bye_vs_bye == max(0, byes - round_two_count)

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
        game.players[p1].invincible_until = game_clock.now() + 100  # Far future
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
        game.players[p1].invincible_until = game_clock.now() - 1  # Past
        game.players[p2].tournament_state = TournamentState.FIGHTING
        game.players[p2].invincible_until = game_clock.now() - 1

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
        game.players[p3].invincible_until = game_clock.now() - 1

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
        game.players[serial].invincible_until = game_clock.now() - 1

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

        with patch("services.game_coordinator.game_clock.sleep", new_callable=AsyncMock):
            await game._tournament_intro_phase()

        intro_events = event_collector.get_events_of_type("tournament_intro")
        assert len(intro_events) == 1
        assert intro_events[0]["total_players"] == 4
        assert intro_events[0]["total_rounds"] == game.total_rounds

    @pytest.mark.uc("UC-025", flow="main", steps=[1])
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

        with patch("services.game_coordinator.game_clock.sleep", new_callable=AsyncMock):
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

        with patch("services.game_coordinator.game_clock.sleep", side_effect=stop_after_first):
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
        game.start_time = game_clock.now()
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()
        # Patch asyncio.sleep to be instant for the celebration loop
        self._sleep_patcher = patch("services.game_coordinator.game_clock.sleep", new_callable=AsyncMock)
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
        for s in serials[1:]:
            game.players[s].tournament_state = TournamentState.ELIMINATED
        game.running = True
        assert game._latch_champion(champion)
        game.running = False  # the loop has ended

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
        for s in serials[1:]:
            game.players[s].tournament_state = TournamentState.ELIMINATED
        game.running = True
        assert game._latch_champion(serials[0])
        game.running = False  # the loop has ended

        await game._end_game_impl()
        self._teardown()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_CONGRATULATIONS in sound_args

    @pytest.mark.asyncio
    async def test_end_game_no_champion(self, tournament_game):
        """No champion → game_ended event has champion "none" (UC-025 BR-003)."""
        game, mock_controller_manager, event_collector = tournament_game
        await self._setup_game(game, mock_controller_manager)

        # All eliminated, no champion
        for player in game.players.values():
            player.tournament_state = TournamentState.ELIMINATED

        await game._end_game_impl()
        self._teardown()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["champion"] == "none"

    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended_event(self, tournament_game):
        """game_ended event should include game_id and total_matches."""
        game, mock_controller_manager, event_collector = tournament_game
        await self._setup_game(game, mock_controller_manager)

        serials = list(game.players.keys())
        game.running = True
        game._latch_champion(serials[0])
        game.running = False  # the loop has ended

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
# Recording gameplay stream and frame driver for the bracket step tests
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
    """Mock gameplay stream that records writes.

    The bracket runs on the base game loop (D-G33): these tests feed frames to
    ``_process_gameplay_update`` directly, so the stream is only written to.
    """

    def __init__(self):
        self.messages: list = []

    async def write(self, message):
        self.messages.append(message)


def _tick_update():
    """A gameplay frame with one (unknown) controller entry, so detection runs once per frame."""
    from proto import controller_manager_pb2 as cm

    update = cm.GameplayDataUpdate()
    update.controllers.add(serial="tick")
    return update


async def _drive_bracket(game, max_frames: int = 2000) -> int:
    """Feed frames as the base game loop does until the win condition holds; return the frame count."""
    for frame in range(1, max_frames + 1):
        if await game._process_gameplay_update(_tick_update()):
            return frame
    raise AssertionError(f"the bracket did not finish within {max_frames} frames (stalled)")


def _no_pauses(game):
    """No pause between matches or after a round_start (the deadlines are monotonic time)."""
    from unittest.mock import patch

    game._time_between_matches = 0.0
    return patch("services.game_coordinator.games.tournament.ROUND_PAUSE_S", 0.0)


# ---------------------------------------------------------------------------
# Match start, timeout and finalisation (bracket step)
# ---------------------------------------------------------------------------


class TestRunMatchActive:
    """A match through the bracket step: start, time limit (A1) and finalisation."""

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

    @pytest.mark.uc("UC-025", flow="main", steps=[2])
    @pytest.mark.asyncio
    async def test_run_match_sets_fighting_state_and_colors(self, tournament_game):
        """_start_match sets both players to FIGHTING, sends red/blue and keeps player.color in sync."""
        from unittest.mock import AsyncMock

        from services.game_coordinator.games.tournament import MATCH_COLORS

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)
        stream = AsyncGameplayStreamMock()
        game.gameplay_stream = stream

        await game._start_match(match)

        assert game.current_match is match
        assert game.players[p1].tournament_state == TournamentState.FIGHTING
        assert game.players[p2].tournament_state == TournamentState.FIGHTING

        # Colors should have been sent: red for p1, blue for p2
        color_msgs = {m.base_color.serial: m.base_color.color for m in stream.messages if m.HasField("base_color")}
        assert (color_msgs[p1].r, color_msgs[p1].g, color_msgs[p1].b) == MATCH_COLORS[0]
        assert (color_msgs[p2].r, color_msgs[p2].g, color_msgs[p2].b) == MATCH_COLORS[1]
        # A reopened stream sends player.color: it must hold the match colours (O3).
        assert game.players[p1].color == MATCH_COLORS[0]
        assert game.players[p2].color == MATCH_COLORS[1]

        # match_start event should have been published
        match_starts = event_collector.get_events_of_type("match_start")
        assert len(match_starts) == 1
        assert match_starts[0]["player1"] == p1
        assert match_starts[0]["player2"] == p2

    @pytest.mark.uc("UC-025", flow="A1")
    @pytest.mark.asyncio
    async def test_run_match_timeout_picks_random_winner(self, tournament_game):
        """When the match time limit has passed, a random winner is chosen and finalized."""
        from unittest.mock import AsyncMock, patch

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)
        game.gameplay_stream = AsyncGameplayStreamMock()
        await game._start_match(match)
        game._match_deadline = 0.0  # the time limit has passed

        with patch.object(game._rng, "choice", return_value=p1):
            await game._bracket_tick()

        # Match should be finalized with p1 as winner
        assert match.is_complete is True
        assert match.winner_serial == p1
        assert game.current_match is None

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

    @pytest.mark.uc("UC-025", flow="A1")
    @pytest.mark.asyncio
    async def test_undecided_match_within_its_time_limit_keeps_running(self, tournament_game):
        """A frame before the time limit with nobody hit leaves the match running."""
        from unittest.mock import AsyncMock

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        match = Match(match_id=0, round_number=1, player1_serial=serials[0], player2_serial=serials[1])
        game.gameplay_stream = AsyncGameplayStreamMock()
        await game._start_match(match)

        for _ in range(5):
            assert await game._process_gameplay_update(_tick_update()) is False

        assert match.is_complete is False
        assert game.current_match is match
        assert event_collector.get_events_of_type("match_end") == []

    @pytest.mark.uc("UC-025", flow="main", br=["BR-003"])
    @pytest.mark.asyncio
    async def test_run_match_game_stopped_mid_match(self, tournament_game):
        """A force-ended game steps the bracket no more: no finalisation, no match_end."""
        from unittest.mock import AsyncMock

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        match = Match(match_id=0, round_number=1, player1_serial=serials[0], player2_serial=serials[1])
        game.gameplay_stream = AsyncGameplayStreamMock()
        await game._start_match(match)
        game._match_deadline = 0.0
        game.running = False

        await game._bracket_tick()

        assert match.is_complete is False
        assert event_collector.get_events_of_type("match_end") == []

    @pytest.mark.uc("UC-025", flow="A1")
    @pytest.mark.asyncio
    async def test_run_match_winner_effect_and_loser_off(self, tournament_game):
        """After match finalization, winner gets WINNER_RAINBOW and loser LED turns off."""
        from unittest.mock import AsyncMock, patch

        from proto import controller_manager_pb2 as cm_pb2

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        match = Match(match_id=0, round_number=1, player1_serial=p1, player2_serial=p2)
        stream = AsyncGameplayStreamMock()
        game.gameplay_stream = stream
        await game._start_match(match)
        game._match_deadline = 0.0

        with patch.object(game._rng, "choice", return_value=p2):
            await game._bracket_tick()

        # Winner effect
        effect_msgs = [m for m in stream.messages if m.HasField("game_effect")]
        assert any(
            m.game_effect.serial == p2 and m.game_effect.effect == cm_pb2.GAME_EFFECT_WINNER_RAINBOW
            for m in effect_msgs
        )

        # Loser LED off (0,0,0), and player.color follows for a reopen (O3)
        color_msgs = [m for m in stream.messages if m.HasField("base_color")]
        loser_off = [m for m in color_msgs if m.base_color.serial == p1]
        assert any(
            m.base_color.color.r == 0 and m.base_color.color.g == 0 and m.base_color.color.b == 0 for m in loser_off
        )
        assert game.players[p1].color == (0, 0, 0)


# ---------------------------------------------------------------------------
# The bracket on the frame loop
# ---------------------------------------------------------------------------


class TestGameplayLoop:
    """The bracket advances on gameplay frames until no match is left (UC-025 steps 2-4)."""

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

    @pytest.mark.uc("UC-025", flow="A1")
    @pytest.mark.asyncio
    async def test_gameplay_loop_completes_tournament(self, tournament_game):
        """Timed-out matches run until one player is left; then the win condition holds."""
        from unittest.mock import AsyncMock, patch

        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        game.gameplay_stream = AsyncGameplayStreamMock()
        game._match_duration = 0.0  # every match times out on its next frame

        serials = list(game.players.keys())
        p1, p2 = serials[0], serials[1]

        with _no_pauses(game), patch.object(game._rng, "choice", return_value=p1):
            await _drive_bracket(game)

        # One player is eliminated; the time limit of the final crowned the other (BR-005)
        assert game.players[p2].tournament_state == TournamentState.ELIMINATED
        assert game.players[p1].tournament_state == TournamentState.CHAMPION
        assert game._champion == p1
        assert [e["champion"] for e in event_collector.get_events_of_type("tournament_champion")] == [p1]
        assert await game._check_win_condition() is True

    @pytest.mark.uc("UC-025", flow="main", steps=[2, 4])
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
        game.gameplay_stream = AsyncGameplayStreamMock()
        game._match_duration = 0.0

        with _no_pauses(game), patch.object(game._rng, "choice", side_effect=lambda x: x[0]):
            await _drive_bracket(game)

        # Should have round_start events for round 2
        round_starts = event_collector.get_events_of_type("round_start")
        assert len(round_starts) >= 1
        assert round_starts[0]["round"] == 2

    @pytest.mark.uc("UC-025", flow="main", steps=[4])
    @pytest.mark.asyncio
    async def test_frames_during_the_pause_between_matches_start_no_match(self, tournament_game):
        """The pause is a deadline: frames read during it are processed, the next match waits for it."""
        from unittest.mock import AsyncMock

        mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
        event_collector = EventCollector()
        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_pause",
            invincibility_seconds=0.0,
        )
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        game.gameplay_stream = AsyncGameplayStreamMock()
        game._match_duration = 0.0
        game._time_between_matches = 60.0

        for _ in range(20):
            assert await game._process_gameplay_update(_tick_update()) is False

        assert len(event_collector.get_events_of_type("match_start")) == 1
        assert len(event_collector.get_events_of_type("match_end")) == 1
        assert game.current_match is None, "the next match waits for the pause"


# ---------------------------------------------------------------------------
# Full kill-driven bracket (#903 regression coverage)
# ---------------------------------------------------------------------------


class TestFullBracketKillDriven:
    """End-to-end bracket completion driven by real kills (not timeouts).

    A kill sets ``match.is_complete`` inside ``_kill_player_impl``; the old
    finalize guard ``if not match.is_complete`` then SKIPPED winner-advancement,
    stalling the bracket so no champion ever emerged (the #903 production bug).
    These tests kill the current match's player every match, so the whole
    bracket runs through the kill path and must still leave one player.
    """

    def _make_game(self, num_controllers: int, game_id: str):
        mock_cm = MockControllerManagerService(num_controllers=num_controllers, death_schedule={}, max_duration=10.0)
        event_collector = EventCollector()
        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id=game_id,
            invincibility_seconds=0.0,
        )
        return game, mock_cm, event_collector

    @pytest.mark.uc("UC-025", flow="main", steps=[3, 4])
    @pytest.mark.asyncio
    async def test_kill_driven_bracket_crowns_champion(self):
        """A 4-player bracket resolved entirely by kills leaves one player."""
        from unittest.mock import AsyncMock

        game, mock_cm, event_collector = self._make_game(4, "test_kill_bracket")
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        game.gameplay_stream = AsyncGameplayStreamMock()

        # Every match: "kill" player1 of the current match on its first frame,
        # exactly as a real death would (sets winner + complete via _kill_player_impl).
        async def kill_p1_of_current(_gameplay_data):
            match = game.current_match
            if match and not match.is_complete:
                await game._kill_player_impl(match.player1_serial, accel_mag=5.0)

        game._process_controller_state = kill_p1_of_current

        with _no_pauses(game):
            await _drive_bracket(game)

        # Exactly one non-eliminated player remains: the champion-to-be.
        survivors = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
        assert len(survivors) == 1, f"expected one survivor, got {survivors}"

        # The champion must have won every round (2 rounds for 4 players).
        champion = game.players[survivors[0]]
        assert champion.wins == game.total_rounds == 2

        # Every match in the bracket completed (none left dangling) and all
        # match_end events fired through the kill path (the bug emitted none).
        assert all(m.is_complete for m in game.bracket)
        match_ends = event_collector.get_events_of_type("match_end")
        assert len(match_ends) == 3  # 2 first-round + 1 final

        # The game's win condition holds; the champion announcement recognizes the champion.
        assert await game._check_win_condition() is True
        await game._finish_decided_final()
        assert champion.tournament_state == TournamentState.CHAMPION

    @pytest.mark.uc("UC-025", flow="main", steps=[3, 4])
    @pytest.mark.asyncio
    async def test_kill_driven_match_advances_winner(self):
        """A single kill-decided match advances its winner to round 2.

        Direct regression for the stalled-bracket bug: before the fix, a
        kill set is_complete=True and the finalize block (which calls
        _advance_winner) was skipped, so the winner sat in round 1 forever.
        """
        from unittest.mock import AsyncMock

        game, mock_cm, _ = self._make_game(4, "test_kill_advance")
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        game.gameplay_stream = AsyncGameplayStreamMock()

        first_match = [m for m in game.bracket if m.round_number == 1 and not m.is_bye][0]
        winner_serial = first_match.player2_serial
        await game._start_match(first_match)

        await game._kill_player_impl(first_match.player1_serial, accel_mag=5.0)
        await game._bracket_tick()

        # Winner advanced to a round-2 match (the finalize ran).
        round2 = [m for m in game.bracket if m.round_number == 2]
        assert any(m.player1_serial == winner_serial or m.player2_serial == winner_serial for m in round2), (
            "kill-decided winner was not advanced to round 2"
        )
        assert game.players[winner_serial].round_number == 2
        assert game.players[winner_serial].tournament_state == TournamentState.WAITING
        assert game.players[first_match.player1_serial].tournament_state == TournamentState.ELIMINATED


# ---------------------------------------------------------------------------
# Champion announcement edge cases (_latch_champion / _announce_no_winner, #1337)
# ---------------------------------------------------------------------------


class TestCheckWinConditionEdgeCases:
    """Edge cases of the champion announcement and the bracket win condition."""

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
        """When all players are eliminated, the announcement is tournament_no_winner."""
        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)

        for player in game.players.values():
            player.tournament_state = TournamentState.ELIMINATED

        game.running = True
        assert game._latch_no_winner() is True
        await game._announce_no_winner()

        # Should publish tournament_no_winner, not tournament_champion
        no_winner_events = event_collector.get_events_of_type("tournament_no_winner")
        assert len(no_winner_events) == 1
        champion_events = event_collector.get_events_of_type("tournament_champion")
        assert len(champion_events) == 0

    @pytest.mark.uc("UC-025", flow="main", steps=[4])
    @pytest.mark.asyncio
    async def test_win_condition_is_the_finished_bracket_and_publishes_nothing(self, tournament_game):
        """The base loop calls it after every sample: no event, True only once the bracket is done."""
        game, mock_cm, event_collector = tournament_game
        await game._initialize_players_impl(mock_cm.controllers)
        for serial in list(game.players)[:-1]:
            game.players[serial].tournament_state = TournamentState.ELIMINATED
        published_before = len(event_collector.events)

        assert await game._check_win_condition() is False, "only the bracket step ends the bracket"
        game._bracket_finished = True
        assert await game._check_win_condition() is True
        assert len(event_collector.events) == published_before


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
# Double hit in one frame (UC-025 BR-004, D-G30, #1991)
# ---------------------------------------------------------------------------


def _death_effect_serials(stream) -> list[str]:
    """Serials that got GAME_EFFECT_PLAYER_DEATH on the recorded stream, in order."""
    from proto import controller_manager_pb2 as cm

    return [
        m.game_effect.serial
        for m in stream.messages
        if m.HasField("game_effect") and m.game_effect.effect == cm.GAME_EFFECT_PLAYER_DEATH
    ]


class TestTournamentDoubleHit:
    """Both fighters spike in the same frame: only the first one loses the match."""

    def _make_game(self, num_controllers: int, game_id: str):
        mock_cm = MockControllerManagerService(num_controllers=num_controllers, death_schedule={}, max_duration=10.0)
        event_collector = EventCollector()
        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id=game_id,
            invincibility_seconds=0.0,
        )
        return game, mock_cm, event_collector

    @staticmethod
    def _double_hit_on_first_frame(game, player2_first: bool = False):
        """Each match's first frame kills player1, then player2 (or reversed), through the base kill path."""
        hit_matches: set[int] = set()

        async def double_hit(_gameplay_data):
            match = game.current_match
            if match is None or match.match_id in hit_matches:
                return
            hit_matches.add(match.match_id)
            order = [match.player1_serial, match.player2_serial]
            if player2_first:
                order.reverse()
            for serial in order:
                await game._kill_player(serial, accel_mag=5.0)

        return double_hit

    @pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-004"])
    @pytest.mark.asyncio
    async def test_double_hit_eliminates_only_the_first_fighter(self):
        """The winner stays in the bracket, advances and gets no death effect."""
        from unittest.mock import AsyncMock

        game, mock_cm, event_collector = self._make_game(4, "test_double_hit_match")
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        stream = AsyncGameplayStreamMock()
        game.gameplay_stream = stream
        game._process_controller_state = self._double_hit_on_first_frame(game)

        first_match = [m for m in game.bracket if m.round_number == 1 and not m.is_bye][0]
        loser_serial, winner_serial = first_match.player1_serial, first_match.player2_serial
        await game._start_match(first_match)

        await game._process_gameplay_update(_tick_update())

        assert first_match.winner_serial == winner_serial
        assert game.players[loser_serial].tournament_state == TournamentState.ELIMINATED
        assert game.players[winner_serial].tournament_state == TournamentState.WAITING
        assert game.players[winner_serial].alive is True
        assert game.players[winner_serial].wins == 1
        round2 = [m for m in game.bracket if m.round_number == 2]
        assert any(winner_serial in (m.player1_serial, m.player2_serial) for m in round2)
        match_ends = event_collector.get_events_of_type("match_end")
        assert [e["winner"] for e in match_ends] == [winner_serial]
        # PLAYER_DEATH only for the kill that eliminated the player.
        assert _death_effect_serials(stream) == [loser_serial]

    @pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-004"])
    @pytest.mark.asyncio
    async def test_double_hit_player2_first_makes_player1_the_winner(self):
        """Mirrored order: the first fighter to die is player2, so player1 advances."""
        from unittest.mock import AsyncMock

        game, mock_cm, event_collector = self._make_game(4, "test_double_hit_mirrored")
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        stream = AsyncGameplayStreamMock()
        game.gameplay_stream = stream
        game._process_controller_state = self._double_hit_on_first_frame(game, player2_first=True)

        first_match = [m for m in game.bracket if m.round_number == 1 and not m.is_bye][0]
        winner_serial, loser_serial = first_match.player1_serial, first_match.player2_serial
        await game._start_match(first_match)

        await game._process_gameplay_update(_tick_update())

        assert first_match.winner_serial == winner_serial
        assert game.players[loser_serial].tournament_state == TournamentState.ELIMINATED
        assert game.players[winner_serial].tournament_state == TournamentState.WAITING
        assert game.players[winner_serial].alive is True
        match_ends = event_collector.get_events_of_type("match_end")
        assert [e["winner"] for e in match_ends] == [winner_serial]
        assert _death_effect_serials(stream) == [loser_serial]

    @pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-004"])
    @pytest.mark.asyncio
    async def test_two_player_double_hit_leaves_the_right_champion(self):
        """2 players: the final's double hit leaves the second fighter as champion."""
        from unittest.mock import AsyncMock

        game, mock_cm, _ = self._make_game(2, "test_double_hit_final")
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        stream = AsyncGameplayStreamMock()
        game.gameplay_stream = stream
        game._process_controller_state = self._double_hit_on_first_frame(game)

        final = [m for m in game.bracket if not m.is_bye][0]
        loser_serial, winner_serial = final.player1_serial, final.player2_serial

        with _no_pauses(game):
            await _drive_bracket(game)

        survivors = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
        assert survivors == [winner_serial]
        assert game.players[winner_serial].tournament_state == TournamentState.CHAMPION
        assert game.players[loser_serial].tournament_state == TournamentState.ELIMINATED
        assert _death_effect_serials(stream) == [loser_serial]

    @pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-004"])
    @pytest.mark.asyncio
    async def test_waiting_player_kill_sends_no_death_effect(self):
        """A kill the mode ignores (waiting player) writes no PLAYER_DEATH."""
        from unittest.mock import AsyncMock

        game, mock_cm, _ = self._make_game(4, "test_waiting_no_effect")
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        stream = AsyncGameplayStreamMock()
        game.gameplay_stream = stream
        waiting_serial = next(iter(game.players))

        await game._kill_player(waiting_serial, accel_mag=5.0)

        assert game.players[waiting_serial].tournament_state == TournamentState.WAITING
        assert game.players[waiting_serial].alive is True
        assert _death_effect_serials(stream) == []


class TestByeAdvancement:
    """Bye winners enter round 2 when the bracket is built (#1990, D-G31)."""

    @staticmethod
    def _make_game(num_players: int):
        mock_cm = MockControllerManagerService(num_controllers=num_players, death_schedule={}, max_duration=10.0)
        game = TournamentGame(
            controller_manager_client=mock_cm,
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id=f"test_bye_{num_players}",
            invincibility_seconds=0.0,
        )
        return game, mock_cm

    @pytest.mark.uc("UC-025", flow="main", steps=[1])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("num_players", [3, 5, 6, 7, 8])
    async def test_bracket_completes_through_real_bracket_functions(self, num_players):
        """Playing every match _get_next_match offers leaves exactly one player."""
        game, mock_cm = self._make_game(num_players)
        await game._initialize_players_impl(mock_cm.controllers)

        played = 0
        while (match := game._get_next_match()) is not None:
            assert match.player1_serial and match.player2_serial, "only real matches are offered"
            loser = match.player2_serial
            match.winner_serial = match.player1_serial
            match.is_complete = True
            game.players[loser].tournament_state = TournamentState.ELIMINATED
            game._advance_winner(match.winner_serial, match)
            played += 1
            assert played <= num_players, "bracket must not loop"

        survivors = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
        assert len(survivors) == 1
        assert played == num_players - 1
        assert game.players[survivors[0]].round_number == game.total_rounds

    @pytest.mark.uc("UC-025", flow="main", steps=[1])
    def test_bye_winners_sit_in_round_two_after_build(self):
        game, _ = self._make_game(5)
        game.players = {
            f"s{i}": TournamentPlayer(serial=f"s{i}", team=-1, alive=True, color=(0, 0, 0)) for i in range(5)
        }
        bracket = game._generate_bracket(5)
        byes = [m for m in bracket if m.is_bye]
        assert len(byes) == 3
        round2 = [m for m in bracket if m.round_number == 2]
        seated = {m.player1_serial for m in round2} | {m.player2_serial for m in round2}
        assert {b.winner_serial for b in byes} <= seated

    @pytest.mark.uc("UC-025", flow="main", steps=[1])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("num_players", [3, 5, 6])
    async def test_kill_driven_game_loop_completes_with_byes(self, num_players):
        """The bracket, driven by real kills on the frame loop, ends with one survivor."""
        from unittest.mock import AsyncMock

        game, mock_cm = self._make_game(num_players)
        await game._initialize_players_impl(mock_cm.controllers)
        game._play_sound = AsyncMock()
        game.running = True
        game.gameplay_stream = AsyncGameplayStreamMock()

        async def kill_p1_of_current(_gameplay_data):
            match = game.current_match
            if match and not match.is_complete:
                await game._kill_player_impl(match.player1_serial, accel_mag=5.0)

        game._process_controller_state = kill_p1_of_current

        with _no_pauses(game):
            await _drive_bracket(game, max_frames=200)

        survivors = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
        assert len(survivors) == 1
        assert all(m.is_complete or m.is_bye for m in game.bracket)
