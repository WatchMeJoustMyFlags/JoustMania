"""Traitor signal over the gameplay stream (UC-022 main step 2, BR-003, D-G10, #1738).

Traitor opens the gameplay stream before its traitor_signal phase, so the team
pulse, the persistent team colour and the traitor rumble reach the controllers,
and drains the stream until the start (same mechanism as UC-021 A1).
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games import traitor as traitor_mod
from services.game_coordinator.games.traitor import TraitorGame

# 6 players: 2 traitors and 4 loyal players with the default count tiers.
SERIALS = [f"mock_controller_{i}" for i in range(6)]


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

    A producer task numbers each frame (``timestamp`` = sequence).
    """

    def __init__(self, log: RecordingLog):
        self.log = log
        self.queue: asyncio.Queue = asyncio.Queue()
        self.seq = 0
        self.producer: asyncio.Task | None = None

    async def write(self, message):
        self.log.entries.append(("write", message))
        if message.HasField("config") and self.producer is None:
            self.producer = asyncio.create_task(self._produce())

    async def _produce(self):
        while True:
            controllers = [
                controller_manager_pb2.GameplayData(serial=s, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0))
                for s in SERIALS
            ]
            await self.queue.put(controller_manager_pb2.GameplayDataUpdate(controllers=controllers, timestamp=self.seq))
            self.seq += 1
            await asyncio.sleep(0.01)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class QueueControllerManager(MockControllerManagerService):
    def __init__(self, log):
        super().__init__(num_controllers=len(SERIALS), infinite=True)
        self.log = log
        self.streams: list[QueueStream] = []

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = QueueStream(self.log)
        self.streams.append(stream)
        return stream


def _dead_stream_error() -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, None, None, "stream broke")


class DyingStream(QueueStream):
    """Gameplay stream that breaks ``die_after`` seconds after its config write.

    ``die_after=None`` breaks it at the config write itself, before any effect.
    Once broken, reads and writes raise and ``done()`` is True.
    """

    def __init__(self, log: RecordingLog, die_after: float | None):
        super().__init__(log)
        self.die_after = die_after
        self.dead = False

    def done(self):
        return self.dead

    def cancel(self):
        self.dead = True

    async def write(self, message):
        if self.dead:
            raise _dead_stream_error()
        await super().write(message)
        if message.HasField("config"):
            if self.die_after is None:
                self._die()
            else:
                asyncio.get_running_loop().call_later(self.die_after, self._die)

    def _die(self):
        self.dead = True
        self.queue.put_nowait(None)

    async def __anext__(self):
        if self.dead:
            raise _dead_stream_error()
        item = await self.queue.get()
        if item is None:
            raise _dead_stream_error()
        return item


class DyingFirstStreamControllerManager(QueueControllerManager):
    """The first gameplay stream breaks; every reopened stream is healthy."""

    def __init__(self, log, die_after: float | None):
        super().__init__(log)
        self.die_after = die_after

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = DyingStream(self.log, self.die_after) if not self.streams else QueueStream(self.log)
        self.streams.append(stream)
        return stream


async def _wait_for(predicate):
    deadline = time.monotonic() + 10.0
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _traitor(log, client) -> TraitorGame:
    return TraitorGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_traitor_signal_stream",
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


@pytest.mark.uc("UC-022", flow="main", steps=[2], br=["BR-003"])
@pytest.mark.asyncio
async def test_traitor_signal_goes_over_the_stream_before_the_countdown():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(traitor_mod, "TRAITOR_SIGNAL_DURATION", 1):
        game = _traitor(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1)
        await _stop(game, task, client)

    assert len(client.streams) == 1, "one stream for the whole game"
    signal_start = log.index_of_event("traitor_signal_start")
    signal_end = log.index_of_event("traitor_signal_end")
    countdown = log.index_of_event(GameEvent.COUNTDOWN_START)
    assert log.events.get_events_of_type("traitor_signal_start")[0]["traitor_count"] == 2

    config_at, config = log.writes("config")[0]
    assert config_at < signal_start, "the stream opens before the traitor signal"
    assert {c.serial for c in config.colors} == set(SERIALS)
    assert {(c.color.r, c.color.g, c.color.b) for c in config.colors} == {(255, 255, 255)}

    effects = log.writes("game_effect")
    rumbles = [(i, e) for i, e in effects if e.effect == controller_manager_pb2.GAME_EFFECT_RUMBLE]
    traitors = set(game.traitor_serials)
    assert len(traitors) == 2
    assert sorted(e.serial for _, e in rumbles) == sorted(traitors), "rumble goes to exactly the traitors"
    for i, _ in rumbles:
        assert signal_start < i < signal_end

    team_rgb = {s: game.team_colors[p.team]["rgb"] for s, p in game.players.items()}
    pulses = [(i, e) for i, e in effects if e.effect == controller_manager_pb2.GAME_EFFECT_PULSE]
    assert sorted(e.serial for _, e in pulses) == sorted(SERIALS)
    for i, effect in pulses:
        assert signal_start < i < signal_end
        assert (effect.color.r, effect.color.g, effect.color.b) == team_rgb[effect.serial]
        assert effect.duration_ms == 1000

    last_pulse = max(i for i, _ in pulses)
    held = [(i, c) for i, c in log.writes("base_color") if last_pulse < i < signal_end]
    assert {c.serial for _, c in held} == set(SERIALS)
    for _, colour in held:
        assert (colour.color.r, colour.color.g, colour.color.b) == team_rgb[colour.serial]
    assert signal_end < countdown


