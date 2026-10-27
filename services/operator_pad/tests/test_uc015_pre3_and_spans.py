"""UC-015 Pre-3 (the pad acts only through the menu) and the A3 gesture span (#2050)."""

import re
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import grpc
import pytest
from opentelemetry import trace

from proto import game_coordinator_pb2, menu_pb2
from services.operator_pad import actions as actions_module
from services.operator_pad.actions import Actions
from services.operator_pad.cues import CuePlayer
from services.operator_pad.gestures import HOLD, KNOB, KNOB_PRESS_GESTURE, LAYER, TAP, Gesture
from services.operator_pad.tests.fakes import CoordinatorStubSpec, MenuStubSpec

PAD_DIR = Path(actions_module.__file__).parent


def lobby_status() -> menu_pb2.GetMenuStatusResponse:
    return menu_pb2.GetMenuStatusResponse(
        lifecycle_live=False,
        lifecycle_generation=1,
        agent_enabled=True,
        agent_enabled_state="on",
        agent_interventions_enabled_state="on",
        agent_level="standard",
        agent_backend="rules",
        audio_volume_master="normal",
    )


def unavailable() -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata(), "down")


@pytest.fixture
def menu():
    return create_autospec(MenuStubSpec, instance=True)


@pytest.fixture
def coordinator():
    stub = create_autospec(CoordinatorStubSpec, instance=True)
    stub.ForceEndGame.return_value = game_coordinator_pb2.ForceEndGameResponse(success=True)
    return stub


@pytest.fixture
def cues():
    return create_autospec(CuePlayer, instance=True)


@pytest.fixture
def actions(menu, coordinator, cues):
    return Actions(menu, coordinator, cues)


@pytest.mark.uc("UC-015", cond=["Pre-3"], br=["BR-001"])
def test_the_pad_service_holds_no_flag_writer():
    """The pad service imports no flag-service writer and never calls a write on one (BR-001)."""
    forbidden = re.compile(
        r"^\s*(from|import)\s.*flag_service_writer|FlagServiceWriter|update_default_variant|\.step_variant\(",
        re.MULTILINE,
    )
    offenders = [path.name for path in PAD_DIR.glob("*.py") if forbidden.search(path.read_text())]
    assert offenders == []


@pytest.mark.uc("UC-015", cond=["Pre-3"], br=["BR-001"])
@pytest.mark.parametrize(
    ("gesture", "expected"),
    [
        (Gesture(TAP, "K1"), "start_game"),
        (Gesture(TAP, "K6"), "step_setting"),
        (Gesture(LAYER, "K2", steps=1), "step_setting"),
        (Gesture(LAYER, "K7", steps=1), "set_backend"),
        (Gesture(HOLD, "K3"), "agent_switch"),
        (Gesture(KNOB_PRESS_GESTURE, KNOB), "step_setting"),
    ],
)
async def test_every_setting_gesture_goes_to_the_menu_process_input(actions, menu, coordinator, gesture, expected):
    menu.ProcessInput.return_value = menu_pb2.ProcessInputResponse(success=True)
    await actions.dispatch(gesture, lobby_status(), lobby_status())
    assert [dict(c.args[0].data)["command"] for c in menu.ProcessInput.await_args_list] == [expected]
    assert all(c.args[0].input_type == "web_command" for c in menu.ProcessInput.await_args_list)
    coordinator.ForceEndGame.assert_not_awaited()


@pytest.mark.uc("UC-015", cond=["Pre-3"], flow="A3", br=["BR-010"])
@pytest.mark.parametrize(
    "gesture",
    [
        Gesture(TAP, "K1"),
        Gesture(TAP, "K6"),
        Gesture(LAYER, "K5", steps=1),
        Gesture(LAYER, "K7", steps=1),
        Gesture(HOLD, "K3"),
        Gesture(HOLD, "K4"),
        Gesture(KNOB_PRESS_GESTURE, KNOB),
    ],
)
async def test_menu_not_running_fails_closed_and_is_not_retried(actions, menu, coordinator, cues, gesture):
    """The menu answers UNAVAILABLE: one attempt, the failure cue, nothing queued, no other service called."""
    menu.ProcessInput.side_effect = unavailable()
    _, result = await actions.dispatch(gesture, lobby_status(), lobby_status())
    assert result == "menu_unavailable"
    assert menu.ProcessInput.await_count == 1
    cues.failure.assert_awaited_once()
    coordinator.ForceEndGame.assert_not_awaited()


def recording_tracer():
    span = MagicMock(spec=trace.Span)
    tracer = MagicMock()
    tracer.start_as_current_span.return_value.__enter__.return_value = span
    return tracer, span


@pytest.mark.uc("UC-015", flow="A3", br=["BR-009"])
async def test_an_unreachable_menu_marks_the_gesture_span_as_an_error(actions, menu):
    menu.ProcessInput.side_effect = unavailable()
    tracer, span = recording_tracer()
    with patch.object(actions_module, "tracer", tracer):
        await actions.dispatch(Gesture(LAYER, "K2", steps=1), lobby_status())

    tracer.start_as_current_span.assert_called_once_with("operator_pad.gesture")
    span.set_attribute.assert_any_call("pad.result", "menu_unavailable")
    (status,) = (c.args[0] for c in span.set_status.call_args_list)
    assert status.status_code == trace.StatusCode.ERROR


@pytest.mark.uc("UC-015", flow="A3", br=["BR-012"])
async def test_a_refused_write_records_its_result_on_the_gesture_span(actions, menu):
    menu.ProcessInput.return_value = menu_pb2.ProcessInputResponse(success=False, error="flag_service_unavailable")
    tracer, span = recording_tracer()
    with patch.object(actions_module, "tracer", tracer):
        await actions.dispatch(Gesture(LAYER, "K2", steps=1), lobby_status())

    span.set_attribute.assert_any_call("pad.result", "flag_service_unavailable")
    span.set_attribute.assert_any_call("pad.action", "step_setting")
