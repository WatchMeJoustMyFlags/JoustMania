"""GetMenuStatus, lifecycle_generation and the force_start stale check (#2050, UC-015 A5, services.md §3.1).

A real MenuServicer (servicer_harness). The flag clients are in-process fakes that resolve from a
dict; the flag-service writers are autospecs, so a test can prove the snapshot never calls them.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, call, create_autospec, patch

import pytest
import pytest_asyncio
from openfeature.client import OpenFeatureClient
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason
from openfeature.provider.no_op_provider import PASSED_IN_DEFAULT

from lib.types import Games
from proto import controller_manager_pb2, controller_manager_pb2_grpc, menu_pb2
from services.menu.handlers.base import ControllerState
from services.menu.tests.servicer_harness import cancel_background_tasks, make_real_servicer

# (flag key) -> (variant, value) the in-process client resolves.
FLAGS = {
    "game": {
        "sensitivity": ("fast", 3),
        "num_teams": ("four", 4),
        "force_all_start": ("on", True),
    },
    "user": {
        "game_instructions": ("on", True),
        "audio_volume.master": ("quiet", 0.3),
    },
    "agent": {
        "enabled": ("on", True),
        "interventions_enabled": ("off", False),
        "interventions_allowed": ("standard", "a,b"),
        "decision.backend": ("claude", "claude"),
    },
}


def in_process_client(values: dict[str, tuple[str, object]]):
    """An OpenFeature client spec resolving from ``values``; unknown keys return the caller's default."""
    client = create_autospec(OpenFeatureClient, instance=True)

    def value(key, default, *_args, **_kwargs):
        return values[key][1] if key in values else default

    def details(key, default, *_args, **_kwargs):
        if key in values:
            variant, resolved = values[key]
            return FlagEvaluationDetails(flag_key=key, value=resolved, variant=variant)
        # What the in-process provider answers for a key it cannot resolve.
        return FlagEvaluationDetails(
            flag_key=key, value=default, variant=None, reason=Reason.ERROR, error_code=ErrorCode.FLAG_NOT_FOUND
        )

    for getter in ("boolean", "string", "integer", "float"):
        getattr(client, f"get_{getter}_value").side_effect = value
        getattr(client, f"get_{getter}_details").side_effect = details
    return client


@pytest_asyncio.fixture
async def menu():
    servicer = make_real_servicer()
    servicer.game_settings_client = in_process_client(FLAGS["game"])
    servicer.user_prefs_client = in_process_client(FLAGS["user"])
    servicer.agent_settings_client = in_process_client(FLAGS["agent"])
    yield servicer
    await cancel_background_tasks(servicer)


async def status(servicer) -> menu_pb2.GetMenuStatusResponse:
    return await servicer.GetMenuStatus(menu_pb2.GetMenuStatusRequest(), None)


async def force_start(servicer, **data) -> menu_pb2.ProcessInputResponse:
    request = menu_pb2.ProcessInputRequest(
        input_type="web_command", data={"command": "force_start", **{k: str(v) for k, v in data.items()}}
    )
    return await servicer.ProcessInput(request, None)


def live_roster(serials):
    response = controller_manager_pb2.GetConnectedControllersResponse(connected_serials=sorted(serials))
    stub = create_autospec(controller_manager_pb2_grpc.ControllerManagerServiceStub, instance=True)
    stub.GetConnectedControllers = AsyncMock(return_value=response)
    return patch("proto.controller_manager_pb2_grpc.ControllerManagerServiceStub", return_value=stub)


def held_lifecycle(servicer) -> asyncio.Event:
    """Patch the game lifecycle with one that runs until the returned event is set."""
    release = asyncio.Event()

    async def lifecycle(*_args, **_kwargs):
        await release.wait()

    servicer._run_game_lifecycle = lifecycle
    return release


