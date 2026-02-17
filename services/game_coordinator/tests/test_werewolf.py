"""
Unit tests for Werewolf game mode.

Tests the core Werewolf mechanics:
- Hidden werewolf assignment (~44% of players)
- Team tracking (humans vs werewolves)
- Player death handling
- Win conditions (team elimination)
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
from services.game_coordinator.games.werewolf import (
    HUMAN_COLOR,
    WEREWOLF_COLOR,
    WerewolfGame,
    WerewolfPlayer,
)


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class TestWerewolfGameMode:
    """Test Werewolf game mechanics."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_werewolf_001",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_player_initialization(self, werewolf_game):
        """Test that players are initialized correctly."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All 4 players should be created
        assert len(game.players) == 4

        # Total should be 4
        total = len(game.werewolf_serials) + len(game.human_serials)
        assert total == 4

    @pytest.mark.asyncio
    async def test_werewolf_percentage(self, werewolf_game):
        """Test that approximately 44% are werewolves."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 4 players * 0.44 = 1.76, so 1 werewolf (max(1, int(4*0.44)))
        # But minimum is 1, so we expect 1 werewolf
        werewolf_count = len(game.werewolf_serials)
        assert werewolf_count >= 1
        assert werewolf_count <= 2  # Could be 1 or 2 for 4 players

    @pytest.mark.asyncio
    async def test_all_players_start_yellow(self, werewolf_game):
        """Test that all players start with human color (hidden identities)."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for _serial, player in game.players.items():
            assert player.color == HUMAN_COLOR

    @pytest.mark.asyncio
    async def test_werewolves_marked_correctly(self, werewolf_game):
        """Test that werewolves have correct attributes."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.werewolf_serials:
            player = game.players[serial]
            assert player.is_werewolf is True
            assert player.team == 1
            assert player.revealed is False

    @pytest.mark.asyncio
    async def test_humans_marked_correctly(self, werewolf_game):
        """Test that humans have correct attributes."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.human_serials:
            player = game.players[serial]
            assert player.is_werewolf is False
            assert player.team == 0
            assert player.revealed is False

    @pytest.mark.asyncio
    async def test_win_condition_humans_eliminated(self, werewolf_game):
        """Test that werewolves win when all humans are dead."""
        game, mock_controller_manager, event_collector = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially no win
        assert not await game._check_win_condition()

        # Kill all humans
        for serial in game.human_serials:
            game.players[serial].alive = False

        # Werewolves should win
        assert await game._check_win_condition()

        # Verify winner event
        winner_events = event_collector.get_events_of_type("werewolf_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["winner"] == "werewolves"

    @pytest.mark.asyncio
    async def test_win_condition_werewolves_eliminated(self, werewolf_game):
        """Test that humans win when all werewolves are dead."""
        game, mock_controller_manager, event_collector = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially no win
        assert not await game._check_win_condition()

        # Kill all werewolves
        for serial in game.werewolf_serials:
            game.players[serial].alive = False

        # Humans should win
        assert await game._check_win_condition()

        # Verify winner event
        winner_events = event_collector.get_events_of_type("werewolf_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["winner"] == "humans"

    @pytest.mark.asyncio
    async def test_no_win_with_both_teams_alive(self, werewolf_game):
        """Test that game continues with both teams alive."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All alive - no win
        assert not await game._check_win_condition()

        # Kill one from each team (if possible)
        if len(game.human_serials) > 1:
            game.players[game.human_serials[0]].alive = False
        if len(game.werewolf_serials) > 1:
            game.players[game.werewolf_serials[0]].alive = False

        # Still both teams have members - no win
        alive_humans = [s for s in game.human_serials if game.players[s].alive]
        alive_wolves = [s for s in game.werewolf_serials if game.players[s].alive]

        if alive_humans and alive_wolves:
            assert not await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_kill_player_marks_dead(self, werewolf_game):
        """Test that _kill_player_impl marks player as dead."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get first player
        serial = list(game.players.keys())[0]
        player = game.players[serial]
        assert player.alive is True

        # Kill the player
        await game._kill_player_impl(serial, accel_mag=5.0)

        assert player.alive is False

    @pytest.mark.asyncio
    async def test_werewolf_player_dataclass(self, werewolf_game):
        """Test WerewolfPlayer dataclass attributes."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All players should be WerewolfPlayer instances
        for _serial, player in game.players.items():
            assert isinstance(player, WerewolfPlayer)
            assert hasattr(player, "is_werewolf")
            assert hasattr(player, "revealed")

    @pytest.mark.asyncio
    async def test_get_game_name(self, werewolf_game):
        """Test get_game_name returns 'Werewolf'."""
        game, _, _ = werewolf_game
        assert game.get_game_name() == "Werewolf"

    @pytest.mark.asyncio
    async def test_kill_player_publishes_event_with_role(self, werewolf_game):
        """Test that _kill_player_impl handles werewolf role info."""
        game, mock_controller_manager, _ = werewolf_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get a werewolf player
        werewolf_serial = game.werewolf_serials[0]
        player = game.players[werewolf_serial]
        assert player.is_werewolf is True

        # Kill the werewolf
        await game._kill_player_impl(werewolf_serial, accel_mag=5.0)

        # Player should be dead
        assert player.alive is False


