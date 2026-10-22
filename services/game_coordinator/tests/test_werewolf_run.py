"""Werewolf ``run()`` start sequence (UC-022 A1, BR-001, Pre-1; #1273, D-G3).

Werewolf opens the gameplay stream before its intro (the werewolf rumble goes
over it), drains the stream during the intro, primes the EMA after the countdown
(UC-030 BR-003) and sets the game's flag evaluation context like the base start.
It has no tempo loop by design (BR-001).

The run tests use #1912's queue stream, which produces a frame every 10 ms, so
the drain stops by ``break`` and never reopens the stream. They never patch
``asyncio.sleep``: the intro length is a module constant and the countdown is
skipped through the runtime config.
"""

import asyncio
import inspect
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
    RecordingLog,
    _players,
    _stop,
    _wait_for,
)

from lib.feature_flags import set_game_session_kind_context
from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games import base as base_mod
from services.game_coordinator.games import werewolf as werewolf_mod
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

INTRO_S = 0.3


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, a short rainbow and a short intro."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(werewolf_mod, "WEREWOLF_INTRO_DURATION_S", INTRO_S),
    ):
        yield config


def _werewolf(log, client, *, players=None, reveal_time=30.0) -> WerewolfGame:
    return WerewolfGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_werewolf_run",
        initial_players=players if players is not None else _players(),
        reveal_time_seconds=reveal_time,
        rng_seed=7,
    )


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_intro_goes_over_one_stream_in_the_werewolf_event_order():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, reveal_time=0.05)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type("werewolf_reveal") == 1)
    await _stop(game, task, client)

    assert len(client.streams) == 1, "one stream for the whole game, never reopened"
    order = _event_order(log)
    expected = [
        GameEvent.GAME_STARTING,
        GameEvent.PLAYERS_INITIALIZED,
        "werewolf_intro_start",
        "werewolf_intro_end",
        GameEvent.COUNTDOWN_START,
        GameEvent.COUNTDOWN_END,
        GameEvent.GAME_STARTED,
        "werewolf_reveal",
    ]
    positions = [order.index(_name(event)) for event in expected]
    assert positions == sorted(positions), order

    config_at = log.writes("config")[0][0]
    intro_start = log.index_of_event("werewolf_intro_start")
    countdown = log.index_of_event(GameEvent.COUNTDOWN_START)
    assert config_at < intro_start, "the stream opens before the intro"
    rumbles = [(i, e) for i, e in log.writes("game_effect") if e.effect == controller_manager_pb2.GAME_EFFECT_RUMBLE]
    assert sorted(e.serial for _, e in rumbles) == sorted(game.werewolf_serials)
    assert game.human_serials, "the seed leaves humans to check against"
    assert all(intro_start < i < countdown for i, _ in rumbles)


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_frames_from_the_intro_never_reach_warmup_or_game_loop():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client)
    seen: list[int] = []
    marks: dict[str, int] = {}
    real_publish = game.event_publisher

    async def publish(event_type, data):
        if event_type == "werewolf_intro_end":
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
    await _wait_for(lambda: len(seen) >= 20)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert marks["intro_end_seq"] > 10, "the intro produced frames"
    assert min(seen) >= marks["intro_end_seq"], "an intro frame reached warmup or the game loop"
    assert all(p.alive for p in game.players.values())


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_warmup_runs_after_the_countdown_and_before_the_game_loop():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, reveal_time=0.01)
    calls: list[str] = []
    real_warmup = game._warmup_ema

    async def warmup():
        assert log.events.count_events_of_type(GameEvent.COUNTDOWN_END) == 1, "warmup before the countdown end"
        calls.append("warmup")
        await real_warmup()
        calls.append("warmup_done")

    game._warmup_ema = warmup
    real_reveal = game._reveal_werewolves

    async def reveal():
        calls.append("reveal")
        await real_reveal()

    game._reveal_werewolves = reveal
    real_process = game._process_gameplay_update

    async def process(update):
        calls.append("process")
        return await real_process(update)

    game._process_gameplay_update = process
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: calls.count("process") >= 5)
    await _wait_for(lambda: log.events.count_events_of_type("werewolf_reveal") == 1)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert calls[0] == "warmup" and calls.count("warmup") == 1, calls[:5]
    # The reveal timer counts from gameplay start, after the warm-up (A1 step 2).
    assert calls.count("reveal") == 1
    assert calls.index("warmup_done") < calls.index("reveal"), calls[:5]


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_disconnect_read_by_the_warmup_eliminates_the_player():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client)
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
    await _wait_for(lambda: bool(victim) and not game.players[victim[0]].alive)
    await _stop(game, task, client)

    assert not game.players[victim[0]].alive
    assert len(client.streams) == 1


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_stream_dead_before_the_intro_writes_reopens_instead_of_ending_the_game():
    """The intro colour and rumble write errors are logged; the drain stop reopens the stream."""
    log = RecordingLog()
    client = FirstStreamDeadControllerManager(log)
    game = _werewolf(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1 or task.done())
    await _stop(game, task, client)

    assert len(client.streams) == 2, "the broken stream is reopened once"
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1
    assert log.events.count_events_of_type(GameEvent.GAME_ERROR) == 0
    assert len(log.writes("config")) == 2


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_early_stream_is_not_reopened_when_the_intro_raises():
    """``reopen=phases_completed``: a failed intro leaves the dead stream closed (one stream only)."""
    log = RecordingLog()
    client = FirstStreamDeadControllerManager(log)
    game = _werewolf(log, client)

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


