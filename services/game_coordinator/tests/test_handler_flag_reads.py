"""
Census of the flags an intervention handler reads (#1872, UC-040 BR-018).

A handler runs on the game's loop through ``GameLoopDispatcher.run``, and the
InterventionManager sets the game's own flag transaction context around it
(``run_in_game_flag_context``, #1970): a flag read in a handler resolves in the
game's context (``set_game_transaction_context``: targetingKey, game_mode,
controller_count, sensitivity, game_kind, experiment_id, arm; flags.md §1.2).
``handler_flag_reads.py`` declares every flag a handler reads and the targeting
variables it may use; ``tests/contract/test_flagd_resolution.py`` checks the flagd
files against it. These tests keep the declaration complete and pin the context:

(i)   every flag read while a handler runs is declared;
(ii)  every registered handler and revert handler has a case here, so a new handler
      cannot slip past the census;
(iii) every one of those reads sees the game's transaction context: an experimental
      arm's experiment and game_kind=shadow, a real game's game_kind=real and no
      experiment, and before the run starts the session kind and experiment.

Each case drives the real handler with a representative value through the manager's
real dispatch path (``_run_on_game_loop`` and ``GameLoopDispatcher``) on a separate
game-loop thread. Reads are recorded by a provider registered on every domain a
coordinator read can use.
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
from openfeature.transaction_context import ContextVarsTransactionContextPropagator

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

import lib.feature_flags as ff
from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.handler_flag_reads import HANDLER_FLAG_READS
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionContext,
    InterventionManager,
    SessionView,
)
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers

GAME_ID = "g-census-1872"
# Every domain a coordinator flag read can name; a read on any other domain lands on the
# default provider, is recorded as "<default>" and so fails the census.
DOMAINS = ("game", "user", "interventions", "agent", "system", "controller", "observability", "rollout")
# Values that make the read path under test reachable (shield trigger mode).
FLAG_VALUES = {
    "shield.activation": "trigger",
    # The group shield's gate open, so its whole path runs (UC-040 A46, #2467).
    "policy.group_grant.enabled": True,
    "policy.group_grant.real_games": True,
    "policy.group_grant.max_per_game": 1.0,
    "policy.group_grant.max_targets": 4.0,
}


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
        # Explicit: an earlier api.shutdown() resets the propagator to a no-op (#1970).
        api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
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
    ("group_shield", "apply"): ("p1,p2|5", "p1,p2|5", None),
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
    """Run every case through its real handler and the manager's dispatch path, then the agent cue."""

    async def _noop_publish(_event_type, _data):
        return None

    session = SessionView(game_id=GAME_ID, game=game, publish=_noop_publish, loop=game_loop.loop)

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
                    await mgr._run_on_game_loop(session, spec, handler, ctx)
            except Exception:  # a handler that refuses still read what it read
                pass
        # The agent's on-device cue is dispatched after a HARD-class action (#818).
        spec = _SPECS["eliminate_player"]
        await mgr._run_on_game_loop(session, spec, mgr._emit_agent_cue, spec, "p1", game)
        # The main loop's own transaction context is untouched by the dispatched effects.
        assert api.get_transaction_context() == EvaluationContext()

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


EXPERIMENT = {"experiment_id": "exp_abc", "arm": "experimental"}
GAME_ATTRS = ("game_mode", "controller_count", "sensitivity")


def _bind(game, game_kind, experiment=None):
    """What GameSession does after create_game: the session kind and experiment on the game."""
    game.game_kind = game_kind
    game.experiment_id = (experiment or {}).get("experiment_id", "")
    game.arm = (experiment or {}).get("arm", "")


def _running(game):
    """The game's run(): its own task sets its transaction context (games/base.py)."""

    async def game_task():
        game._set_flag_eval_context()

    return game_task


def _is_windows_baseline_read(domain, key, ctx):
    """handle_pacing_profile's second read pins game_kind=real on purpose (UC-043 BR-005)."""
    return (domain, key) == ("game", "windows") and ctx.attributes.get("game_kind") == "real"


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
@pytest.mark.uc("UC-043", br=["BR-005"])
async def test_handler_reads_carry_the_experiment_arm_game_context(seen):
    """(iii) A shadow game in an experiment's experimental arm: every handler read sees
    game_kind=shadow, the experiment and arm and the game's mode, count and sensitivity;
    windows keeps the profile as its targetingKey, the other reads get the game mode."""
    game = _make_ffa()
    _bind(game, "shadow", EXPERIMENT)
    reads = await _run(game, seen, game_task=_running(game))
    assert reads
    keys = set()
    for domain, key, ctx in reads:
        where = f"{domain}.{key}"
        keys.add((domain, key))
        assert ctx.attributes.get("experiment_id") == "exp_abc", where
        assert ctx.attributes.get("arm") == "experimental", where
        assert ctx.attributes.get("game_mode") == "FFA", where
        for name in GAME_ATTRS:
            assert name in ctx.attributes, f"{where} misses {name}"
        if _is_windows_baseline_read(domain, key, ctx):
            continue
        assert ctx.attributes.get("game_kind") == "shadow", where
        if (domain, key) == ("game", "windows"):
            assert ctx.targeting_key == "calm", where  # the read's own key wins
        else:
            assert ctx.targeting_key == "FFA", where
    assert ("game", "windows") in keys
    windows = [ctx for d, k, ctx in reads if (d, k) == ("game", "windows")]
    assert any(ctx.attributes.get("game_kind") == "shadow" for ctx in windows)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
async def test_handler_reads_of_a_real_game_resolve_as_real(seen):
    """(iii) A real (primary) game: game_kind=real, no experiment or arm, its game context."""
    game = _make_ffa()
    _bind(game, "primary")
    reads = await _run(game, seen, game_task=_running(game))
    assert reads
    for domain, key, ctx in reads:
        where = f"{domain}.{key}"
        assert ctx.attributes.get("game_kind") == "real", where
        assert "experiment_id" not in ctx.attributes and "arm" not in ctx.attributes, where
        assert ctx.attributes.get("game_mode") == "FFA", where


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
async def test_handler_reads_before_the_run_see_the_session_kind(seen):
    """(iii) Init phase (the run has not set the game's context yet): the reads see the
    session-kind context the game's init reads saw, game_kind and the experiment."""
    game = _make_ffa()
    _bind(game, "shadow", EXPERIMENT)
    reads = await _run(game, seen)
    assert reads
    for domain, key, ctx in reads:
        where = f"{domain}.{key}"
        assert ctx.attributes.get("experiment_id") == "exp_abc", where
        assert ctx.attributes.get("arm") == "experimental", where
        assert "game_mode" not in ctx.attributes, where
        if not _is_windows_baseline_read(domain, key, ctx):
            assert ctx.attributes.get("game_kind") == "shadow", where
