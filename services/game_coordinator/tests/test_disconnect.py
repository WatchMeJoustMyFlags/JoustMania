"""
Unit tests for player death on controller disconnect (#580).

Tests:
- _handle_disconnect kills alive player
- _handle_disconnect is a no-op for dead/unknown players
- _process_gameplay_update handles disconnect events and checks win condition
- 2-player game ends when one disconnects
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
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame


class MockGameplayStream:
    """Mock bidirectional stream for testing."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class TestHandleDisconnect:
    """Tests for _handle_disconnect method."""

    @pytest.fixture
    def game_with_players(self):
        """Create a game with two alive players."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        event_collector = EventCollector()

        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_disconnect",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = game_clock.now()

        game.players["serial_a"] = Player(
            serial="serial_a",
            team=0,
            alive=True,
            color=(255, 0, 0),
        )
        game.players["serial_b"] = Player(
            serial="serial_b",
            team=0,
            alive=True,
            color=(0, 255, 0),
        )

        return game, event_collector

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2")
    @pytest.mark.uc("UC-030", flow="A5")
    async def test_disconnect_kills_alive_player(self, game_with_players):
        """Disconnecting an alive player should kill them."""
        game, _ = game_with_players
        player = game.players["serial_a"]
        assert player.alive is True

        await game._handle_disconnect("serial_a")

        assert player.alive is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A5", steps=[1])
    async def test_disconnect_increments_dead_count(self, game_with_players):
        """UC-030 A5 step 1: a disconnect counts as a death (dead_count +1), the same path as step 9."""
        game, _ = game_with_players
        initial = game.dead_count

        await game._handle_disconnect("serial_a")

        assert game.dead_count == initial + 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2")
    @pytest.mark.uc("UC-030", flow="A5")
    async def test_disconnect_sends_death_effect(self, game_with_players):
        """Disconnect should send death effect via gameplay stream."""
        game, _ = game_with_players

        await game._handle_disconnect("serial_a")

        # _kill_player sends GAME_EFFECT_PLAYER_DEATH via the stream
        death_effects = [
            m
            for m in game.gameplay_stream.messages
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
        ]
        assert len(death_effects) == 1
        assert death_effects[0].game_effect.serial == "serial_a"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A5", cond=["Pre-1"])
    async def test_disconnect_noop_for_dead_player(self, game_with_players):
        """UC-030 A5 applies to an alive player only (precondition): a dead player's disconnect changes nothing."""
        game, _ = game_with_players
        player = game.players["serial_a"]
        player.alive = False

        initial_dead_count = game.dead_count
        initial_messages = len(game.gameplay_stream.messages)

        await game._handle_disconnect("serial_a")

        # Nothing should change
        assert game.dead_count == initial_dead_count
        assert len(game.gameplay_stream.messages) == initial_messages

    @pytest.mark.asyncio
    async def test_disconnect_noop_for_unknown_serial(self, game_with_players):
        """Disconnecting an unknown serial should be a no-op."""
        game, _ = game_with_players

        initial_dead_count = game.dead_count
        initial_messages = len(game.gameplay_stream.messages)

        await game._handle_disconnect("unknown_serial")

        assert game.dead_count == initial_dead_count
        assert len(game.gameplay_stream.messages) == initial_messages

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A5")
    async def test_disconnect_adds_span_event(self, game_with_players):
        """Disconnect should add player_disconnected event to span."""
        from unittest.mock import MagicMock

        game, _ = game_with_players
        player = game.players["serial_a"]
        mock_span = MagicMock()
        player.span = mock_span

        await game._handle_disconnect("serial_a")

        # _kill_player_impl clears player.span, so check the saved reference
        mock_span.add_event.assert_any_call(
            "player_disconnected",
            attributes={"reason": "controller_disconnect"},
        )


