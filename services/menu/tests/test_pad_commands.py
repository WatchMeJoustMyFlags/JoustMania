"""Operator pad commands of the menu (#2050, UC-015, services.md §3.1).

A real MenuServicer whose flag-service writers are real ``FlagServiceWriter``s over a
fake flag service: every PUT body (and its ``persist`` field) is recorded, and the
fake refuses a persisted ``agent/enabled`` write as the registry does (HTTP 400).
"""

import asyncio
import urllib.error
import urllib.parse
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
import pytest_asyncio
from opentelemetry import trace

from lib.flag_service_writer import FlagServiceWriter
from lib.types import Sound
from proto import controller_manager_pb2, controller_manager_pb2_grpc, menu_pb2
from services.menu import metrics as menu_metrics
from services.menu import settings_ops
from services.menu.event_publisher import EventPublisher
from services.menu.handlers.base import ControllerState
from services.menu.tests.servicer_harness import (
    cancel_background_tasks,
    default_returning_flag_client,
    make_real_servicer,
)
from services.menu.utils.led import GAME_MODE_COLORS
from services.menu.utils.settings import GAME_MODES

AGENT_LEVELS = {
    "none": "",
    "probe": "p",
    "ambient": "a",
    "standard": "a,b",
    "full": "a,b,c",
    "shadow_experimental": "s",
}
BASELINE = {
    ("game", "sensitivity"): ({"ultra_slow": 0, "slow": 1, "medium": 2, "fast": 3, "ultra_fast": 4}, "medium"),
    ("game", "num_teams"): ({"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}, "two"),
    ("game", "force_all_start"): ({"on": True, "off": False}, "off"),
    ("user", "game_instructions"): ({"on": True, "off": False}, "off"),
    ("user", "current_game"): ({name: name for name in GAME_MODES}, "JoustFFA"),
    (
        "user",
        "audio_volume.master",
    ): ({"mute": 0.0, "quiet": 0.3, "normal": 0.6, "loud": 0.8, "max": 1.0}, "max"),
    ("agent", "interventions_allowed"): (AGENT_LEVELS, "standard"),
    ("agent", "enabled"): ({"on": True, "off": False}, "on"),
    ("agent", "decision.backend"): ({"rules": "rules", "claude": "claude", "openai": "openai", "jev": "jev"}, "rules"),
}
# Registry rows without persist (services/flag-service/registry.yaml): a persisted write is 400.
NO_PERSIST = {("agent", "enabled")}


class FakeFlagService:
    """The flag service's GET / PUT for the menu's three sets, with failure injection."""

    def __init__(self):
        self.values = {key: default for key, (_variants, default) in BASELINE.items()}
        self.puts: list[tuple[str, str, dict]] = []
        self.failing: set[tuple[str, str]] = set()
        # Keys whose GET answers 503 while their PUT still works.
        self.failing_get: set[tuple[str, str]] = set()
        self.down = False

    def __call__(self, method, url, body):
        flag_set, key = url.split("/")[-2:]
        key = urllib.parse.unquote(key)
        if self.down:
            raise urllib.error.URLError("connection refused")
        variants, _default = BASELINE[(flag_set, key)]
        if method == "GET":
            if (flag_set, key) in self.failing_get:
                return 503, {"error": "no_sync_stream"}
            return 200, {
                "variants": list(variants),
                "variant_values": dict(variants),
                "effective_default": self.values[(flag_set, key)],
            }
        self.puts.append((flag_set, key, dict(body)))
        if (flag_set, key) in self.failing:
            return 503, {"error": "no_sync_stream"}
        if body.get("persist") and (flag_set, key) in NO_PERSIST:
            return 400, {"error": "bad_request", "detail": f"{key} cannot persist in scope stack"}
        self.values[(flag_set, key)] = body["variant"]
        return 200, {"variant": body["variant"], "persisted": bool(body.get("persist"))}

    def written(self) -> list[tuple[str, str, str]]:
        return [(flag_set, key, body["variant"]) for flag_set, key, body in self.puts]


@pytest.fixture
def flags():
    return FakeFlagService()


@pytest_asyncio.fixture
async def servicer(flags):
    def writer_factory(flag_set, *args, **kwargs):
        return FlagServiceWriter(flag_set, "http://flag-service:8070", *args, transport=flags, **kwargs)

    menu = make_real_servicer(writer_factory=writer_factory)
    # The agent client resolves to the caller's default (the flag service's value): no agent overlay.
    menu.state_manager.audio.play_sound = AsyncMock()
    with (
        patch.object(settings_ops, "FAILURE_CUE_GAP_SECONDS", 0),
        patch("lib.feature_flags.get_flag_client", side_effect=default_returning_flag_client),
    ):
        yield menu
    await cancel_background_tasks(menu)


async def pad(menu, command: str, **data) -> menu_pb2.ProcessInputResponse:
    request = menu_pb2.ProcessInputRequest(
        input_type="web_command", data={"command": command, **{k: str(v) for k, v in data.items()}}
    )
    return await menu.ProcessInput(request, None)


def live_lifecycle(menu) -> asyncio.Future:
    """Make the game lifecycle look live (a game starting or running)."""
    future = asyncio.get_running_loop().create_future()
    menu._game_lifecycle_task = future
    return future


def live_roster(serials):
    response = controller_manager_pb2.GetConnectedControllersResponse(connected_serials=sorted(serials))
    stub = create_autospec(controller_manager_pb2_grpc.ControllerManagerServiceStub, instance=True)
    stub.GetConnectedControllers = AsyncMock(return_value=response)
    return patch("proto.controller_manager_pb2_grpc.ControllerManagerServiceStub", return_value=stub)


def failure_cues(menu) -> int:
    return [c.args[0] for c in menu.state_manager.audio.play_sound.await_args_list].count(Sound.SFX_BEEP_LOUD)


# --- Writers: enabled without persist, the rest persisted (BR-001) ------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", br=["BR-001"])
async def test_pad_key_set_and_persist_bodies(servicer, flags):
    """Every pad command writes only its keys; agent/enabled has no persist field, the rest do."""
    servicer.state_manager.controller_states = {}
    await pad(servicer, "step_setting", option="sensitivity", direction="up")
    await pad(servicer, "step_setting", option="num_teams", direction="up")
    await pad(servicer, "step_setting", option="force_all_start", direction="up")
    await pad(servicer, "step_setting", option="game_instructions", direction="up")
    await pad(servicer, "step_setting", option="audio_volume.master", direction="down")
    await pad(servicer, "step_setting", option="current_game", direction="up")
    await pad(servicer, "step_setting", option="agent_level", direction="up")
    await pad(servicer, "set_backend", backend="claude")
    await pad(servicer, "agent_switch", direction="stop")
    await pad(servicer, "agent_switch", direction="resume")

    keys = {(flag_set, key, "persist" in body) for flag_set, key, body in flags.puts}
    assert keys == {
        ("game", "sensitivity", True),
        ("game", "num_teams", True),
        ("game", "force_all_start", True),
        ("user", "game_instructions", True),
        ("user", "audio_volume.master", True),
        ("user", "current_game", True),
        ("agent", "interventions_allowed", True),
        ("agent", "decision.backend", True),
        ("agent", "enabled", False),
    }
    assert all(body["writer"] == "menu" for _set, _key, body in flags.puts)


# --- force_start (step 7, A1, A2, BR-008) ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-008"])
async def test_force_start_starts_with_the_ready_roster_and_no_host_serial(servicer):
    servicer.state_manager.controller_states = {
        "a": ControllerState.READY,
        "b": ControllerState.READY,
        "c": ControllerState.CONNECTED,
    }
    with (
        live_roster({"a", "b", "c"}),
        patch.object(servicer, "_run_game_lifecycle", autospec=True) as lifecycle,
    ):
        reply = await pad(servicer, "force_start", expected_generation=0)
        await servicer._game_lifecycle_task

    assert (reply.success, reply.error) == (True, "")
    serial, source, roster = lifecycle.await_args.args
    assert (serial, source, sorted(roster)) == ("", "operator_pad", ["a", "b"])
    assert servicer.state == menu_pb2.MenuState.GAME_STARTING


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-008"])
async def test_force_start_prunes_ghosts_and_keeps_no_serial(servicer):
    servicer.state_manager.controller_states = {"a": ControllerState.READY, "ghost": ControllerState.READY}
    with live_roster({"a"}), patch.object(servicer, "_run_game_lifecycle", autospec=True) as lifecycle:
        await pad(servicer, "force_start")
        await servicer._game_lifecycle_task

    assert lifecycle.await_args.args[2] == ["a"]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A2", br=["BR-008"])
async def test_force_start_closes_an_open_admin_session_without_adding_the_admin(servicer):
    admin = servicer.admin_handler
    admin.active = True
    admin.controller_serial = "adm"
    span = create_autospec(trace.Span, instance=True)
    admin.session_span = span
    servicer.state_manager.controller_states = {
        "a": ControllerState.READY,
        "b": ControllerState.READY,
        "adm": ControllerState.ADMIN,
    }
    with live_roster({"a", "b", "adm"}), patch.object(servicer, "_run_game_lifecycle", autospec=True) as lifecycle:
        reply = await pad(servicer, "force_start")
        await servicer._game_lifecycle_task

    assert reply.success
    assert not admin.is_active
    span.set_attribute.assert_any_call("operator_pad_start", True)
    span.end.assert_called_once()
    assert "adm" not in lifecycle.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1", br=["BR-012"])
async def test_force_start_refuses_while_a_game_is_live(servicer):
    live = live_lifecycle(servicer)
    servicer.state_manager.controller_states = {"a": ControllerState.READY, "b": ControllerState.READY}
    with patch.object(servicer, "_run_game_lifecycle", autospec=True) as lifecycle:
        reply = await pad(servicer, "force_start")

    assert (reply.success, reply.error) == (False, "game_active")
    lifecycle.assert_not_called()
    assert servicer._game_lifecycle_task is live
    assert failure_cues(servicer) == 0
    live.cancel()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1", br=["BR-012"])
async def test_force_start_refuses_when_a_start_wins_during_the_roster_lookup(servicer):
    """A start that becomes live while the live roster is queried wins: no second start."""
    servicer.state_manager.controller_states = {"a": ControllerState.READY, "b": ControllerState.READY}
    state_before = servicer.state
    started: list[asyncio.Future] = []

    async def roster_lookup(_request):
        started.append(live_lifecycle(servicer))
        return controller_manager_pb2.GetConnectedControllersResponse(connected_serials=["a", "b"])

    stub = create_autospec(controller_manager_pb2_grpc.ControllerManagerServiceStub, instance=True)
    stub.GetConnectedControllers = AsyncMock(side_effect=roster_lookup)
    with (
        patch("proto.controller_manager_pb2_grpc.ControllerManagerServiceStub", return_value=stub),
        patch.object(servicer, "_run_game_lifecycle", autospec=True) as lifecycle,
    ):
        reply = await pad(servicer, "force_start")

    assert (reply.success, reply.error) == (False, "game_active")
    lifecycle.assert_not_called()
    assert servicer._game_lifecycle_task is started[0]
    assert servicer.state == state_before
    published = [c.args[0] for c in servicer.event_publisher.publish.await_args_list]
    assert "game_requested" not in published
    started[0].cancel()


# --- step_setting (steps 4, 5, 8, A1, A7, A8, BR-005) ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-005"])
async def test_step_setting_writes_once_for_n_steps_and_announces(servicer, flags):
    reply = await pad(servicer, "step_setting", option="sensitivity", direction="up", steps=2)

    assert (reply.success, reply.error) == (True, "")
    assert flags.written() == [("game", "sensitivity", "ultra_fast")]
    servicer.state_manager.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_SENSITIVITY_ULTRA_LOW)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", br=["BR-005"])
@pytest.mark.parametrize(
    ("option", "direction", "start"),
    [
        ("num_teams", "down", "two"),
        ("num_teams", "up", "six"),
        ("sensitivity", "up", "ultra_fast"),
        ("force_all_start", "down", "off"),
        ("game_instructions", "up", "on"),
        ("audio_volume.master", "up", "max"),
    ],
)
async def test_step_setting_stops_at_the_end_without_writing(servicer, flags, option, direction, start):
    flag_set = "user" if option in ("game_instructions", "audio_volume.master") else "game"
    flags.values[(flag_set, option)] = start

    reply = await pad(servicer, "step_setting", option=option, direction=direction, steps=3)

    assert (reply.success, reply.error) == (False, "at_limit")
    assert flags.puts == []
    assert failure_cues(servicer) == 0


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", br=["BR-005"])
async def test_step_setting_clamps_a_partial_step_to_the_end(servicer, flags):
    flags.values[("game", "num_teams")] = "five"
    reply = await pad(servicer, "step_setting", option="num_teams", direction="up", steps=4)
    assert reply.success
    assert flags.written() == [("game", "num_teams", "six")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main")
async def test_step_setting_bool_up_means_on(servicer, flags):
    reply = await pad(servicer, "step_setting", option="game_instructions", direction="up")
    assert reply.success
    assert flags.written() == [("user", "game_instructions", "on")]
    servicer.state_manager.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_INSTRUCTIONS_ON)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-005"])
async def test_step_setting_mode_selects_without_wrapping(servicer, flags):
    reply = await pad(servicer, "step_setting", option="current_game", direction="up", steps=2)
    assert reply.success
    assert servicer.current_selection.name == GAME_MODES[2]
    assert flags.written()[-1] == ("user", "current_game", GAME_MODES[2])

    flags.puts.clear()
    reply = await pad(servicer, "step_setting", option="current_game", direction="down", steps=9)
    assert reply.success
    assert servicer.current_selection.name == GAME_MODES[0]

    reply = await pad(servicer, "step_setting", option="current_game", direction="down")
    assert (reply.success, reply.error) == (False, "at_limit")
    assert servicer.current_selection.name == GAME_MODES[0]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1", br=["BR-012"])
@pytest.mark.parametrize("option", ["current_game", "sensitivity", "num_teams", "game_instructions", "force_all_start"])
async def test_step_setting_refuses_lobby_options_while_a_game_is_live(servicer, flags, option):
    live = live_lifecycle(servicer)
    reply = await pad(servicer, "step_setting", option=option, direction="up")
    assert (reply.success, reply.error) == (False, "game_active")
    assert flags.puts == []
    live.cancel()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A1")
@pytest.mark.parametrize(
    ("option", "key"), [("audio_volume.master", "audio_volume.master"), ("agent_level", "interventions_allowed")]
)
async def test_step_setting_volume_and_agent_level_work_while_a_game_is_live(servicer, flags, option, key):
    live = live_lifecycle(servicer)
    reply = await pad(servicer, "step_setting", option=option, direction="down")
    assert reply.success
    assert [k for _s, k, _v in flags.written()] == [key]
    live.cancel()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A8", br=["BR-012"])
@pytest.mark.parametrize(
    "data",
    [
        {"option": "random_assignment", "direction": "up"},
        {"option": "sensitivity", "direction": "sideways"},
        {"option": "sensitivity", "direction": "up", "steps": "0"},
        {"option": "sensitivity", "direction": "up", "steps": "two"},
    ],
)
async def test_step_setting_bad_option(servicer, flags, data):
    reply = await pad(servicer, "step_setting", **data)
    assert (reply.success, reply.error) == (False, "bad_option")
    assert flags.puts == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-009", "BR-010"])
async def test_step_setting_flag_service_down_fails_closed_with_the_failure_cue(servicer, flags):
    flags.down = True
    reply = await pad(servicer, "step_setting", option="sensitivity", direction="up")
    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert failure_cues(servicer) == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-010"])
async def test_step_setting_refused_write_is_not_retried(servicer, flags):
    flags.failing.add(("game", "sensitivity"))
    reply = await pad(servicer, "step_setting", option="sensitivity", direction="up")
    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert len(flags.puts) == 1
    assert failure_cues(servicer) == 2


# --- agent_switch (step 6, A3, A9, BR-002, BR-003) ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-002"])
async def test_stop_writes_the_level_first_then_the_kill_switch(servicer, flags):
    reply = await pad(servicer, "agent_switch", direction="stop")

    assert (reply.success, reply.error) == (True, "ok")
    assert flags.written() == [("agent", "interventions_allowed", "none"), ("agent", "enabled", "off")]
    assert servicer.settings_ops.saved_agent_level == "standard"
    servicer.state_manager.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_ADMINOP_OFF)
    assert failure_cues(servicer) == 0


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-002", "BR-009"])
@pytest.mark.parametrize(
    ("failing", "error"),
    [
        ({("agent", "enabled")}, "partial:enabled"),
        ({("agent", "interventions_allowed")}, "partial:interventions_allowed"),
        ({("agent", "enabled"), ("agent", "interventions_allowed")}, "failed"),
    ],
)
async def test_stop_attempts_both_writes_and_reports_partial_results(servicer, flags, failing, error):
    flags.failing = failing
    reply = await pad(servicer, "agent_switch", direction="stop")

    assert (reply.success, reply.error) == (False, error)
    assert [k for _s, k, _v in flags.written()] == ["interventions_allowed", "enabled"]
    assert failure_cues(servicer) == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-002", "BR-010"])
