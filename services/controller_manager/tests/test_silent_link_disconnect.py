"""A silent link is a disconnect, and a (re)registered controller's LED is written by its owner (#2466).

UC-002 BR-007: a tracked controller with no fresh report for `silent_disconnect_seconds`
is disconnected (so a running game eliminates the player, UC-020 A2) and registers
again once it serves a new sample. UC-002 BR-008: on (re)registration the LED is sent
again even when unchanged, and the colour comes from the current owner.

The discovery loop runs on a fake clock patched into its own module only, so the 100 Hz
poll batches and the 1 Hz discovery passes are driven step by step.
"""

import asyncio
import math
from unittest.mock import create_autospec, patch

import pytest

from services.controller_manager import discovery_loop as discovery_module
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import (
    DiscoveryLoop,
    resolve_silent_disconnect_seconds,
)
from services.controller_manager.event_publisher import EventPublisher
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.tests.backend_doubles import autospec_backend

SERIAL = "00:07:04:A3:67:E8"
POLL = 0.01


class FakeClock:
    """Stands in for the ``time`` module inside discovery_loop only."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


def _sample() -> dict:
    return {"serial": SERIAL, "battery": 4, "accel": {"x": 0.0, "y": 0.0, "z": 1.0}}


class Rig:
    def __init__(self, reconnect_color=None) -> None:
        self.clock = FakeClock()
        self.current = _sample()  # the object the backend serves; replace it for a fresh frame
        self.backend = autospec_backend()
        self.backend.get_connected_controllers.return_value = [SERIAL]

        async def get_state(_serial):
            return self.current

        self.backend.get_controller_state.side_effect = get_state
        rescan = create_autospec(PeriodicRescanTimer, instance=True)
        rescan.should_force_rescan.return_value = False
        self.buttons = create_autospec(ButtonDetector, instance=True)
        self.feedback = create_autospec(FeedbackManager, instance=True)
        self.loop = DiscoveryLoop(
            backend=self.backend,
            tracked_controllers={},
            controller_states={},
            button_detector=self.buttons,
            feedback_manager=self.feedback,
            monitoring=create_autospec(ControllerMonitoring, instance=True),
            rescan_timer=rescan,
            paired_serials=[],
            base_colors={SERIAL: (0, 51, 255)},
            event_publisher=create_autospec(EventPublisher, instance=True),
            reconnect_color=reconnect_color,
        )
        self.loop.backend_initialized = True
        self.loop._led_resend_delay = 0.01

    def fresh(self) -> None:
        self.current = _sample()

    async def poll_for(self, seconds: float, fresh_every: int | None = None) -> None:
        """Run 100 Hz poll batches for ``seconds`` of fake time; a fresh frame every ``fresh_every`` polls."""
        for step in range(round(seconds / POLL)):
            if fresh_every and step % fresh_every == 0:
                self.fresh()
            await self.loop._update_controller_states()
            self.clock.now += POLL

    async def discover(self) -> None:
        await self.loop._check_for_new_controllers()

    def connect_events(self) -> list[bool]:
        return [c.kwargs["is_connect"] for c in self.buttons.publish_connection_event.call_args_list]


@pytest.fixture
def silent_limit():
    with patch.object(discovery_module, "read_silent_disconnect_seconds", return_value=3.0) as limit:
        yield limit


@pytest.fixture
def rig(silent_limit):  # noqa: ARG001
    rig = Rig()
    with patch.object(discovery_module, "time", rig.clock):
        yield rig


async def _registered(rig: Rig) -> None:
    await rig.discover()
    assert SERIAL in rig.loop.tracked_controllers
    await rig.poll_for(0.5, fresh_every=2)


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1", br=["BR-007"])
async def test_a_controller_silent_for_the_limit_is_disconnected(rig):
    """Out of range: the link stays listed, the reports stop, the last sample is replayed (#2466 item 1)."""
    await _registered(rig)

    await rig.poll_for(3.1)  # same object every poll: no fresh frame
    await rig.discover()

    assert SERIAL not in rig.loop.tracked_controllers
    assert rig.loop.drain_disconnects() == [SERIAL]  # the game gets it (UC-020 A2)
    assert rig.connect_events() == [True, False]


@pytest.mark.asyncio
@pytest.mark.uc("UC-020", flow="A2")
async def test_a_silent_link_reaches_the_game_as_a_disconnect(rig):
    """UC-020 A2: a player whose controller went silent is reported to the game stream."""
    await _registered(rig)

    await rig.poll_for(2.9)
    await rig.discover()
    assert rig.loop.drain_disconnects() == []  # below the limit

    await rig.poll_for(0.2)
    await rig.discover()
    assert rig.loop.drain_disconnects() == [SERIAL]


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", br=["BR-007"])
async def test_normal_report_gaps_never_trip_the_limit(rig):
    """A working link (a report every 50 ms, one 2.5 s hiccup) stays connected."""
    await _registered(rig)

    for _ in range(5):
        await rig.poll_for(1.0, fresh_every=5)
        await rig.discover()
    await rig.poll_for(2.5)
    await rig.discover()
    await rig.poll_for(1.0, fresh_every=5)
    await rig.discover()

    assert SERIAL in rig.loop.tracked_controllers
    assert rig.loop.drain_disconnects() == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", br=["BR-007"])
async def test_a_stalled_poll_loop_does_not_make_a_link_silent(rig):
    """The silence is measured up to the last poll batch: a stalled loop disconnects nobody."""
    await _registered(rig)

    rig.clock.now += 5.0  # no poll batch ran for 5 s (event loop blocked)
    await rig.discover()

    assert SERIAL in rig.loop.tracked_controllers
    assert rig.loop.drain_disconnects() == []


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A4", br=["BR-007"])
async def test_a_silenced_controller_registers_again_when_reports_resume(rig):
    """Back in range: the next discovery pass that polls a new sample registers it like a reconnect."""
    await _registered(rig)
    await rig.poll_for(3.1)
    await rig.discover()
    assert SERIAL not in rig.loop.tracked_controllers

    await rig.discover()  # still the replayed sample: stays out
    assert SERIAL not in rig.loop.tracked_controllers
    assert rig.connect_events() == [True, False]

    rig.fresh()
    await rig.discover()

    assert SERIAL in rig.loop.tracked_controllers
    assert rig.connect_events() == [True, False, True]


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", br=["BR-007"])
async def test_the_limit_is_read_live(rig, silent_limit):
    """A flag flip applies on the next discovery pass."""
    silent_limit.return_value = 5.0
    await _registered(rig)

    await rig.poll_for(3.1)
    await rig.discover()
    assert SERIAL in rig.loop.tracked_controllers

    silent_limit.return_value = 3.0
    await rig.discover()
    assert SERIAL not in rig.loop.tracked_controllers


@pytest.mark.uc("UC-002", br=["BR-007"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, 3.0), (5.5, 5.5), (0, 1.0), (-2, 1.0), (60, 10.0), ("3", 3.0), (True, 3.0), (math.nan, 3.0), (math.inf, 3.0)],
)
def test_the_limit_is_bounded(value, expected):
    assert resolve_silent_disconnect_seconds(value) == expected


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
async def test_a_registration_sends_the_led_again_now_and_after_a_delay(rig):
    """#2466 item 2: the first LED write after a re-open may be dropped; it is never deduplicated away."""
    await rig.discover()

    rig.backend.force_led_resend.assert_called_once_with(SERIAL)
    rig.feedback.set_controller_color.assert_awaited_once_with(SERIAL, (0, 51, 255))

    await asyncio.sleep(0.05)
    assert rig.backend.force_led_resend.call_count == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
