"""Random Teams formation over the gameplay stream (UC-021 A1, D-G10, #1738).

Random Teams opens the gameplay stream before its formation phase, so the
team-colour pulse reaches the controllers, and drains the stream until the
start. FFA keeps its stream open after colour assignment (UC-020 main step 1).
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games import random_teams as random_teams_mod
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.random_teams import RandomTeamsGame

SERIALS = [f"mock_controller_{i}" for i in range(4)]


def _players():
    return [type("P", (), {"serial": s})() for s in SERIALS]


class RecordingLog:
    """One ordered log of stream writes and published events."""

    def __init__(self):
        self.entries: list[tuple[str, object]] = []
        self.events = EventCollector()

    async def publish(self, event_type, data):
        self.entries.append(("event", event_type))
        await self.events.publish(event_type, data)

    def index_of_event(self, event_type) -> int:
        return next(i for i, (kind, value) in enumerate(self.entries) if kind == "event" and value == event_type)

    def writes(self, field: str) -> list[tuple[int, object]]:
        return [
            (i, getattr(value, field))
            for i, (kind, value) in enumerate(self.entries)
            if kind == "write" and value.HasField(field)
        ]


class QueueStream:
    """Gameplay stream that buffers frames from the moment it opens, like grpc flow control.

    A producer task numbers each frame (``timestamp`` = sequence). ``gaps`` maps a
    serial to the sequence range in which it sends no frames; ``disconnect_at``
    maps a sequence to the serial reported as disconnected in that frame.
    """

    def __init__(self, log: RecordingLog, gaps=None, disconnect_at=None):
        self.log = log
        self.queue: asyncio.Queue = asyncio.Queue()
        self.seq = 0
        self.gaps = gaps or {}
        self.disconnect_at = disconnect_at or {}
        self.producer: asyncio.Task | None = None

    async def write(self, message):
        self.log.entries.append(("write", message))
        if message.HasField("config") and self.producer is None:
            self.producer = asyncio.create_task(self._produce())

    async def _produce(self):
        while True:
            controllers = []
            for serial in SERIALS:
                start, end = self.gaps.get(serial, (None, None))
                if start is not None and start <= self.seq and (end is None or self.seq < end):
                    continue
                controllers.append(
                    controller_manager_pb2.GameplayData(
                        serial=serial, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
                    )
                )
            disconnects = []
            if self.seq in self.disconnect_at:
                disconnects.append(controller_manager_pb2.DisconnectEvent(serial=self.disconnect_at[self.seq]))
            await self.queue.put(
                controller_manager_pb2.GameplayDataUpdate(
                    controllers=controllers, disconnects=disconnects, timestamp=self.seq
                )
            )
            self.seq += 1
            await asyncio.sleep(0.01)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class QueueControllerManager(MockControllerManagerService):
    def __init__(self, log, **stream_kwargs):
        super().__init__(num_controllers=4, infinite=True)
        self.log = log
        self.stream_kwargs = stream_kwargs
        self.streams: list[QueueStream] = []

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = QueueStream(self.log, **self.stream_kwargs)
        self.streams.append(stream)
        return stream


async def _wait_for(predicate):
    deadline = time.monotonic() + 10.0
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _random_teams(log, client) -> RandomTeamsGame:
    return RandomTeamsGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_random_teams_formation",
        num_teams=2,
        initial_players=_players(),
        rng_seed=7,
    )


async def _stop(game, task, client):
    game.force_end("test_done")
    await asyncio.wait_for(task, timeout=10)
    for stream in client.streams:
        if stream.producer:
            stream.producer.cancel()


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
async def test_formation_pulse_goes_over_the_stream_before_the_countdown():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(random_teams_mod, "TEAM_FORMATION_DURATION", 1):
        game = _random_teams(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1)
        await _stop(game, task, client)

    assert len(client.streams) == 1, "one stream for the whole game"
    start = log.events.get_events_of_type("team_formation_start")
    assert start and start[0]["duration"] == 1
    formation_start = log.index_of_event("team_formation_start")
    formation_end = log.index_of_event("team_formation_end")
    countdown = log.index_of_event(GameEvent.COUNTDOWN_START)

    config_at = log.writes("config")[0][0]
    assert config_at < formation_start, "the stream opens before the formation"

    team_rgb = {s: game.team_colors[p.team]["rgb"] for s, p in game.players.items()}
    pulses = [(i, e) for i, e in log.writes("game_effect") if e.effect == controller_manager_pb2.GAME_EFFECT_PULSE]
    assert {e.serial for _, e in pulses} == set(SERIALS)
    for i, effect in pulses:
        assert formation_start < i < formation_end
        assert (effect.color.r, effect.color.g, effect.color.b) == team_rgb[effect.serial]
        assert effect.duration_ms == 1000

    last_pulse = max(i for i, _ in pulses)
    held = [(i, c) for i, c in log.writes("base_color") if last_pulse < i < formation_end]
    assert {c.serial for _, c in held} == set(SERIALS)
    for _, colour in held:
        assert (colour.color.r, colour.color.g, colour.color.b) == team_rgb[colour.serial]
    assert formation_end < countdown


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
async def test_frames_from_the_formation_never_reach_warmup_or_game_loop():
    log = RecordingLog()
    client = QueueControllerManager(log)
    seen: list[int] = []
    marks: dict[str, int] = {}

    with patch.object(random_teams_mod, "TEAM_FORMATION_DURATION", 1):
        game = _random_teams(log, client)
        real_publish = game.event_publisher

        async def publish(event_type, data):
            if event_type == "team_formation_end":
                marks["formation_end_seq"] = client.streams[0].seq
            await real_publish(event_type, data)

        game.event_publisher = publish
        real_process = game._process_gameplay_update

        async def process(update):
            seen.append(update.timestamp)
            return await real_process(update)

        game._process_gameplay_update = process

        real_warmup = game._warmup_ema

        async def warmup():
            stream = game.gameplay_stream

            class Spy:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    update = await stream.__anext__()
                    seen.append(update.timestamp)
                    return update

            game.gameplay_stream = Spy()
            try:
                await real_warmup()
            finally:
                game.gameplay_stream = stream

        game._warmup_ema = warmup
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: len(seen) >= 20)
        await _stop(game, task, client)

    assert marks["formation_end_seq"] > 50, "the formation produced frames"
    assert min(seen) >= marks["formation_end_seq"], "a formation frame reached warmup or the game loop"


async def _run_with_formation_disconnect(gaps, disconnect_seq=5):
    log = RecordingLog()
    client = QueueControllerManager(log, gaps=gaps, disconnect_at={disconnect_seq: SERIALS[0]})
    calls: list[tuple[str, bool]] = []
    with patch.object(random_teams_mod, "TEAM_FORMATION_DURATION", 1):
        game = _random_teams(log, client)
        real_disconnect = game._handle_disconnect

        async def handle_disconnect(serial):
            countdown_done = log.events.count_events_of_type(GameEvent.COUNTDOWN_END) == 1
            calls.append((serial, countdown_done and game.start_time is None))
            await real_disconnect(serial)

        game._handle_disconnect = handle_disconnect
        frames: list[int] = []
        real_process = game._process_gameplay_update

        async def process(update):
            frames.append(update.timestamp)
            return await real_process(update)

        game._process_gameplay_update = process
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: len(frames) >= 10)
        await _stop(game, task, client)
    return game, calls


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
async def test_disconnect_during_formation_eliminates_once_at_loop_start():
    game, calls = await _run_with_formation_disconnect(gaps={SERIALS[0]: (5, None)})

    assert calls == [(SERIALS[0], True)]
    assert not game.players[SERIALS[0]].alive


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
async def test_drop_and_reconnect_during_formation_is_not_eliminated():
    game, calls = await _run_with_formation_disconnect(gaps={SERIALS[0]: (5, 15)})

    assert calls == []
    assert game.players[SERIALS[0]].alive


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
@pytest.mark.real_durations
async def test_drop_and_reconnect_during_the_countdown_is_still_eliminated():
    # 1 s formation is ~100 frames; seq 150 lies in the countdown, after the drain stopped.
    game, calls = await _run_with_formation_disconnect(gaps={SERIALS[0]: (150, 160)}, disconnect_seq=150)

    assert calls == [(SERIALS[0], True)]
    assert not game.players[SERIALS[0]].alive


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
async def test_force_end_during_formation_ends_the_drain():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _random_teams(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type("team_formation_start") == 1)
    drain = game._early_drain_task
    assert drain is not None and not drain.done()

    started = time.monotonic()
    await _stop(game, task, client)

    assert time.monotonic() - started < 2.0
    # The stop in run()'s finally ended it; a cancel from cleanup() would cancel the call.
    assert drain.done() and not drain.cancelled()
    assert game._tasks == set()
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 0
    assert game.force_ended


@pytest.mark.uc("UC-021", flow="A1")
@pytest.mark.asyncio
async def test_cancelling_the_drain_is_not_swallowed():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _random_teams(log, client)
    await game._initialize_players()
    await game._start_gameplay_stream()
    game._start_early_stream_drain()
    drain = game._early_drain_task
    await asyncio.sleep(0.05)

    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert drain.cancelled()
    client.streams[0].producer.cancel()


@pytest.mark.uc("UC-020", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_ffa_opens_the_stream_after_colour_assignment_with_the_assigned_colours():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = FFAGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_ffa_initial_colours",
        initial_players=_players(),
    )
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1)
    await _stop(game, task, client)

    assert game._opens_stream_before_additional_phases() is False
    config = log.writes("config")[0][1]
    sent = {c.serial: (c.color.r, c.color.g, c.color.b) for c in config.colors}
    assert sent == {s: tuple(p.color) for s, p in game.players.items()}
    assert all(colour != (255, 255, 255) for colour in sent.values())
