"""Gestures to commands (UC-015 steps 1 to 8, A1, A3, A5 to A7, BR-002 to BR-012), autospec'd stubs."""

import asyncio
from unittest.mock import create_autospec, patch

import grpc
import pytest

from lib.setting_ladders import BACKENDS, preview_value
from lib.types import Sound
from proto import game_coordinator_pb2, menu_pb2
from services.operator_pad import actions as actions_module
from services.operator_pad.actions import Actions
from services.operator_pad.cues import CuePlayer
from services.operator_pad.gestures import CANCEL, HOLD, KNOB, KNOB_PRESS_GESTURE, KNOB_TURN, LAYER, LOCK, TAP, Gesture
from services.operator_pad.tests.fakes import CoordinatorStubSpec, GatedSleep, MenuStubSpec


def status(**kw) -> menu_pb2.GetMenuStatusResponse:
    base = {
        "lifecycle_live": False,
        "lifecycle_generation": 2,
        "agent_enabled": True,
        "agent_level": "standard",
        "agent_backend": "claude",
        "sensitivity": 2,
        "num_teams": 2,
        "instructions": False,
        "audio_volume_master": "normal",
    }
    base.update(kw)
    # A resolved kill switch unless the test says otherwise (services.md §3.1, #2101).
    base.setdefault("agent_enabled_state", "on" if base["agent_enabled"] else "off")
    # An open, resolved write gate unless the test says otherwise (#2093).
    base.setdefault("agent_interventions_enabled_state", "on")
    return menu_pb2.GetMenuStatusResponse(**base)


def reply(success=True, error=""):
    return menu_pb2.ProcessInputResponse(success=success, error=error)


@pytest.fixture
def menu():
    stub = create_autospec(MenuStubSpec, instance=True)
    stub.ProcessInput.return_value = reply()
    return stub


@pytest.fixture
def coordinator():
    stub = create_autospec(CoordinatorStubSpec, instance=True)
    stub.ForceEndGame.return_value = game_coordinator_pb2.ForceEndGameResponse(success=True)
    return stub


@pytest.fixture
def cues():
    return create_autospec(CuePlayer, instance=True)


@pytest.fixture
def sleep():
    return GatedSleep()


@pytest.fixture
def actions(menu, coordinator, cues, sleep):
    return Actions(menu, coordinator, cues, sleep=sleep)


def sent(menu) -> list[dict]:
    return [dict(c.args[0].data) for c in menu.ProcessInput.await_args_list]


# --- K4: stale state (A5) -------------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="A5", br=["BR-009", "BR-010"])
@pytest.mark.parametrize(
    "release",
    [status(lifecycle_generation=4), status(lifecycle_live=True, lifecycle_generation=2)],
    ids=["generation-changed", "state-class-changed"],
)
async def test_k4_generation_or_state_change_cancels_and_sends_nothing(actions, menu, coordinator, cues, release):
    result = await actions.dispatch(Gesture(HOLD, "K4", held_ms=1500), release, status(lifecycle_generation=2))
    assert result == ("force_start", "cancelled")
    menu.ProcessInput.assert_not_awaited()
    coordinator.ForceEndGame.assert_not_awaited()
    cues.cancel.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A5", br=["BR-008"], steps=[7])
async def test_k4_lobby_force_start_sends_the_generation_captured_at_press(actions, menu):
    await actions.dispatch(Gesture(HOLD, "K4"), status(lifecycle_generation=6), status(lifecycle_generation=6))
    assert sent(menu) == [{"command": "force_start", "expected_generation": "6"}]


@pytest.mark.uc("UC-015", flow="main", steps=[7])
async def test_k4_running_game_force_ends_through_the_coordinator(actions, menu, coordinator):
    live = status(lifecycle_live=True, lifecycle_generation=3)
    result = await actions.dispatch(Gesture(HOLD, "K4"), live, live)
    assert result == ("force_end", "ok")
    coordinator.ForceEndGame.assert_awaited_once()
    assert coordinator.ForceEndGame.await_args.args[0].reason == "operator_pad"
    menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A5", br=["BR-012"])
