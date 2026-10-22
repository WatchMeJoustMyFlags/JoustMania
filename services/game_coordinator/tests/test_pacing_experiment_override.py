"""
An experiment on ``windows`` overrides the agent's pacing lever (#1970).

UC-043 BR-005 / UC-040 A22: a ``pacing_profile`` intervention runs in its game's flag
context (UC-040 BR-018), so in an experimental-arm shadow game the experiment's
``windows`` rung wins over the requested preset. When the experiment's value differs
from the preset's, the coordinator applies nothing (the windows stay), records
``blocked=true``, ``block_reason=experiment_override`` and refunds the backstop
reservation. An equal value is applied. The control arm, a real game and a
non-experiment shadow game resolve the preset as before.

The ``game`` set is an in-memory provider whose ``windows`` targeting has the shape
the flag service renders (render.go): the experiment rung
``game_kind != real AND experiment_id == X AND arm == experimental`` with a generated
variant name and an inline value, in front of the preset ladder (targetingKey).
"""

import asyncio
import sys
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
from lib.types import GameEvent
from services.game_coordinator.difficulty_handlers import BLOCK_EXPERIMENT_OVERRIDE, register_difficulty_handlers
from services.game_coordinator.games.base import Player, resolve_music_windows
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.interventions import INTERVENTION_SPECS, SessionView
from services.game_coordinator.tests.test_interventions import make_manager

EXPERIMENT_ID = "exp_1970"
DEFAULT = {
    "min_music_fast_time": 4,
    "max_music_fast_time": 8,
    "min_music_slow_time": 10,
    "max_music_slow_time": 23,
    "end_min_music_fast_time": 6,
    "end_max_music_fast_time": 10,
    "end_min_music_slow_time": 8,
    "end_max_music_slow_time": 12,
}
CALM = {**DEFAULT, "min_music_slow_time": 13, "max_music_slow_time": 30}
FRANTIC = {**DEFAULT, "min_music_fast_time": 6, "max_music_fast_time": 11}
# An experiment value the agent never asked for (the flag service inlines it under r<n>).
EXPERIMENT_VALUE = {**DEFAULT, "min_music_slow_time": 17, "max_music_slow_time": 40}
SPEC = next(s for s in INTERVENTION_SPECS if s.flag_key == "pacing_profile")


def _windows_flag(experiment_value: dict) -> InMemoryFlag:
    variants = {"default": DEFAULT, "calm": CALM, "frantic": FRANTIC, "r7": experiment_value}

    def evaluate(flag, ctx):
        attrs = ctx.attributes
        if (
            attrs.get("game_kind") != "real"
            and attrs.get("experiment_id") == EXPERIMENT_ID
            and attrs.get("arm") == "experimental"
        ):
            variant = "r7"
        elif ctx.targeting_key in ("calm", "frantic"):
            variant = ctx.targeting_key
        else:
            variant = "default"
        return FlagResolutionDetails(value=flag.variants[variant], variant=variant, reason=Reason.TARGETING_MATCH)

    return InMemoryFlag("default", variants, context_evaluator=evaluate)


@pytest.fixture
def game_flags():
    """The real OpenFeature API wired as lib.feature_flags does; returns a setter for the experiment value."""
    saved_ctx = api.get_evaluation_context()
    saved_propagator = of_tx._evaluation_transaction_context_propagator
    saved_registered = ff._propagator_registered
    ff._propagator_registered = False

    def serve(experiment_value: dict = EXPERIMENT_VALUE) -> None:
        api.set_provider(InMemoryProvider({"windows": _windows_flag(experiment_value)}), domain="game")

    with patch.object(ff, "init_flag_domain"):
        ff._init_global_context()
        api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
        serve()
        try:
            yield serve
        finally:
            api.clear_providers()
            api.set_evaluation_context(saved_ctx)
            of_tx.set_transaction_context_propagator(saved_propagator)
            ff._propagator_registered = saved_registered


async def _game(game_kind: str, experiment_id: str = "", arm: str = "") -> FFAGame:
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=AsyncMock(),
        game_id="g-1970",
    )
    for serial in ("p1", "p2"):
        game.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    game.game_kind, game.experiment_id, game.arm = game_kind, experiment_id, arm

    async def run_starts():  # the game's own task (games/base.py run())
        game._set_flag_eval_context()

    await asyncio.create_task(run_starts())
    # The caller (the manager's main loop) carries no game context.
    api.set_transaction_context(EvaluationContext())
    return game


async def _apply(game, profile: str):
    mgr, events = make_manager(game=game)
    register_difficulty_handlers(mgr)
    session = SessionView(game_id="g-1970", game=game, publish=mgr._publish)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr._enforce_and_dispatch(SPEC, profile, "", None, session)
    [event] = [d for et, d in events if et == GameEvent.AGENT_INTERVENTION]
    return mgr, event


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", br=["BR-005"])
@pytest.mark.uc("UC-040", flow="A22", br=["BR-018"])
@pytest.mark.usefixtures("game_flags")
async def test_experimental_arm_blocks_the_lever_and_keeps_the_experiment_windows():
    """Experimental arm, the experiment sets other windows: not applied, windows unchanged,
    blocked experiment_override, the backstop reservation refunded."""
    game = await _game("shadow", EXPERIMENT_ID, "experimental")
    before = game.music_windows
    mgr, event = await _apply(game, "calm")
    assert event["blocked"] == "true"
    assert event["block_reason"] == BLOCK_EXPERIMENT_OVERRIDE
    assert game.music_windows is before
    assert mgr._rate_limiter.current_weight() == 0.0


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", br=["BR-005"])
async def test_experiment_value_equal_to_another_preset_still_blocks(game_flags):
    """No variant names: an experiment value byte-identical to frantic overrides a calm request."""
    game_flags(FRANTIC)
    game = await _game("shadow", EXPERIMENT_ID, "experimental")
    before = game.music_windows
    _, event = await _apply(game, "calm")
    assert event["block_reason"] == BLOCK_EXPERIMENT_OVERRIDE
    assert game.music_windows is before


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", br=["BR-005"])
async def test_experiment_value_equal_to_the_requested_preset_is_applied(game_flags):
    """The experiment's windows equal the requested preset's (the seed experiment's
    windows_frantic): the lever changes nothing the experiment set, so it is applied."""
    game_flags(FRANTIC)
    game = await _game("shadow", EXPERIMENT_ID, "experimental")
    mgr, event = await _apply(game, "frantic")
    assert event["blocked"] == "false"
    assert game.music_windows == resolve_music_windows(FRANTIC)
    assert mgr._rate_limiter.current_weight() > 0.0


@pytest.mark.asyncio
@pytest.mark.uc("UC-043", br=["BR-005"])
@pytest.mark.uc("UC-040", br=["BR-018"])
@pytest.mark.parametrize(
    ("game_kind", "experiment_id", "arm"),
    [
        ("shadow", EXPERIMENT_ID, "control"),  # control arm: no experiment value
        ("primary", "", ""),  # real game
        ("shadow", "", ""),  # shadow game in no experiment
    ],
    ids=["control-arm", "real-game", "non-experiment-shadow"],
)
@pytest.mark.usefixtures("game_flags")
async def test_games_without_the_experiment_rung_apply_the_preset(game_kind, experiment_id, arm):
    """Control arm, real game, non-experiment shadow game: the preset applies as before."""
    game = await _game(game_kind, experiment_id, arm)
    _, event = await _apply(game, "calm")
    assert event["blocked"] == "false"
    assert game.music_windows == resolve_music_windows(CALM)