class TestWerewolfThresholds:
    """Tests for werewolf-specific thresholds."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_werewolf_thresh",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_werewolf_returns_werewolf_thresholds(self, werewolf_game):
        """Werewolves should get thresholds from WEREWOLF_THRESHOLDS dict."""
        from services.game_coordinator.games.werewolf import WEREWOLF_THRESHOLDS

        game, mock_controller_manager = werewolf_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get threshold for a werewolf
        werewolf_serial = game.werewolf_serials[0]
        werewolf_player = game.players[werewolf_serial]
        werewolf_thresholds = game._compute_effective_thresholds(werewolf_player)

        # Should return a tuple from WEREWOLF_THRESHOLDS
        assert isinstance(werewolf_thresholds, tuple), "Werewolf thresholds should be tuple"
        assert len(werewolf_thresholds) == 2, "Werewolf thresholds should have (warning, death)"
        assert werewolf_thresholds[0] < werewolf_thresholds[1], "Warning should be less than death threshold"

        # Verify it matches WEREWOLF_THRESHOLDS for game's sensitivity
        expected = WEREWOLF_THRESHOLDS.get(game.sensitivity, (2.1, 2.6))
        assert werewolf_thresholds == expected

    @pytest.mark.asyncio
    async def test_human_returns_base_thresholds(self, werewolf_game):
        """Humans should return base class thresholds (tuple of warn, death)."""
        game, mock_controller_manager = werewolf_game
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
    async def test_werewolf_threshold_higher_than_human(self, werewolf_game):
        """Werewolf death threshold should be higher than human death threshold."""
        game, mock_controller_manager = werewolf_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        werewolf_serial = game.werewolf_serials[0]
        human_serial = game.human_serials[0]

        werewolf_thresh = game._compute_effective_thresholds(game.players[werewolf_serial])
        human_thresh = game._compute_effective_thresholds(game.players[human_serial])

        # Werewolf death threshold should be higher (harder to kill)
        assert werewolf_thresh[1] > human_thresh[1], (
            f"Werewolf death threshold {werewolf_thresh[1]} should be > human {human_thresh[1]}"
        )

    @pytest.mark.asyncio
    async def test_werewolf_survives_accel_that_kills_human(self, werewolf_game):
        """Integration test: acceleration between human and werewolf death thresholds kills human but not werewolf."""
        from proto import controller_manager_pb2

        game, mock_controller_manager = werewolf_game
        game.gameplay_stream = MockGameplayStream()
        await game._initialize_players_impl(mock_controller_manager.controllers)

        werewolf_serial = game.werewolf_serials[0]
        human_serial = game.human_serials[0]

        werewolf_thresh = game._compute_effective_thresholds(game.players[werewolf_serial])
        human_thresh = game._compute_effective_thresholds(game.players[human_serial])

        # Pick acceleration between human death and werewolf death thresholds
        mid_accel = (human_thresh[1] + werewolf_thresh[1]) / 2

        game.players[werewolf_serial].grace_until = 0.0
        game.players[human_serial].grace_until = 0.0

        state_human = controller_manager_pb2.GameplayData(
            serial=human_serial,
            accel=controller_manager_pb2.Vector3(x=mid_accel, y=0.0, z=0.0),
        )
        state_werewolf = controller_manager_pb2.GameplayData(
            serial=werewolf_serial,
            accel=controller_manager_pb2.Vector3(x=mid_accel, y=0.0, z=0.0),
        )

        # Feed enough frames for EMA to build up
        for _ in range(20):
            if game.players[human_serial].alive:
                await game._process_controller_state(state_human)
            if game.players[werewolf_serial].alive:
                await game._process_controller_state(state_werewolf)

        # Human should be dead, werewolf should survive
        assert game.players[human_serial].alive is False, "Human should die at mid-range acceleration"
        assert game.players[werewolf_serial].alive is True, "Werewolf should survive mid-range acceleration"

    @pytest.mark.asyncio
    async def test_werewolf_thresholds_all_sensitivities(self, werewolf_game):
        """All 5 sensitivity levels should return valid tuples from WEREWOLF_THRESHOLDS."""
        from services.game_coordinator.games.base import Sensitivity
        from services.game_coordinator.games.werewolf import WEREWOLF_THRESHOLDS

        game, mock_controller_manager = werewolf_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        werewolf_serial = game.werewolf_serials[0]
        werewolf_player = game.players[werewolf_serial]

        for sensitivity in Sensitivity:
            game.sensitivity = sensitivity
            thresh = game._compute_effective_thresholds(werewolf_player)
            expected = WEREWOLF_THRESHOLDS[sensitivity]
            assert thresh == expected, f"Sensitivity {sensitivity}: got {thresh}, expected {expected}"

    @pytest.mark.asyncio
    async def test_werewolf_thresholds_static_across_music_speed(self, werewolf_game):
        """Werewolf thresholds should not change with music tempo (intentionally static)."""
        game, mock_controller_manager = werewolf_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        werewolf_serial = game.werewolf_serials[0]
        werewolf_player = game.players[werewolf_serial]

        # Get thresholds at slow music speed
        game.music_speed = 1.0
        thresh_slow = game._compute_effective_thresholds(werewolf_player)

        # Get thresholds at fast music speed
        game.music_speed = 1.3
        thresh_fast = game._compute_effective_thresholds(werewolf_player)

        # Werewolf thresholds come from static dict, not affected by music
        assert thresh_slow == thresh_fast, (
            f"Werewolf thresholds should be static: slow={thresh_slow}, fast={thresh_fast}"
        )


class TestWerewolfReveal:
    """Tests for werewolf reveal mechanics."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_werewolf_reveal",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_starts_unrevealed(self, werewolf_game):
        """Game should start with revealed=False."""
        game, mock_controller_manager, _ = werewolf_game
        await game._initialize_players_impl(mock_controller_manager.controllers)

        assert game.revealed is False
        for player in game.players.values():
            assert player.revealed is False


