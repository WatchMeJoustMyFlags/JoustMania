"""
The intervention target Protocol matches the real game modes and the shared fake (#2361).

``intervention_target.InterventionTarget`` lists what intervention handlers may use on a
live game. Nothing imports it at runtime, so these tests are what keep it honest:
- every mode in ``game_factory._GAME_CLASSES`` has every member, with the Protocol's
  parameter names;
- ``FakeInterventionGame`` (the double the handler tests share) has the same members, so a
  handler that relies on something the Protocol lacks fails against the fake first.
"""

import dataclasses
import inspect
import sys
import typing
from pathlib import Path

import pytest
from openfeature.evaluation_context import EvaluationContext

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.game_coordinator.game_factory import _GAME_CLASSES, GameFactory
from services.game_coordinator.games.base import Player
from services.game_coordinator.intervention_target import InterventionPlayer, InterventionTarget
from services.game_coordinator.interventions import MODE_CAPABILITY_DENY
from services.game_coordinator.tests.conftest import FakeInterventionGame, async_noop

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