async def test_stop_with_the_flag_service_down_fails(servicer, flags):
    flags.down = True
    reply = await pad(servicer, "agent_switch", direction="stop")
    assert (reply.success, reply.error) == (False, "failed")
    assert failure_cues(servicer) == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A9", br=["BR-002"])
@pytest.mark.parametrize(("level", "enabled"), [("standard", "off"), ("none", "on")])
async def test_stop_when_already_stopped_is_a_no_op(servicer, flags, level, enabled):
    flags.values[("agent", "interventions_allowed")] = level
    flags.values[("agent", "enabled")] = enabled
    servicer.settings_ops.saved_agent_level = "full"

    reply = await pad(servicer, "agent_switch", direction="stop")

    assert (reply.success, reply.error) == (True, "ok")
    assert flags.puts == []
    assert servicer.settings_ops.saved_agent_level == "full"
    servicer.state_manager.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_ADMINOP_OFF)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-003"])
async def test_resume_restores_the_saved_level_then_enables(servicer, flags):
    await pad(servicer, "agent_switch", direction="stop")
    flags.puts.clear()

    reply = await pad(servicer, "agent_switch", direction="resume")

    assert (reply.success, reply.error) == (True, "ok")
    assert flags.written() == [("agent", "interventions_allowed", "standard"), ("agent", "enabled", "on")]
    servicer.state_manager.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_ADMINOP_ON)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-003"])