async def test_menu_stale_refusal_plays_the_cancel_cue(actions, menu, cues):
    menu.ProcessInput.return_value = reply(False, "stale")
    result = await actions.dispatch(Gesture(HOLD, "K4"), status(), status())
    assert result == ("force_start", "stale")
    cues.cancel.assert_awaited_once()


# --- disconnected: never queued (A3, BR-010) ------------------------------------------------


@pytest.mark.uc("UC-015", flow="A3", br=["BR-009", "BR-010"])
@pytest.mark.parametrize(
    "gesture",
    [
        Gesture(TAP, "K1"),
        Gesture(TAP, "K6"),
        Gesture(HOLD, "K4"),
        Gesture(LAYER, "K2", steps=1),
        Gesture(LAYER, "K7", steps=1),
        Gesture(KNOB_PRESS_GESTURE, KNOB),
    ],
)
async def test_menu_unreachable_sends_nothing_and_plays_the_failure_cue(actions, menu, coordinator, cues, gesture):
    _, result = await actions.dispatch(gesture, None, None)
    assert result == "menu_unavailable"
    menu.ProcessInput.assert_not_awaited()
    coordinator.ForceEndGame.assert_not_awaited()
    cues.failure.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A3", br=["BR-010"], cond=["Post-F-1"])
async def test_a_failed_command_rpc_is_not_retried(actions, menu, cues):
    menu.ProcessInput.side_effect = grpc.aio.AioRpcError(
        grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata(), "down"
    )
    result = await actions.dispatch(Gesture(HOLD, "K3"), status(), None)
    assert result == ("agent_switch", "menu_unavailable")
    assert menu.ProcessInput.await_count == 1
    cues.failure.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A4", br=["BR-010"])
async def test_a_volume_turn_pending_at_disconnect_is_dropped(actions, menu, sleep):
    await actions.dispatch(Gesture(KNOB_TURN, KNOB, steps=1), None)
    actions.cancel_pending()
    sleep.gate.set()
    await actions.drain()
    await asyncio.sleep(0)
    menu.ProcessInput.assert_not_awaited()


# --- error code -> cue (BR-009, BR-012) -----------------------------------------------------


@pytest.mark.uc("UC-015", flow="A3", br=["BR-009", "BR-012"])
@pytest.mark.parametrize(
    ("success", "error", "label", "cue"),
    [
        (False, "game_active", "game_active", "refusal"),
        (False, "at_limit", "at_limit", "limit"),
        (False, "stale", "stale", "cancel"),
        (False, "bad_option", "bad_option", "refusal"),
        (False, "flag_service_unavailable", "flag_service_unavailable", None),
        (False, "failed", "failed", None),
        (False, "partial:enabled", "partial", None),
        (True, "ok", "ok", None),
        (True, "", "ok", None),
    ],
)
async def test_error_codes_map_to_cues(actions, menu, cues, success, error, label, cue):
    menu.ProcessInput.return_value = reply(success, error)
    with patch.object(actions_module.metrics, "gestures_total") as counter:
        result = await actions.dispatch(Gesture(LAYER, "K3", steps=1), status())
    assert result == ("step_setting", label)
    counter.labels.assert_called_once_with(action="step_setting", result=label)
    for name in ("refusal", "limit", "cancel", "failure"):
        mock = getattr(cues, name)
        if name == cue:
            mock.assert_awaited_once()
        else:
            mock.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A7", br=["BR-009"])
@pytest.mark.parametrize(("steps", "up"), [(2, True), (-3, False)])
async def test_a_menu_at_limit_says_maximum_up_and_minimum_down(actions, menu, cues, steps, up):
    """#2106: the limit word follows the direction of the step the menu refused."""
    menu.ProcessInput.return_value = reply(False, "at_limit")
    await actions.dispatch(Gesture(LAYER, "K2", steps=steps), status())
    cues.limit.assert_awaited_once_with(up)


