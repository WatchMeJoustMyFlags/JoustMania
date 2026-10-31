"""
Intervention side effects run on the owning game's event loop (UC-040 A26 / BR-018, #1250).

Each game runs on its own thread + asyncio loop (``GameSession``) and its gRPC
clients and gameplay stream belong to that loop. The ``InterventionManager``
runs on the servicer's main loop. Before #1250 it awaited the game's clients
directly, and ``volume_override`` ended in ``handler_error`` with "Future
attached to a different loop". These tests run a game loop in a real thread
(like ``GameSession._run_game_loop_threaded``) with clients bound to it, and
drive the manager from the pytest loop.
"""

import asyncio
import threading
import time
from unittest.mock import patch

import pytest
from openfeature.flag_evaluation import FlagEvaluationDetails

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.game_loop_dispatch import (
    REASON_LOOP_LATE,
    REASON_LOOP_TIMEOUT,
    REASON_LOOP_UNAVAILABLE,
    GameLoopDispatcher,
    GameLoopUnavailableError,
)
from services.game_coordinator.games.base import GAME_VOLUME
from services.game_coordinator.interventions import InterventionManager, SessionView
from services.game_coordinator.tests.conftest import FakeInterventionGame
from services.game_coordinator.tests.event_list_helpers import edge_event, object_details

UC040_A26 = pytest.mark.uc("UC-040", flow="A26", br=["BR-018"])

ALLOWED = "adjust_volume,play_audio_cue,send_controller_effect,end_game,eliminate_player"


# --------------------------------------------------------------------------- #
# A game loop in a thread, and clients bound to it (the grpc.aio mechanism)
# --------------------------------------------------------------------------- #
class GameLoopThread:
    """Runs an asyncio loop in a daemon thread, like GameSession does."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._main, daemon=True)
        self.thread.start()
        assert self._ready.wait(2.0)

    def _main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()
        # GameSession's shutdown order: cancel what is left, then close.
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.wait(pending, timeout=1.0))
        self.loop.close()

    def call(self, fn):
        """Run sync ``fn()`` on the game loop and return its result."""
        done = threading.Event()
        box = {}

        def _run():
            box["value"] = fn()
            done.set()

        self.loop.call_soon_threadsafe(_run)
        assert done.wait(2.0)
        return box["value"]

    def stop(self):
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3.0)
        assert not self.thread.is_alive()


async def _complete_on(loop):
    """Await a pending future of ``loop``, completed by ``loop`` itself.

    From ``loop`` this completes on the next iteration. From any other loop the
    future is still pending when awaited, so asyncio raises its real "Future
    attached to a different loop" error, deterministically (as grpc.aio does).
    """
    fut = loop.create_future()
    if asyncio.get_running_loop() is loop:
        loop.call_soon(fut.set_result, None)
    await fut


class LoopBoundAudioClient:
    """Audio stub whose calls complete through a future of its creation loop.

    Same failure mode as a grpc.aio stub: awaiting it from any other loop raises
    "... attached to a different loop".
    """

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.volumes: list[float] = []
        self.call_threads: list[int] = []

    async def SetVolume(self, request):  # noqa: N802 - gRPC method name
        await _complete_on(self.loop)
        self.volumes.append(request.volume)
        self.call_threads.append(threading.get_ident())


class LoopBoundStream:
    """Gameplay stream stub bound to its creation loop (like a grpc.aio call)."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.writes = []

    async def write(self, msg):
        await _complete_on(self.loop)
        self.writes.append(msg)


def _loop_bound_game(game_loop: GameLoopThread) -> FakeInterventionGame:
    """A shared fake whose audio client and stream are bound to ``game_loop``."""
    return FakeInterventionGame(
        serials=("S1", "S2"),
        audio_client=game_loop.call(LoopBoundAudioClient),
        gameplay_stream=game_loop.call(LoopBoundStream),
    )


