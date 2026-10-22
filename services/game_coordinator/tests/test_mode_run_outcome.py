"""Root-span outcome of the non-FFA modes (#1328, telemetry.md §3.2).

Each mode UC owns a rule (UC-021 BR-002, UC-022 BR-004, UC-023 BR-003, UC-024 BR-003,
UC-025 BR-003, UC-026 BR-002): the root span records ``completed`` for a natural end, ``force_ended``
(with the reason) when force-ended. FightClub, Werewolf, Zombies and Tournament run their
own ``run()``; the team modes and NonStop use the base ``run()``. One test per UC so each
carries its own UC tag; the helpers are shared.
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
TEAM_MODES = [Games.JoustTeams, Games.JoustRandomTeams]
ONE_V_ONE_MODES = [Games.FightClub, Games.Tournament]


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


async def _assert_force_ended_records_reason(mode):
    game, root = _game(mode)
    task = asyncio.create_task(game.run())
    await asyncio.sleep(0.3)  # somewhere in intro / countdown / gameplay
    game.force_end("dry_run_2")
    await asyncio.wait_for(task, timeout=20)

    assert _root_attrs(root) == {"game.outcome": "force_ended", "game.end_reason": "dry_run_2"}
    assert root.set_attributes.call_count == 1


def _assert_natural_end_is_completed_and_survives_late_force_end(mode):
    game, root = _game(mode)
    game._capture_game_root_span()
    game._latch_game_outcome()  # what the run() does when its loop returns
    game.force_end("late")
    teardown = _span()
    game._record_game_outcome(teardown)
    assert _root_attrs(root) == {"game.outcome": "completed"}
    teardown.add_event.assert_called_once_with("game.outcome", attributes={"game.outcome": "completed"})


def _assert_run_wires_the_outcome(mode):
    import inspect

    src = inspect.getsource(_GAME_CLASSES[mode].run)
    assert "self._capture_game_root_span()" in src
    assert "self._latch_game_outcome()" in src
    assert "self._record_game_outcome(teardown_span)" in src
    assert "self._record_force_ended_outcome_if_unrecorded()" in src


@pytest.mark.parametrize("mode", TEAM_MODES, ids=[m.name for m in TEAM_MODES])
@pytest.mark.uc("UC-021", br=["BR-002"])
@pytest.mark.asyncio
async def test_teams_force_ended_records_reason(mode):
    """UC-021 BR-002: a force-ended team-mode game records force_ended + reason on the root span."""
    await _assert_force_ended_records_reason(mode)


@pytest.mark.parametrize("mode", TEAM_MODES, ids=[m.name for m in TEAM_MODES])
@pytest.mark.uc("UC-021", br=["BR-002"])
def test_teams_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-021 BR-002: a natural end latches completed; a later force-end does not rewrite it."""
    _assert_natural_end_is_completed_and_survives_late_force_end(mode)


@pytest.mark.parametrize("mode", [Games.Werewolf], ids=[m.name for m in [Games.Werewolf]])
@pytest.mark.uc("UC-022", br=["BR-004"])
@pytest.mark.asyncio
async def test_werewolf_force_ended_records_reason(mode):
    """UC-022 BR-004: a force-ended Werewolf game records force_ended + reason on the root span."""
    await _assert_force_ended_records_reason(mode)


@pytest.mark.parametrize("mode", [Games.Werewolf], ids=[m.name for m in [Games.Werewolf]])
@pytest.mark.uc("UC-022", br=["BR-004"])
def test_werewolf_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-022 BR-004: a natural end latches completed; a later force-end does not rewrite it."""
    _assert_natural_end_is_completed_and_survives_late_force_end(mode)


@pytest.mark.parametrize("mode", [Games.Zombies], ids=[m.name for m in [Games.Zombies]])
@pytest.mark.uc("UC-023", br=["BR-003"])
@pytest.mark.asyncio
async def test_zombies_force_ended_records_reason(mode):
    """UC-023 BR-003: a force-ended Zombies game records force_ended + reason on the root span."""
    await _assert_force_ended_records_reason(mode)


@pytest.mark.parametrize("mode", [Games.Zombies], ids=[m.name for m in [Games.Zombies]])
@pytest.mark.uc("UC-023", br=["BR-003"])
def test_zombies_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-023 BR-003: a natural end latches completed; a later force-end does not rewrite it."""
    _assert_natural_end_is_completed_and_survives_late_force_end(mode)