async def test_resume_keeps_a_level_set_after_the_kill(servicer, flags):
    await pad(servicer, "agent_switch", direction="stop")
    flags.values[("agent", "interventions_allowed")] = "full"  # an admin raised it after the kill
    flags.puts.clear()

    reply = await pad(servicer, "agent_switch", direction="resume")

    assert reply.success
    assert flags.written() == [("agent", "enabled", "on")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-003"])
async def test_resume_without_a_saved_level_falls_back_to_ambient(servicer, flags):
    """A menu restart loses the saved level: the resume level is ambient, never full."""
    flags.values[("agent", "interventions_allowed")] = "none"
    flags.values[("agent", "enabled")] = "off"

    reply = await pad(servicer, "agent_switch", direction="resume")

    assert reply.success
    assert flags.written() == [("agent", "interventions_allowed", "ambient"), ("agent", "enabled", "on")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-003", "BR-010"])
async def test_resume_never_enables_when_the_level_is_unreadable(servicer, flags):
    flags.values[("agent", "enabled")] = "off"
    # Only the level read fails: the enabled read and its PUT would still work.
    flags.failing_get = {("agent", "interventions_allowed")}
    reply = await pad(servicer, "agent_switch", direction="resume")
    assert (reply.success, reply.error) == (False, "failed")
    assert flags.puts == []
    assert failure_cues(servicer) == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-003"])
async def test_resume_with_the_enable_refused_is_partial(servicer, flags):
    await pad(servicer, "agent_switch", direction="stop")
    flags.failing = {("agent", "enabled")}
    reply = await pad(servicer, "agent_switch", direction="resume")
    assert (reply.success, reply.error) == (False, "partial:enabled")
    assert servicer.settings_ops.saved_agent_level == "standard"


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A9")
async def test_resume_when_running_is_a_no_op(servicer, flags):
    reply = await pad(servicer, "agent_switch", direction="resume")
    assert (reply.success, reply.error) == (True, "ok")
    assert flags.puts == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A8")
async def test_agent_switch_bad_direction(servicer, flags):
    reply = await pad(servicer, "agent_switch", direction="pause")
    assert (reply.success, reply.error) == (False, "bad_option")
    assert flags.puts == []


# --- agent level ladder (BR-003, BR-004) -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-004"])
async def test_ladder_steps_the_level_clamped(servicer, flags):
    flags.values[("agent", "interventions_allowed")] = "ambient"
    reply = await pad(servicer, "step_setting", option="agent_level", direction="up", steps=5)
    assert reply.success
    assert flags.written() == [("agent", "interventions_allowed", "full")]
    servicer.state_manager.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_ADMINOP_3)

    reply = await pad(servicer, "step_setting", option="agent_level", direction="up")
    assert (reply.success, reply.error) == (False, "at_limit")


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-003", "BR-004"])
async def test_ladder_while_stopped_changes_only_the_saved_level(servicer, flags):
    await pad(servicer, "agent_switch", direction="stop")
    flags.puts.clear()

    reply = await pad(servicer, "step_setting", option="agent_level", direction="up")

    assert reply.success
    assert flags.puts == []
    assert servicer.settings_ops.saved_agent_level == "full"
    reply = await pad(servicer, "agent_switch", direction="resume")
    assert flags.written() == [("agent", "interventions_allowed", "full"), ("agent", "enabled", "on")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", br=["BR-003"])
async def test_ladder_while_stopped_never_saves_none(servicer, flags):
    flags.values[("agent", "enabled")] = "off"
    reply = await pad(servicer, "step_setting", option="agent_level", direction="down", steps=3)
    assert (reply.success, reply.error) == (False, "at_limit")
    assert servicer.settings_ops.saved_agent_level is None
    assert flags.puts == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", br=["BR-002", "BR-003", "BR-004"])
async def test_ladder_step_down_to_none_saves_the_level_it_left(servicer, flags):
    reply = await pad(servicer, "step_setting", option="agent_level", direction="down", steps=5)
    assert reply.success
    assert flags.written() == [("agent", "interventions_allowed", "none")]
    assert servicer.settings_ops.saved_agent_level == "standard"

    flags.puts.clear()
    await pad(servicer, "agent_switch", direction="resume")
    assert flags.written() == [("agent", "interventions_allowed", "standard"), ("agent", "enabled", "on")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-003", "BR-010"])
@pytest.mark.parametrize("direction", ["up", "down"])
async def test_ladder_with_the_switch_unreadable_writes_nothing(servicer, flags, direction):
    servicer.settings_ops.saved_agent_level = "standard"
    flags.failing_get = {("agent", "enabled")}

    reply = await pad(servicer, "step_setting", option="agent_level", direction=direction)

    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert flags.puts == []
    assert servicer.settings_ops.saved_agent_level == "standard"
    servicer.state_manager.audio.play_voice.assert_not_awaited()
    assert failure_cues(servicer) == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-002"])
async def test_stop_with_the_switch_unreadable_still_writes_both_keys(servicer, flags):
    flags.failing_get = {("agent", "enabled")}
    reply = await pad(servicer, "agent_switch", direction="stop")
    assert (reply.success, reply.error) == (True, "ok")
    assert flags.written() == [("agent", "interventions_allowed", "none"), ("agent", "enabled", "off")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", br=["BR-005"])
async def test_ladder_caps_a_huge_step_count(servicer, flags):
    """``steps`` is unbounded input: the ladder moves at most once per rung, then clamps."""
    real_next = settings_ops.agent_level_next
    calls = []

    def counting_next(current, forward=True):
        calls.append(current)
        if len(calls) > len(settings_ops.AGENT_POLICY_LEVELS):
            raise AssertionError("ladder looped past its length")
        return real_next(current, forward)

    with patch.object(settings_ops, "agent_level_next", side_effect=counting_next):
        reply = await pad(servicer, "step_setting", option="agent_level", direction="up", steps=10**9)

    assert (reply.success, reply.error) == (True, "")
    assert flags.written() == [("agent", "interventions_allowed", "full")]


# --- set_backend (BR-004) --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", br=["BR-004"])
async def test_set_backend_writes_the_backend(servicer, flags):
    reply = await pad(servicer, "set_backend", backend="jev")
    assert (reply.success, reply.error) == (True, "")
    assert flags.written() == [("agent", "decision.backend", "jev")]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A8")
async def test_set_backend_unknown_backend(servicer, flags):
    reply = await pad(servicer, "set_backend", backend="gpt")
    assert (reply.success, reply.error) == (False, "bad_option")
    assert flags.puts == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A3", br=["BR-009"])
async def test_set_backend_refused(servicer, flags):
    flags.failing.add(("agent", "decision.backend"))
    reply = await pad(servicer, "set_backend", backend="claude")
    assert (reply.success, reply.error) == (False, "flag_service_unavailable")
    assert failure_cues(servicer) == 2


# --- Mode feedback on every lobby controller (#2105, UC-015 BR-015, UC-011 A4) ---------------


def _pulses(menu) -> list[tuple]:
    return [
        (c.args[0], c.args[1], c.kwargs["color"], c.kwargs["duration_ms"])
        for c in menu.state_manager.led.send_game_effect.await_args_list
    ]


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[4], br=["BR-015", "BR-005"])
@pytest.mark.uc("UC-011", flow="A4")
async def test_pad_mode_change_voices_the_final_mode_and_pulses_every_lobby_controller(servicer):
    servicer.state_manager.controller_states = {
        "dim": ControllerState.CONNECTED,
        "ready": ControllerState.READY,
        "admin": ControllerState.ADMIN,
        "idle": ControllerState.IDLE,
    }
    reply = await pad(servicer, "step_setting", option="current_game", direction="up", steps=3)

    assert reply.success
    final = GAME_MODES[3]
    # One voice line, for the final mode only (the turns are coalesced into one step).
    servicer.state_manager.audio.play_game_mode_voice.assert_awaited_once_with(final)
    pulse = (controller_manager_pb2.GAME_EFFECT_PULSE, GAME_MODE_COLORS[final], settings_ops.MODE_PULSE_MS)
    assert sorted(_pulses(servicer)) == [("dim", *pulse), ("ready", *pulse)]
    # The pulse is an effect: the base (lobby) colours are not repainted, so each controller
    # returns to its own dim / bright colour when the pulse ends.
    servicer.state_manager.led.send_base_color.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="A7", br=["BR-015"])
async def test_pad_mode_step_at_the_limit_gives_no_voice_and_no_pulse(servicer):
    servicer.state_manager.controller_states = {"dim": ControllerState.CONNECTED}
    reply = await pad(servicer, "step_setting", option="current_game", direction="down")
    assert (reply.success, reply.error) == (False, "at_limit")
    servicer.state_manager.audio.play_game_mode_voice.assert_not_awaited()
    servicer.state_manager.led.send_game_effect.assert_not_awaited()


# --- Refused start: the not-enough-players path with a real publisher (#2105, UC-010 A3) -------


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="A3", br=["BR-001"])
@pytest.mark.uc("UC-015", flow="main", steps=[7])
@pytest.mark.parametrize("command", ["force_start", "start_game"])
async def test_refused_start_publishes_string_data_and_plays_not_enough_players(servicer, command):
    """The pad force-start and the web start below the minimum: the event carries strings, the voice plays."""
    publisher = EventPublisher(trace.get_tracer(__name__), menu_metrics)
    servicer.event_publisher = publisher
    events = await publisher.subscribe("test")
    servicer.state_manager.controller_states = {}

    with live_roster(set()):
        reply = await pad(servicer, command)
        # Without the fix the lifecycle task dies with a TypeError from MenuEvent(data=...).
        await servicer._game_lifecycle_task

    assert reply.success
    published = []
    while not events.empty():
        published.append(events.get_nowait())
    cancelled = [e for e in published if e.event_type == "game_start_cancelled"]
    assert len(cancelled) == 1
    data = {k: v for k, v in cancelled[0].data.items() if not k.startswith("_")}
    assert data == {"game_name": "JoustFFA", "player_count": "0", "minimum_players": "2"}
    servicer.audio.play_voice.assert_awaited_with(Sound.MENU_VOX_NOT_ENOUGH_PLAYERS)
    assert servicer.state != menu_pb2.MenuState.GAME_STARTING  # never left stuck in a start


# --- Construction ----------------------------------------------------------------------------


@pytest.mark.uc("UC-015", br=["BR-001"])
def test_the_kill_switch_writer_never_persists():
    calls = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return create_autospec(FlagServiceWriter, instance=True)

    menu = make_real_servicer(writer_factory=factory)
    assert (("agent",), {"persist": False}) in calls
    assert menu.settings_ops.agent_enabled_writer is menu.agent_enabled_writer
    assert menu.admin_handler.settings_ops is menu.settings_ops
