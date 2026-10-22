"""
The evaluation context of the shield flags on the agent's grant path (#1837, UC-040 A31).

An agent grant reaches ``grant_shield`` through the InterventionManager on the
servicer's MAIN loop and ``GameLoopDispatcher.run``, and the manager sets the game's
flag transaction context around it (``run_in_game_flag_context``, #1970). So the
coordinator reads ``shield.activation`` and ``shield.charge_expiry_seconds`` with the
API-level attributes, the game's context (targetingKey and game_mode = the mode,
game_kind) and ``gameId``. The agent's charge ledger resolves them with only
``gameId`` and ``game_kind=real``, for real games only
(services/agent/flags/game.go shieldChargeContext); that is sound because the
targeting of these flags may reference only ``gameId`` and ``game_kind`` (flags.md
§4, tests/contract/test_flagd_resolution.py). This test pins the coordinator's side
with the real OpenFeature API and a recording provider.
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import openfeature.transaction_context as of_tx
import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagResolutionDetails, Reason
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from openfeature.transaction_context import ContextVarsTransactionContextPropagator

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

import lib.feature_flags as ff
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionManager, SessionView

GAME_ID = "g-context-1837"


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


class _Recorder:
    """Records the merged evaluation context of each shield flag read."""

    def __init__(self):
        self.seen: list[tuple[str, EvaluationContext]] = []

    def flag(self, key, value):
        def evaluate(_flag, ctx):
            self.seen.append((key, ctx))
            return FlagResolutionDetails(value=value, reason=Reason.STATIC, variant="v")

        return InMemoryFlag("v", {"v": value}, context_evaluator=evaluate)


@pytest.fixture
def recorder():
    """The real OpenFeature API wired as lib.feature_flags does, with a recording game set."""
    saved_ctx = api.get_evaluation_context()
    saved_propagator = of_tx._evaluation_transaction_context_propagator
    saved_registered = ff._propagator_registered
    rec = _Recorder()
    ff._propagator_registered = False
    with patch.object(ff, "init_flag_domain"):  # no flagd provider; the recorder serves "game"
        ff._init_global_context()
        api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
        api.set_provider(
            InMemoryProvider(
                {
                    "shield.activation": rec.flag("shield.activation", "trigger"),
                    "shield.charge_expiry_seconds": rec.flag("shield.charge_expiry_seconds", 20.0),
                }
            ),
            domain="game",
        )
        try:
            yield rec
        finally:
            api.clear_providers()
            api.set_evaluation_context(saved_ctx)
            of_tx.set_transaction_context_propagator(saved_propagator)
            ff._propagator_registered = saved_registered


def _make_ffa():
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=GAME_ID,
    )
    g.gameplay_stream = AsyncMock()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A31", steps=[1])
async def test_dispatched_grant_reads_the_shield_flags_in_the_game_transaction_context(recorder):
    """A31 step 1, BR-018: a dispatched grant_shield in a real FFA game resolves both shield
    flags with the game's context (targetingKey and game_mode FFA, game_kind=real) plus
    gameId; the agent's gameId + game_kind=real is a subset that the targeting limit keeps
    equivalent."""
    game = _make_ffa()
    game_loop = _GameLoop()
    try:

        async def game_task():
            # games/base.py sets this inside the game's own task; a read there sees it.
            ff.set_game_transaction_context(game_mode="FFA", controller_count=2, sensitivity=2)
            return game._shield_activation_mode()

        assert asyncio.run_coroutine_threadsafe(game_task(), game_loop.loop).result(2.0) == "trigger"
        [(_, in_game)] = recorder.seen
        # Control: the recorder sees a transaction context where one is in effect.
        assert in_game.targeting_key == "FFA" and in_game.attributes["game_mode"] == "FFA"
        recorder.seen.clear()

        # The game's run kept its context (games/base.py _set_flag_eval_context).
        assert asyncio.run_coroutine_threadsafe(_set_context(game), game_loop.loop).result(2.0) is None
        recorder.seen.clear()

        # The servicer's main loop carries no game transaction context.
        api.set_transaction_context(EvaluationContext())
        spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "shield_seconds")

        async def publish(_event_type, _data):
            return None

        mgr = InterventionManager(event_publisher=publish)
        session = SessionView(game_id=GAME_ID, game=game, publish=publish, is_real=True, loop=game_loop.loop)
        granted = []

        async def grant():
            granted.append(await game.grant_shield("p1", 5.0))

        with patch.object(game, "_play_sound", new_callable=AsyncMock):
            await mgr._run_on_game_loop(session, spec, grant)
        assert granted == [True]
    finally:
        game_loop.stop()

    assert [key for key, _ in recorder.seen] == ["shield.activation", "shield.charge_expiry_seconds"]
    for key, ctx in recorder.seen:
        assert ctx.targeting_key == "FFA", key
        assert ctx.attributes["game_mode"] == "FFA", key
        assert ctx.attributes["gameId"] == GAME_ID, key
        assert ctx.attributes["game_kind"] == "real", key
        assert "experiment_id" not in ctx.attributes, key
    assert game.players["p1"].shield_charge_seconds == 5.0


async def _set_context(game):
    game._set_flag_eval_context()
