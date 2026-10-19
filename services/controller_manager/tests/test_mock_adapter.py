"""
Unit tests for MockAdapter — thin I/O adapter for simulated controllers.
"""

import asyncio
import math
import sys
import threading
import time
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.mock_adapter import MockAdapter


class TestMockAdapterInit:
    def test_adapter_type(self):
        adapter = MockAdapter(num_controllers=2)
        assert adapter.adapter_type == "mock"

    def test_default_zero_controllers(self):
        adapter = MockAdapter()
        assert adapter._num_controllers == 0
        assert len(adapter.controllers) == 0

    def test_starts_empty(self):
        adapter = MockAdapter(num_controllers=2)
        assert len(adapter.controllers) == 0


class TestMockAdapterDiscover:
    def test_creates_controllers_on_first_discover(self):
        adapter = MockAdapter(num_controllers=3)
        serials = adapter.discover()
        assert len(serials) == 3
        assert "mock_controller_0" in serials
        assert "mock_controller_1" in serials
        assert "mock_controller_2" in serials

    def test_discover_zero_controllers_returns_empty(self):
        adapter = MockAdapter(num_controllers=0)
        serials = adapter.discover()
        assert serials == []

    def test_discover_zero_does_not_recreate_on_subsequent_calls(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        adapter.add_controller("DYN01")
        # Second discover should return the dynamically added controller
        serials = adapter.discover()
        assert serials == ["DYN01"]

    def test_returns_existing_controllers_on_subsequent_discover(self):
        adapter = MockAdapter(num_controllers=2)
        first = adapter.discover()
        second = adapter.discover()
        assert first == second

    def test_includes_dynamically_added_controllers(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        adapter.add_controller("CUSTOM_01")
        serials = adapter.discover()
        assert "CUSTOM_01" in serials
        assert "mock_controller_0" in serials


class TestMockAdapterOpen:
    def test_open_existing_controller(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        assert adapter.open("mock_controller_0") is True

    def test_open_creates_new_controller(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        assert adapter.open("NEW_CTRL") is True
        assert "NEW_CTRL" in adapter.controllers


class TestMockAdapterPoll:
    def test_poll_returns_state_dict(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        state = adapter.poll("mock_controller_0")
        assert state is not None
        assert state[StateKey.SERIAL] == "mock_controller_0"
        assert StateKey.BATTERY in state
        assert StateKey.ACCEL in state
        assert StateKey.GYRO in state
        assert ButtonKey.MOVE in state
        assert state["connection_type"] == "mock"

    def test_poll_unknown_returns_none(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        assert adapter.poll("NONEXISTENT") is None

    def test_poll_generates_noise(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        states = [adapter.poll("mock_controller_0") for _ in range(10)]
        # Accel Z values should vary around 1.0 (gravity)
        z_values = [s[StateKey.ACCEL][AxisKey.Z] for s in states]
        assert not all(z == z_values[0] for z in z_values)

    def test_poll_death_simulation(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        ctrl = adapter.controllers["mock_controller_0"]
        ctrl["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
        ctrl["death_frames_remaining"] = 5  # delivered-frame budget (#926)
        ctrl["death_hold_until"] = time.time() + 10.0

        state = adapter.poll("mock_controller_0")
        assert state[StateKey.ACCEL][AxisKey.X] == 5.0
        assert state[StateKey.ACCEL][AxisKey.Y] == 3.0
        assert state[StateKey.ACCEL][AxisKey.Z] == 4.0

    def test_poll_clears_expired_death(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        ctrl = adapter.controllers["mock_controller_0"]
        ctrl["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
        ctrl["death_frames_remaining"] = 5
        ctrl["death_hold_until"] = time.time() - 1.0  # Safety cap already expired

        state = adapter.poll("mock_controller_0")
        # Should use normal accel (noise), not death accel
        assert abs(state[StateKey.ACCEL][AxisKey.X]) < 2.0  # Normal noise range

    def test_poll_holds_death_until_frame_budget_drained(self):
        """Death-accel persists across polls regardless of wall-clock (#926).

        The latch is frame-budget based: as long as frames remain (and the
        safety cap has not passed), every poll reports the death accel — so a
        slow/starved gameplay send loop can never skip the death window.
        """
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        ctrl = adapter.controllers["mock_controller_0"]
        ctrl["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
        ctrl["death_frames_remaining"] = 3
        ctrl["death_hold_until"] = 0.0  # no wall-clock cap (controller in a game)

        # Many polls, no frame consumption: death accel stays latched.
        for _ in range(50):
            state = adapter.poll("mock_controller_0")
            assert state[StateKey.ACCEL][AxisKey.X] == 5.0

    def test_consume_death_frame_drains_budget_then_reverts(self):
        """consume_death_frame counts DELIVERED frames; latch clears at zero."""
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        ctrl = adapter.controllers["mock_controller_0"]
        ctrl["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
        ctrl["death_frames_remaining"] = 3
        ctrl["death_hold_until"] = 0.0

        # Three delivered frames drain the budget.
        for _ in range(3):
            assert adapter.poll("mock_controller_0")[StateKey.ACCEL][AxisKey.X] == 5.0
            adapter.consume_death_frame("mock_controller_0")

        assert ctrl["death_frames_remaining"] == 0
        # Budget exhausted: next poll reverts to noise and clears the latch.
        state = adapter.poll("mock_controller_0")
        assert abs(state[StateKey.ACCEL][AxisKey.X]) < 2.0
        assert ctrl["death_accel"] is None

    def test_consume_death_frame_noop_without_latch(self):
        """consume_death_frame is a safe no-op when no death is pending."""
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        # No death set — must not raise or go negative.
        adapter.consume_death_frame("mock_controller_0")
        adapter.consume_death_frame("NONEXISTENT")
        assert adapter.controllers["mock_controller_0"]["death_frames_remaining"] == 0

    def test_poll_idle_fallback_clears_unconsumed_death(self):
        """Idle wall-clock fallback clears a latch no gameplay stream is draining.

        This is the no-consumer case: a SimulateDeath on a controller not in a
        running game has no send loop draining its budget, so the generous
        wall-clock fallback is what eventually clears it (so it never latches
        forever). In a real game the budget drains first; see the production-
        state test below.
        """
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        ctrl = adapter.controllers["mock_controller_0"]
        ctrl["death_accel"] = {AxisKey.X: 5.0, AxisKey.Y: 3.0, AxisKey.Z: 4.0}
        ctrl["death_frames_remaining"] = 99  # plenty of budget left...
        ctrl["death_hold_until"] = time.time() - 0.1  # ...but fallback already passed

        state = adapter.poll("mock_controller_0")
        assert abs(state[StateKey.ACCEL][AxisKey.X]) < 2.0  # reverted to noise
        assert ctrl["death_accel"] is None


class TestMockAdapterSetOutput:
    def test_set_output_stores_led_and_rumble(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        result = adapter.set_output("mock_controller_0", 255, 128, 0, 64)
        assert result is True
        ctrl = adapter.controllers["mock_controller_0"]
        assert ctrl["led"] == {"r": 255, "g": 128, "b": 0}
        assert ctrl["rumble"] == 64

    def test_set_output_unknown_returns_false(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        assert adapter.set_output("NONEXISTENT", 0, 0, 0, 0) is False


class TestMockAdapterClose:
    def test_close_removes_controller(self):
        adapter = MockAdapter(num_controllers=2)
        adapter.discover()
        adapter.close("mock_controller_0")
        assert "mock_controller_0" not in adapter.controllers
        assert "mock_controller_1" in adapter.controllers

    def test_close_all_clears_everything(self):
        adapter = MockAdapter(num_controllers=3)
        adapter.discover()
        adapter.close_all()
        assert len(adapter.controllers) == 0


class TestMockAdapterExtraMethods:
    def test_add_controller(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        serial = adapter.add_controller("TEST01")
        assert serial == "TEST01"
        assert "TEST01" in adapter.controllers

    def test_add_controller_auto_serial(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        serial = adapter.add_controller()
        assert serial == "MOCK0000"
        assert serial in adapter.controllers

    def test_add_controller_already_exists(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        serial = adapter.add_controller("mock_controller_0")
        assert serial == "mock_controller_0"

    def test_auto_serial_is_monotonic_and_never_reused_after_removal(self):
        """Regression for #1013: auto-generated serials must never be reused.

        The old len()-based scheme generated MOCK{len(controllers):04d}, so a
        removal shrank len() and the NEXT add re-minted a just-removed serial.
        When the previous shadow game's controllers were still being removed, a
        new AddControllers could hand back a serial already held by a not-yet-
        removed controller, collapsing two distinct players to one serial and
        aborting FFA with "Need at least 2 players, got 1". A monotonic counter
        guarantees every auto serial is unique for the adapter's lifetime.
        """
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()

        # Game 1 reserves two controllers.
        a = adapter.add_controller()
        b = adapter.add_controller()
        assert [a, b] == ["MOCK0000", "MOCK0001"]

        # Game 1 ends: remove ONE controller (simulating an in-flight cleanup
        # where the other removal has not landed yet).
        assert adapter.remove_controller(a) is True

        # Game 2 reserves two more. Under the old len()-based scheme call 1 would
        # have re-minted MOCK0001 (len()==1) — colliding with the surviving b and
        # returning a DUPLICATE. The monotonic counter keeps every serial unique.
        c = adapter.add_controller()
        d = adapter.add_controller()
        assert c not in (a, b), f"reused a prior serial: {c}"
        assert d not in (a, b, c), f"reused a prior serial: {d}"
        assert c != d, "AddControllers handed back two identical serials"
        # All currently-tracked serials are distinct.
        assert len(set(adapter.controllers)) == len(adapter.controllers)

    def test_auto_serial_skips_collisions_with_explicit_serials(self):
        """A monotonic auto serial that collides with an earlier explicit add
        is skipped, preserving the uniqueness guarantee (#1013)."""
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        # Pre-seed the serial the counter would generate first.
        adapter.add_controller("MOCK0000")
        auto = adapter.add_controller()
        assert auto != "MOCK0000"
        assert auto in adapter.controllers

    def test_remove_controller(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        assert adapter.remove_controller("mock_controller_0") is True
        assert "mock_controller_0" not in adapter.controllers

    def test_remove_nonexistent_controller(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        assert adapter.remove_controller("NONE") is False

    def test_get_led_color(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        adapter.set_output("mock_controller_0", 100, 200, 50, 0)
        color = adapter.get_led_color("mock_controller_0")
        assert color == (100, 200, 50)

    def test_get_led_color_nonexistent(self):
        adapter = MockAdapter(num_controllers=0)
        adapter.discover()
        assert adapter.get_led_color("NONE") is None


class TestMockAdapterObserver:
    def test_add_and_remove_observer(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        queue = adapter.add_observer()
        assert len(adapter._observers) == 1
        adapter.remove_observer(queue)
        assert len(adapter._observers) == 0

    def test_observer_receives_led_change(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        queue = adapter.add_observer()
        adapter.set_output("mock_controller_0", 255, 0, 0, 0)
        event = queue.get_nowait()
        assert event["type"] == "led_change"
        assert event["r"] == 255
        assert event["serial"] == "mock_controller_0"

    def test_observer_receives_rumble_change(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        # Set initial LED to non-default so first set_output changes both LED and rumble
        adapter.set_output("mock_controller_0", 0, 0, 0, 0)
        queue = adapter.add_observer()
        # Now set LED and rumble (both change from previous values)
        adapter.set_output("mock_controller_0", 255, 255, 255, 128)
        # Drain LED event
        queue.get_nowait()
        # Get rumble event
        event = queue.get_nowait()
        assert event["type"] == "rumble_change"
        assert event["intensity"] == 128

    def test_observer_no_event_when_unchanged(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        # Set initial output (generates events)
        adapter.set_output("mock_controller_0", 100, 100, 100, 0)
        queue = adapter.add_observer()
        # Set same output again — should not generate events
        adapter.set_output("mock_controller_0", 100, 100, 100, 0)
        assert queue.empty()


class TestMockAdapterObserverThreadSafety:
    """#1478: set_output runs in the 20 Hz LED worker thread; asyncio.Queue is not thread-safe."""

    @pytest.mark.uc("UC-003")
    @pytest.mark.asyncio
    async def test_publish_from_worker_thread_goes_through_call_soon_threadsafe(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        queue = adapter.add_observer()

        scheduled_from: list[int] = []
        put_from: list[int] = []
        original_schedule = loop.call_soon_threadsafe
        original_put = queue.put_nowait

        def spy_schedule(callback, *args):
            scheduled_from.append(threading.get_ident())
            return original_schedule(callback, *args)

        def spy_put(item):
            put_from.append(threading.get_ident())
            original_put(item)

        loop.call_soon_threadsafe = spy_schedule
        queue.put_nowait = spy_put
        try:
            worker = threading.Thread(target=adapter.set_output, args=("mock_controller_0", 255, 0, 0, 0))
            worker.start()
            worker.join()
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            del loop.call_soon_threadsafe

        assert event["type"] == "led_change"
        assert scheduled_from, "publish from a worker thread must use loop.call_soon_threadsafe"
        assert all(ident != loop_thread for ident in scheduled_from)
        assert put_from == [loop_thread], "the queue may only be touched on its own loop"

    @pytest.mark.uc("UC-003")
    @pytest.mark.asyncio
    async def test_publish_on_owning_loop_puts_directly(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        queue = adapter.add_observer()

        adapter.set_output("mock_controller_0", 0, 255, 0, 0)
        await asyncio.sleep(0)  # the put is scheduled on this loop, not done inline

        assert queue.get_nowait()["g"] == 255

    @pytest.mark.uc("UC-003")
    @pytest.mark.asyncio
    async def test_worker_then_loop_publish_keeps_order(self):
        """Review of #1629: a loop publish must not overtake an earlier worker publish."""
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        queue = adapter.add_observer()

        worker = threading.Thread(target=adapter.set_output, args=("mock_controller_0", 255, 0, 0, 0))
        worker.start()
        worker.join()  # red is now scheduled via call_soon_threadsafe
        adapter.set_output("mock_controller_0", 0, 255, 0, 0)  # green, published on the loop

        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        second = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert (first["r"], first["g"]) == (255, 0)
        assert (second["r"], second["g"]) == (0, 255)

    @pytest.mark.uc("UC-003")
    def test_publish_to_closed_loop_is_dropped(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()

        async def subscribe():
            return adapter.add_observer()

        loop = asyncio.new_event_loop()
        queue = loop.run_until_complete(subscribe())
        loop.close()

        adapter.set_output("mock_controller_0", 0, 0, 255, 0)  # must not raise

        assert queue.empty()


class TestMockAdapterHeldAccel:
    """#1279: hold_accel / clear_held_accel at the adapter level."""

    def test_hold_accel_persists_across_polls(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        assert adapter.hold_accel("mock_controller_0", 1.0, 1.0, 1.0)

        for _ in range(10):
            state = adapter.poll("mock_controller_0")
            assert state[StateKey.ACCEL] == {AxisKey.X: 1.0, AxisKey.Y: 1.0, AxisKey.Z: 1.0}

    def test_poll_returns_a_copy_of_the_held_vector(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        adapter.hold_accel("mock_controller_0", 0.5, 0.5, 1.0)

        state = adapter.poll("mock_controller_0")
        state[StateKey.ACCEL][AxisKey.X] = 99.0

        assert adapter.poll("mock_controller_0")[StateKey.ACCEL][AxisKey.X] == 0.5

    def test_clear_held_accel_returns_to_noise(self):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        adapter.hold_accel("mock_controller_0", 2.0, 0.0, 0.0)
        assert adapter.clear_held_accel("mock_controller_0")

        z_values = [adapter.poll("mock_controller_0")[StateKey.ACCEL][AxisKey.Z] for _ in range(10)]
        assert not all(z == z_values[0] for z in z_values)

    def test_hold_and_clear_unknown_controller_return_false(self):
        adapter = MockAdapter(num_controllers=0)
        assert not adapter.hold_accel("NOPE", 1.0, 0.0, 0.0)
        assert not adapter.clear_held_accel("NOPE")

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_hold_accel_rejects_non_finite(self, bad):
        adapter = MockAdapter(num_controllers=1)
        adapter.discover()
        with pytest.raises(ValueError, match="finite"):
            adapter.hold_accel("mock_controller_0", 0.0, bad, 1.0)
        assert adapter.controllers["mock_controller_0"]["held_accel"] is None