@pytest.mark.uc("UC-022", br=["BR-001"], cond=["Pre-1"])
@pytest.mark.asyncio
async def test_one_player_raises_before_the_stream_opens():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, players=_players()[:1])

    with pytest.raises(ValueError, match="at least 2 players"):
        await asyncio.wait_for(game.run(), timeout=10)

    assert client.streams == []
    order = _event_order(log)
    assert order[0] == _name(GameEvent.GAME_STARTING)
    assert _name(GameEvent.GAME_STARTED) not in order
    assert _name(GameEvent.GAME_ERROR) not in order, "the session publishes game_error once"


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_force_end_during_the_intro_stops_the_drain_without_a_reopen():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(werewolf_mod, "WEREWOLF_INTRO_DURATION_S", 5.0):
        game = _werewolf(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type("werewolf_intro_start") == 1)
        drain = game._early_drain_task
        assert drain is not None and not drain.done()
        await _stop(game, task, client)

    assert len(client.streams) == 1
    # The stop in run()'s finally ended it; a cancel from cleanup() would cancel the call.
    assert drain.done() and not drain.cancelled()
    assert log.events.count_events_of_type(GameEvent.GAME_STARTED) == 0
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 0
    assert game.force_ended
    assert game._game_outcome_attributes()["game.outcome"] == "force_ended"
    # run() returns early here (before teardown_phase): its finally drops the stream.
    assert game.gameplay_stream is None, "the early-return path keeps the stream reference"


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_force_end_during_the_reveal_flash_writes_nothing_after_game_ended():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, reveal_time=0.01)
    task = asyncio.create_task(game.run())
    await _wait_for(
        lambda: any(e.effect == controller_manager_pb2.GAME_EFFECT_FLASH for _, e in log.writes("game_effect"))
    )
    await _stop(game, task, client)
    await asyncio.sleep(0.4)  # past the 0.3 s flash hold

    ended = log.index_of_event("game_ended")
    assert [kind for kind, _ in log.entries[ended + 1 :]] == [], "a write after game_ended"
    assert game.reveal_task.done()


def _spy_teardown(game, calls):
    """Record the teardown steps run() must reach, calling through to the real ones."""
    real_clear = base_mod.metrics.clear_session_player_analytics

    def clear(*args, **kwargs):
        calls.append("clear_analytics")
        return real_clear(*args, **kwargs)

    patches = [patch.object(base_mod.metrics, "clear_session_player_analytics", side_effect=clear)]
    for name in ("_end_shield_feedback", "_close_shield_charge_spans", "_end_game_impl"):
        original = getattr(game, name)

        async def awaiting(*a, _n=name, _o=original, **k):
            calls.append(_n)
            return await _o(*a, **k)

        def plain(*a, _n=name, _o=original, **k):
            calls.append(_n)
            return _o(*a, **k)

        patches.append(
            patch.object(game, name, side_effect=awaiting if inspect.iscoroutinefunction(original) else plain)
        )
    return patches


def _assert_teardown_reached(game, calls):
    assert "clear_analytics" in calls, calls
    assert "_close_shield_charge_spans" in calls, calls
    assert game.gameplay_stream is None, "the stream reference is dropped"


@pytest.mark.uc("UC-022", flow="A1")
@pytest.mark.asyncio
async def test_natural_end_runs_the_base_teardown_steps():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, reveal_time=0.01)
    calls: list[str] = []

    async def instant_loop():
        # A real natural end: every human dies and the win condition latches the result.
        await asyncio.sleep(0.05)
        for serial in game.human_serials:
            game.players[serial].alive = False
        assert await game._check_win_condition()

    patches = _spy_teardown(game, calls)
    with patch.object(game, "_game_loop", side_effect=instant_loop):
        for p in patches:
            p.start()
        try:
            await asyncio.wait_for(game.run(), timeout=10)
        finally:
            for p in patches:
                p.stop()
            for stream in client.streams:
                if stream.producer:
                    stream.producer.cancel()

    _assert_teardown_reached(game, calls)
    assert calls.index("_end_shield_feedback") < calls.index("_end_game_impl"), calls
    assert calls.index("_end_game_impl") < calls.index("clear_analytics"), calls
    # The winner rainbow must go out before the stream reference is dropped.
    rainbow_serials = {
        e.serial for _, e in log.writes("game_effect") if e.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
    }
    assert rainbow_serials == set(game.werewolf_serials), rainbow_serials
    assert game._winner_side == "werewolves"
    assert game._game_outcome_attributes()["game.outcome"] == "completed"