async def test_the_restored_colour_comes_from_the_owner(silent_limit):  # noqa: ARG001
    """#2466 item 3: the colour is asked from the current owner, not replayed from the cache."""
    asked = []

    def owner(serial):
        asked.append(serial)
        return (0, 0, 0)

    rig = Rig(reconnect_color=owner)
    with patch.object(discovery_module, "time", rig.clock):
        await rig.discover()

    assert asked == [SERIAL]
    rig.feedback.set_controller_color.assert_awaited_once_with(SERIAL, (0, 0, 0))


# --- servicer: the owner's answer (UC-002 BR-008) ---------------------------------------


@pytest.fixture
def servicer():
    with (
        patch("services.controller_manager.servicer.create_backend", return_value=autospec_backend()),
        patch("services.controller_manager.servicer.DiscoveryLoop") as loop_cls,
    ):
        from services.controller_manager.servicer import ControllerManagerServicer

        servicer = ControllerManagerServicer()
    servicer.discovery_loop.drain_disconnects.return_value = []
    servicer.loop_kwargs = loop_cls.call_args.kwargs
    return servicer


@pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
def test_the_servicer_hands_its_owner_lookup_to_the_discovery_loop(servicer):
    assert servicer.loop_kwargs["reconnect_color"] == servicer._reconnect_color


@pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
def test_a_player_reconnecting_into_its_running_game_gets_the_out_state(servicer):
    """The game eliminated it on the disconnect: never its cached game colour back."""
    servicer.feedback_manager.base_colors[SERIAL] = (0, 51, 255)
    servicer._open_game_stream("game", {SERIAL, "other"})

    assert servicer._reconnect_color(SERIAL) == (0, 0, 0)
    assert servicer.feedback_manager.base_colors[SERIAL] == (0, 0, 0)  # effects restore to off


@pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
def test_a_controller_outside_the_game_gets_its_lobby_colour(servicer):
    servicer.feedback_manager.base_colors[SERIAL] = (10, 20, 30)
    servicer._open_game_stream("game", {"other"})

    assert servicer._reconnect_color(SERIAL) == (10, 20, 30)


@pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
def test_a_closed_game_drops_the_colours_of_its_absent_players(servicer):
    """A player who dropped during the game and returns in the lobby shows no old game colour."""
    servicer.tracked_controllers["present"] = {}
    servicer.feedback_manager.base_colors.update({SERIAL: (0, 51, 255), "present": (255, 0, 0)})
    servicer._open_game_stream("game", {SERIAL, "present"})

    servicer._release_game_stream("game")

    assert SERIAL not in servicer.feedback_manager.base_colors
    assert servicer.feedback_manager.base_colors["present"] == (255, 0, 0)
    assert servicer._reconnect_color(SERIAL) is None
