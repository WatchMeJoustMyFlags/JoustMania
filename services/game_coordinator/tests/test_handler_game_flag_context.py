"""
A dispatched intervention effect resolves flags in its game's context (#1970, UC-040 BR-018).

``InterventionManager._run_on_game_loop`` hands ``run_in_game_flag_context`` to the
game's ``GameLoopDispatcher``: on the game loop it sets the game's
``flag_eval_context`` as the OpenFeature transaction context around the effect and
restores the previous one afterwards. These tests pin that the effect sees its own
game's context and that nothing leaks: not into the main loop, not into the game's
own task, not into the caller's task on the inline path, and not between two games
dispatched at the same time.
"""

import asyncio
import sys
import threading
from pathlib import Path

import openfeature.transaction_context as of_tx
import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.transaction_context import ContextVarsTransactionContextPropagator

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionManager,
    SessionView,
    run_in_game_flag_context,
)
from services.game_coordinator.tests.conftest import FakeInterventionGame

SPEC = next(s for s in INTERVENTION_SPECS if s.flag_key == "pacing_profile")
MAIN_CONTEXT = EvaluationContext(attributes={"marker": "main"})


def _game(name: str) -> FakeInterventionGame:
    """A shared fake whose flag context carries the game's marker."""
    return FakeInterventionGame(
        flag_eval_context=EvaluationContext(
            targeting_key="FFA",
            attributes={"marker": name, "game_kind": "shadow", "experiment_id": f"exp_{name}", "arm": "experimental"},
        )
    )


class _GameLoop:
    """An asyncio loop in its own thread, like GameSession._run_game_loop_threaded."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2.0)
        self.loop.close()


@pytest.fixture(autouse=True)
def propagator():
    """Register the contextvars propagator explicitly: an earlier api.shutdown() leaves a no-op."""
    saved = of_tx._evaluation_transaction_context_propagator
    api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
    try:
        yield
    finally:
        of_tx.set_transaction_context_propagator(saved)


@pytest.fixture
def game_loop():
    loop = _GameLoop()
    try:
        yield loop
    finally:
        loop.stop()


def _manager():
    async def publish(_event_type, _data):
        return None

    return InterventionManager(event_publisher=publish)


def _session(game, loop):
    async def publish(_event_type, _data):
        return None

    return SessionView(game_id=f"g-{id(game)}", game=game, publish=publish, loop=loop)


def _marker() -> str | None:
    return api.get_transaction_context().attributes.get("marker")


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A26", br=["BR-018"])
async def test_cross_loop_effect_sees_the_game_context_and_leaks_nowhere(game_loop):
    """Cross-loop: the effect sees its game's context; the main loop's and the game
    task's own transaction contexts are unchanged afterwards."""
    game = _game("a")
    api.set_transaction_context(MAIN_CONTEXT)

    game_task_ready = threading.Event()
    release_game_task = asyncio.Event()

    async def game_task():
        api.set_transaction_context(EvaluationContext(attributes={"marker": "game-task"}))
        game_task_ready.set()
        await release_game_task.wait()
        return _marker()

    game_task_future = asyncio.run_coroutine_threadsafe(game_task(), game_loop.loop)
    assert game_task_ready.wait(2.0)

    seen = []

    async def effect():
        seen.append((api.get_transaction_context(), threading.get_ident()))

    await _manager()._run_on_game_loop(_session(game, game_loop.loop), SPEC, effect)

    [(ctx, thread_id)] = seen
    assert thread_id == game_loop.thread.ident
    assert ctx == game.flag_eval_context
    assert api.get_transaction_context() == MAIN_CONTEXT
    game_loop.loop.call_soon_threadsafe(release_game_task.set)
    assert await asyncio.wrap_future(game_task_future) == "game-task"


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A26", br=["BR-018"])
async def test_inline_effect_restores_the_callers_context_even_when_it_raises():
    """Inline path (no game loop): the effect runs in the caller's task, so the caller's
    transaction context is restored after it, also when the effect raises."""
    game = _game("inline")
    api.set_transaction_context(MAIN_CONTEXT)
    seen = []

    async def effect():
        seen.append(_marker())

    async def failing_effect():
        seen.append(_marker())
        raise RuntimeError("handler failed")

    mgr = _manager()
    await mgr._run_on_game_loop(_session(game, None), SPEC, effect)
    assert api.get_transaction_context() == MAIN_CONTEXT
    with pytest.raises(RuntimeError):
        await mgr._run_on_game_loop(_session(game, None), SPEC, failing_effect)
    assert api.get_transaction_context() == MAIN_CONTEXT
    assert seen == ["inline", "inline"]


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A26", br=["BR-018"])
async def test_two_games_dispatched_together_each_see_their_own_context():
    """Two games on two loops, effects overlapping in time: each sees only its game's context."""
    loops = (_GameLoop(), _GameLoop())
    games = (_game("one"), _game("two"))
    both_started = threading.Barrier(2, timeout=2.0)
    seen: dict[str, list] = {"one": [], "two": []}

    def effect_for(name):
        async def effect():
            seen[name].append(_marker())
            await asyncio.to_thread(both_started.wait)  # both effects are now in flight
            seen[name].append(_marker())

        return effect

    mgr = _manager()
    try:
        await asyncio.gather(
            *(
                mgr._run_on_game_loop(_session(game, loop.loop), SPEC, effect_for(name))
                for game, loop, name in zip(games, loops, ("one", "two"), strict=True)
            )
        )
    finally:
        for loop in loops:
            loop.stop()
    assert seen == {"one": ["one", "one"], "two": ["two", "two"]}


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A26", br=["BR-018"])
async def test_no_game_runs_the_effect_unchanged():
    """None-safe: no game leaves the caller's context as it is."""
    api.set_transaction_context(MAIN_CONTEXT)
    seen = []

    async def effect(value):
        seen.append((value, _marker()))
        return value

    assert await run_in_game_flag_context(None, effect, 1) == 1
    assert seen == [(1, "main")]
    assert api.get_transaction_context() == MAIN_CONTEXT