@pytest.mark.uc("UC-022", flow="A1")
@pytest.mark.asyncio
async def test_force_end_runs_the_base_teardown_steps():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, reveal_time=0.01)
    calls: list[str] = []
    patches = _spy_teardown(game, calls)
    for p in patches:
        p.start()
    try:
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type(GameEvent.GAME_STARTED) == 1)
        await _stop(game, task, client)
    finally:
        for p in patches:
            p.stop()

    _assert_teardown_reached(game, calls)
    assert game.force_ended


@pytest.mark.uc("UC-022", cond=["Pre-1"])
@pytest.mark.asyncio
async def test_error_exit_runs_the_base_teardown_steps():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client, players=_players()[:1])
    calls: list[str] = []
    patches = _spy_teardown(game, calls)
    for p in patches:
        p.start()
    try:
        with pytest.raises(ValueError, match="at least 2 players"):
            await asyncio.wait_for(game.run(), timeout=10)
    finally:
        for p in patches:
            p.stop()

    _assert_teardown_reached(game, calls)


@pytest.mark.uc("UC-022", br=["BR-001"])
@pytest.mark.asyncio
async def test_no_tempo_loop_and_the_slow_table_holds_during_play():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _werewolf(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.GAME_STARTED) == 1)
    human = game.players[game.human_serials[0]]
    at_start = game._compute_effective_thresholds(human)
    await asyncio.sleep(0.2)

    assert game.music_loop_task is None
    assert game._compute_effective_thresholds(human) == at_start
    await _stop(game, task, client)


@pytest.mark.uc("UC-022", br=["BR-001"])
@pytest.mark.asyncio
async def test_shadow_werewolf_adds_the_game_attributes_to_the_session_context():
    """The session set game_kind / experiment already; the start adds game_mode and the rest."""
    previous_propagator = tc._evaluation_transaction_context_propagator
    api.set_transaction_context_propagator(tc.ContextVarsTransactionContextPropagator())
    try:
        await _run_shadow_werewolf_context_check()
    finally:
        api.set_transaction_context_propagator(previous_propagator)


async def _run_shadow_werewolf_context_check():
    log = RecordingLog()
    client = QueueControllerManager(log)
    contexts: dict[str, object] = {}

    async def session():
        set_game_session_kind_context("shadow", experiment_id="exp_1", arm="b")
        game = _werewolf(log, client, reveal_time=0.05)
        game.game_kind = base_mod.GAME_KIND_SHADOW
        game.experiment_id, game.arm = "exp_1", "b"
        contexts["before"] = api.get_transaction_context()
        real_stream = game._start_gameplay_stream

        async def start_stream():
            contexts["stream"] = api.get_transaction_context()
            await real_stream()

        game._start_gameplay_stream = start_stream
        real_reveal = game._reveal_werewolves

        async def reveal():
            contexts["reveal"] = api.get_transaction_context()
            await real_reveal()

        game._reveal_werewolves = reveal
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type("werewolf_reveal") == 1)
        await _stop(game, task, client)

    await asyncio.create_task(session())

    before = contexts["before"]
    assert before.attributes["game_kind"] == "shadow"
    assert before.attributes["experiment_id"] == "exp_1"
    assert "game_mode" not in before.attributes and before.targeting_key is None
    for when in ("stream", "reveal"):
        ctx = contexts[when]
        assert ctx.targeting_key == "Werewolf", when
        assert ctx.attributes["game_mode"] == "Werewolf", when
        assert ctx.attributes["controller_count"] == len(SERIALS), when
        assert ctx.attributes["game_kind"] == "shadow", when
        assert ctx.attributes["experiment_id"] == "exp_1", when


@pytest.mark.uc("UC-020", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_ffa_sets_the_flag_context_before_the_stream_opens():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = FFAGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_ffa_flag_context",
        initial_players=_players(),
    )
    calls: list[str] = []
    real_set = base_mod.set_game_transaction_context

    def set_context(**kwargs):
        calls.append("context")
        real_set(**kwargs)

    real_stream = game._start_gameplay_stream

    async def start_stream():
        calls.append("stream")
        await real_stream()

    game._start_gameplay_stream = start_stream
    with patch.object(base_mod, "set_game_transaction_context", side_effect=set_context):
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1)
        await _stop(game, task, client)

    assert calls == ["context", "stream"]