class FakeFlagClient:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def set(self, key, value):
        self.values[key] = value

    def get_string_value(self, key, default, _ctx=None):
        return str(self.values.get(key, default))

    def get_object_details(self, key, _default, _ctx=None):
        # One-shot flags are per-game event lists read via details (FR-137).
        return object_details(key, self.values.get(key))

    def get_string_details(self, key, default, _ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=self.get_string_value(key, default, _ctx))

    def get_float_details(self, key, default, _ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=self.get_float_value(key, default, _ctx))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_object_value(self, key, default, _ctx=None):
        return self.values.get(key, default)

    def get_boolean_value(self, key, default, _ctx=None):
        return bool(self.values.get(key, default))


def make_manager(sessions_fn, *, interventions=None, end_game_fn=None, timeout_s=None):
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(event_publisher=publisher, get_sessions=sessions_fn, end_game_fn=end_game_fn)
    mgr._interventions_client = FakeFlagClient(interventions or {})
    mgr._agent_client = FakeFlagClient(
        {"interventions_allowed": ALLOWED, "policy.battery_threshold": 20, "policy.coordinator_backstop_per_minute": 60}
    )
    mgr._user_client = FakeFlagClient({"agent_intervention_cue": False})
    if timeout_s is not None:
        mgr._dispatch_timeout_s = timeout_s
    mgr.register_ambient_handlers()
    return mgr, events


def _session(game_id, game, game_loop, events, *, is_real=False):
    async def publish(event_type, data):
        events.append((game_id, event_type, data))

    return SessionView(game_id=game_id, game=game, publish=publish, is_real=is_real, loop=game_loop)


def _outcomes(events, type_id):
    return [
        data
        for entry in events
        for event_type, data in [entry[-2:]]
        if event_type == GameEvent.AGENT_INTERVENTION and data["type"] == type_id
    ]


@pytest.fixture
def game_loop():
    loop_thread = GameLoopThread()
    yield loop_thread
    loop_thread.stop()


@pytest.fixture
def metrics_patched():
    with (
        patch("services.game_coordinator.metrics.interventions_total"),
        patch("services.game_coordinator.metrics.intervention_dispatch_failures_total") as failures,
    ):
        yield failures


async def _set_volume_and_evaluate(mgr, volume):
    await mgr.evaluate_all()  # baseline: none (-1)
    mgr._interventions_client.set("volume_override", volume)
    await mgr.evaluate_all()


# --------------------------------------------------------------------------- #
# Reproduction: volume_override with a client created on the game loop
# --------------------------------------------------------------------------- #
@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
async def test_volume_override_cross_loop_fails_when_run_on_main_loop(game_loop):
    """Pre-#1250 behaviour: no game loop on the view, handler awaited inline on
    the main loop, the game-loop client fails with the cross-loop error."""
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, None, events, is_real=True)  # loop unknown -> inline (old path)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1})

    with patch("services.game_coordinator.interventions.logger") as log:
        await _set_volume_and_evaluate(mgr, 0.4)

    outcome = _outcomes(events, "adjust_volume")
    assert outcome[-1]["blocked"] == "true"
    assert outcome[-1]["block_reason"] == "handler_error"
    assert "attached to a different loop" in str(log.error.call_args)
    assert game.audio_client.volumes == []


@UC040_A26
@pytest.mark.asyncio
async def test_volume_override_runs_on_owning_game_loop(game_loop, metrics_patched):
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1})

    await _set_volume_and_evaluate(mgr, 0.4)

    assert game.audio_client.volumes == [pytest.approx(0.4)]
    assert game.audio_client.call_threads == [game_loop.thread.ident]
    assert _outcomes(events, "adjust_volume")[-1]["blocked"] == "false"
    metrics_patched.labels.assert_not_called()


@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
async def test_volume_revert_runs_on_owning_game_loop(game_loop):
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1})

    await _set_volume_and_evaluate(mgr, 0.4)
    mgr._interventions_client.set("volume_override", -1)
    await mgr.evaluate_all()

    assert game.audio_client.volumes == [pytest.approx(0.4), pytest.approx(GAME_VOLUME)]