# --- coalescing (BR-005) --------------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-005", "BR-006"], steps=[8])
async def test_volume_turns_coalesce_into_one_write_after_the_pause(actions, menu, sleep):
    for step in (1, 1, 1, -1):
        await actions.dispatch(Gesture(KNOB_TURN, KNOB, steps=step), None)
        await asyncio.sleep(0)
    menu.ProcessInput.assert_not_awaited()
    assert sleep.calls and set(sleep.calls) == {0.25}
    sleep.gate.set()
    await actions.drain()
    assert sent(menu) == [{"command": "step_setting", "option": "audio_volume.master", "direction": "up", "steps": "2"}]


@pytest.mark.uc("UC-015", flow="main", br=["BR-005"], steps=[8])
async def test_volume_turns_that_net_to_zero_write_nothing(actions, menu, sleep):
    await actions.dispatch(Gesture(KNOB_TURN, KNOB, steps=1), None)
    await actions.dispatch(Gesture(KNOB_TURN, KNOB, steps=-1), None)
    sleep.gate.set()
    await actions.drain()
    menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-004", "BR-005"], steps=[4, 5])
@pytest.mark.parametrize(
    ("key", "steps", "expected"),
    [
        ("K1", 2, {"option": "current_game", "direction": "up", "steps": "2"}),
        ("K2", -3, {"option": "sensitivity", "direction": "down", "steps": "3"}),
        ("K5", 1, {"option": "num_teams", "direction": "up", "steps": "1"}),
        ("K3", -1, {"option": "agent_level", "direction": "down", "steps": "1"}),
    ],
)
async def test_layer_release_commits_one_step_setting_with_steps_n(actions, menu, key, steps, expected):
    await actions.dispatch(Gesture(LAYER, key, steps=steps), status())
    assert sent(menu) == [{"command": "step_setting", **expected}]


@pytest.mark.uc("UC-015", flow="main", br=["BR-005"], steps=[5])
async def test_layer_release_with_zero_net_turns_writes_nothing(actions, menu):
    assert await actions.dispatch(Gesture(LAYER, "K2", steps=0), status()) == ("step_setting", "noop")
    menu.ProcessInput.assert_not_awaited()


# --- lobby-only refusals (A1) ---------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="A1", br=["BR-009"])
@pytest.mark.parametrize(
    "gesture",
    [
        Gesture(TAP, "K1"),
        Gesture(TAP, "K6"),
        Gesture(LAYER, "K1", steps=1),
        Gesture(LAYER, "K2", steps=1),
        Gesture(LAYER, "K5", steps=-1),
    ],
)
async def test_lobby_only_gestures_are_refused_by_the_pad_while_a_game_is_live(actions, menu, cues, gesture):
    _, result = await actions.dispatch(gesture, status(lifecycle_live=True))
    assert result == "refused"
    menu.ProcessInput.assert_not_awaited()
    cues.refusal.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A1", br=["BR-004"])
async def test_agent_level_and_backend_are_not_refused_while_a_game_is_live(actions, menu):
    await actions.dispatch(Gesture(LAYER, "K3", steps=1), status(lifecycle_live=True))
    await actions.dispatch(Gesture(LAYER, "K7", steps=1), status(lifecycle_live=True))
    assert [d["command"] for d in sent(menu)] == ["step_setting", "set_backend"]


@pytest.mark.uc("UC-015", flow="main", steps=[1, 2])
async def test_k1_tap_in_the_lobby_starts_a_game(actions, menu):
    assert await actions.dispatch(Gesture(TAP, "K1"), status()) == ("start_game", "ok")
    assert sent(menu) == [{"command": "start_game"}]


@pytest.mark.uc("UC-015", flow="main", br=["BR-011"])
@pytest.mark.parametrize(("instructions", "direction"), [(False, "up"), (True, "down")])
async def test_k6_tap_toggles_instructions(actions, menu, instructions, direction):
    await actions.dispatch(Gesture(TAP, "K6"), status(instructions=instructions))
    assert sent(menu) == [
        {"command": "step_setting", "option": "game_instructions", "direction": direction, "steps": "1"}
    ]