@pytest.mark.real_game_time
@pytest.mark.uc("UC-022", flow="main", steps=[2], br=["BR-003"])
@pytest.mark.asyncio
async def test_frames_from_the_traitor_signal_never_reach_warmup_or_game_loop():
    log = RecordingLog()
    client = QueueControllerManager(log)
    seen: list[int] = []
    marks: dict[str, int] = {}

    with patch.object(traitor_mod, "TRAITOR_SIGNAL_DURATION", 1):
        game = _traitor(log, client)
        real_publish = game.event_publisher

        async def publish(event_type, data):
            if event_type == "traitor_signal_end":
                marks["signal_end_seq"] = client.streams[0].seq
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

    assert marks["signal_end_seq"] > 50, "the signal phase produced frames"
    assert min(seen) >= marks["signal_end_seq"], "a signal-phase frame reached warmup or the game loop"


def _event_types(log: RecordingLog) -> list:
    return [value for kind, value in log.entries if kind == "event"]


async def _run_until_countdown_with_dying_stream(die_after: float | None):
    log = RecordingLog()
    client = DyingFirstStreamControllerManager(log, die_after)
    with patch.object(traitor_mod, "TRAITOR_SIGNAL_DURATION", 1):
        game = _traitor(log, client)
        task = asyncio.create_task(game.run())
        try:
            await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1 or task.done())
        finally:
            if not task.done():
                await _stop(game, task, client)
            for stream in client.streams:
                if stream.producer:
                    stream.producer.cancel()
    return log, client, game


def _assert_reopened_once_with_team_colours(log: RecordingLog, client, game):
    types = _event_types(log)
    assert GameEvent.GAME_ERROR not in types
    assert GameEvent.COUNTDOWN_START in types
    assert len(client.streams) == 2, "the broken stream is reopened once"
    configs = [config for _, config in log.writes("config")]
    assert len(configs) == 2
    team_rgb = {s: game.team_colors[p.team]["rgb"] for s, p in game.players.items()}
    assert {c.serial for c in configs[1].colors} == set(SERIALS)
    for colour in configs[1].colors:
        assert (colour.color.r, colour.color.g, colour.color.b) == team_rgb[colour.serial]


@pytest.mark.uc("UC-022", flow="main", steps=[2], br=["BR-003"])
@pytest.mark.asyncio
async def test_stream_broken_before_the_rumble_reopens_instead_of_ending_the_game():
    """The early stream is dead before the traitor rumble: the rumble write error
    is logged, and the drain stop reopens the stream with the team colours."""
    log, client, game = await _run_until_countdown_with_dying_stream(die_after=None)

    assert log.events.count_events_of_type("traitor_signal_end") == 1
    _assert_reopened_once_with_team_colours(log, client, game)


@pytest.mark.real_game_time
@pytest.mark.uc("UC-022", flow="main", steps=[2], br=["BR-003"])
@pytest.mark.parametrize("die_after", [0.0, 0.3])
@pytest.mark.asyncio
async def test_stream_drop_during_the_signal_reopens_once_with_team_colours(die_after):
    log, client, game = await _run_until_countdown_with_dying_stream(die_after=die_after)

    _assert_reopened_once_with_team_colours(log, client, game)


@pytest.mark.real_game_time
@pytest.mark.uc("UC-022", flow="main", steps=[2], br=["BR-003"])
@pytest.mark.asyncio
async def test_force_end_during_the_signal_stops_without_reopen_or_countdown():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(traitor_mod, "TRAITOR_SIGNAL_DURATION", 3):
        game = _traitor(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type("traitor_signal_start") == 1)
        await asyncio.sleep(0.5)
        game.force_end("test_force_end_during_signal")
        await asyncio.wait_for(task, timeout=5)
    for stream in client.streams:
        if stream.producer:
            stream.producer.cancel()

    types = _event_types(log)
    assert len(client.streams) == 1, "no reopen after a force end"
    assert GameEvent.GAME_ERROR not in types
    assert GameEvent.COUNTDOWN_START not in types
    assert game._early_drain_task is None


@pytest.mark.uc("UC-022", cond=["Pre-1"])
@pytest.mark.asyncio
async def test_one_player_raises_before_the_stream_opens():
    """Pre-1: fewer than two players raises before the stream opens or the signal phase starts."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = TraitorGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_traitor_pre1",
        num_teams=2,
        initial_players=_players()[:1],
        rng_seed=7,
    )

    with pytest.raises(ValueError, match="at least 2 players"):
        await asyncio.wait_for(game.run(), timeout=10)

    assert client.streams == []
    types = _event_types(log)
    assert GameEvent.GAME_STARTED not in types
    assert GameEvent.COUNTDOWN_START not in types