@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
async def test_controller_effect_writes_stream_on_owning_game_loop(game_loop):
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={})

    await mgr.evaluate_all()
    mgr._interventions_client.set("controller_effect", edge_event("controller_effect", "S1:pulse"))
    await mgr.evaluate_all()

    assert len(game.gameplay_stream.writes) == 1
    assert _outcomes(events, "send_controller_effect")[-1]["blocked"] == "false"


@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
async def test_end_game_stays_on_main_loop(game_loop):
    """end_game joins the game thread, so it must not run on that thread's loop."""
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    seen = {}

    async def end_game_fn(_game_id, _reason):
        seen["loop"] = asyncio.get_running_loop()
        return True, ""

    mgr, _ = make_manager(lambda: [view], interventions={}, end_game_fn=end_game_fn)
    await mgr.evaluate_all()
    mgr._interventions_client.set("end_game", edge_event("end_game"))
    await mgr.evaluate_all()

    assert seen["loop"] is asyncio.get_running_loop()


# --------------------------------------------------------------------------- #
# Fail closed: closed loop, stuck effect, loop stopping mid-effect
# --------------------------------------------------------------------------- #
@UC040_A26
@pytest.mark.asyncio
async def test_closed_game_loop_fails_closed_without_hanging(game_loop, metrics_patched):
    game = _loop_bound_game(game_loop)
    game_loop.stop()  # the game ended: loop closed
    assert game_loop.loop.is_closed()
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1})

    started = time.monotonic()
    await asyncio.wait_for(_set_volume_and_evaluate(mgr, 0.4), timeout=2.0)

    assert time.monotonic() - started < 1.0
    outcome = _outcomes(events, "adjust_volume")[-1]
    assert outcome["blocked"] == "true"
    assert outcome["block_reason"] == REASON_LOOP_UNAVAILABLE
    metrics_patched.labels.assert_called_with(type="adjust_volume", reason=REASON_LOOP_UNAVAILABLE)
    metrics_patched.labels.return_value.inc.assert_called()


@UC040_A26
@pytest.mark.asyncio
async def test_started_effect_is_never_cancelled_and_reported_late(game_loop, metrics_patched):
    """Half-apply guard: an effect still running at the deadline finishes on the
    game loop (both halves applied) and is reported game_loop_late, not blocked."""
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1}, timeout_s=0.1)
    steps: list[str] = []
    finished = threading.Event()

    async def two_step(_ctx):
        steps.append("first half")  # e.g. eliminate_player bumping dead_count
        await asyncio.sleep(0.4)  # e.g. awaiting _play_sound with no deadline
        steps.append("second half")  # e.g. alive=False, span closed
        finished.set()

    mgr.register_handler("volume_override", two_step)
    await asyncio.wait_for(_set_volume_and_evaluate(mgr, 0.4), timeout=2.0)

    outcome = _outcomes(events, "adjust_volume")[-1]
    assert outcome["block_reason"] == REASON_LOOP_LATE
    assert outcome["blocked"] == "false"  # outcome unknown, not "not applied"
    metrics_patched.labels.assert_called_with(type="adjust_volume", reason=REASON_LOOP_LATE)
    assert await asyncio.to_thread(finished.wait, 2.0)
    assert steps == ["first half", "second half"], "a started effect must never be half-applied"


@UC040_A26
@pytest.mark.asyncio
async def test_ordering_holds_after_a_timeout(game_loop):
    """A slow effect times out (late); the effect queued behind it is skipped
    (game_loop_timeout, never runs); an effect submitted afterwards still waits
    for the slow one to really finish."""
    dispatcher = GameLoopDispatcher(game_loop.loop, timeout_s=0.15)
    log: list[str] = []
    running = {"now": 0, "max": 0}

    async def effect(name, delay):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        log.append(f"{name} start")
        await asyncio.sleep(delay)
        log.append(f"{name} end")
        running["now"] -= 1

    slow, queued = await asyncio.gather(
        dispatcher.run(effect, "slow", 0.4),
        dispatcher.run(effect, "queued", 0.0),
        return_exceptions=True,
    )
    assert isinstance(slow, GameLoopUnavailableError) and slow.reason == REASON_LOOP_LATE
    assert isinstance(queued, GameLoopUnavailableError) and queued.reason == REASON_LOOP_TIMEOUT

    dispatcher._timeout_s = 2.0
    await dispatcher.run(effect, "after", 0.0)

    assert log == ["slow start", "slow end", "after start", "after end"]
    assert running["max"] == 1


