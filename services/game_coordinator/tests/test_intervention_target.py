"""
The intervention target Protocol matches the real game modes and the shared fake (#2361).

``intervention_target.InterventionTarget`` lists what intervention handlers may use on a
live game. Nothing imports it at runtime, so these tests are what keep it honest:
- every mode in ``game_factory._GAME_CLASSES`` has every member, with the Protocol's
  parameter names;
- ``FakeInterventionGame`` (the double the handler tests share) has the same members, so a
  handler that relies on something the Protocol lacks fails against the fake first;
- the handler modules only use Protocol members on the game and its players, and never
  probe the game with ``getattr`` / ``hasattr``;
- every registered handler runs on the shared fake without raising.
"""

import ast
import dataclasses
import inspect
import sys
import typing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openfeature.evaluation_context import EvaluationContext

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.game_factory import _GAME_CLASSES, GameFactory
from services.game_coordinator.games.base import Player
from services.game_coordinator.intervention_target import InterventionPlayer, InterventionTarget
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    MODE_CAPABILITY_DENY,
    InterventionContext,
    InterventionManager,
    run_in_game_flag_context,
)
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers
from services.game_coordinator.tests.conftest import FakeInterventionGame, MockGameplayStream, async_noop

# Attributes a mode only sets once its game starts (BaseGameMode.on_music_start), not in __init__.
SET_AT_GAME_START = frozenset({"music_speed"})

TARGET_MEMBERS = sorted(typing.get_protocol_members(InterventionTarget))
METHOD_MEMBERS = [m for m in TARGET_MEMBERS if inspect.isfunction(inspect.getattr_static(InterventionTarget, m, None))]


def _build_mode(mode):
    return GameFactory.create_game(
        game_name=mode.name,
        controller_manager_client=object(),
        event_publisher=async_noop,
        audio_client=object(),
        game_id=f"target_{mode.name}",
        initial_players=[],
    )


def _all_mode_names() -> set[str]:
    return {_build_mode(mode).get_game_name() for mode in _GAME_CLASSES}


def _check_target(game, *, skip=frozenset()):
    """Every Protocol member exists on ``game`` and each method takes the Protocol's parameters."""
    for name in TARGET_MEMBERS:
        if name in skip:
            continue
        assert hasattr(game, name), f"{type(game).__name__} lacks {name}"
    for name in METHOD_MEMBERS:
        assert callable(getattr(game, name, None)), f"{type(game).__name__}.{name} is not callable"
        wanted = list(inspect.signature(getattr(InterventionTarget, name)).parameters)[1:]
        actual = set(inspect.signature(getattr(game, name)).parameters)
        assert set(wanted) <= actual, f"{type(game).__name__}.{name} takes {sorted(actual)}, Protocol has {wanted}"
    assert isinstance(game.flag_eval_context, EvaluationContext)
    player_fields = set(typing.get_type_hints(InterventionPlayer))
    assert player_fields <= {f.name for f in dataclasses.fields(Player)}
    for player in game.players.values():
        missing = [f for f in player_fields if not hasattr(player, f)]
        assert not missing, f"player lacks {missing}"


@pytest.mark.parametrize("mode", list(_GAME_CLASSES), ids=lambda m: m.name)
def test_every_game_mode_implements_the_intervention_target(mode):
    game = _build_mode(mode)
    _check_target(game, skip=SET_AT_GAME_START)
    # The deny matrix is keyed by get_game_name(): a key no mode reports would silently never deny.
    names = _all_mode_names()
    assert set(MODE_CAPABILITY_DENY) <= names, sorted(set(MODE_CAPABILITY_DENY) - names)
    assert game.get_game_name() in names


def test_shared_fake_implements_the_intervention_target():
    fake = FakeInterventionGame()
    _check_target(fake)
    # Its default name is a real mode that denies nothing, so no handler test is blocked by the mode matrix.
    assert fake.get_game_name() in _all_mode_names()
    assert fake.get_game_name() not in MODE_CAPABILITY_DENY


# --------------------------------------------------------------------------- #
# The handler modules stay on the Protocol (#2361 PR 2)
# --------------------------------------------------------------------------- #
HANDLER_MODULES = ("interventions.py", "lifecycle_handlers.py", "difficulty_handlers.py")
PLAYER_NAMES = frozenset({"player", "p"})  # the loop names the handlers use for a game's players


def _is_game(node: ast.expr) -> bool:
    """``game`` or ``<anything>.game`` (``ctx.game``, ``session.game``)."""
    return (isinstance(node, ast.Name) and node.id == "game") or (
        isinstance(node, ast.Attribute) and node.attr == "game"
    )


