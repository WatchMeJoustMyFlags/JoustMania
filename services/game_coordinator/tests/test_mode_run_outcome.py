"""Outcome recording for modes that override run() (#1328, UC-020 BR-003 / telemetry.md §3.2).

FightClub, Werewolf, Zombies and Tournament run their own ``run()``. They must
put the outcome on the game root span too: ``completed`` for a natural end,
``force_ended`` (with the reason) when force-ended.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from opentelemetry import trace

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import Games
from services.game_coordinator.game_factory import _GAME_CLASSES

RUN_OVERRIDE_MODES = [Games.FightClub, Games.Werewolf, Games.Zombies, Games.Tournament]


def _span():
    span = create_autospec(trace.Span, instance=True)
    span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
    return span


def _game(mode: Games):
    events = EventCollector()
    game = _GAME_CLASSES[mode](
        controller_manager_client=MockControllerManagerService(num_controllers=4, infinite=True),
        event_publisher=events.publish,
        audio_client=None,
        game_id=f"test_outcome_{mode.name.lower()}",
        initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(4)],
    )
    root = _span()

    def _capture():
        game._game_root_span = root

    game._capture_game_root_span = _capture
    return game, root


def _root_attrs(root):
    attrs = {}
    for c in root.set_attributes.call_args_list:
        attrs.update(c.args[0])
    return attrs


@pytest.mark.parametrize("mode", RUN_OVERRIDE_MODES, ids=[m.name for m in RUN_OVERRIDE_MODES])
@pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
@pytest.mark.asyncio
async def test_run_override_force_ended_records_reason(mode):
    """UC-020 BR-003: a force-ended run() override records force_ended + reason on the root span."""
    game, root = _game(mode)
    task = asyncio.create_task(game.run())
    await asyncio.sleep(0.3)  # somewhere in intro / countdown / gameplay
    game.force_end("dry_run_2")
    await asyncio.wait_for(task, timeout=20)

    assert _root_attrs(root) == {"game.outcome": "force_ended", "game.end_reason": "dry_run_2"}
    assert root.set_attributes.call_count == 1


@pytest.mark.parametrize("mode", RUN_OVERRIDE_MODES, ids=[m.name for m in RUN_OVERRIDE_MODES])
@pytest.mark.uc("UC-020", br=["BR-003"])
def test_run_override_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-020 BR-003: a natural end latches completed; a later force-end does not rewrite it."""
    game, root = _game(mode)
    game._capture_game_root_span()
    game._latch_game_outcome()  # what the run() override does when its loop returns
    game.force_end("late")
    teardown = _span()
    game._record_game_outcome(teardown)
    assert _root_attrs(root) == {"game.outcome": "completed"}
    teardown.add_event.assert_called_once_with("game.outcome", attributes={"game.outcome": "completed"})


@pytest.mark.parametrize("mode", RUN_OVERRIDE_MODES, ids=[m.name for m in RUN_OVERRIDE_MODES])
@pytest.mark.uc("UC-020", br=["BR-003"])
def test_run_override_wires_the_outcome(mode):
    """UC-020 BR-003: the override captures the root span, latches after its loop and records in teardown."""
    import inspect

    src = inspect.getsource(_GAME_CLASSES[mode].run)
    assert "self._capture_game_root_span()" in src
    assert "self._latch_game_outcome()" in src
    assert "self._record_game_outcome(teardown_span)" in src
    assert "self._record_force_ended_outcome_if_unrecorded()" in src