@UC040_A26
@pytest.mark.asyncio
async def test_game_loop_stopping_mid_effect_reports_late(game_loop):
    started = threading.Event()

    async def waits_forever():
        started.set()
        await asyncio.sleep(30)

    dispatcher = GameLoopDispatcher(game_loop.loop, timeout_s=5.0)
    run = asyncio.ensure_future(dispatcher.run(waits_forever))
    queued = asyncio.ensure_future(dispatcher.run(waits_forever))
    assert await asyncio.to_thread(started.wait, 2.0)
    await asyncio.to_thread(game_loop.stop)  # shutdown cancels every pending task

    with pytest.raises(GameLoopUnavailableError) as err:
        await asyncio.wait_for(run, timeout=2.0)
    assert err.value.reason == REASON_LOOP_LATE  # it had started: outcome unknown
    with pytest.raises(GameLoopUnavailableError) as err:
        await asyncio.wait_for(queued, timeout=2.0)
    assert err.value.reason == REASON_LOOP_UNAVAILABLE  # never started: not applied


@UC040_A26
@pytest.mark.asyncio
async def test_end_game_that_closes_the_loop_logs_no_failure(game_loop, metrics_patched):
    """end_game ends the game (its loop closes): no cue is dispatched to the dead
    loop, so no false 'not applied' warning and no failure count."""
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)

    async def end_game_fn(_game_id, _reason):
        await asyncio.to_thread(game_loop.stop)
        return True, ""

    mgr, _ = make_manager(lambda: [view], interventions={}, end_game_fn=end_game_fn)
    mgr._user_client.set("agent_intervention_cue", True)
    await mgr.evaluate_all()
    mgr._interventions_client.set("end_game", edge_event("end_game"))
    with patch("services.game_coordinator.interventions.logger") as log:
        await mgr.evaluate_all()

    assert game_loop.loop.is_closed()
    assert _outcomes(events, "end_game")[-1]["blocked"] == "false"
    log.warning.assert_not_called()
    log.error.assert_not_called()
    metrics_patched.labels.assert_not_called()


@UC040_A26
@pytest.mark.asyncio
async def test_agent_cue_runs_on_game_loop_and_respects_mute(game_loop, metrics_patched):
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={})

    async def noop(_ctx):
        return None

    mgr.register_handler("eliminate_player", noop)
    await mgr.evaluate_all()

    mgr._interventions_client.set(
        "eliminate_player", edge_event("eliminate_player", "S1")
    )  # cue muted (fixture default)
    with patch.object(mgr, "_emit_agent_cue") as emit:
        await mgr.evaluate_all()
    emit.assert_not_called()  # muted on the main loop: never dispatched

    mgr._user_client.set("agent_intervention_cue", True)
    mgr._interventions_client.set("eliminate_player", edge_event("eliminate_player", "S1", seq=2))
    await mgr.evaluate_all()
    assert len(game.gameplay_stream.writes) == 1  # pulse written through the game-loop stream
    metrics_patched.labels.assert_not_called()


@UC040_A26
@pytest.mark.asyncio
async def test_revert_failure_is_logged_and_counted_once(game_loop, metrics_patched):
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1})
    await _set_volume_and_evaluate(mgr, 0.4)
    game_loop.stop()

    mgr._interventions_client.set("volume_override", -1)
    with patch("services.game_coordinator.interventions.logger") as log:
        await mgr.evaluate_all()

    assert log.warning.call_count == 1
    log.error.assert_not_called()
    metrics_patched.labels.assert_called_once_with(type="adjust_volume", reason=REASON_LOOP_UNAVAILABLE)