# --- The snapshot ------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
async def test_status_reports_every_field_from_memory_and_the_in_process_clients(menu):
    menu.state = menu_pb2.MenuState.RUNNING
    menu.current_selection = Games.Werewolf
    menu.state_manager.controller_states = {
        "a": ControllerState.READY,
        "b": ControllerState.READY,
        "c": ControllerState.CONNECTED,
        "adm": ControllerState.ADMIN,
    }
    menu.admin_handler.active = True
    menu.admin_handler.controller_serial = "adm"
    menu.settings_ops.saved_agent_level = "full"

    reply = await status(menu)

    assert reply == menu_pb2.GetMenuStatusResponse(
        menu_state=menu_pb2.MenuState.RUNNING,
        lifecycle_live=False,
        lifecycle_generation=0,
        selection="Werewolf",
        ready_count=2,
        connected_count=4,
        admin_serial="adm",
        agent_enabled=True,
        agent_level="standard",
        agent_saved_level="full",
        agent_backend="claude",
        sensitivity=3,
        num_teams=4,
        instructions=True,
        force_all_start=True,
        audio_volume_master="quiet",
        agent_enabled_state="on",
        agent_interventions_enabled_state="off",
    )
    # Never the HTTP flag writer (0.5 s per flag): in-process clients and menu memory only.
    for writer in (
        menu.game_settings_writer,
        menu.user_prefs_writer,
        menu.agent_settings_writer,
        menu.agent_enabled_writer,
    ):
        assert writer.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
async def test_status_empty_values_when_nothing_is_set_or_resolvable(menu):
    menu.agent_settings_client = in_process_client({})
    menu.user_prefs_client = in_process_client({})

    reply = await status(menu)

    assert (reply.admin_serial, reply.agent_saved_level) == ("", "")
    assert (reply.agent_level, reply.agent_backend, reply.audio_volume_master) == ("", "", "")
    # The kill switch fails closed: unreadable reads as off, and says it is unknown (#2101).
    assert reply.agent_enabled is False
    assert reply.agent_enabled_state == "unknown"
    # So does the write gate (#2093).
    assert reply.agent_interventions_enabled_state == "unknown"


def details_client(details: FlagEvaluationDetails, flag_key: str = "enabled"):
    """An agent client whose ``flag_key`` evaluation returns ``details`` (every other key resolves)."""
    client = in_process_client(FLAGS["agent"])
    resolve = client.get_boolean_details.side_effect

    def boolean_details(key, default, *args, **kwargs):
        return details if key == flag_key else resolve(key, default, *args, **kwargs)

    client.get_boolean_details.side_effect = boolean_details
    return client


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A10", br=["BR-013"])
@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (FlagEvaluationDetails(flag_key="enabled", value=True, variant="on", reason=Reason.STATIC), "on"),
        (FlagEvaluationDetails(flag_key="enabled", value=False, variant="off", reason=Reason.STATIC), "off"),
        # flagd's own default variant is a real resolution.
        (FlagEvaluationDetails(flag_key="enabled", value=False, variant="off", reason=Reason.DEFAULT), "off"),
        # A provider that is not ready: the SDK answers the caller's default with an error code.
        (
            FlagEvaluationDetails(
                flag_key="enabled",
                value=False,
                reason=Reason.ERROR,
                error_code=ErrorCode.PROVIDER_NOT_READY,
            ),
            "unknown",
        ),
        # An error code without the ERROR reason is still not a resolution.
        (
            FlagEvaluationDetails(flag_key="enabled", value=False, variant="off", error_code=ErrorCode.GENERAL),
            "unknown",
        ),
        # No provider set yet: the no-op provider passes the default back with reason DEFAULT.
        (
            FlagEvaluationDetails(flag_key="enabled", value=False, variant=PASSED_IN_DEFAULT, reason=Reason.DEFAULT),
            "unknown",
        ),
        # No variant: the client fell back to the default.
        (FlagEvaluationDetails(flag_key="enabled", value=False, variant=None), "unknown"),
    ],
    ids=["on", "off", "flagd-default-variant", "not-ready", "error-code", "no-op-provider", "no-variant"],
)
async def test_status_agent_enabled_state_tells_unknown_from_off(menu, details, expected):
    menu.agent_settings_client = details_client(details)

    reply = await status(menu)

    assert reply.agent_enabled_state == expected
    assert reply.agent_enabled is (expected == "on")


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A10", br=["BR-002", "BR-013"])
@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (FlagEvaluationDetails(flag_key="interventions_enabled", value=True, variant="on"), "on"),
        (FlagEvaluationDetails(flag_key="interventions_enabled", value=False, variant="off"), "off"),
        (
            FlagEvaluationDetails(
                flag_key="interventions_enabled",
                value=False,
                reason=Reason.ERROR,
                error_code=ErrorCode.PROVIDER_NOT_READY,
            ),
            "unknown",
        ),
    ],
    ids=["on", "off", "not-ready"],
)
async def test_status_reports_the_write_gate_state(menu, details, expected):
    """#2093: the pad counts a closed write gate as stopped, so the status carries its tri-state."""
    menu.agent_settings_client = details_client(details, "interventions_enabled")

    reply = await status(menu)

    assert reply.agent_interventions_enabled_state == expected
    assert reply.agent_enabled_state == "on"  # the kill switch is reported on its own


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
@patch("services.menu.metrics.grpc_request_duration_seconds")
@patch("services.menu.metrics.grpc_requests_total")
async def test_status_records_the_grpc_request_metrics(requests_total, duration, menu):
    await status(menu)

    requests_total.labels.assert_called_once_with(method="GetMenuStatus", status="ok")
    requests_total.labels.return_value.inc.assert_called_once_with()
    assert duration.labels.call_args_list == [call(method="GetMenuStatus")]
    duration.labels.return_value.observe.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
