"""
Writes during a disconnect and feedback left over after one (#2471).

UC-002 A1 (disconnect), A4 / BR-008 (reconnect LED):
- H1: an LED or rumble write for a serial with no adapter route is kept, and held
  (writes after the re-route too) until the controller registers again; the owner's
  colour written on the registration wins;
- the output from before the disconnect is still dropped;
- H2: a disconnect ends the controller's feedback (effects, layers, timers);
- D1: a second registration cancels the first delayed LED resend;
- D2: ``clear_led_output`` takes the LED lock and drops the forced resend and effect marks;
- D3: a dead player is off from the death effect's first moment, and a closed game
  drops the output it wrote to a player that is away.
"""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from proto import controller_manager_pb2
from services.controller_manager import discovery_loop as discovery_module
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend
from services.controller_manager.tests.backend_doubles import autospec_adapter, autospec_backend
from services.controller_manager.tests.test_silent_link_disconnect import SERIAL as RIG_SERIAL
from services.controller_manager.tests.test_silent_link_disconnect import Rig

SERIAL = "AA:AA"


def _adapter(serials):
    adapter = autospec_adapter()
    adapter.adapter_type = "mock"
    adapter.discover.return_value = list(serials)
    adapter.open.return_value = True
    adapter.poll.return_value = None
    adapter.set_output.return_value = True
    adapter.get_adapter_for_serial.return_value = None
    adapter.inner = None
    return adapter


@pytest.fixture(autouse=True)
def _metrics():
    with patch("services.controller_manager.multiplexer.multiplexer_backend.metrics"):
        yield


@pytest.fixture
def routed():
    """A backend with SERIAL routed and lit green, then SERIAL gone (no route)."""
    adapter = _adapter([SERIAL])
    mux = MultiplexerBackend(adapters=[adapter])
    mux.get_connected_controllers()
    mux._led_colors[SERIAL] = (0, 255, 0)
    adapter.discover.return_value = []
    mux.get_connected_controllers()
    adapter.set_output.reset_mock()
    return mux, adapter


def _reroute(mux, adapter):
    adapter.discover.return_value = [SERIAL]
    mux.get_connected_controllers()