# --- backend (BR-004, A7) -------------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="A7", br=["BR-004", "BR-005"])
@pytest.mark.parametrize(
    ("current", "steps", "target"),
    [("claude", 1, "openai"), ("rules", 5, "jev"), ("openai", -1, "claude"), ("claude", -9, "rules")],
)
async def test_backend_layer_steps_clamped(actions, menu, current, steps, target):
    await actions.dispatch(Gesture(LAYER, "K7", steps=steps), status(agent_backend=current))
    assert sent(menu) == [{"command": "set_backend", "backend": target}]


@pytest.mark.uc("UC-015", flow="A7", br=["BR-004", "BR-009"])
@pytest.mark.parametrize(("current", "steps"), [("jev", 1), ("rules", -2)])
async def test_backend_already_at_the_end_is_at_limit_and_sends_nothing(actions, menu, cues, current, steps):
    result = await actions.dispatch(Gesture(LAYER, "K7", steps=steps), status(agent_backend=current))
    assert result == ("set_backend", "at_limit")
    menu.ProcessInput.assert_not_awaited()
    cues.limit.assert_awaited_once_with(steps > 0)  # "Maximum" up, "Minimum" down (#2106)


@pytest.mark.uc("UC-015", flow="A7", br=["BR-004", "BR-005"])
@pytest.mark.parametrize("current", BACKENDS)
async def test_backend_preview_equals_commit(menu, coordinator, cues, sleep, current):
    """#2106: the backend a K7 release commits is the shared ladder's preview, for every start and step."""
    for steps in [n for n in range(-6, 7) if n != 0]:
        actions = Actions(menu, coordinator, cues, sleep=sleep)
        menu.ProcessInput.reset_mock()
        preview = preview_value("decision.backend", status(agent_backend=current), steps)
        result = await actions.dispatch(Gesture(LAYER, "K7", steps=steps), status(agent_backend=current))
        if preview.at_limit:
            assert result == ("set_backend", "at_limit"), (current, steps)
            assert sent(menu) == []
        else:
            assert sent(menu) == [{"command": "set_backend", "backend": preview.value}], (current, steps)


# --- agent switch (step 6, A6, BR-002, BR-003) ----------------------------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-002", "BR-003"], steps=[6])
@pytest.mark.parametrize(
    ("agent", "direction"),
    [
        ({"agent_enabled": True, "agent_level": "standard"}, "stop"),
        ({"agent_enabled": False, "agent_level": "standard"}, "resume"),
        ({"agent_enabled": True, "agent_level": "none"}, "resume"),
    ],
)
async def test_k3_hold_stops_a_running_agent_and_resumes_a_stopped_one(actions, menu, agent, direction):
    await actions.dispatch(Gesture(HOLD, "K3"), status(**agent), status(**agent))
    assert sent(menu) == [{"command": "agent_switch", "direction": direction}]


@pytest.mark.uc("UC-015", flow="A6", br=["BR-002", "BR-013"])
@pytest.mark.parametrize(
    ("at_press", "at_release"),
    [
        (status(), status()),
        # Fully stopped: the stop goes out anyway and the menu's A9 makes it a no-op.
        (status(agent_enabled=False), status(agent_enabled=False)),
        (status(agent_level="none"), status(agent_level="none")),
        # Only the write gate closed (#2093): the stop still ends remote decide calls.
        (status(agent_interventions_enabled_state="off"), status(agent_interventions_enabled_state="off")),
        # A change during the hold does not cancel a locked hold: it is kill only.
        (status(agent_enabled=False), status()),
    ],
    ids=["running", "enabled-off", "level-none", "gate-closed", "changed-during-the-hold"],
)
async def test_locked_k3_hold_always_sends_the_stop(actions, menu, cues, at_press, at_release):
    """A6: a locked pad is kill only; a locked K3 hold sends the stop whatever the status, never the resume."""
    result = await actions.dispatch(Gesture(HOLD, "K3", held_ms=1200, locked=True), at_release, at_press)
    assert result == ("agent_switch", "ok")
    assert sent(menu) == [{"command": "agent_switch", "direction": "stop"}]
    cues.cancel.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="A3", br=["BR-002", "BR-003"])