@patch("services.menu.metrics.grpc_requests_total")
async def test_status_counts_a_failed_read_as_an_error(requests_total, menu):
    menu.agent_settings_client.get_boolean_details.side_effect = RuntimeError("provider gone")

    with pytest.raises(RuntimeError):
        await status(menu)

    requests_total.labels.assert_called_once_with(method="GetMenuStatus", status="error")
    requests_total.labels.return_value.inc.assert_called_once_with()


# --- lifecycle_generation ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
async def test_generation_goes_up_on_start_and_on_lobby_restore(menu):
    release = held_lifecycle(menu)
    before = await status(menu)

    await menu._start_game(serial="a", source="controller")
    started = await status(menu)
    release.set()
    await menu._game_lifecycle_task
    restored = await status(menu)

    assert [(r.lifecycle_live, r.lifecycle_generation) for r in (before, started, restored)] == [
        (False, 0),
        (True, 1),
        (False, 2),
    ]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
async def test_generation_changes_in_the_same_step_as_the_lifecycle(menu):
    """A read never pairs a lifecycle state with an older generation, even before any done callback ran."""
    release = held_lifecycle(menu)
    seen: list[tuple[bool, int]] = []
    await menu._start_game(serial="a", source="controller")
    task = menu._game_lifecycle_task
    # Registered first, so it runs before any other done callback (the awaiter's included).
    task.add_done_callback(lambda _t: seen.append((menu._lifecycle_live(), menu.lifecycle_generation)))
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert seen == [(False, 2)]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
async def test_generation_counts_a_lifecycle_cancelled_before_it_ran(menu):
    held_lifecycle(menu)
    await menu._start_game(serial="a", source="controller")
    menu._game_lifecycle_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await menu._game_lifecycle_task

    reply = await status(menu)
    assert (reply.lifecycle_live, reply.lifecycle_generation) == (False, 2)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5")
async def test_generation_goes_up_on_the_reset_menu_restore(menu):
    """reset_menu cancels a stuck lifecycle and restores the lobby as a new lifecycle task (UC-010 BR-008)."""
    held_lifecycle(menu)
    await menu._start_game(serial="a", source="controller")
    during_restore: list[tuple[bool, int]] = []

    async def restore():
        during_restore.append((menu._lifecycle_live(), menu.lifecycle_generation))

    with (
        patch.object(menu, "_primary_game_active", AsyncMock(return_value=False)),
        patch.object(menu, "_restart_lobby", side_effect=restore),
    ):
        await menu.ProcessInput(menu_pb2.ProcessInputRequest(input_type="reset_menu"), None)

    reply = await status(menu)
    assert during_restore == [(True, 3)]
    assert (reply.lifecycle_live, reply.lifecycle_generation) == (False, 4)