@UC040_A26
@pytest.mark.asyncio
async def test_handler_exception_still_propagates_as_handler_error(game_loop, metrics_patched):
    game = _loop_bound_game(game_loop)
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={"volume_override": -1})

    async def boom(_ctx):
        raise ValueError("nope")

    mgr.register_handler("volume_override", boom)
    await _set_volume_and_evaluate(mgr, 0.4)

    assert _outcomes(events, "adjust_volume")[-1]["block_reason"] == "handler_error"
    metrics_patched.labels.assert_not_called()


# --------------------------------------------------------------------------- #
# Ordering and concurrent games
# --------------------------------------------------------------------------- #
@UC040_A26
@pytest.mark.asyncio
async def test_effects_run_in_submission_order_per_game(game_loop):
    """Overlapping submissions from the main loop run one at a time, in order,
    even when an earlier effect is slower than a later one."""
    dispatcher = GameLoopDispatcher(game_loop.loop)
    order: list[str] = []
    running = {"now": 0, "max": 0}

    async def effect(name, delay):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(delay)
        order.append(name)
        running["now"] -= 1

    await asyncio.gather(
        dispatcher.run(effect, "first", 0.1),
        dispatcher.run(effect, "second", 0.0),
        dispatcher.run(effect, "third", 0.05),
    )

    assert order == ["first", "second", "third"]
    assert running["max"] == 1


@UC040_A26
@pytest.mark.asyncio
async def test_inline_when_already_on_game_loop():
    dispatcher = GameLoopDispatcher(asyncio.get_running_loop())

    async def where():
        return asyncio.get_running_loop()

    assert await dispatcher.run(where) is asyncio.get_running_loop()


@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
async def test_real_and_shadow_games_each_use_their_own_loop():
    real_loop, shadow_loop = GameLoopThread(), GameLoopThread()
    try:
        real_game, shadow_game = _loop_bound_game(real_loop), _loop_bound_game(shadow_loop)
        events = []
        views = [
            _session("real-1", real_game, real_loop.loop, events, is_real=True),
            _session("shadow-1", shadow_game, shadow_loop.loop, events),
        ]
        mgr, _ = make_manager(lambda: views, interventions={"volume_override": -1})

        await _set_volume_and_evaluate(mgr, 0.6)

        assert real_game.audio_client.volumes == [pytest.approx(0.6)]
        assert shadow_game.audio_client.volumes == [pytest.approx(0.6)]
        assert real_game.audio_client.call_threads == [real_loop.thread.ident]
        assert shadow_game.audio_client.call_threads == [shadow_loop.thread.ident]
        assert mgr._dispatchers["real-1"].loop is real_loop.loop
        assert mgr._dispatchers["shadow-1"].loop is shadow_loop.loop
        blocked = [d for _, et, d in events if et == GameEvent.AGENT_INTERVENTION and d["blocked"] == "true"]
        assert blocked == []
    finally:
        real_loop.stop()
        shadow_loop.stop()


@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
async def test_retired_session_drops_its_dispatcher(game_loop):
    game = _loop_bound_game(game_loop)
    events = []
    views = [_session("g1", game, game_loop.loop, events, is_real=True)]
    mgr, _ = make_manager(lambda: views, interventions={"volume_override": -1})
    await _set_volume_and_evaluate(mgr, 0.4)
    assert "g1" in mgr._dispatchers

    views.clear()
    await mgr.evaluate_all()
    assert "g1" not in mgr._dispatchers


# --------------------------------------------------------------------------- #
# Wiring: GameSession exposes its loop, the servicer passes it on
# --------------------------------------------------------------------------- #
@UC040_A26
def test_game_session_records_its_thread_loop():
    from services.game_coordinator.event_bus import EventBus

    session = game_session_mod.GameSession(
        game_id="g1",
        game_name="FFA",
        players=[],
        game_config=None,
        event_bus=EventBus(),
        game_kind=game_session_mod.GAME_KIND_SHADOW,
    )
    seen = {}

    async def fake_run(_self):
        seen["loop"] = asyncio.get_running_loop()

    with patch.object(game_session_mod.GameSession, "_run_game_loop_async", fake_run):
        session.start_thread()
        session.game_thread.join(timeout=3.0)

    assert seen["loop"] is session.game_loop


