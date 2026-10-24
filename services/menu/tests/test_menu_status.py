"""GetMenuStatus, lifecycle_generation and the force_start stale check (#2050, UC-015 A5, services.md §3.1).

A real MenuServicer (servicer_harness). The flag clients are in-process fakes that resolve from a
dict; the flag-service writers are autospecs, so a test can prove the snapshot never calls them.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
import pytest_asyncio
from openfeature.client import OpenFeatureClient
from openfeature.flag_evaluation import FlagEvaluationDetails

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
        return FlagEvaluationDetails(flag_key=key, value=default, variant=None)

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
    # The kill switch fails closed: unreadable reads as off.
    assert reply.agent_enabled is False


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
