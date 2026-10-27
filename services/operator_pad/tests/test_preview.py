"""Live preview while a layer key is held and the knob turns (UC-015 BR-017, BR-005, A1; #2106).

A fake clock and sleep drive the debounce; the audio stub records every clip with its group.
"""

import asyncio
from unittest.mock import create_autospec, patch

import pytest

from lib.setting_ladders import MODE_LADDER
from lib.types import Sound
from proto import audio_pb2, menu_pb2
from services.operator_pad import preview as preview_module
from services.operator_pad.actions import Actions
from services.operator_pad.config import DEFAULT_KEYMAP, PadConfig, Timings, parse_keymap
from services.operator_pad.controller import PadController
from services.operator_pad.cues import ANNOUNCE_GROUP, CuePlayer
from services.operator_pad.gestures import LAYER, PRESS, RELEASE, Gesture
from services.operator_pad.preview import PREVIEW_GROUP, Previewer
from services.operator_pad.state import MenuStatusReader
from services.operator_pad.tests.fakes import AudioStubSpec, CoordinatorStubSpec, MenuStubSpec

LABEL_SECONDS = 0.4
VALUE_SECONDS = 0.3

TEAMS = Sound.MENU_VOX_LABEL_TEAMS.value
NUMBER = {n: getattr(Sound, f"MENU_VOX_ADMINOP_{n}").value for n in range(1, 7)}


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class FakeTime:
    """A monotonic clock and a sleep that returns only when the test advances the clock past it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Future]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            await settle()
            self._waiters = [w for w in self._waiters if not w[1].done()]
            due = sorted((w for w in self._waiters if w[0] <= target), key=lambda w: w[0])
            if not due:
                break
            deadline, future = due[0]
            self.now = max(self.now, deadline)
            future.set_result(None)
        self.now = target
        await settle()


class Rig:
    def __init__(self, enabled: bool = True, debounce_ms: int = 150, label_seconds: float = LABEL_SECONDS) -> None:
        self.label_seconds = label_seconds
        self.time = FakeTime()
        self.clips: list[tuple[str, str]] = []  # (file_path, group)
        self.stops: list[str] = []
        self.audio = create_autospec(AudioStubSpec, instance=True)
        self.audio.PlaySound.side_effect = self._play
        self.audio.StopSound.side_effect = self._stop
        self.config = create_autospec(PadConfig, instance=True)
        self.config.preview_enabled.return_value = enabled
        self.config.preview_debounce_ms.return_value = debounce_ms
        self.cues = CuePlayer(self.audio, sleep=self.time.sleep, clock=self.time.clock)
        self.menu = create_autospec(MenuStubSpec, instance=True)
        self.menu.ProcessInput.return_value = menu_pb2.ProcessInputResponse(success=True)
        self.previewer = Previewer(
            self.cues, self.config, sleep=self.time.sleep, clock=self.time.clock, menu_stub=self.menu
        )

    async def _play(self, request, **_kwargs):
        self.clips.append((request.file_path, request.group))
        seconds = self.label_seconds if "label" in request.file_path.lower() else VALUE_SECONDS
        return audio_pb2.PlaySoundResponse(success=True, duration_seconds=seconds)

    async def _stop(self, request, **_kwargs):
        self.stops.append(request.group)
        return audio_pb2.StopSoundResponse(success=True)

    def files(self) -> list[str]:
        return [f for f, _ in self.clips]

    def mode_previews(self) -> list[dict]:
        """The `preview_setting` commands sent to the menu, in order (the K1 LED preview)."""
        return [dict(c.args[0].data) for c in self.menu.ProcessInput.await_args_list]

    def shown(self) -> list[str]:
        return [data["mode"] for data in self.mode_previews()]

    def step_mode(self, net: int, status=None) -> None:
        if status is None:
            status = menu_pb2.GetMenuStatusResponse(selection=MODE_LADDER[0])
        self.previewer.step("K1", "current_game", status, net, True)

    def step(self, net: int, status=None, key="K5", option="num_teams", lobby_only=True) -> None:
        if status is None:
            status = menu_pb2.GetMenuStatusResponse(num_teams=2)
        self.previewer.step(key, option, status, net, lobby_only)


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-005"], steps=[5])
async def test_a_fast_spin_speaks_one_label_and_only_the_final_value():
    rig = Rig()
    for net in range(1, 5):  # 2 -> 6 teams, a step every 50 ms
        rig.step(net)
        await rig.time.advance(0.05)
    await rig.time.advance(1.0)
    assert rig.clips == [(TEAMS, PREVIEW_GROUP), (NUMBER[6], PREVIEW_GROUP)]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
async def test_slow_steps_speak_each_value_in_the_preview_group():
    rig = Rig()
    for net in range(1, 4):  # a step every 600 ms, longer than the label and the debounce
        rig.step(net)
        await rig.time.advance(0.6)
    assert rig.clips == [
        (TEAMS, PREVIEW_GROUP),
        (NUMBER[3], PREVIEW_GROUP),
        (NUMBER[4], PREVIEW_GROUP),
        (NUMBER[5], PREVIEW_GROUP),
    ]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
async def test_the_value_waits_for_the_debounce_and_the_end_of_the_label():
    rig = Rig()
    rig.step(1)
    await rig.time.advance(0.2)  # past the 150 ms debounce, the label (0.4 s) still plays
    assert rig.files() == [TEAMS]
    await rig.time.advance(0.19)
    assert rig.files() == [TEAMS]
    await rig.time.advance(0.02)
    assert rig.files() == [TEAMS, NUMBER[3]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
async def test_a_value_equal_to_the_last_one_spoken_is_not_repeated():
    rig = Rig()
    with patch.object(preview_module.metrics, "previews_total") as counter:
        rig.step(1)
        await rig.time.advance(0.6)  # "3"
        rig.step(2)
        await rig.time.advance(0.05)
        rig.step(1)  # back to 3 before "4" was spoken
        await rig.time.advance(0.6)
    assert rig.files() == [TEAMS, NUMBER[3]]
    results = [c.kwargs["result"] for c in counter.labels.call_args_list]
    assert results == ["spoken", "replaced", "suppressed"]


@pytest.mark.uc("UC-015", flow="A7", br=["BR-017", "BR-009"], steps=[5])
async def test_pushing_past_the_end_gives_the_limit_word_at_most_once_per_second():
    rig = Rig()
    at_max = menu_pb2.GetMenuStatusResponse(num_teams=6)
    rig.step(1, at_max)
    await rig.time.advance(0.6)
    rig.step(2, at_max)  # its limit word would come 0.35 s after the first one
    await rig.time.advance(0.3)
    assert rig.files() == [TEAMS, Sound.MENU_VOX_STATUS_MAXIMUM.value]
    await rig.time.advance(0.6)
    rig.step(3, at_max)  # more than 1 s later
    await rig.time.advance(0.6)
    assert rig.files() == [TEAMS, Sound.MENU_VOX_STATUS_MAXIMUM.value, Sound.MENU_VOX_STATUS_MAXIMUM.value]


@pytest.mark.uc("UC-015", flow="A7", br=["BR-017"], steps=[5])
async def test_a_step_past_the_end_after_moving_gives_the_limit_word_not_the_value_again():
    rig = Rig()
    five = menu_pb2.GetMenuStatusResponse(num_teams=5)
    rig.step(1, five)
    await rig.time.advance(0.6)  # "6"
    rig.step(2, five)  # still 6: past the end
    await rig.time.advance(0.6)
    assert rig.files() == [TEAMS, NUMBER[6], Sound.MENU_VOX_STATUS_MAXIMUM.value]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-013"], steps=[5])
async def test_no_status_at_the_press_is_silent():
    rig = Rig()
    for net in (1, 2):
        rig.previewer.step("K5", "num_teams", None, net, True)
        await rig.time.advance(0.6)
    assert rig.clips == []


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-013"], steps=[5])
async def test_an_unreadable_agent_switch_is_silent():
    rig = Rig()
    unknown = menu_pb2.GetMenuStatusResponse(agent_level="standard", agent_enabled_state="unknown")
    rig.step(1, unknown, key="K3", option="agent_level", lobby_only=False)
    await rig.time.advance(0.6)
    assert rig.clips == []


@pytest.mark.uc("UC-015", flow="A1", br=["BR-017"], steps=[4, 5])
async def test_a_lobby_only_layer_while_live_says_not_possible_once_and_no_values():
    rig = Rig()
    live = menu_pb2.GetMenuStatusResponse(num_teams=2, lifecycle_live=True)
    for net in (1, 2, 3):
        rig.step(net, live)
        await rig.time.advance(0.6)
    assert rig.files() == [Sound.MENU_VOX_STATUS_NOT_POSSIBLE.value]


@pytest.mark.uc("UC-015", flow="A1", br=["BR-017"], steps=[5])
async def test_the_agent_level_previews_while_a_game_is_live():
    rig = Rig()
    live = menu_pb2.GetMenuStatusResponse(
        lifecycle_live=True, agent_level="ambient", agent_enabled_state="on", agent_interventions_enabled_state="on"
    )
    rig.step(1, live, key="K3", option="agent_level", lobby_only=False)
    await rig.time.advance(0.6)
    assert rig.files() == [Sound.MENU_VOX_LABEL_AGENT.value, Sound.MENU_VOX_ADMINOP_2.value]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
async def test_preview_disabled_by_flag_is_silent():
    rig = Rig(enabled=False)
    rig.step(1)
    await rig.time.advance(0.6)
    assert rig.clips == []


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-005"], steps=[5])
async def test_end_hold_drops_the_waiting_value_and_stops_the_preview_group():
    rig = Rig()
    rig.step(1)
    await rig.time.advance(0.05)
    rig.previewer.end_hold()
    await rig.time.advance(1.0)
    assert rig.files() == [TEAMS]
    assert rig.stops == [ANNOUNCE_GROUP, PREVIEW_GROUP]
    assert rig.previewer.hold_key is None


@pytest.mark.uc("UC-015", flow="A4", br=["BR-017", "BR-010"])
async def test_cancel_mid_hold_leaves_no_task():
    rig = Rig()
    rig.step(1)
    await rig.time.advance(0.05)
    rig.previewer.cancel()
    await settle()
    assert rig.previewer._tasks == set()
    await rig.time.advance(1.0)
    assert rig.files() == [TEAMS]


# --- through the controller: nothing is written before the release (BR-005) ---------------------------

KEYMAP = parse_keymap(DEFAULT_KEYMAP)
CODE = {name: c for c, name in KEYMAP.items()}


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-005"], steps=[5])
async def test_no_command_is_sent_before_the_release_and_the_release_stops_the_preview():
    rig = Rig()
    menu = create_autospec(MenuStubSpec, instance=True)
    stops_at_commit: list[list[str]] = []

    async def commit(_request, **_kwargs):
        await settle()  # the StopSound is a task: give it the chance to run first
        stops_at_commit.append(list(rig.stops))
        return menu_pb2.ProcessInputResponse(success=True)

    menu.ProcessInput.side_effect = commit
    actions = Actions(menu, create_autospec(CoordinatorStubSpec, instance=True), rig.cues, previewer=rig.previewer)
    rig.config.enabled.return_value = True
    rig.config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(num_teams=2)
    pad = PadController(rig.config, reader, actions, rig.cues)
    pad.attached(KEYMAP)

    await pad.on_code(CODE["K5"], PRESS, 0)
    for i in range(3):
        await pad.on_code(CODE["KNOB_TURN_B"], PRESS, 10 + i * 10)
        await pad.on_code(CODE["KNOB_TURN_B"], RELEASE, 11 + i * 10)
        await rig.time.advance(0.6)
    menu.ProcessInput.assert_not_awaited()
    assert rig.files() == [TEAMS, NUMBER[3], NUMBER[4], NUMBER[5]]
    await pad.on_code(CODE["K5"], RELEASE, 2000)
    await settle()
    (call,) = menu.ProcessInput.await_args_list
    assert dict(call.args[0].data) == {
        "command": "step_setting",
        "option": "num_teams",
        "direction": "up",
        "steps": "3",
    }
    # BR-017: the preview group is stopped before the commit, so no preview clip plays over the menu's voice.
    assert stops_at_commit == [[ANNOUNCE_GROUP, PREVIEW_GROUP]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-007"], steps=[5])
async def test_a_chord_ends_the_preview():
    rig = Rig()
    actions = Actions(
        create_autospec(MenuStubSpec, instance=True),
        create_autospec(CoordinatorStubSpec, instance=True),
        rig.cues,
        previewer=rig.previewer,
    )
    rig.config.enabled.return_value = True
    rig.config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(num_teams=2)
    pad = PadController(rig.config, reader, actions, rig.cues)
    pad.attached(KEYMAP)

    await pad.on_code(CODE["K5"], PRESS, 0)
    await pad.on_code(CODE["KNOB_TURN_B"], PRESS, 10)
    await rig.time.advance(0.05)
    await pad.on_code(CODE["K2"], PRESS, 100)  # a chord: the hold commits nothing, the preview stops
    await rig.time.advance(1.0)
    assert rig.files() == [TEAMS]
    assert rig.stops == [ANNOUNCE_GROUP, PREVIEW_GROUP]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
async def test_with_the_label_over_a_fast_spin_still_speaks_only_the_final_value():
    rig = Rig(label_seconds=0.0)
    for net in range(1, 5):
        rig.step(net)
        await rig.time.advance(0.05)
    await rig.time.advance(1.0)
    assert rig.files() == [TEAMS, NUMBER[6]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
async def test_the_value_waits_the_debounce_after_the_last_step():
    rig = Rig(label_seconds=0.0)
    rig.step(1)
    await rig.time.advance(0.1)
    rig.step(2)  # restarts the wait
    await rig.time.advance(0.14)
    assert rig.files() == [TEAMS]
    await rig.time.advance(0.02)
    assert rig.files() == [TEAMS, NUMBER[4]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-011"], steps=[5])
async def test_a_new_hold_cuts_a_tap_announcement_still_playing():
    rig = Rig()
    await rig.cues.announce(Sound.MENU_VOX_LABEL_SENSITIVITY, Sound.MENU_VOX_SENSITIVITY_HIGH)
    await settle()  # the announcement label plays, its value waits 0.4 s
    rig.step(1)
    await rig.time.advance(1.0)
    assert Sound.MENU_VOX_SENSITIVITY_HIGH.value not in rig.files()
    assert rig.stops[0] == ANNOUNCE_GROUP


@pytest.mark.uc("UC-015", flow="A4", br=["BR-017"])
async def test_shutdown_cancels_a_pending_stop():
    rig = Rig()
    hang = asyncio.Event()

    async def hanging_stop(_request, **_kwargs):
        await hang.wait()

    rig.audio.StopSound.side_effect = hanging_stop
    rig.step(1)
    rig.previewer.end_hold()
    await settle()
    pending = set(rig.previewer._stops)
    assert pending
    rig.previewer.cancel()
    await settle()
    assert rig.previewer._stops == set()
    assert all(task.cancelled() for task in pending)


@pytest.mark.uc("UC-015", flow="A1", br=["BR-017"], steps=[5])
async def test_a_refused_hold_says_not_possible_once_including_the_release():
    rig = Rig()
    menu = create_autospec(MenuStubSpec, instance=True)
    actions = Actions(menu, create_autospec(CoordinatorStubSpec, instance=True), rig.cues, previewer=rig.previewer)
    rig.config.enabled.return_value = True
    rig.config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(num_teams=2, lifecycle_live=True)
    pad = PadController(rig.config, reader, actions, rig.cues)
    pad.attached(KEYMAP)

    await pad.on_code(CODE["K5"], PRESS, 0)
    for i in range(2):
        await pad.on_code(CODE["KNOB_TURN_B"], PRESS, 10 + i * 10)
        await rig.time.advance(0.6)
    _, result = await actions.dispatch(Gesture(LAYER, "K5", steps=2, held_ms=900), reader.fetch.return_value)
    await settle()
    assert result == "refused"
    assert rig.files() == [Sound.MENU_VOX_STATUS_NOT_POSSIBLE.value]
    menu.ProcessInput.assert_not_awaited()


# --- The mode's LED preview: `preview_setting` while K1 is held (#2106, BR-017, BR-015) -----------


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015", "BR-005"], steps=[4])
async def test_a_k1_hold_pulses_each_previewed_mode_and_sends_no_step():
    rig = Rig()
    for net in (1, 2):
        rig.step_mode(net)
        await rig.time.advance(0.6)
    assert rig.mode_previews() == [
        {"command": "preview_setting", "option": "current_game", "mode": MODE_LADDER[1]},
        {"command": "preview_setting", "option": "current_game", "mode": MODE_LADDER[2]},
    ]
    for call in rig.menu.ProcessInput.await_args_list:
        assert call.args[0].input_type == "web_command"
        assert call.kwargs["timeout"] == preview_module.MODE_PREVIEW_TIMEOUT_SECONDS


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015"], steps=[4])
async def test_a_fast_k1_spin_pulses_only_the_final_mode():
    rig = Rig()
    for net in range(1, 4):
        rig.step_mode(net)
        await rig.time.advance(0.05)
    await rig.time.advance(1.0)
    assert rig.shown() == [MODE_LADDER[3]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-009"], steps=[4])
async def test_the_mode_pulse_does_not_wait_for_the_label():
    rig = Rig(label_seconds=2.0)
    rig.step_mode(1)
    await rig.time.advance(0.2)
    # LED first (BR-009): the pulse goes out after the debounce, the value voice still waits for the label.
    assert rig.shown() == [MODE_LADDER[1]]
    assert rig.files() == [Sound.MENU_VOX_LABEL_GAME_MODE.value]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[5])
@pytest.mark.parametrize(
    ("key", "option", "status"),
    [
        ("K5", "num_teams", menu_pb2.GetMenuStatusResponse(num_teams=2)),
        ("K2", "sensitivity", menu_pb2.GetMenuStatusResponse(sensitivity=1)),
        ("K7", "decision.backend", menu_pb2.GetMenuStatusResponse(agent_backend="rules")),
    ],
)
async def test_other_layers_send_no_led_preview(key, option, status):
    rig = Rig()
    rig.step(1, status=status, key=key, option=option, lobby_only=key != "K7")
    await rig.time.advance(1.0)
    rig.previewer.end_hold()
    await settle()
    rig.menu.ProcessInput.assert_not_awaited()


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015"], steps=[4])
async def test_a_k1_hold_ending_without_a_commit_restores_the_selected_mode():
    rig = Rig()
    rig.step_mode(2)
    await rig.time.advance(0.6)
    rig.previewer.end_hold()  # chord, lost device, a net of zero: nothing is committed
    await settle()
    assert rig.shown() == [MODE_LADDER[2], MODE_LADDER[0]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015"], steps=[4])
async def test_a_k1_release_that_commits_another_mode_sends_no_restore():
    rig = Rig()
    rig.step_mode(2)
    await rig.time.advance(0.6)
    rig.previewer.end_hold(commit_steps=2)  # the menu's commit pulses the new mode itself
    await settle()
    assert rig.shown() == [MODE_LADDER[2]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017"], steps=[4])
async def test_no_restore_when_the_last_pulse_was_the_selected_mode_or_none_was_sent():
    rig = Rig()
    rig.step_mode(1)
    await rig.time.advance(0.6)
    rig.step_mode(0)  # back to the start
    await rig.time.advance(0.6)
    rig.previewer.end_hold()
    await settle()
    assert rig.shown() == [MODE_LADDER[1], MODE_LADDER[0]]

    rig.step_mode(1)  # released before the debounce: nothing was shown
    await rig.time.advance(0.05)
    rig.previewer.end_hold()
    await settle()
    assert rig.shown() == [MODE_LADDER[1], MODE_LADDER[0]]


@pytest.mark.uc("UC-015", flow="A1", br=["BR-017"], steps=[4])
async def test_a_refused_k1_hold_sends_no_led_preview_and_no_restore():
    rig = Rig()
    live = menu_pb2.GetMenuStatusResponse(selection=MODE_LADDER[0], lifecycle_live=True)
    for net in (1, 2):
        rig.step_mode(net, status=live)
        await rig.time.advance(0.6)
    rig.previewer.end_hold()
    await settle()
    rig.menu.ProcessInput.assert_not_awaited()
    assert rig.files() == [Sound.MENU_VOX_STATUS_NOT_POSSIBLE.value]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-010"], steps=[4])
async def test_a_failed_led_preview_is_not_retried_and_plays_no_cue():
    rig = Rig()
    rig.menu.ProcessInput.side_effect = ConnectionError("menu down")
    rig.step_mode(1)
    await rig.time.advance(1.0)
    assert rig.menu.ProcessInput.await_count == 1
    value = preview_module.value_voice("current_game", MODE_LADDER[1])
    assert rig.files() == [Sound.MENU_VOX_LABEL_GAME_MODE.value, value.value]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-005", "BR-015"], steps=[4])
@pytest.mark.parametrize(
    ("back_turn", "expected"),
    [
        # Turned back to the start and released at once: nothing is committed, the selected mode is restored.
        (True, [("preview_setting", MODE_LADDER[1]), ("preview_setting", MODE_LADDER[0])]),
        # Released one mode on: the commit pulses the new mode itself, no restore.
        (False, [("preview_setting", MODE_LADDER[1]), ("step_setting", "1")]),
    ],
)
async def test_a_k1_release_restores_only_when_nothing_is_committed(back_turn, expected):
    rig = Rig()
    menu = rig.menu  # one menu stub: the commit and the LED preview share it
    actions = Actions(menu, create_autospec(CoordinatorStubSpec, instance=True), rig.cues, previewer=rig.previewer)
    rig.config.enabled.return_value = True
    rig.config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(selection=MODE_LADDER[0])
    pad = PadController(rig.config, reader, actions, rig.cues)
    pad.attached(KEYMAP)

    await pad.on_code(CODE["K1"], PRESS, 0)
    await pad.on_code(CODE["KNOB_TURN_B"], PRESS, 10)
    await pad.on_code(CODE["KNOB_TURN_B"], RELEASE, 11)
    await rig.time.advance(0.6)
    if back_turn:
        await pad.on_code(CODE["KNOB_TURN_A"], PRESS, 700)
        await pad.on_code(CODE["KNOB_TURN_A"], RELEASE, 701)
    await pad.on_code(CODE["K1"], RELEASE, 720)
    await rig.time.advance(1.0)
    assert [(d["command"], d.get("mode", d.get("steps"))) for d in rig.mode_previews()] == expected


@pytest.mark.uc("UC-015", flow="A1", br=["BR-017", "BR-015"], steps=[4])
@pytest.mark.parametrize("release_status", ["missing", "live"])
async def test_a_k1_release_the_pad_does_not_send_restores_the_selected_mode(release_status):
    rig = Rig()
    actions = Actions(rig.menu, create_autospec(CoordinatorStubSpec, instance=True), rig.cues, previewer=rig.previewer)
    rig.config.enabled.return_value = True
    rig.config.timings.return_value = Timings(knob_events_per_step=1)
    reader = create_autospec(MenuStatusReader, instance=True)
    reader.fetch.return_value = menu_pb2.GetMenuStatusResponse(selection=MODE_LADDER[0])
    pad = PadController(rig.config, reader, actions, rig.cues)
    pad.attached(KEYMAP)

    await pad.on_code(CODE["K1"], PRESS, 0)
    await pad.on_code(CODE["KNOB_TURN_B"], PRESS, 10)
    await pad.on_code(CODE["KNOB_TURN_B"], RELEASE, 11)
    await rig.time.advance(0.6)
    # The status read at the release: unreadable (the release sends nothing) or live (refused, A1).
    reader.fetch.return_value = (
        None
        if release_status == "missing"
        else menu_pb2.GetMenuStatusResponse(selection=MODE_LADDER[0], lifecycle_live=True)
    )
    await pad.on_code(CODE["K1"], RELEASE, 720)
    await rig.time.advance(1.0)
    assert [(d["command"], d.get("mode")) for d in rig.mode_previews()] == [
        ("preview_setting", MODE_LADDER[1]),
        ("preview_setting", MODE_LADDER[0]),
    ]


class BlockedMenu:
    """A menu whose `preview_setting` answers only when the test lets it; records each mode as it goes out."""

    def __init__(self, rig: Rig) -> None:
        self.sent: list[str] = []
        self.answered: list[str] = []  # calls that ran to the menu's answer (not cancelled mid-flight)
        self.answer = asyncio.Event()
        rig.menu.ProcessInput.side_effect = self._process_input

    async def _process_input(self, request, **_kwargs):
        self.sent.append(request.data["mode"])
        await self.answer.wait()
        self.answered.append(request.data["mode"])
        return menu_pb2.ProcessInputResponse(success=True)


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015"], steps=[4])
async def test_the_restore_waits_for_a_pulse_still_in_flight():
    rig = Rig()
    menu = BlockedMenu(rig)
    rig.step_mode(1)
    await rig.time.advance(0.2)  # the M1 pulse is on the wire, unanswered
    rig.previewer.end_hold()
    await settle()
    assert menu.sent == [MODE_LADDER[1]]  # the restore never overtakes it
    menu.answer.set()
    await rig.previewer.drain()
    assert menu.sent == [MODE_LADDER[1], MODE_LADDER[0]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015"], steps=[4])
async def test_a_pulse_back_to_the_selected_mode_is_sent_even_when_the_hold_ends_first():
    rig = Rig()
    menu = BlockedMenu(rig)
    rig.step_mode(1)
    await rig.time.advance(0.2)  # M1 on the wire
    rig.step_mode(0)
    await rig.time.advance(0.2)  # M0 queued behind it
    rig.previewer.end_hold()  # net zero: nothing is committed
    await settle()
    menu.answer.set()
    await rig.previewer.drain()
    # M0 is the last pulse that went out, so no extra restore; the hold end cancelled neither call.
    assert menu.sent == [MODE_LADDER[1], MODE_LADDER[0]]
    assert menu.answered == [MODE_LADDER[1], MODE_LADDER[0]]


@pytest.mark.uc("UC-015", flow="main", br=["BR-017", "BR-015"], steps=[4])
async def test_m1_m2_m1_then_a_quick_commit_sends_every_pulse_in_order_and_no_restore():
    rig = Rig()
    menu = BlockedMenu(rig)
    for net in (1, 2, 1):
        rig.step_mode(net)
        await rig.time.advance(0.2)
    rig.previewer.end_hold(commit_steps=1)  # released before the last pulse went out: M1 is committed
    await settle()
    assert menu.sent == [MODE_LADDER[1]]
    menu.answer.set()
    await rig.previewer.drain()
    assert menu.sent == [MODE_LADDER[1], MODE_LADDER[2], MODE_LADDER[1]]