def _scan(module: str) -> tuple[set[str], set[str], list[str]]:
    tree = ast.parse((test_dir.parent / module).read_text())
    game_attrs: set[str] = set()
    player_attrs: set[str] = set()
    probes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if _is_game(node.value):
                game_attrs.add(node.attr)
            elif isinstance(node.value, ast.Name) and node.value.id in PLAYER_NAMES:
                player_attrs.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and node.args
            and _is_game(node.args[0])
        ):
            probes.append(f"{module}:{node.lineno} {ast.unparse(node)}")
    return game_attrs, player_attrs, probes


@pytest.mark.parametrize("module", HANDLER_MODULES)
def test_handlers_only_touch_the_intervention_target(module):
    game_attrs, player_attrs, probes = _scan(module)
    assert not probes, f"probe the game through the Protocol, not getattr/hasattr: {probes}"
    target_members = set(typing.get_protocol_members(InterventionTarget))
    assert game_attrs <= target_members, f"not on InterventionTarget: {sorted(game_attrs - target_members)}"
    player_members = set(typing.get_protocol_members(InterventionPlayer))
    assert player_attrs <= player_members, f"not on InterventionPlayer: {sorted(player_attrs - player_members)}"


# --------------------------------------------------------------------------- #
# Every registered handler runs on the shared fake (#2361 PR 2)
# --------------------------------------------------------------------------- #
SPECS_BY_FLAG = {spec.flag_key: spec for spec in INTERVENTION_SPECS}
# flag_key -> (value, payload, target_serial) for an apply that changes something.
APPLY_SAMPLES = {
    "music_tempo_override": (1.2, "", None),
    "global_sensitivity_override": (3, "", None),
    "player_sensitivity_factor": ({"p1": 1.5}, "", None),
    "player_handicap_factor": ({"p1": 1.5}, "", None),
    "shield_seconds": (5.0, "", "p1"),
    "toughness_boost_seconds": ("5:1.5", "", "p1"),
    "soft_penalty_action": ("tighten", "", "p1"),
    "auto_rubberband": ("gentle", "", None),
    "volume_override": (0.5, "", None),
    "global_difficulty_factor": (1.2, "", None),
    "pacing_profile": ("fast", "", None),
    "tempo_ramp": ("1.3:8:linear", "", None),
    "eliminate_player": (None, "p1", None),
    "revive_player": (None, "p2", None),
    "audio_cue": (None, "wolfdown", None),
    "controller_effect": (None, ":pulse", None),
    "end_game": (None, "", None),
}
# flag_key -> value for the revert-to-neutral path.
REVERT_SAMPLES = {
    "volume_override": -1,
    "music_tempo_override": 0,
    "player_sensitivity_factor": {"p1": 1.0},
    "player_handicap_factor": {"p1": 1.0},
    "global_difficulty_factor": 1.0,
    "pacing_profile": "none",
    "tempo_ramp": "none",
}


def _wired_manager() -> InterventionManager:
    manager = InterventionManager(event_publisher=AsyncMock(), end_game_fn=AsyncMock(return_value=(True, "")))
    manager.register_ambient_handlers()
    register_lifecycle_handlers(manager)
    register_difficulty_handlers(manager)
    return manager


def _ctx(flag_key, game, value, payload="", target=None) -> InterventionContext:
    return InterventionContext(
        spec=SPECS_BY_FLAG[flag_key],
        value=value,
        payload=payload,
        target_serial=target,
        game=game,
        objective="",
    )


def _fake_game() -> FakeInterventionGame:
    return FakeInterventionGame(audio_client=AsyncMock(), gameplay_stream=MockGameplayStream())


def test_every_registered_handler_has_a_sample():
    """A new handler forces a row here, so it runs on the shared fake below."""
    manager = _wired_manager()
    assert set(APPLY_SAMPLES) == set(manager._handlers)
    assert set(REVERT_SAMPLES) == set(manager._revert_handlers)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_key", sorted(APPLY_SAMPLES))
async def test_every_registered_handler_runs_on_the_shared_fake(flag_key):
    """A handler that uses something the fake (so the Protocol) lacks raises AttributeError,
    which the manager would record as ``handler_error``."""
    manager = _wired_manager()
    game = _fake_game()
    game.players["p2"].alive = False  # revive_player needs a dead player
    value, payload, target = APPLY_SAMPLES[flag_key]
    handler = manager._handlers[flag_key]
    await run_in_game_flag_context(game, handler, _ctx(flag_key, game, value, payload, target))


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_key", sorted(REVERT_SAMPLES))
async def test_every_revert_handler_runs_on_the_shared_fake(flag_key):
    manager = _wired_manager()
    game = _fake_game()
    handler = manager._revert_handlers[flag_key]
    await run_in_game_flag_context(game, handler, _ctx(flag_key, game, REVERT_SAMPLES[flag_key]))