async def test_agent_switch_waits_for_the_slowest_resume(actions, menu):
    """The resume from none is 2 reads + 3 settled writes (4.0 s of flag-service deadlines, #2093)."""
    stopped = status(agent_level="none")
    await actions.dispatch(Gesture(HOLD, "K3", held_ms=1200), stopped, stopped)
    assert sent(menu) == [{"command": "agent_switch", "direction": "resume"}]
    assert menu.ProcessInput.await_args.kwargs["timeout"] >= 5.0


@pytest.mark.uc("UC-015", flow="A10", br=["BR-009", "BR-013"])
@pytest.mark.parametrize(
    ("at_press", "at_release"),
    [
        (status(), status(agent_enabled=False)),  # stopped from the dashboard during a kill hold
        (status(agent_level="none"), status()),  # resumed by someone else during a resume hold
    ],
    ids=["stopped-during-the-hold", "resumed-during-the-hold"],
)
async def test_k3_status_changed_during_the_hold_cancels_and_sends_nothing(actions, menu, cues, at_press, at_release):
    result = await actions.dispatch(Gesture(HOLD, "K3", held_ms=1200), at_release, at_press)
    assert result == ("agent_switch", "cancelled")
    menu.ProcessInput.assert_not_awaited()
    cues.cancel.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A10", br=["BR-002", "BR-013"])
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize(
    ("at_press", "at_release"),
    [
        (None, None),  # GetMenuStatus failed or timed out
        (None, status(agent_enabled=False)),
        (status(agent_enabled=False), None),  # a resume hold whose release read failed
        (status(agent_enabled=False, agent_level=""), status(agent_enabled=False, agent_level="")),  # unresolvable
        (status(agent_level="bogus"), status(agent_level="bogus")),
        # `enabled` unresolvable (#2101): the menu reports it off, so without the state it read as stopped.
        (status(agent_enabled=False, agent_enabled_state="unknown"), status(agent_enabled=False)),
        (status(agent_enabled=False), status(agent_enabled=False, agent_enabled_state="unknown")),
        # A kill hold whose release read lost `enabled`: a stop, not the cancel of a disagreement.
        (status(), status(agent_enabled=False, agent_enabled_state="unknown")),
        # A menu older than #2101 sends no state.
        (status(agent_enabled=False, agent_enabled_state=""), status(agent_enabled=False, agent_enabled_state="")),
        # probe and shadow_experimental are off the ladder (BR-013).
        (status(agent_level="probe"), status(agent_level="probe")),
        (status(agent_level="shadow_experimental"), status(agent_level="shadow_experimental")),
        # The write gate unresolvable (#2093): a closed gate would read as stopped and send the resume.
        (status(agent_interventions_enabled_state="unknown"), status(agent_interventions_enabled_state="unknown")),
        (status(agent_interventions_enabled_state="off"), status(agent_interventions_enabled_state="unknown")),
        # A menu older than #2093 sends no gate state.
        (status(agent_interventions_enabled_state=""), status(agent_interventions_enabled_state="")),
    ],
    ids=[
        "both-unread",
        "press-unread",
        "release-unread",
        "flags-unresolvable",
        "level-off-the-ladder",
        "enabled-unknown-at-press",
        "enabled-unknown-at-release",
        "enabled-lost-during-a-kill-hold",
        "enabled-state-missing",
        "level-probe",
        "level-shadow-experimental",
        "gate-unknown",
        "gate-lost-during-a-resume-hold",
        "gate-state-missing",
    ],
)
async def test_k3_hold_with_an_unknown_status_sends_the_stop_never_the_resume(
    actions, menu, at_press, at_release, locked
):
    result = await actions.dispatch(Gesture(HOLD, "K3", held_ms=1200, locked=locked), at_release, at_press)
    assert result == ("agent_switch", "ok")
    assert sent(menu) == [{"command": "agent_switch", "direction": "stop"}]