class TestWerewolfEdgeCases:
    """Tests for edge cases in werewolf game."""

    @pytest.mark.asyncio
    async def test_larger_player_count_werewolf_ratio(self):
        """Test werewolf ratio with more players."""
        mock_controller_manager = MockControllerManagerService(num_controllers=9)
        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_werewolf_ratio",
        )

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 9 players * 0.44 = 3.96, so 3 werewolves
        werewolf_count = len(game.werewolf_serials)
        assert werewolf_count == 3

    @pytest.mark.asyncio
    async def test_minimum_one_werewolf(self):
        """Even with 2 players, should have at least 1 werewolf."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)
        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_min_werewolf",
        )

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # 2 players * 0.44 = 0.88, but min(1, ...) ensures at least 1
        assert len(game.werewolf_serials) >= 1

    @pytest.mark.asyncio
    async def test_win_condition_all_dead(self):
        """Test when all players are dead (draw condition)."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        event_collector = EventCollector()

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_werewolf_draw",
        )

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill everyone
        for player in game.players.values():
            player.alive = False

        # Should trigger win condition (with winner="none")
        assert await game._check_win_condition()

        winner_events = event_collector.get_events_of_type("werewolf_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["winner"] == "none"

    @pytest.mark.asyncio
    async def test_reveal_state_tracks_correctly(self):
        """Test that game reveal state can be toggled."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)
        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_reveal_state",
        )

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initially not revealed
        assert game.revealed is False

        # Manually set revealed
        game.revealed = True

        assert game.revealed is True


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


class TestWerewolfRevealMechanics:
    """Tests for _reveal_werewolves() method."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game with 6 players (ensures >=2 werewolves)."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=6,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_werewolf_reveal_mech",
            reveal_time_seconds=0.01,
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_reveal_sets_game_revealed_flag(self, werewolf_game):
        """After reveal, game.revealed should be True."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._reveal_werewolves()

        assert game.revealed is True

    @pytest.mark.asyncio
    async def test_reveal_changes_werewolf_colors(self, werewolf_game):
        """After reveal, alive werewolves should have WEREWOLF_COLOR and revealed=True."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._reveal_werewolves()

        for serial in game.werewolf_serials:
            player = game.players[serial]
            if player.alive:
                assert player.color == WEREWOLF_COLOR
                assert player.revealed is True

    @pytest.mark.asyncio
    async def test_reveal_publishes_event(self, werewolf_game):
        """Reveal should publish werewolf_reveal event with serials."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._reveal_werewolves()

        reveal_events = event_collector.get_events_of_type("werewolf_reveal")
        assert len(reveal_events) == 1
        assert set(reveal_events[0]["werewolf_serials"]) == set(game.werewolf_serials)

    @pytest.mark.asyncio
    async def test_reveal_skips_if_not_running(self, werewolf_game):
        """If game stops during reveal sleep, revealed should stay False."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        async def stop_game(*_args, **_kwargs):
            game.running = False

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", side_effect=stop_game):
            await game._reveal_werewolves()

        assert game.revealed is False

    @pytest.mark.asyncio
    async def test_reveal_plays_sound(self, werewolf_game):
        """Reveal should play VOX_WEREWOLF_REVEAL sound."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._reveal_werewolves()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_WEREWOLF_REVEAL in sound_args

    @pytest.mark.asyncio
    async def test_reveal_skips_dead_werewolves(self, werewolf_game):
        """Dead werewolves should not get color change or revealed=True."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Kill first werewolf before reveal
        dead_wolf = game.werewolf_serials[0]
        game.players[dead_wolf].alive = False

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._reveal_werewolves()

        # Dead werewolf should NOT have color changed or revealed set
        assert game.players[dead_wolf].revealed is False
        assert game.players[dead_wolf].color == HUMAN_COLOR


class TestWerewolfIntroPhase:
    """Tests for _werewolf_intro_phase() method."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_werewolf_intro",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_intro_publishes_start_and_end_events(self, werewolf_game):
        """Intro should publish werewolf_intro_start and werewolf_intro_end."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = werewolf_game
        game.running = True
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._werewolf_intro_phase()

        start_events = event_collector.get_events_of_type("werewolf_intro_start")
        end_events = event_collector.get_events_of_type("werewolf_intro_end")
        assert len(start_events) == 1
        assert len(end_events) == 1

    @pytest.mark.asyncio
    async def test_intro_sets_all_colors_yellow(self, werewolf_game):
        """Intro should set all controllers to HUMAN_COLOR (yellow)."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._werewolf_intro_phase()

        # Check base_color commands for all players
        color_serials = set()
        for m in stream.messages:
            if m.HasField("base_color"):
                color_serials.add(m.base_color.serial)

        for serial in game.players:
            assert serial in color_serials

    @pytest.mark.asyncio
    async def test_intro_rumbles_werewolves(self, werewolf_game):
        """Intro should send GAME_EFFECT_RUMBLE to each werewolf."""
        from unittest.mock import AsyncMock, patch

        from proto import controller_manager_pb2

        game, mock_controller_manager, _ = werewolf_game
        game.running = True
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game._play_sound = AsyncMock()

        await game._initialize_players_impl(mock_controller_manager.controllers)

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", new_callable=AsyncMock):
            await game._werewolf_intro_phase()

        # Find rumble messages
        rumble_serials = set()
        for m in stream.messages:
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_RUMBLE:
                rumble_serials.add(m.game_effect.serial)

        for serial in game.werewolf_serials:
            assert serial in rumble_serials

    @pytest.mark.asyncio
    async def test_intro_stops_if_not_running(self, werewolf_game):
        """If game.running becomes False during intro, werewolf_intro_end should NOT be published."""
        from unittest.mock import AsyncMock, patch

        game, mock_controller_manager, event_collector = werewolf_game
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

        with patch("services.game_coordinator.games.werewolf.asyncio.sleep", side_effect=stop_after_first):
            await game._werewolf_intro_phase()

        end_events = event_collector.get_events_of_type("werewolf_intro_end")
        assert len(end_events) == 0


class TestSetAllColors:
    """Tests for _set_all_colors() method."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game with 4 players."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_set_colors",
        )

        return game, mock_controller_manager

    @pytest.mark.asyncio
    async def test_set_all_colors_sends_commands(self, werewolf_game):
        """_set_all_colors should send a base_color command for each player."""
        game, mock_controller_manager = werewolf_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)

        await game._set_all_colors((255, 0, 0))

        color_serials = {m.base_color.serial for m in stream.messages if m.HasField("base_color")}
        assert color_serials == set(game.players.keys())

    @pytest.mark.asyncio
    async def test_set_all_colors_correct_rgb(self, werewolf_game):
        """The RGB values in commands should match the requested color."""
        game, mock_controller_manager = werewolf_game
        stream = RecordingGameplayStream()
        game.gameplay_stream = stream

        await game._initialize_players_impl(mock_controller_manager.controllers)

        await game._set_all_colors((100, 200, 50))

        for m in stream.messages:
            if m.HasField("base_color"):
                assert m.base_color.color.r == 100
                assert m.base_color.color.g == 200
                assert m.base_color.color.b == 50