@pytest.mark.parametrize("mode", ONE_V_ONE_MODES, ids=[m.name for m in ONE_V_ONE_MODES])
@pytest.mark.uc("UC-025", br=["BR-003"])
@pytest.mark.asyncio
async def test_fight_club_tournament_force_ended_records_reason(mode):
    """UC-025 BR-003: a force-ended Fight Club / Tournament game records force_ended + reason on the root span."""
    await _assert_force_ended_records_reason(mode)


@pytest.mark.parametrize("mode", ONE_V_ONE_MODES, ids=[m.name for m in ONE_V_ONE_MODES])
@pytest.mark.uc("UC-025", br=["BR-003"])
def test_fight_club_tournament_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-025 BR-003: a natural end latches completed; a later force-end does not rewrite it."""
    _assert_natural_end_is_completed_and_survives_late_force_end(mode)


@pytest.mark.parametrize("mode", [Games.NonStop], ids=[m.name for m in [Games.NonStop]])
@pytest.mark.uc("UC-026", br=["BR-002"])
@pytest.mark.asyncio
async def test_nonstop_force_ended_records_reason(mode):
    """UC-026 BR-002: a force-ended NonStop game records force_ended + reason on the root span."""
    await _assert_force_ended_records_reason(mode)


@pytest.mark.parametrize("mode", [Games.NonStop], ids=[m.name for m in [Games.NonStop]])
@pytest.mark.uc("UC-026", br=["BR-002"])
def test_nonstop_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-026 BR-002: a natural end latches completed; a later force-end does not rewrite it."""
    _assert_natural_end_is_completed_and_survives_late_force_end(mode)


@pytest.mark.parametrize("mode", [Games.Swapper], ids=[m.name for m in [Games.Swapper]])
@pytest.mark.uc("UC-024", br=["BR-003"])
@pytest.mark.asyncio
async def test_swapper_force_ended_records_reason(mode):
    """UC-024 BR-003: a force-ended Swapper game records force_ended + reason on the root span."""
    await _assert_force_ended_records_reason(mode)


@pytest.mark.parametrize("mode", [Games.Swapper], ids=[m.name for m in [Games.Swapper]])
@pytest.mark.uc("UC-024", br=["BR-003"])
def test_swapper_natural_end_is_completed_and_survives_late_force_end(mode):
    """UC-024 BR-003: a natural end latches completed; a later force-end does not rewrite it."""
    _assert_natural_end_is_completed_and_survives_late_force_end(mode)


@pytest.mark.parametrize("mode", [Games.Werewolf], ids=[m.name for m in [Games.Werewolf]])
@pytest.mark.uc("UC-022", br=["BR-004"])
def test_werewolf_run_override_wires_the_outcome(mode):
    """UC-022 BR-004: the run() override captures the root span, latches after its loop and records in teardown."""
    _assert_run_wires_the_outcome(mode)


@pytest.mark.parametrize("mode", [Games.Zombies], ids=[m.name for m in [Games.Zombies]])
@pytest.mark.uc("UC-023", br=["BR-003"])
def test_zombies_run_override_wires_the_outcome(mode):
    """UC-023 BR-003: the run() override captures the root span, latches after its loop and records in teardown."""
    _assert_run_wires_the_outcome(mode)


@pytest.mark.parametrize("mode", ONE_V_ONE_MODES, ids=[m.name for m in ONE_V_ONE_MODES])
@pytest.mark.uc("UC-025", br=["BR-003"])
def test_fight_club_tournament_run_override_wires_the_outcome(mode):
    """UC-025 BR-003: the run() override captures the root span, latches after its loop and records in teardown."""
    _assert_run_wires_the_outcome(mode)