class TestProcessGameplayUpdateWithDisconnects:
    """Tests for _process_gameplay_update handling disconnect events."""

    @pytest.fixture
    def game_with_players(self):
        """Create a game with three alive players."""
        mock_cm = MockControllerManagerService(num_controllers=3)
        event_collector = EventCollector()

        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_disconnect_update",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = game_clock.now()

        for i in range(3):
            serial = f"player_{i}"
            game.players[serial] = Player(
                serial=serial,
                team=0,
                alive=True,
                color=(255, 255, 255),
            )

        return game, event_collector

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A5")
    async def test_disconnect_event_processed_before_controllers(self, game_with_players):
        """Disconnect events should be processed before controller state data."""
        game, _ = game_with_players

        # Track the order of operations
        call_order = []
        original_handle = game._handle_disconnect
        original_process = game._process_controller_state

        async def tracked_handle(serial):
            call_order.append(("disconnect", serial))
            await original_handle(serial)

        async def tracked_process(state):
            call_order.append(("controller", state.serial))
            await original_process(state)

        game._handle_disconnect = tracked_handle
        game._process_controller_state = tracked_process

        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[
                controller_manager_pb2.GameplayData(
                    serial="player_1",
                    accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
                ),
            ],
            disconnects=[
                controller_manager_pb2.DisconnectEvent(serial="player_0"),
            ],
            timestamp=int(time.time() * 1000),
        )

        await game._process_gameplay_update(update)

        # Disconnects should be processed first
        assert call_order[0] == ("disconnect", "player_0")
        assert call_order[1] == ("controller", "player_1")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2", br=["BR-001"])
    @pytest.mark.uc("UC-030", flow="A5")
    async def test_disconnect_triggers_win_condition_check(self, game_with_players):
        """Win condition should be checked after each disconnect."""
        game, _ = game_with_players

        # Kill player_0, so only player_1 and player_2 are alive
        game.players["player_0"].alive = False

        # Now disconnect player_1 -- only player_2 remains => win
        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[],
            disconnects=[
                controller_manager_pb2.DisconnectEvent(serial="player_1"),
            ],
            timestamp=int(time.time() * 1000),
        )

        result = await game._process_gameplay_update(update)

        assert result is True  # Win condition met
        assert game.players["player_1"].alive is False

    @pytest.mark.asyncio
    async def test_empty_disconnects_no_effect(self, game_with_players):
        """Empty disconnect list should not affect game state."""
        game, _ = game_with_players

        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[
                controller_manager_pb2.GameplayData(
                    serial="player_0",
                    accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0),
                ),
            ],
            disconnects=[],
            timestamp=int(time.time() * 1000),
        )

        result = await game._process_gameplay_update(update)

        assert result is False  # Game continues
        for p in game.players.values():
            assert p.alive is True