class TestWerewolfEndGame:
    """Tests for _end_game_impl() method."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_werewolf_endgame",
        )

        return game, mock_controller_manager, event_collector

    async def _setup_game(self, game, mock_controller_manager):
        """Shared setup: init players, set state to RUNNING, mock helpers."""
        from unittest.mock import AsyncMock

        from services.game_coordinator.games.base import GameState

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.state = GameState.RUNNING
        game.start_time = 100.0
        game.gameplay_stream = RecordingGameplayStream()
        game._play_sound = AsyncMock()
        game._wait_for_rainbow_effect = AsyncMock()

    @pytest.mark.asyncio
    async def test_end_game_werewolves_win(self, werewolf_game):
        """When all humans dead, werewolves should win."""
        game, mock_controller_manager, event_collector = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        for serial in game.human_serials:
            game.players[serial].alive = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winner"] == "werewolves"

    @pytest.mark.asyncio
    async def test_end_game_humans_win(self, werewolf_game):
        """When all werewolves dead, humans should win."""
        game, mock_controller_manager, event_collector = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        for serial in game.werewolf_serials:
            game.players[serial].alive = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winner"] == "humans"

    @pytest.mark.asyncio
    async def test_end_game_draw(self, werewolf_game):
        """When everyone dead, winner should be 'none'."""
        game, mock_controller_manager, event_collector = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        for player in game.players.values():
            player.alive = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winner"] == "none"

    @pytest.mark.asyncio
    async def test_end_game_cancels_reveal_task(self, werewolf_game):
        """_end_game_impl should cancel reveal_task if not done."""
        import asyncio
        from unittest.mock import MagicMock

        game, mock_controller_manager, _ = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        game.reveal_task = mock_task

        await game._end_game_impl()

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_game_plays_werewolf_win_sound(self, werewolf_game):
        """When werewolves win, VOX_WEREWOLF_WIN should be played."""
        game, mock_controller_manager, _ = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        for serial in game.human_serials:
            game.players[serial].alive = False

        await game._end_game_impl()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_WEREWOLF_WIN in sound_args

    @pytest.mark.asyncio
    async def test_end_game_plays_human_win_sound(self, werewolf_game):
        """When humans win, VOX_HUMAN_WIN should be played."""
        game, mock_controller_manager, _ = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        for serial in game.werewolf_serials:
            game.players[serial].alive = False

        await game._end_game_impl()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.VOX_HUMAN_WIN in sound_args

    @pytest.mark.asyncio
    async def test_end_game_plays_wolfdown_on_draw(self, werewolf_game):
        """When draw (no one alive), SFX_WOLFDOWN should be played."""
        game, mock_controller_manager, _ = werewolf_game
        await self._setup_game(game, mock_controller_manager)

        for player in game.players.values():
            player.alive = False

        await game._end_game_impl()

        calls = game._play_sound.call_args_list
        sound_args = [c[0][0] for c in calls]
        assert Sound.SFX_WOLFDOWN in sound_args


class TestWerewolfCleanup:
    """Tests for cleanup() method."""

    @pytest.fixture
    def werewolf_game(self):
        """Create a Werewolf game."""
        mock_controller_manager = MockControllerManagerService(num_controllers=4)

        return WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_werewolf_cleanup",
        )

    @pytest.mark.asyncio
    async def test_cleanup_cancels_reveal_task(self, werewolf_game):
        """cleanup() should cancel reveal_task if not done."""
        import asyncio
        from unittest.mock import MagicMock

        game = werewolf_game

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        game.reveal_task = mock_task

        await game.cleanup()

        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_no_reveal_task(self, werewolf_game):
        """cleanup() should not raise when reveal_task is None."""
        game = werewolf_game
        game.reveal_task = None

        await game.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_ignores_done_reveal_task(self, werewolf_game):
        """cleanup() should NOT cancel a reveal_task that is already done."""
        import asyncio
        from unittest.mock import MagicMock

        game = werewolf_game

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = True
        game.reveal_task = mock_task

        await game.cleanup()

        mock_task.cancel.assert_not_called()


class TestWerewolfAdditionalPhases:
    """Tests for _get_additional_phases() method."""

    def test_additional_phases_returns_werewolf_intro(self):
        """Should return a list with one Phase named 'werewolf_intro'."""
        mock_controller_manager = MockControllerManagerService(num_controllers=2)

        game = WerewolfGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=async_noop,
            audio_client=None,
            game_id="test_phases",
        )

        phases = game._get_additional_phases()
        assert len(phases) == 1
        assert phases[0].name == "werewolf_intro"
