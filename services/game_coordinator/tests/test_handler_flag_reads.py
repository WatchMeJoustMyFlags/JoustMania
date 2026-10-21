"""
Census of the flags an intervention handler reads (#1872, UC-040 BR-018).

A handler runs on the game's loop through ``GameLoopDispatcher.run``, which copies
the contextvars of the servicer's MAIN loop. The game's own transaction context
(``set_game_transaction_context``: targetingKey, game_mode, controller_count,
sensitivity, game_kind, experiment_id, arm) is therefore not visible to a flag read
in a handler (flags.md §1.2). That is deliberate and documented, not a bug; what
must not happen is a handler-read flag whose flagd targeting depends on that
missing context. ``handler_flag_reads.py`` declares every flag a handler reads and
the targeting variables it may use; ``tests/contract/test_flagd_resolution.py``
checks the flagd files against it. These tests keep the declaration complete:

(i)   every flag read while a handler runs is declared;
(ii)  every registered handler and revert handler has a case here, so a new handler
      cannot slip past the census;
(iii) none of those reads sees the game's transaction context.

Each case drives the real handler with a representative value through the real
``GameLoopDispatcher`` on a separate game-loop thread. Reads are recorded by a
provider registered on every domain a coordinator read can use.
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
from openfeature.provider import AbstractProvider
from openfeature.provider.metadata import Metadata

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

import lib.feature_flags as ff
from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.game_loop_dispatch import GameLoopDispatcher
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.handler_flag_reads import HANDLER_FLAG_READS
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
)
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers

GAME_ID = "g-census-1872"
# Every domain a coordinator flag read can name; a read on any other domain lands on the
# default provider, is recorded as "<default>" and so fails the census.
DOMAINS = ("game", "user", "interventions", "agent", "system", "controller", "observability", "rollout")
# Values that make the read path under test reachable (shield trigger mode).
FLAG_VALUES = {"shield.activation": "trigger"}


class _RecordingProvider(AbstractProvider):
    def __init__(self, domain: str, seen: list):
        super().__init__()
        self._domain = domain
        self._seen = seen

    def get_metadata(self) -> Metadata:
        return Metadata(name=f"census-{self._domain}")

    def _resolve(self, key, default, ctx):
        self._seen.append((self._domain, key, ctx))
        return FlagResolutionDetails(value=FLAG_VALUES.get(key, default), reason=Reason.STATIC, variant="v")

    def resolve_boolean_details(self, flag_key, default_value, evaluation_context=None):
        return self._resolve(flag_key, default_value, evaluation_context)

    def resolve_string_details(self, flag_key, default_value, evaluation_context=None):
        return self._resolve(flag_key, default_value, evaluation_context)

    def resolve_integer_details(self, flag_key, default_value, evaluation_context=None):
        return self._resolve(flag_key, default_value, evaluation_context)

    def resolve_float_details(self, flag_key, default_value, evaluation_context=None):
        return self._resolve(flag_key, default_value, evaluation_context)

    def resolve_object_details(self, flag_key, default_value, evaluation_context=None):
        return self._resolve(flag_key, default_value, evaluation_context)


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


@pytest.fixture
def seen():
    """The real OpenFeature API wired as lib.feature_flags does, a recorder on every domain."""
    saved_ctx = api.get_evaluation_context()
    saved_propagator = of_tx._evaluation_transaction_context_propagator
    saved_registered = ff._propagator_registered
    reads: list[tuple[str, str, EvaluationContext]] = []
    ff._propagator_registered = False
    with patch.object(ff, "init_flag_domain"):  # no flagd provider; the recorders serve every domain
        ff._init_global_context()
        api.set_provider(_RecordingProvider("<default>", reads))
        for domain in DOMAINS:
            api.set_provider(_RecordingProvider(domain, reads), domain=domain)
        try:
            yield reads
        finally:
            api.clear_providers()
            api.set_evaluation_context(saved_ctx)
            of_tx.set_transaction_context_propagator(saved_propagator)
            ff._propagator_registered = saved_registered


def _make_ffa():
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=AsyncMock(),
        game_id=GAME_ID,
    )
    g.gameplay_stream = AsyncMock()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    g._play_sound = AsyncMock()
    return g


_SPECS = {spec.flag_key: spec for spec in INTERVENTION_SPECS}

# (flag key, "apply" | "revert") -> (value, payload, target_serial). A representative
# value that reaches the effect; per-player flags carry target_serial like the manager's
# fan-out does (a direct call without one resolves every serial through the manager's
# own interventions client, which is not started here).
CASES = {
    ("music_tempo_override", "apply"): (1.5, "", None),
    ("music_tempo_override", "revert"): (0.0, "", None),
    ("global_sensitivity_override", "apply"): (3, "", None),
    ("player_sensitivity_factor", "apply"): ({"p1": 1.2}, "", None),
    ("player_sensitivity_factor", "revert"): ({}, "", None),
    ("player_handicap_factor", "apply"): ({"p1": 1.2}, "", None),
    ("player_handicap_factor", "revert"): ({}, "", None),
    ("shield_seconds", "apply"): (5.0, "", "p1"),
    ("toughness_boost_seconds", "apply"): ("5:1.5", "", "p1"),
    ("soft_penalty_action", "apply"): ("warn", "", "p1"),
    ("auto_rubberband", "apply"): ("gentle", "", None),
    ("volume_override", "apply"): (0.5, "", None),
    ("volume_override", "revert"): (-1.0, "", None),
    ("global_difficulty_factor", "apply"): (1.5, "", None),
    ("global_difficulty_factor", "revert"): (1.0, "", None),
    ("pacing_profile", "apply"): ("calm", "", None),
    ("pacing_profile", "revert"): ("default", "", None),
    ("tempo_ramp", "apply"): ("1.5:10:linear", "", None),
    ("tempo_ramp", "revert"): ("none", "", None),
    ("eliminate_player", "apply"): ("p2", "p2", None),
    ("revive_player", "apply"): ("p2", "p2", None),
    ("audio_cue", "apply"): ("ding", "ding", None),
    ("controller_effect", "apply"): ("p1:pulse", "p1:pulse", None),
}
# end_game runs on the main loop (it joins the game thread), so it is not dispatched.
MAIN_LOOP_CASES = {("end_game", "apply"): ("", "", None)}


def _manager(game):
    mgr = InterventionManager(event_publisher=EventCollector().publish, get_game=lambda: game)
    mgr.register_ambient_handlers()
    register_difficulty_handlers(mgr)
    register_lifecycle_handlers(mgr)
    mgr._end_game_fn = AsyncMock(return_value=(True, ""))
    # The client _cue_enabled reads through once the manager has started.
    mgr._user_client = api.get_client("user")
    return mgr


def _registered(mgr):
    return {(k, "apply") for k in mgr._handlers} | {(k, "revert") for k in mgr._revert_handlers}


def _drive(mgr, game, game_loop):
    """Run every case through its real handler and the real dispatcher, then the agent cue."""
    dispatcher = GameLoopDispatcher(game_loop.loop)

    async def run_all():
        # The main loop carries no game transaction context (the servicer never sets one).
        api.set_transaction_context(EvaluationContext())
        for (key, kind), (value, payload, target) in {**CASES, **MAIN_LOOP_CASES}.items():
            spec = _SPECS[key]
            handler = mgr._handlers[key] if kind == "apply" else mgr._revert_handlers[key]
            ctx = InterventionContext(
                spec=spec,
                value=value,
                payload=payload,
                target_serial=target,
                game=game,
                objective="",
                game_id=GAME_ID,
            )
            try:
                if key in mgr._main_loop_flags:
                    await handler(ctx)
                else:
                    await dispatcher.run(handler, ctx)
            except Exception:  # a handler that refuses still read what it read
                pass
        # The agent's on-device cue is dispatched after a HARD-class action (#818).
        await dispatcher.run(mgr._emit_agent_cue, _SPECS["eliminate_player"], "p1", game)

    return run_all()


async def _run(game, seen_reads, *, game_task=None):
    game_loop = _GameLoop()
    try:
        if game_task is not None:
            asyncio.run_coroutine_threadsafe(game_task(), game_loop.loop).result(2.0)
        mgr = _manager(game)
        assert set(CASES) | set(MAIN_LOOP_CASES) == _registered(mgr), "census cases differ from the registered handlers"
        seen_reads.clear()  # construction and manager setup read flags too; only handlers count
        await _drive(mgr, game, game_loop)
    finally:
        game_loop.stop()
    return list(seen_reads)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
async def test_every_handler_flag_read_is_declared(seen):
    """(i) A flag read while a handler runs is declared in handler_flag_reads.py."""
    game = _make_ffa()
    reads = await _run(game, seen)
    read_keys = {(domain, key) for domain, key, _ in reads}
    assert read_keys, "the census recorded no reads: the recorder is not wired"
    undeclared = sorted(read_keys - set(HANDLER_FLAG_READS))
    assert not undeclared, (
        f"flags read inside an intervention handler but not declared in handler_flag_reads.py: {undeclared}"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("seen")
@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
async def test_census_covers_every_registered_handler():
    """(ii) The case table equals the registered handlers and revert handlers."""
    mgr = _manager(_make_ffa())
    assert set(CASES) | set(MAIN_LOOP_CASES) == _registered(mgr)
    assert {k for k, _ in MAIN_LOOP_CASES} == mgr._main_loop_flags
    assert {k for k, _ in CASES} | {k for k, _ in MAIN_LOOP_CASES} == set(_SPECS)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
async def test_handler_reads_carry_no_game_transaction_context(seen):
    """(iii) A handler read has no targetingKey, game_mode, controller_count, sensitivity,
    experiment or arm, and game_kind is real, even when the game itself is a shadow game
    in an experiment (its context lives in the game's own task only)."""
    game = _make_ffa()

    async def game_task():
        ff.set_game_transaction_context(
            game_mode="FFA",
            controller_count=2,
            sensitivity=2,
            game_kind="shadow",
            experiment_id="exp_abc",
            arm="experimental",
        )

    reads = await _run(game, seen, game_task=game_task)
    assert reads
    for domain, key, ctx in reads:
        where = f"{domain}.{key}"
        assert ctx.attributes.get("game_kind") == "real", where
        for name in ("game_mode", "controller_count", "sensitivity", "experiment_id", "arm"):
            assert name not in ctx.attributes, f"{where} sees {name}"
        # A key a read passes itself is allowed; the game's own targetingKey ("FFA") is not.
        assert ctx.targeting_key != "FFA", where