@pytest.mark.uc("UC-015", flow="A9", br=["BR-002", "BR-003", "BR-013"])
async def test_k3_hold_with_the_write_gate_off_sends_the_resume(actions, menu):
    """#2093: enabled on and a level set but the gate off is stopped, so the hold resumes (opens the gate)."""
    gate_off = status(agent_interventions_enabled_state="off")
    result = await actions.dispatch(Gesture(HOLD, "K3", held_ms=1200), gate_off, gate_off)
    assert result == ("agent_switch", "ok")
    assert sent(menu) == [{"command": "agent_switch", "direction": "resume"}]


@pytest.mark.uc("UC-015", flow="A10", br=["BR-002", "BR-013"])
async def test_k3_hold_whose_write_gate_opened_during_the_hold_cancels(actions, menu, cues):
    """#2093: the gate is part of "stopped", so a gate opened by someone else mid-hold is a disagreement."""
    at_press = status(agent_interventions_enabled_state="off")
    result = await actions.dispatch(Gesture(HOLD, "K3", held_ms=1200), status(), at_press)
    assert result == ("agent_switch", "cancelled")
    menu.ProcessInput.assert_not_awaited()
    cues.cancel.assert_awaited_once()


@pytest.mark.uc("UC-015", flow="A6", br=["BR-009"])
async def test_lock_gesture_sets_the_scroll_lock_led_and_plays_the_direction(menu, coordinator, cues):
    leds: list[bool] = []
    actions = Actions(menu, coordinator, cues, set_lock_led=leds.append)
    await actions.dispatch(Gesture(LOCK, "K8", locked=True), None)
    await actions.dispatch(Gesture(LOCK, "K8", locked=False), None)
    assert leds == [True, False]
    assert [c.args[0] for c in cues.lock.await_args_list] == [True, False]
    menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-007", "BR-009"])
async def test_cancel_gesture_plays_the_cancel_cue_only(actions, menu, cues):
    assert await actions.dispatch(Gesture(CANCEL, "K3"), status()) == ("cancel", "cancelled")
    cues.cancel.assert_awaited_once()
    menu.ProcessInput.assert_not_awaited()


# --- mute (BR-006) --------------------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-006"], steps=[8])
async def test_mute_remembers_the_variant_and_unmute_restores_it(actions, menu):
    assert await actions.dispatch(Gesture(KNOB_PRESS_GESTURE, KNOB), status(audio_volume_master="loud")) == (
        "mute",
        "ok",
    )
    await actions.dispatch(Gesture(KNOB_PRESS_GESTURE, KNOB), status(audio_volume_master="mute"))
    assert sent(menu) == [
        {"command": "step_setting", "option": "audio_volume.master", "direction": "down", "steps": "3"},
        {"command": "step_setting", "option": "audio_volume.master", "direction": "up", "steps": "3"},
    ]


@pytest.mark.uc("UC-015", flow="main", br=["BR-006"], steps=[8])
async def test_unmute_without_a_remembered_variant_restores_the_default(actions, menu):
    result = await actions.dispatch(Gesture(KNOB_PRESS_GESTURE, KNOB), status(audio_volume_master="mute"))
    assert result == ("unmute", "ok")
    assert sent(menu) == [{"command": "step_setting", "option": "audio_volume.master", "direction": "up", "steps": "4"}]


# --- tap announcements (BR-011) -------------------------------------------------------------