# --- force_start expected_generation (A5 step 2) ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5", br=["BR-012"])
async def test_force_start_refuses_a_stale_generation(menu):
    menu.state_manager.controller_states = {"a": ControllerState.READY, "b": ControllerState.READY}
    menu.admin_handler.active = True
    menu.admin_handler.controller_serial = "adm"
    state_before = menu.state
    with live_roster({"a", "b"}), patch.object(menu, "_run_game_lifecycle", autospec=True) as lifecycle:
        reply = await force_start(menu, expected_generation=2)

    assert (reply.success, reply.error) == (False, "stale")
    lifecycle.assert_not_called()
    assert menu._game_lifecycle_task is None
    assert menu.state == state_before
    # Nothing changed: the admin session stays open and no game was requested.
    assert menu.admin_handler.is_active
    assert "game_requested" not in [c.args[0] for c in menu.event_publisher.publish.await_args_list]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5", br=["BR-012"])
@pytest.mark.parametrize("value", ["two", "1.0", " "])
async def test_force_start_refuses_a_generation_that_is_not_an_integer(menu, value):
    with patch.object(menu, "_run_game_lifecycle", autospec=True) as lifecycle:
        reply = await force_start(menu, expected_generation=value)

    assert (reply.success, reply.error) == (False, "stale")
    lifecycle.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5", br=["BR-008"])
async def test_force_start_with_the_matching_generation_starts(menu):
    release = held_lifecycle(menu)
    await menu._start_game(serial="x", source="controller")
    release.set()
    await menu._game_lifecycle_task
    generation = (await status(menu)).lifecycle_generation
    menu.state_manager.controller_states = {"a": ControllerState.READY, "b": ControllerState.READY}

    with live_roster({"a", "b"}), patch.object(menu, "_run_game_lifecycle", autospec=True) as lifecycle:
        reply = await force_start(menu, expected_generation=generation)
        await menu._game_lifecycle_task

    assert generation == 2
    assert (reply.success, reply.error) == (True, "")
    lifecycle.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5", br=["BR-008"])
async def test_force_start_without_a_generation_skips_the_check(menu):
    menu.state_manager.controller_states = {"a": ControllerState.READY, "b": ControllerState.READY}
    with live_roster({"a", "b"}), patch.object(menu, "_run_game_lifecycle", autospec=True) as lifecycle:
        reply = await force_start(menu)
        await menu._game_lifecycle_task

    assert (reply.success, reply.error) == (True, "")
    lifecycle.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A5", br=["BR-012"])
async def test_force_start_is_stale_when_a_game_came_and_went_during_the_roster_lookup(menu):
    """A start and a lobby restore inside the roster lookup leave the lifecycle idle but the generation moved."""
    menu.state_manager.controller_states = {"a": ControllerState.READY, "b": ControllerState.READY}

    async def roster_lookup(_request):
        finished = asyncio.get_running_loop().create_future()
        finished.set_result(None)
        menu._game_lifecycle_task = finished
        return controller_manager_pb2.GetConnectedControllersResponse(connected_serials=["a", "b"])

    stub = create_autospec(controller_manager_pb2_grpc.ControllerManagerServiceStub, instance=True)
    stub.GetConnectedControllers = AsyncMock(side_effect=roster_lookup)
    with (
        patch("proto.controller_manager_pb2_grpc.ControllerManagerServiceStub", return_value=stub),
        patch.object(menu, "_run_game_lifecycle", autospec=True) as lifecycle,
    ):
        reply = await force_start(menu, expected_generation=0)

    assert (reply.success, reply.error) == (False, "stale")
    lifecycle.assert_not_called()
    assert "game_requested" not in [c.args[0] for c in menu.event_publisher.publish.await_args_list]