class TestTwoPlayerDisconnectScenario:
    """Tests for the 2-player scenario where disconnect should end the game."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2", br=["BR-001"])
    async def test_two_player_game_ends_on_disconnect(self):
        """In a 2-player game, one disconnect should end the game with the other winning."""
        mock_cm = MockControllerManagerService(num_controllers=2)
        event_collector = EventCollector()

        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_2p_disconnect",
        )
        game.gameplay_stream = MockGameplayStream()
        game.running = True
        game.start_time = game_clock.now()

        game.players["player_a"] = Player(serial="player_a", team=0, alive=True, color=(255, 0, 0))
        game.players["player_b"] = Player(serial="player_b", team=0, alive=True, color=(0, 255, 0))

        # Simulate player_a disconnecting
        update = controller_manager_pb2.GameplayDataUpdate(
            controllers=[],
            disconnects=[
                controller_manager_pb2.DisconnectEvent(serial="player_a"),
            ],
            timestamp=int(time.time() * 1000),
        )

        result = await game._process_gameplay_update(update)

        # Game should end
        assert result is True
        assert game.players["player_a"].alive is False
        assert game.players["player_b"].alive is True

        # Winner event should be published
        from lib.types import GameEvent

        winner_events = event_collector.get_events_of_type(GameEvent.GAME_WINNER)
        assert len(winner_events) == 1
        assert winner_events[0]["serial"] == "player_b"


class ScriptedGameplayStream(MockGameplayStream):
    """Gameplay stream that yields scripted frames, one every ``frame_interval_s`` (60 Hz by default)."""

    def __init__(self, frames, frame_interval_s: float = 1 / 60):
        super().__init__()
        self._frames = list(frames)
        self._frame_interval_s = frame_interval_s
        # Frames still unconsumed when each message was written.
        self.frames_left_at_write: list[int] = []

    async def write(self, message):
        self.frames_left_at_write.append(len(self._frames))
        await super().write(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio

        if not self._frames:
            raise StopAsyncIteration
        await asyncio.sleep(self._frame_interval_s)
        return self._frames.pop(0)


class TestDisconnectDuringWarmup:
    """#1582: a disconnect in the ~100 ms EMA warmup after the countdown still eliminates the player."""

    STILL = controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)

    def _game(self, serials):
        event_collector = EventCollector()
        game = FFAGame(
            controller_manager_client=MockControllerManagerService(num_controllers=len(serials)),
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_warmup_disconnect",
        )
        game.running = True
        for serial in serials:
            game.players[serial] = Player(serial=serial, team=0, alive=True, color=(255, 0, 0))
        return game, event_collector

    def _frames(self, serials, disconnected: str, count: int = 20):
        """First frame carries the one-shot disconnect (controller-manager drains it once); the rest are still."""
        frames = []
        for i in range(count):
            alive = [s for s in serials if i == 0 or s != disconnected]
            frames.append(
                controller_manager_pb2.GameplayDataUpdate(
                    controllers=[controller_manager_pb2.GameplayData(serial=s, accel=self.STILL) for s in alive],
                    disconnects=[controller_manager_pb2.DisconnectEvent(serial=disconnected)] if i == 0 else [],
                    timestamp=int(time.time() * 1000),
                )
            )
        return frames

    async def _run_warmup_then_loop(self, game, frames) -> tuple[int, int]:
        """Returns the frames left after warmup and after the game loop."""
        game.gameplay_stream = ScriptedGameplayStream(frames)
        await game._warmup_ema()
        left_after_warmup = len(game.gameplay_stream._frames)
        assert left_after_warmup, "warmup must leave frames for the game loop"
        assert left_after_warmup < len(frames) - 1, "the disconnect frame must fall in warmup"
        await game._game_loop()
        return left_after_warmup, len(game.gameplay_stream._frames)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2")
    @pytest.mark.uc("UC-030", flow="A5")
    async def test_disconnect_during_warmup_eliminates_player(self):
        """3 players: the controller that disconnects during warmup is dead once the game loop runs."""
        serials = ["p0", "p1", "p2"]
        game, events = self._game(serials)
        from lib.types import GameEvent

        left_after_warmup, _ = await self._run_warmup_then_loop(game, self._frames(serials, disconnected="p0"))

        assert game.players["p0"].alive is False
        assert game.players["p1"].alive is True
        assert game.players["p2"].alive is True
        assert [e["serial"] for e in events.get_events_of_type(GameEvent.PLAYER_DEATH)] == ["p0"]
        # The alive filter drops p0 before the loop reads its first frame.
        stream = game.gameplay_stream
        writes = zip(stream.messages, stream.frames_left_at_write, strict=True)
        filter_writes = [(msg, left) for msg, left in writes if msg.HasField("filter_update")]
        assert filter_writes, "an alive-filter FilterUpdate must be sent"
        first_filter, frames_left = filter_writes[0]
        assert set(first_filter.filter_update.serials) == {"p1", "p2"}
        assert frames_left == left_after_warmup

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2", br=["BR-001"])
    async def test_disconnect_during_warmup_ends_two_player_game(self):
        """2 players: a disconnect during warmup ends the game with the other player winning."""
        serials = ["p0", "p1"]
        game, events = self._game(serials)
        from lib.types import GameEvent

        left_after_warmup, left_after_loop = await self._run_warmup_then_loop(
            game, self._frames(serials, disconnected="p0")
        )

        assert game.players["p0"].alive is False
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "p1"}]
        # The game ended before the first gameplay frame was read.
        assert game.start_time is None
        assert left_after_loop == left_after_warmup

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A2", br=["BR-003"])
    async def test_force_end_before_loop_ignores_warmup_disconnect(self):
        """A game force-ended during the countdown or warmup stays force_ended despite a buffered disconnect."""
        serials = ["p0", "p1"]
        game, events = self._game(serials)
        from lib.types import GameEvent

        game.force_end("test")
        game.gameplay_stream = ScriptedGameplayStream(self._frames(serials, disconnected="p0"))
        await game._warmup_ema()
        await game._game_loop()

        assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == []
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
        assert game.game_outcome() == "force_ended"