@pytest.mark.uc("UC-015", flow="main", br=["BR-011"])
async def test_taps_announce_the_label_then_the_value_and_send_nothing(actions, menu, cues):
    """#2106: each tap says what it is before the value; K7 says the backend by name."""
    for key in ("K2", "K3", "K5", "K7"):
        assert await actions.dispatch(Gesture(TAP, key), status(num_teams=3)) == ("announce", "ok")
    assert [c.args for c in cues.announce.await_args_list] == [
        (Sound.MENU_VOX_LABEL_SENSITIVITY, Sound.MENU_VOX_SENSITIVITY_MEDIUM),
        (Sound.MENU_VOX_LABEL_AGENT, Sound.MENU_VOX_ADMINOP_2),
        (Sound.MENU_VOX_LABEL_TEAMS, Sound.MENU_VOX_ADMINOP_3),
        (Sound.MENU_VOX_LABEL_BACKEND, Sound.MENU_VOX_BACKEND_CLAUDE),
    ]
    cues.voice.assert_not_awaited()
    menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-011"])
@pytest.mark.parametrize(
    ("backend", "voice"),
    [
        ("rules", Sound.MENU_VOX_BACKEND_RULES),
        ("claude", Sound.MENU_VOX_BACKEND_CLAUDE),
        ("openai", Sound.MENU_VOX_BACKEND_OPENAI),
        ("jev", Sound.MENU_VOX_BACKEND_JEV),
    ],
)
async def test_k7_tap_says_the_backend_by_name(actions, cues, backend, voice):
    await actions.dispatch(Gesture(TAP, "K7"), status(agent_backend=backend))
    cues.announce.assert_awaited_once_with(Sound.MENU_VOX_LABEL_BACKEND, voice)


@pytest.mark.uc("UC-015", flow="main", br=["BR-011", "BR-009"])
async def test_k7_tap_with_an_unknown_backend_refuses(actions, cues):
    await actions.dispatch(Gesture(TAP, "K7"), status(agent_backend="mystery"))
    cues.refusal.assert_awaited_once()
    cues.announce.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-011", "BR-013"])
@pytest.mark.parametrize("state", ["unknown", ""], ids=["unknown", "missing"])
async def test_k3_tap_with_an_unresolved_kill_switch_refuses_never_announces_stopped(actions, menu, cues, state):
    result = await actions.dispatch(Gesture(TAP, "K3"), status(agent_enabled=False, agent_enabled_state=state))
    assert result == ("announce", "ok")
    cues.refusal.assert_awaited_once()
    cues.announce.assert_not_awaited()
    menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-011", "BR-013"])
async def test_k3_tap_with_a_resolved_off_and_an_empty_level_announces_stopped(actions, cues):
    await actions.dispatch(Gesture(TAP, "K3"), status(agent_enabled=False, agent_level=""))
    cues.announce.assert_awaited_once_with(Sound.MENU_VOX_LABEL_AGENT, actions_module.cues.AGENT_STOPPED_VOICE)


@pytest.mark.uc("UC-015", flow="main", br=["BR-011", "BR-002"])
async def test_k3_tap_with_the_write_gate_off_announces_stopped(actions, cues):
    """#2093: a running agent whose write gate is off cannot act, so the tap says stopped."""
    await actions.dispatch(Gesture(TAP, "K3"), status(agent_interventions_enabled_state="off"))
    cues.announce.assert_awaited_once_with(Sound.MENU_VOX_LABEL_AGENT, actions_module.cues.AGENT_STOPPED_VOICE)


@pytest.mark.uc("UC-015", flow="main", br=["BR-011", "BR-013"])
@pytest.mark.parametrize("state", ["unknown", ""], ids=["unknown", "missing"])
async def test_k3_tap_with_an_unresolved_write_gate_refuses(actions, menu, cues, state):
    """#2093: an unresolved gate is neither announced as running nor as stopped."""
    result = await actions.dispatch(Gesture(TAP, "K3"), status(agent_interventions_enabled_state=state))
    assert result == ("announce", "ok")
    cues.refusal.assert_awaited_once()
    cues.announce.assert_not_awaited()
    menu.ProcessInput.assert_not_awaited()