class TestUC002H1WritesDuringADisconnect:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_kept_write_is_never_sent_before_the_registration(self, routed):
        """Routed again but not registered yet (or the spawn failed): the LED stays unwritten, not stale."""
        mux, adapter = routed
        assert await mux.set_led_color(SERIAL, 0, 255, 0) is False  # e.g. an effect's restore after the drop
        adapter.set_output.assert_not_called()

        mux.get_connected_controllers()  # further passes while it is away keep the write
        _reroute(mux, adapter)
        for _ in range(3):
            mux.update_all_leds()

        adapter.set_output.assert_not_called()
        assert mux._led_colors[SERIAL] == (0, 255, 0)  # kept for the registration

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_write_after_the_reroute_but_before_the_registration_stays_held(self, routed):
        """A cue's rumble (or any write) to the routed, unregistered serial sends neither it nor the kept colour."""
        mux, adapter = routed
        assert await mux.set_led_color(SERIAL, 0, 255, 0) is False  # kept stale game colour
        _reroute(mux, adapter)

        assert await mux.set_rumble(SERIAL, 80) is False
        assert await mux.set_led_color(SERIAL, 255, 255, 255) is False
        mux.update_all_leds()

        adapter.set_output.assert_not_called()
        mux.force_led_resend(SERIAL)  # the registration
        assert await mux.set_led_color(SERIAL, 0, 0, 0) is True  # the owner's off
        sent = [c.args[1:4] for c in adapter.set_output.call_args_list]
        assert (0, 255, 0) not in sent

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_the_registration_releases_the_kept_write(self, routed):
        """With no owner colour, the registration's forced resend sends the kept write."""
        mux, adapter = routed
        assert await mux.set_led_color(SERIAL, 0, 0, 0) is False
        _reroute(mux, adapter)

        mux.force_led_resend(SERIAL)  # _restore_led, first thing on (re)registration
        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(SERIAL, 0, 0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_rumble_written_while_unrouted_is_kept_with_its_colour(self, routed):
        """A rumble write (a stop at 0 included) is kept like a colour, with the colour it goes with."""
        mux, adapter = routed
        assert await mux.set_led_color(SERIAL, 255, 0, 0) is False
        assert await mux.set_rumble(SERIAL, 0) is False

        _reroute(mux, adapter)
        mux.force_led_resend(SERIAL)
        mux.update_all_leds()

        adapter.set_output.assert_called_once_with(SERIAL, 255, 0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_the_output_from_before_the_disconnect_is_still_dropped(self, routed):
        """Without a write during the disconnect nothing is replayed: the pre-disconnect green is gone."""
        mux, adapter = routed
        _reroute(mux, adapter)
        mux.force_led_resend(SERIAL)
        mux.update_all_leds()

        adapter.set_output.assert_not_called()
        assert SERIAL not in mux._led_colors

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_a_kept_write_is_dropped_like_any_output_at_the_next_disconnect(self, routed):
        """Once registered again the serial is ordinary: its output from before a later disconnect is not replayed."""
        mux, adapter = routed
        await mux.set_led_color(SERIAL, 0, 0, 255)  # kept while away
        _reroute(mux, adapter)
        mux.force_led_resend(SERIAL)
        adapter.discover.return_value = []  # gone again, with nothing written meanwhile
        mux.get_connected_controllers()
        _reroute(mux, adapter)
        mux.force_led_resend(SERIAL)
        adapter.set_output.reset_mock()

        mux.update_all_leds()

        adapter.set_output.assert_not_called()
        assert SERIAL not in mux._led_colors

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_the_owners_colour_on_registration_wins_over_the_kept_write(self, routed):
        """Registration order (force_led_resend, then the owner's colour): the stale green never goes out."""
        mux, adapter = routed
        await mux.set_led_color(SERIAL, 0, 255, 0)  # a stale game colour, kept while away
        _reroute(mux, adapter)
        mux.update_all_leds()  # a batch tick during the spawn

        mux.force_led_resend(SERIAL)
        assert await mux.set_led_color(SERIAL, 0, 0, 0) is True  # the owner's off
        mux.update_all_leds()

        sent = [c.args[1:4] for c in adapter.set_output.call_args_list]
        assert (0, 255, 0) not in sent
        assert sent[0] == (0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_shutdown_forgets_unrouted_writes(self, routed):
        mux, _ = routed
        await mux.set_led_color(SERIAL, 0, 0, 0)
        await mux.shutdown()
        assert SERIAL not in mux._led_colors
        assert not mux._unrouted_output


# --- H2 / D1: the discovery loop ----------------------------------------------------------


@pytest.fixture
def rig():
    rig = Rig()
    with (
        patch.object(discovery_module, "read_silent_disconnect_seconds", return_value=3.0),
        patch.object(discovery_module, "time", rig.clock),
    ):
        yield rig


class TestUC002H2DisconnectEndsFeedback:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_a_disconnect_clears_the_controllers_feedback(self, rig):
        await rig.discover()
        rig.feedback.clear_controller.assert_not_called()

        rig.backend.get_connected_controllers.return_value = []
        await rig.discover()

        rig.feedback.clear_controller.assert_called_once_with(RIG_SERIAL)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1")
    async def test_clear_controller_ends_effects_layers_and_timers(self):
        """The background rumble, shield layers, cue tasks and stop timers end; the LED is no longer an effect's."""
        backend = autospec_backend()
        feedback = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {}})
        sleeper = asyncio.create_task(asyncio.sleep(10))
        timer = asyncio.create_task(asyncio.sleep(10))
        cue = asyncio.create_task(asyncio.sleep(10))
        feedback.vibration_tasks[SERIAL] = timer
        feedback._shield_tasks[SERIAL] = sleeper
        feedback._hold_cue_tasks[SERIAL] = cue
        feedback._shield_layers[SERIAL] = {"shield": object()}
        feedback._background_rumble[SERIAL] = 80

        feedback.clear_controller(SERIAL)
        await asyncio.sleep(0)

        assert sleeper.cancelled() and timer.cancelled() and cue.cancelled()
        assert SERIAL not in feedback._shield_layers
        assert SERIAL not in feedback._background_rumble
        backend.set_effect_active.assert_called_with(SERIAL, False)


class TestUC002BR008RegistrationOrder:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_the_registration_releases_the_output_before_the_owners_colour(self, rig):
        """``_restore_led`` calls force_led_resend before it writes the owner's colour (#2471)."""
        order = []
        rig.backend.force_led_resend.side_effect = lambda s: order.append(("release", s))

        async def owner_write(serial, _color):
            order.append(("owner", serial))

        rig.feedback.set_controller_color.side_effect = owner_write
        await rig.discover()

        assert order[:2] == [("release", RIG_SERIAL), ("owner", RIG_SERIAL)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_failed_spawn_releases_nothing(self, rig):
        """No registration, no release: a kept output stays unsent (the next pass retries)."""
        with patch.object(rig.loop, "_spawn_controller_process"):  # the spawn tracked nothing
            await rig.discover()

        rig.backend.force_led_resend.assert_not_called()
        rig.feedback.set_controller_color.assert_not_called()


class TestUC002D1SecondRegistration:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_second_registration_cancels_the_first_delayed_resend(self, rig):
        rig.loop._led_resend_delay = 0.05
        await rig.discover()
        first = rig.loop._led_resend_handles[RIG_SERIAL]

        await rig.loop._restore_led(RIG_SERIAL)  # re-registered within the delay

        assert first.cancelled()
        assert rig.loop._led_resend_handles[RIG_SERIAL] is not first
        await asyncio.sleep(0.1)
        assert rig.backend.force_led_resend.call_count == 3  # two registrations + one delayed resend


# --- D2: clear_led_output -------------------------------------------------------------------


class TestUC002D2ClearLedOutput:
    @pytest.mark.uc("UC-002", br=["BR-007", "BR-008"])
    def test_clear_led_output_drops_the_forced_resend_and_effect_marks(self):
        adapter = _adapter([SERIAL])
        mux = MultiplexerBackend(adapters=[adapter])
        mux.get_connected_controllers()
        mux._led_colors[SERIAL] = (0, 255, 0)
        mux.force_led_resend(SERIAL)
        mux.set_effect_active(SERIAL, True)

        mux.clear_led_output(SERIAL)

        assert SERIAL not in mux._led_colors
        assert SERIAL not in mux._force_led_resend
        assert SERIAL not in mux._effect_active

    @pytest.mark.uc("UC-002", br=["BR-007", "BR-008"])
    def test_clear_led_output_waits_for_a_running_led_batch(self):
        """A batch holding the LED lock finishes before the output is dropped."""
        adapter = _adapter([SERIAL])
        mux = MultiplexerBackend(adapters=[adapter])
        mux.get_connected_controllers()
        mux._led_colors[SERIAL] = (0, 255, 0)
        done = threading.Event()

        mux._led_lock.acquire()
        worker = threading.Thread(target=lambda: (mux.clear_led_output(SERIAL), done.set()))
        worker.start()
        try:
            assert not done.wait(0.1)
            assert mux._led_colors[SERIAL] == (0, 255, 0)
        finally:
            mux._led_lock.release()
        worker.join(1.0)
        assert done.is_set()
        assert SERIAL not in mux._led_colors


# --- D3: a dead player stays off -----------------------------------------------------------


class TestUC002D3DeadPlayerStaysOff:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_the_death_effect_marks_the_player_off_before_the_red_hold(self):
        """A reconnect during the red hold restores off (base colour), not the game colour."""
        backend = autospec_backend()
        feedback = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {}})
        feedback.base_colors[SERIAL] = (0, 255, 0)

        await feedback._effect_player_death(SERIAL)
        task = feedback.active_effects[SERIAL].task  # the red hold runs as the effect task (#2540)
        for _ in range(5):
            await asyncio.sleep(0)
        try:
            assert not task.done()  # still in the red hold
            assert feedback.base_colors[SERIAL] == (0, 0, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    def test_a_closed_game_drops_the_output_it_wrote_to_an_absent_player(self):
        with (
            patch("services.controller_manager.servicer.create_backend", return_value=autospec_backend()),
            patch("services.controller_manager.servicer.DiscoveryLoop"),
        ):
            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
        servicer.discovery_loop.drain_disconnects.return_value = []
        servicer.tracked_controllers["present"] = {}
        servicer._open_game_stream("game", {SERIAL, "present"})

        servicer._release_game_stream("game")

        servicer.backend.clear_led_output.assert_called_once_with(SERIAL)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A4", br=["BR-008"])
    async def test_a_reopened_stream_makes_a_dead_player_reconnect_off(self):
        """After a reopen (no disconnect handed to the new stream) the config's off is the reconnect colour."""
        backend = autospec_backend()
        with (
            patch("services.controller_manager.servicer.create_backend", return_value=backend),
            patch("services.controller_manager.servicer.DiscoveryLoop"),
        ):
            from services.controller_manager.servicer import ControllerManagerServicer

            servicer = ControllerManagerServicer()
        servicer.discovery_loop.drain_disconnects.return_value = []
        config = controller_manager_pb2.GameplayStreamConfig(
            colors=[
                controller_manager_pb2.ControllerColorConfig(serial=SERIAL, color=controller_manager_pb2.RGB()),
                controller_manager_pb2.ControllerColorConfig(
                    serial="alive", color=controller_manager_pb2.RGB(r=0, g=255, b=0)
                ),
            ]
        )
        span = create_autospec(_Span, instance=True)

        await servicer._process_gameplay_config(config, "reopened", span)

        assert servicer._reconnect_color(SERIAL) == (0, 0, 0)
        assert servicer._reconnect_color("alive") == (0, 255, 0)


class _Span:
    def set_attribute(self, key, value): ...


# --- #2484: a silent drop cancels the effect without a write-back -----------------------


class TestUC002SilentDropEffectRestore:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", flow="A1", br=["BR-007", "BR-008"])
    async def test_a_cancelled_effect_does_not_write_the_game_colour_back_on_a_silent_drop(self):
        """Silent drop: clear_led_output, the drop, then the effect's finally. No colour reaches the backend after."""
        backend = autospec_backend()
        tracked = {SERIAL: {}}
        feedback = FeedbackManager(backend=backend, tracked_controllers=tracked)
        feedback.base_colors[SERIAL] = (0, 255, 0)
        await feedback.play_effect_with_restore(SERIAL, "pulse", (255, 0, 0), 10_000, 5, (0, 255, 0))
        await asyncio.sleep(0.05)

        backend.clear_led_output(SERIAL)
        del tracked[SERIAL]  # DiscoveryLoop._drop_controller
        feedback.clear_controller(SERIAL)
        backend.set_led_color.reset_mock()
        await asyncio.sleep(0.05)  # the cancelled effect's finally runs

        backend.set_led_color.assert_not_called()
