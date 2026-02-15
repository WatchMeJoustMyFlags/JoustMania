"""
Unit tests for extracted helper methods in TeamsGameBase._end_game_impl.

Tests each helper independently to verify the refactoring preserved behavior:
- _get_winning_team_num: determine winner from alive teams
- _send_winner_rainbow_effects: send LED effects to winning team
- _play_team_victory_sound: play correct victory sound
- _build_survived_span_attrs: build span attributes for surviving players
- _end_surviving_player_spans: close spans for surviving players
- _publish_player_analytics: publish analytics and update metrics
- _end_surviving_team_spans: close spans for surviving teams
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from services.game_coordinator.games.base import Player
from services.game_coordinator.games.teams_base import (
    TEAM_WIN_SOUNDS,
    TeamsGameBase,
)


class MockTeamsGame(TeamsGameBase):
    """Minimal concrete subclass of TeamsGameBase for testing helpers."""

    def get_game_name(self):
        return "Mock Teams"

    async def _initialize_players_impl(self, controllers):
        for idx, controller in enumerate(controllers):
            team_num = idx % self.num_teams
            self.players[controller.serial] = Player(
                serial=controller.serial,
                team=team_num,
                alive=True,
                color=self.team_colors[team_num]["rgb"],
            )

    def _get_additional_phases(self):
        return []

    async def _kill_player_impl(self, serial, accel_mag):
        self.players[serial].alive = False

    def _create_player_spans(self, game_context):
        pass


@pytest.fixture
def mock_event_publisher():
    """Create mock async event publisher."""
    return AsyncMock()


@pytest.fixture
def game(mock_event_publisher):
    """Create a MockTeamsGame with 2 teams for testing."""
    return MockTeamsGame(
        controller_manager_client=AsyncMock(),
        event_publisher=mock_event_publisher,
        num_teams=2,
        game_id="test_helpers_001",
    )


def _add_players(game, specs):
    """
    Add players to the game from a list of (serial, team, alive) tuples.

    Args:
        game: MockTeamsGame instance
        specs: List of (serial, team, alive) tuples
    """
    for serial, team, alive in specs:
        game.players[serial] = Player(
            serial=serial,
            team=team,
            alive=alive,
            color=game.team_colors[team]["rgb"],
        )


class TestGetWinningTeamNum:
    """Test _get_winning_team_num helper."""

    def test_one_team_alive_returns_that_team(self, game):
        """Returns the single surviving team number."""
        assert game._get_winning_team_num({0}) == 0
        assert game._get_winning_team_num({1}) == 1

    def test_no_teams_alive_returns_none(self, game):
        """Returns None when no teams remain (tie)."""
        assert game._get_winning_team_num(set()) is None

    def test_multiple_teams_alive_returns_none(self, game):
        """Returns None when multiple teams remain."""
        assert game._get_winning_team_num({0, 1}) is None
        assert game._get_winning_team_num({0, 1, 2}) is None

    def test_single_higher_team_number(self, game):
        """Returns the correct team even for higher team numbers."""
        assert game._get_winning_team_num({5}) == 5


class TestSendWinnerRainbowEffects:
    """Test _send_winner_rainbow_effects helper."""

    @pytest.mark.asyncio
    async def test_sends_effects_to_alive_winning_players(self, game):
        """Should send rainbow effect to alive players on the winning team."""
        mock_stream = AsyncMock()
        game.gameplay_stream = mock_stream

        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 0, True),
                ("p3", 1, False),
            ],
        )

        await game._send_winner_rainbow_effects(0)

        assert mock_stream.write.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_dead_players_on_winning_team(self, game):
        """Should not send effects to dead players even if on winning team."""
        mock_stream = AsyncMock()
        game.gameplay_stream = mock_stream

        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 0, False),
            ],
        )

        await game._send_winner_rainbow_effects(0)

        assert mock_stream.write.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_players_on_other_teams(self, game):
        """Should not send effects to players on non-winning teams."""
        mock_stream = AsyncMock()
        game.gameplay_stream = mock_stream

        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 1, True),
            ],
        )

        await game._send_winner_rainbow_effects(0)

        assert mock_stream.write.call_count == 1

    @pytest.mark.asyncio
    async def test_no_stream_does_nothing(self, game):
        """Should return without error when no gameplay stream exists."""
        game.gameplay_stream = None

        _add_players(game, [("p1", 0, True)])

        # Should not raise
        await game._send_winner_rainbow_effects(0)


class TestPlayTeamVictorySound:
    """Test _play_team_victory_sound helper."""

    @pytest.mark.asyncio
    async def test_plays_specific_team_sound(self, game):
        """Should play the team-specific victory sound when one exists."""
        game._play_sound = AsyncMock()

        # Blue has a specific sound
        game.teams[0].name = "Blue"

        await game._play_team_victory_sound(0)

        game._play_sound.assert_awaited_once_with(TEAM_WIN_SOUNDS["Blue"], priority=2)

    @pytest.mark.asyncio
    async def test_falls_back_to_congratulations(self, game):
        """Should fall back to congratulations for teams without specific sounds."""
        from lib.types import Sound

        game._play_sound = AsyncMock()

        # Pink has no specific sound in TEAM_WIN_SOUNDS
        game.teams[0].name = "Pink"

        await game._play_team_victory_sound(0)

        game._play_sound.assert_awaited_once_with(Sound.VOX_CONGRATULATIONS, priority=2)

    @pytest.mark.asyncio
    async def test_invalid_team_num_does_nothing(self, game):
        """Should handle missing team number gracefully."""
        game._play_sound = AsyncMock()

        await game._play_team_victory_sound(99)

        game._play_sound.assert_not_awaited()


class TestBuildSurvivedSpanAttrs:
    """Test _build_survived_span_attrs helper."""

    def test_basic_attributes(self, game):
        """Should include game_duration, winner, and team."""
        game.start_time = time.time() - 10.0
        player = Player(serial="p1", team=0, alive=True)

        attrs = game._build_survived_span_attrs(player, is_winner=True)

        assert "game_duration" in attrs
        assert attrs["game_duration"] > 0
        assert attrs["winner"] is True
        assert attrs["team"] == 0

    def test_winner_false(self, game):
        """Should correctly mark non-winner."""
        game.start_time = time.time()
        player = Player(serial="p1", team=1, alive=True)

        attrs = game._build_survived_span_attrs(player, is_winner=False)

        assert attrs["winner"] is False
        assert attrs["team"] == 1

    def test_no_start_time(self, game):
        """Should handle None start_time with duration 0."""
        game.start_time = None
        player = Player(serial="p1", team=0, alive=True)

        attrs = game._build_survived_span_attrs(player, is_winner=True)

        assert attrs["game_duration"] == 0

    def test_includes_analytics_summary(self, game):
        """Should include analytics data when player has analytics."""
        game.start_time = time.time()
        player = Player(serial="p1", team=0, alive=True)

        # Mock analytics
        mock_analytics = MagicMock()
        mock_analytics.get_summary.return_value = {
            "peak_accel": 3.5,
            "playstyle": "aggressive",
        }
        player.analytics = mock_analytics

        attrs = game._build_survived_span_attrs(player, is_winner=True)

        assert attrs["analytics.peak_accel"] == 3.5
        assert attrs["analytics.playstyle"] == "aggressive"

    def test_no_analytics(self, game):
        """Should not include analytics keys when analytics is None."""
        game.start_time = time.time()
        player = Player(serial="p1", team=0, alive=True)
        player.analytics = None

        attrs = game._build_survived_span_attrs(player, is_winner=True)

        # Should only have the base keys
        assert set(attrs.keys()) == {"game_duration", "winner", "team"}


class TestEndSurvivingPlayerSpans:
    """Test _end_surviving_player_spans helper."""

    def test_ends_spans_for_alive_players_with_spans(self, game):
        """Should end spans for alive players that have open spans."""
        mock_span = MagicMock()

        game.start_time = time.time()
        _add_players(game, [("p1", 0, True)])
        game.players["p1"].span = mock_span

        game._end_surviving_player_spans(winning_team_num=0)

        mock_span.add_event.assert_called_once()
        mock_span.set_status.assert_called_once()
        mock_span.end.assert_called_once()

    def test_skips_dead_players(self, game):
        """Should not touch spans of dead players."""
        mock_span = MagicMock()

        game.start_time = time.time()
        _add_players(game, [("p1", 0, False)])
        game.players["p1"].span = mock_span

        game._end_surviving_player_spans(winning_team_num=0)

        mock_span.end.assert_not_called()

    def test_skips_players_without_spans(self, game):
        """Should not error for alive players with no span."""
        game.start_time = time.time()
        _add_players(game, [("p1", 0, True)])
        game.players["p1"].span = None

        # Should not raise
        game._end_surviving_player_spans(winning_team_num=0)

    def test_marks_winners_correctly(self, game):
        """Should pass is_winner=True for players on the winning team."""
        mock_span = MagicMock()

        game.start_time = time.time()
        _add_players(game, [("p1", 0, True)])
        game.players["p1"].span = mock_span

        game._end_surviving_player_spans(winning_team_num=0)

        event_args = mock_span.add_event.call_args
        assert event_args[1]["attributes"]["winner"] is True

    def test_marks_non_winners_correctly(self, game):
        """Should pass is_winner=False for players not on winning team."""
        mock_span = MagicMock()

        game.start_time = time.time()
        _add_players(game, [("p1", 1, True)])
        game.players["p1"].span = mock_span

        game._end_surviving_player_spans(winning_team_num=0)

        event_args = mock_span.add_event.call_args
        assert event_args[1]["attributes"]["winner"] is False

    def test_no_winner_all_marked_false(self, game):
        """Should pass is_winner=False for all players when no winner."""
        mock_span = MagicMock()

        game.start_time = time.time()
        _add_players(game, [("p1", 0, True)])
        game.players["p1"].span = mock_span

        game._end_surviving_player_spans(winning_team_num=None)

        event_args = mock_span.add_event.call_args
        assert event_args[1]["attributes"]["winner"] is False


class TestPublishPlayerAnalytics:
    """Test _publish_player_analytics helper."""

    @pytest.mark.asyncio
    async def test_publishes_analytics_for_players_with_data(self, game, mock_event_publisher):
        """Should publish analytics event for players that have analytics."""
        mock_analytics = MagicMock()
        mock_analytics.get_summary.return_value = {
            "peak_accel": 2.5,
            "playstyle": "cautious",
            "near_death_count": 3,
        }
        mock_analytics.total_time_ms = 15000
        mock_analytics.sample_count = 900
        mock_analytics.near_death_count = 3

        _add_players(game, [("p1", 0, True)])
        game.players["p1"].analytics = mock_analytics

        await game._publish_player_analytics(winning_team_num=0)

        # Verify event published
        from lib.types import GameEvent

        mock_event_publisher.assert_awaited_once()
        call_args = mock_event_publisher.call_args
        assert call_args[0][0] == GameEvent.PLAYER_ANALYTICS
        summary = call_args[0][1]
        assert summary["game_id"] == "test_helpers_001"
        assert summary["winner"] is True
        assert summary["team"] == 0
        assert summary["survival_time_ms"] == 15000

    @pytest.mark.asyncio
    async def test_skips_players_without_analytics(self, game, mock_event_publisher):
        """Should skip players that have no analytics data."""
        _add_players(game, [("p1", 0, True)])
        game.players["p1"].analytics = None

        await game._publish_player_analytics(winning_team_num=0)

        mock_event_publisher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_winner_flag_reflects_team(self, game, mock_event_publisher):
        """Should correctly set winner=False for losing team players."""
        mock_analytics = MagicMock()
        mock_analytics.get_summary.return_value = {
            "peak_accel": 1.0,
            "playstyle": "cautious",
            "near_death_count": 0,
        }
        mock_analytics.total_time_ms = 5000
        mock_analytics.sample_count = 300
        mock_analytics.near_death_count = 0

        _add_players(game, [("p1", 1, True)])
        game.players["p1"].analytics = mock_analytics

        await game._publish_player_analytics(winning_team_num=0)

        summary = mock_event_publisher.call_args[0][1]
        assert summary["winner"] is False


class TestEndSurvivingTeamSpans:
    """Test _end_surviving_team_spans helper."""

    def test_ends_span_for_winning_team(self, game):
        """Should end span for the winning team with team_victory event."""
        mock_span = MagicMock()
        game.start_time = time.time()
        game.teams[0].span = mock_span

        game._end_surviving_team_spans(alive_teams={0}, winning_team_num=0)

        mock_span.add_event.assert_called_once()
        event_name = mock_span.add_event.call_args[0][0]
        assert event_name == "team_victory"
        mock_span.end.assert_called_once()

    def test_ends_span_for_surviving_non_winner(self, game):
        """Should end span with team_survived event for non-winning alive teams."""
        mock_span = MagicMock()
        game.start_time = time.time()
        game.teams[1].span = mock_span

        game._end_surviving_team_spans(alive_teams={0, 1}, winning_team_num=0)

        event_name = mock_span.add_event.call_args[0][0]
        assert event_name == "team_survived"
        attrs = mock_span.add_event.call_args[1]["attributes"]
        assert attrs["winner"] is False
        mock_span.end.assert_called_once()

    def test_skips_eliminated_teams(self, game):
        """Should not touch spans for teams not in alive_teams."""
        mock_span = MagicMock()
        game.teams[1].span = mock_span

        game._end_surviving_team_spans(alive_teams={0}, winning_team_num=0)

        mock_span.end.assert_not_called()

    def test_skips_teams_without_spans(self, game):
        """Should not error for alive teams with no span."""
        game.teams[0].span = None

        # Should not raise
        game._end_surviving_team_spans(alive_teams={0}, winning_team_num=0)

    def test_no_winner_all_survived(self, game):
        """Should mark all teams as survived when no winner (tie/draw)."""
        mock_span_0 = MagicMock()
        mock_span_1 = MagicMock()
        game.start_time = time.time()
        game.teams[0].span = mock_span_0
        game.teams[1].span = mock_span_1

        game._end_surviving_team_spans(alive_teams={0, 1}, winning_team_num=None)

        # Both should get "team_survived"
        event_0 = mock_span_0.add_event.call_args[0][0]
        event_1 = mock_span_1.add_event.call_args[0][0]
        assert event_0 == "team_survived"
        assert event_1 == "team_survived"


class TestGetAliveTeams:
    """Test _get_alive_teams method."""

    def test_all_players_alive(self, game):
        """Should return all team numbers when all players are alive."""
        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 0, True),
                ("p3", 1, True),
                ("p4", 1, True),
            ],
        )
        assert game._get_alive_teams() == {0, 1}

    def test_one_team_eliminated(self, game):
        """Should exclude a team whose players are all dead."""
        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 0, True),
                ("p3", 1, False),
                ("p4", 1, False),
            ],
        )
        assert game._get_alive_teams() == {0}

    def test_all_players_dead(self, game):
        """Should return empty set when all players are dead."""
        _add_players(
            game,
            [
                ("p1", 0, False),
                ("p2", 1, False),
            ],
        )
        assert game._get_alive_teams() == set()

    def test_no_players(self, game):
        """Should return empty set with no players."""
        assert game._get_alive_teams() == set()

    def test_mixed_alive_dead_per_team(self, game):
        """Team should be alive if at least one player on it is alive."""
        _add_players(
            game,
            [
                ("p1", 0, False),
                ("p2", 0, True),  # Team 0 still alive via p2
                ("p3", 1, False),
                ("p4", 1, False),  # Team 1 all dead
            ],
        )
        assert game._get_alive_teams() == {0}


class TestCheckWinCondition:
    """Test _check_win_condition method."""

    @pytest.mark.asyncio
    async def test_one_team_remaining_returns_true(self, game, mock_event_publisher):
        """Should return True and publish team_winner when one team remains."""
        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 0, True),
                ("p3", 1, False),
                ("p4", 1, False),
            ],
        )

        result = await game._check_win_condition()

        assert result is True
        # Should have published team_winner event
        mock_event_publisher.assert_awaited_once()
        event_type = mock_event_publisher.call_args[0][0]
        event_data = mock_event_publisher.call_args[0][1]
        assert event_type == "team_winner"
        assert event_data["team"] == 0
        assert event_data["team_name"] == "Pink"  # Team 0 color is Pink
        assert set(event_data["winning_players"]) == {"p1", "p2"}
        assert event_data["winner_count"] == 2

    @pytest.mark.asyncio
    async def test_no_teams_remaining_tie(self, game, mock_event_publisher):
        """Should return True and publish game_tie when no teams remain."""
        from lib.types import GameEvent

        _add_players(
            game,
            [
                ("p1", 0, False),
                ("p2", 1, False),
            ],
        )

        result = await game._check_win_condition()

        assert result is True
        mock_event_publisher.assert_awaited_once()
        event_type = mock_event_publisher.call_args[0][0]
        assert event_type == GameEvent.GAME_TIE

    @pytest.mark.asyncio
    async def test_multiple_teams_remaining_returns_false(self, game, mock_event_publisher):
        """Should return False when multiple teams are still alive."""
        _add_players(
            game,
            [
                ("p1", 0, True),
                ("p2", 1, True),
            ],
        )

        result = await game._check_win_condition()

        assert result is False
        mock_event_publisher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_surviving_player_on_winning_team(self, game, mock_event_publisher):
        """Should correctly detect winner with just one surviving player."""
        _add_players(
            game,
            [
                ("p1", 0, False),
                ("p2", 0, False),
                ("p3", 1, True),
            ],
        )

        result = await game._check_win_condition()

        assert result is True
        event_data = mock_event_publisher.call_args[0][1]
        assert event_data["team"] == 1
        assert event_data["winning_players"] == ["p3"]
        assert event_data["winner_count"] == 1


class TestEndGameImpl:
    """Test _end_game_impl method for team-based games."""

    @pytest.fixture
    def game_with_mocks(self, game):
        """Set up a game with mocked helper methods."""
        from services.game_coordinator.games.base import GameState

        game.start_time = time.time() - 30.0  # Game ran for 30 seconds
        game.state = GameState.RUNNING
        game._send_winner_rainbow_effects = AsyncMock()
        game._play_team_victory_sound = AsyncMock()
        game._wait_for_rainbow_effect = AsyncMock()
        game._end_surviving_player_spans = MagicMock()
        game._publish_player_analytics = AsyncMock()
        game._end_surviving_team_spans = MagicMock()
        return game

    @pytest.mark.asyncio
    async def test_end_game_with_winner(self, game_with_mocks, mock_event_publisher):
        """Should send rainbow effects and victory sound when there is a winner."""
        _add_players(
            game_with_mocks,
            [
                ("p1", 0, True),
                ("p2", 1, False),
            ],
        )

        await game_with_mocks._end_game_impl()

        game_with_mocks._send_winner_rainbow_effects.assert_awaited_once_with(0)
        game_with_mocks._play_team_victory_sound.assert_awaited_once_with(0)
        game_with_mocks._wait_for_rainbow_effect.assert_awaited_once()
        game_with_mocks._end_surviving_player_spans.assert_called_once_with(0)
        game_with_mocks._publish_player_analytics.assert_awaited_once_with(0)

    @pytest.mark.asyncio
    async def test_end_game_tie(self, game_with_mocks, mock_event_publisher):
        """Should skip rainbow and victory sound when there is no winner (tie)."""
        _add_players(
            game_with_mocks,
            [
                ("p1", 0, False),
                ("p2", 1, False),
            ],
        )

        await game_with_mocks._end_game_impl()

        game_with_mocks._send_winner_rainbow_effects.assert_not_awaited()
        game_with_mocks._play_team_victory_sound.assert_not_awaited()
        game_with_mocks._wait_for_rainbow_effect.assert_awaited_once()
        game_with_mocks._end_surviving_player_spans.assert_called_once_with(None)
        game_with_mocks._publish_player_analytics.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended(self, game_with_mocks, mock_event_publisher):
        """Should publish GAME_ENDED event with game_id and duration."""
        from lib.types import GameEvent

        _add_players(game_with_mocks, [("p1", 0, True)])

        await game_with_mocks._end_game_impl()

        # Last call to event_publisher should be GAME_ENDED
        last_call = mock_event_publisher.call_args
        assert last_call[0][0] == GameEvent.GAME_ENDED
        assert last_call[0][1]["game_id"] == "test_helpers_001"
        assert "duration" in last_call[0][1]
        assert last_call[0][1]["duration"] > 0

    @pytest.mark.asyncio
    async def test_end_game_calls_end_surviving_team_spans(self, game_with_mocks, mock_event_publisher):
        """Should pass alive_teams and winning_team_num to _end_surviving_team_spans."""
        _add_players(
            game_with_mocks,
            [
                ("p1", 0, True),
                ("p2", 0, True),
                ("p3", 1, False),
            ],
        )

        await game_with_mocks._end_game_impl()

        call_args = game_with_mocks._end_surviving_team_spans.call_args
        alive_teams_arg = call_args[0][0]
        winning_team_arg = call_args[0][1]
        assert alive_teams_arg == {0}
        assert winning_team_arg == 0
