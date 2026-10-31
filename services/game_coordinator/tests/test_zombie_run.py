"""Zombies ``run()`` start sequence (UC-023 step 1, BR-002, Pre-1; #1273, D-G3).

Zombies opens the gameplay stream before its intro (the role colours go over
it), drains the stream during the intro, primes the EMA after the countdown
(UC-030 BR-003), starts the game timer only after the warm-up and sets the
game's flag evaluation context like the base start. It has no tempo loop by
design (BR-002).

The run tests use #1912's queue stream, which produces a frame every 10 ms, so
the drain stops by ``break`` and never reopens the stream. They never patch
``game_clock.sleep``: the intro length is a module constant and the countdown is
skipped through the runtime config.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature import api
from openfeature import transaction_context as tc

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from run_fakes import FirstStreamDeadControllerManager
from run_fakes import event_order as _event_order
from run_fakes import name as _name
from test_random_teams_formation import (
    SERIALS,
    QueueControllerManager,
    QueueStream,
    RecordingLog,
    _players,
    _stop,
    _wait_for,
)

from lib.feature_flags import set_game_session_kind_context
from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games import base as base_mod
from services.game_coordinator.games import zombie as zombie_mod
from services.game_coordinator.games.zombie import HUMAN_COLOR, ZOMBIE_COLOR, ZombieGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

INTRO_S = 0.3


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, a short rainbow and a short intro."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(zombie_mod, "ZOMBIE_INTRO_DURATION_S", INTRO_S),
    ):
        yield config


def _zombie(log, client, *, players=None) -> ZombieGame:
    return ZombieGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_zombie_run",
        initial_players=players if players is not None else _players(),
        rng_seed=7,
    )


def _rgb(color) -> tuple[int, int, int]:
    return (color.r, color.g, color.b)


@pytest.mark.uc("UC-023", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_intro_goes_over_one_stream_in_the_zombie_event_order():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.GAME_STARTED) == 1)
    await _stop(game, task, client)

    assert len(client.streams) == 1, "one stream for the whole game, never reopened"
    order = _event_order(log)
    expected = [
        GameEvent.GAME_STARTING,
        GameEvent.PLAYERS_INITIALIZED,
        "zombie_intro_start",
        "zombie_intro_end",
        GameEvent.COUNTDOWN_START,
        GameEvent.COUNTDOWN_END,
        GameEvent.GAME_STARTED,
    ]
    positions = [order.index(_name(event)) for event in expected]
    assert positions == sorted(positions), order

    config_at = log.writes("config")[0][0]
    intro_start = log.index_of_event("zombie_intro_start")
    countdown = log.index_of_event(GameEvent.COUNTDOWN_START)
    assert config_at < intro_start, "the stream opens before the intro"
    intro_colors = {e.serial: _rgb(e.color) for i, e in log.writes("base_color") if intro_start < i < countdown}
    assert game.zombie_serials and game.human_serials
    assert intro_colors == {
        **{s: tuple(ZOMBIE_COLOR) for s in game.zombie_serials},
        **{s: tuple(HUMAN_COLOR) for s in game.human_serials},
    }


class SpikeDuringIntroStream(QueueStream):
    """Every human sends a large spike while ``spiking`` is set (the intro)."""

    spiking = False
    spike_serials: list[str] = []

    async def _produce(self):
        while True:
            controllers = [
                controller_manager_pb2.GameplayData(
                    serial=serial,
                    accel=controller_manager_pb2.Vector3(
                        x=0.0, y=0.0, z=9.0 if self.spiking and serial in self.spike_serials else 1.0
                    ),
                )
                for serial in SERIALS
            ]
            await self.queue.put(controller_manager_pb2.GameplayDataUpdate(controllers=controllers, timestamp=self.seq))
            self.seq += 1
            await asyncio.sleep(0.01)


class SpikeControllerManager(QueueControllerManager):
    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = SpikeDuringIntroStream(self.log)
        self.streams.append(stream)
        return stream


@pytest.mark.real_game_time
@pytest.mark.uc("UC-023", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_spikes_from_the_intro_never_reach_warmup_or_game_loop():
    log = RecordingLog()
    client = SpikeControllerManager(log)
    game = _zombie(log, client)
    seen: list[int] = []
    marks: dict[str, int] = {}
    real_publish = game.event_publisher

    async def publish(event_type, data):
        if event_type == "zombie_intro_start":
            stream = client.streams[0]
            stream.spike_serials = list(game.human_serials)
            stream.spiking = True
        if event_type == "zombie_intro_end":
            client.streams[0].spiking = False
            marks["intro_end_seq"] = client.streams[0].seq
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
    await _wait_for(lambda: len(seen) >= 40)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert marks["intro_end_seq"] > 10, "the intro produced frames"
    assert min(seen) >= marks["intro_end_seq"], "an intro frame reached warmup or the game loop"
    assert log.events.count_events_of_type("human_converted") == 0
    assert len(game.human_serials) == 2


@pytest.mark.uc("UC-023", flow="main", steps=[1, 2])
@pytest.mark.asyncio
async def test_warmup_runs_after_the_countdown_and_before_the_game_loop_and_the_timer():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client)
    calls: list[str] = []
    real_warmup = game._warmup_ema

    async def warmup():
        assert log.events.count_events_of_type(GameEvent.COUNTDOWN_END) == 1, "warmup before the countdown end"
        calls.append("warmup")
        await real_warmup()
        calls.append("warmup_done")

    game._warmup_ema = warmup
    real_timer = game._game_timer

    async def timer():
        calls.append("timer")
        await real_timer()

    game._game_timer = timer
    real_process = game._process_gameplay_update

    async def process(update):
        calls.append("process")
        return await real_process(update)

    game._process_gameplay_update = process
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: calls.count("process") >= 5 and "timer" in calls)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert calls[0] == "warmup" and calls.count("warmup") == 1, calls[:5]
    # The game timer counts from gameplay start, after the warm-up (step 2).
    assert calls.count("timer") == 1
    assert calls.index("warmup_done") < calls.index("timer"), calls[:5]
    assert calls.index("warmup_done") < calls.index("process"), calls[:5]


@pytest.mark.uc("UC-023", flow="main", steps=[1, 3])
@pytest.mark.asyncio
async def test_disconnect_read_by_the_warmup_converts_the_human():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client)
    victim: list[str] = []
    real_warmup = game._warmup_ema

    async def warmup():
        stream = game.gameplay_stream
        victim.append(game.human_serials[0])

        class DisconnectOnFirstFrame:
            injected = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                update = await stream.__anext__()
                if not self.injected:
                    self.injected = True
                    update.disconnects.append(controller_manager_pb2.DisconnectEvent(serial=victim[0]))
                return update

        game.gameplay_stream = DisconnectOnFirstFrame()
        try:
            await real_warmup()
        finally:
            game.gameplay_stream = stream

    game._warmup_ema = warmup
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type("human_converted") == 1)
    await _stop(game, task, client)

    assert game.players[victim[0]].is_zombie
    assert victim[0] not in game.human_serials
    assert len(client.streams) == 1


@pytest.mark.uc("UC-023", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_stream_dead_before_the_intro_writes_reopens_instead_of_ending_the_game():
    """The intro colour write errors are logged; the drain stop reopens the stream."""
    log = RecordingLog()
    client = FirstStreamDeadControllerManager(log)
    game = _zombie(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1 or task.done())
    await _stop(game, task, client)

    assert len(client.streams) == 2, "the broken stream is reopened once"
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1
    assert log.events.count_events_of_type(GameEvent.GAME_ERROR) == 0
    assert len(log.writes("config")) == 2


@pytest.mark.uc("UC-023", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_early_stream_is_not_reopened_when_the_intro_raises():
    """``reopen=phases_completed``: a failed intro leaves the dead stream closed (one stream only)."""
    log = RecordingLog()
    client = FirstStreamDeadControllerManager(log)
    game = _zombie(log, client)

    class FailingIntro:
        name = "failing_intro"

        async def execute(self):
            await asyncio.sleep(0.1)  # the drain meets the dead stream first
            raise RuntimeError("intro failed")

    game._get_additional_phases = lambda: [FailingIntro()]

    with pytest.raises(RuntimeError, match="intro failed"):
        await asyncio.wait_for(game.run(), timeout=10)

    assert len(client.streams) == 1, "no reopen after a failed phase"
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 0


@pytest.mark.uc("UC-023", br=["BR-002"], cond=["Pre-1"])
@pytest.mark.asyncio
async def test_one_player_raises_before_the_stream_opens():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client, players=_players()[:1])

    with pytest.raises(ValueError, match="at least 2 players"):
        await asyncio.wait_for(game.run(), timeout=10)

    assert client.streams == []
    order = _event_order(log)
    assert order[0] == _name(GameEvent.GAME_STARTING)
    assert _name(GameEvent.GAME_STARTED) not in order
    assert _name(GameEvent.GAME_ERROR) not in order, "the session publishes game_error once"


@pytest.mark.uc("UC-023", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_force_end_during_the_intro_stops_the_drain_without_a_reopen():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(zombie_mod, "ZOMBIE_INTRO_DURATION_S", 5.0):
        game = _zombie(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type("zombie_intro_start") == 1)
        drain = game._early_drain_task
        assert drain is not None and not drain.done()
        await _stop(game, task, client)

    assert len(client.streams) == 1
    # The stop in run()'s finally ended it; a cancel from cleanup() would cancel the call.
    assert drain.done() and not drain.cancelled()
    assert log.events.count_events_of_type(GameEvent.GAME_STARTED) == 0
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 0
    assert game.timer_task is None
    assert game.force_ended
    assert game._game_outcome_attributes()["game.outcome"] == "force_ended"


@pytest.mark.uc("UC-023", br=["BR-002"])
@pytest.mark.asyncio
async def test_no_tempo_loop_and_a_tempo_override_has_no_effect():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.GAME_STARTED) == 1)
    human = game.players[game.human_serials[0]]
    zombie = game.players[game.zombie_serials[0]]
    at_start = (game._compute_effective_thresholds(human), game._compute_effective_thresholds(zombie))
    game.tempo_override = 2.0  # what the adjust_music_tempo intervention writes
    await asyncio.sleep(0.2)

    assert game.music_loop_task is None
    assert (game._compute_effective_thresholds(human), game._compute_effective_thresholds(zombie)) == at_start
    await _stop(game, task, client)
    assert len(client.streams) == 1


@pytest.mark.uc("UC-023", br=["BR-002"])
@pytest.mark.asyncio
async def test_shadow_zombies_adds_the_game_attributes_to_the_session_context():
    """The session set game_kind / experiment already; the start adds game_mode and the rest."""
    previous_propagator = tc._evaluation_transaction_context_propagator
    api.set_transaction_context_propagator(tc.ContextVarsTransactionContextPropagator())
    try:
        await _run_shadow_zombie_context_check()
    finally:
        api.set_transaction_context_propagator(previous_propagator)


async def _run_shadow_zombie_context_check():
    log = RecordingLog()
    client = QueueControllerManager(log)
    contexts: dict[str, object] = {}

    async def session():
        set_game_session_kind_context("shadow", experiment_id="exp_1", arm="b")
        game = _zombie(log, client)
        game.game_kind = base_mod.GAME_KIND_SHADOW
        game.experiment_id, game.arm = "exp_1", "b"
        contexts["before"] = api.get_transaction_context()
        real_stream = game._start_gameplay_stream

        async def start_stream():
            contexts["stream"] = api.get_transaction_context()
            await real_stream()

        game._start_gameplay_stream = start_stream
        real_timer = game._game_timer

        async def timer():
            contexts["timer"] = api.get_transaction_context()
            await real_timer()

        game._game_timer = timer
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: "timer" in contexts)
        await _stop(game, task, client)
        assert len(client.streams) == 1

    await asyncio.create_task(session())

    before = contexts["before"]
    assert before.attributes["game_kind"] == "shadow"
    assert before.attributes["experiment_id"] == "exp_1"
    assert "game_mode" not in before.attributes and before.targeting_key is None
    for when in ("stream", "timer"):
        ctx = contexts[when]
        assert ctx.targeting_key == "Zombie", when
        assert ctx.attributes["game_mode"] == "Zombie", when
        assert ctx.attributes["controller_count"] == len(SERIALS), when
        assert ctx.attributes["game_kind"] == "shadow", when
        assert ctx.attributes["experiment_id"] == "exp_1", when


def _spy_run_end_cleanup(game):
    """Spy the shield-span close and the analytics clear, calling through."""
    calls: list[str] = []
    real_clear = base_mod.metrics.clear_session_player_analytics
    real_close = game._close_shield_charge_spans

    def clear(*args, **kwargs):
        calls.append("clear_analytics")
        return real_clear(*args, **kwargs)

    def close(*args, **kwargs):
        calls.append("close_shield_spans")
        return real_close(*args, **kwargs)

    return calls, [
        patch.object(base_mod.metrics, "clear_session_player_analytics", side_effect=clear),
        patch.object(game, "_close_shield_charge_spans", side_effect=close),
    ]


@pytest.mark.uc("UC-023", br=["BR-006"])
@pytest.mark.asyncio
async def test_force_end_runs_the_shared_run_end_cleanup():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client)
    calls, patches = _spy_run_end_cleanup(game)
    for p in patches:
        p.start()
    try:
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type(GameEvent.GAME_STARTED) == 1)
        await _stop(game, task, client)
    finally:
        for p in patches:
            p.stop()

    assert "close_shield_spans" in calls, calls
    assert "clear_analytics" in calls, calls
    assert game.force_ended


@pytest.mark.uc("UC-023", br=["BR-006"])
@pytest.mark.asyncio
async def test_natural_end_runs_the_shared_run_end_cleanup_and_still_cancels_the_timer():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _zombie(log, client)
    calls, patches = _spy_run_end_cleanup(game)

    async def instant_loop():
        await asyncio.sleep(0.05)

    for p in patches:
        p.start()
    try:
        with patch.object(game, "_game_loop", side_effect=instant_loop):
            await asyncio.wait_for(game.run(), timeout=10)
    finally:
        for p in patches:
            p.stop()
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()

    assert "close_shield_spans" in calls, calls
    assert "clear_analytics" in calls, calls
    assert game.timer_task is None or game.timer_task.done()