@UC040_A26
def test_servicer_session_view_carries_game_loop():
    from services.game_coordinator.servicer import GameCoordinatorServicer

    servicer = GameCoordinatorServicer()
    session = game_session_mod.GameSession(
        game_id="g1",
        game_name="FFA",
        players=[],
        game_config=None,
        event_bus=servicer.primary_event_bus,
        game_kind=game_session_mod.GAME_KIND_PRIMARY,
    )
    session.current_game = object()
    session.game_loop = asyncio.new_event_loop()
    session.game_state = game_coordinator_pb2.GameState.RUNNING
    try:
        with servicer._sessions_lock:
            servicer.sessions["g1"] = session
        views = servicer.get_intervention_sessions()
        assert [v.loop for v in views if v.game_id == "g1"] == [session.game_loop]
    finally:
        session.game_loop.close()


# --------------------------------------------------------------------------- #
# Per-player level reverts (#1418's _run_revert) hop loops and keep their order
# --------------------------------------------------------------------------- #
class FactorPlayer:
    def __init__(self, serial):
        self.serial = serial
        self.alive = True
        self.sensitivity_factor = 1.0
        self.handicap_factor = 1.0


@UC040_A26
@pytest.mark.asyncio
@pytest.mark.usefixtures("metrics_patched")
@pytest.mark.parametrize(
    ("flag_key", "attr"),
    [("player_sensitivity_factor", "sensitivity_factor"), ("player_handicap_factor", "handicap_factor")],
)
async def test_factor_revert_runs_on_game_loop_after_queued_apply(game_loop, flag_key, attr):
    """A level returning to neutral is reverted on the owning game's loop, and
    a revert decided while the level's apply is still queued there runs after
    it: the player ends neutral, not stuck on the applied factor."""
    from services.game_coordinator.difficulty_handlers import register_difficulty_handlers

    game = _loop_bound_game(game_loop)
    game.players = {"S1": FactorPlayer("S1")}
    events = []
    view = _session("g1", game, game_loop.loop, events, is_real=True)
    mgr, _ = make_manager(lambda: [view], interventions={flag_key: 1.0})
    mgr._agent_client.set("interventions_allowed", "adjust_player_sensitivity,set_player_handicap")
    register_difficulty_handlers(mgr)

    order: list[tuple[str, int, float]] = []
    gate = threading.Event()
    apply_handler = mgr._handlers[flag_key]
    revert_handler = mgr._revert_handlers[flag_key]

    async def gated_apply(ctx):
        await asyncio.to_thread(gate.wait, 2.0)  # keep the apply queued on the game loop
        await apply_handler(ctx)
        order.append(("apply", threading.get_ident(), getattr(game.players["S1"], attr)))

    async def recorded_revert(ctx):
        await revert_handler(ctx)
        order.append(("revert", threading.get_ident(), getattr(game.players["S1"], attr)))

    mgr.register_handler(flag_key, gated_apply)
    mgr._revert_handlers[flag_key] = recorded_revert

    await mgr.evaluate_all()  # baseline: neutral
    mgr._interventions_client.set(flag_key, 1.5)
    apply_pass = asyncio.ensure_future(mgr.evaluate_all())
    while not mgr._dispatchers.get("g1") or mgr._dispatchers["g1"]._tail is None:  # noqa: ASYNC110 - polls a cross-thread condition
        await asyncio.sleep(0.01)  # the apply is queued on the game loop
    mgr._interventions_client.set(flag_key, 1.0)
    revert_pass = asyncio.ensure_future(mgr.evaluate_all())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(asyncio.gather(apply_pass, revert_pass), timeout=3.0)

    assert [step for step, _, _ in order] == ["apply", "revert"]
    assert {thread for _, thread, _ in order} == {game_loop.thread.ident}
    assert order[0][2] == pytest.approx(1.5)
    assert getattr(game.players["S1"], attr) == pytest.approx(1.0)
