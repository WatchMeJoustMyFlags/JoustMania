"""
Unit tests for MobileAdapter — thin I/O adapter for phone-based controllers.
"""

import sys
import threading
import time
from pathlib import Path

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.controller_constants import AxisKey, ButtonKey, StateKey
from services.controller_manager.multiplexer.mobile_adapter import MobileAdapter, PhoneConnection


class TestPhoneConnection:
    def test_initial_state(self):
        phone = PhoneConnection("PHONE0000", name="Test Phone")
        assert phone.serial == "PHONE0000"
        assert phone.name == "Test Phone"
        state = phone.read_state()
        assert state[StateKey.SERIAL] == "PHONE0000"
        assert state[StateKey.ACCEL][AxisKey.Z] == 1.0
        assert state["connection_type"] == "mobile"

    def test_update_motion(self):
        phone = PhoneConnection("PHONE0000")
        phone.update_motion(1.5, -0.3, 0.8, 0.1, 0.2, -0.1, 0, 85, 42)
        state = phone.read_state()
        assert state[StateKey.ACCEL][AxisKey.X] == 1.5
        assert state[StateKey.ACCEL][AxisKey.Y] == -0.3
        assert state[StateKey.ACCEL][AxisKey.Z] == 0.8
        assert state[StateKey.GYRO][AxisKey.X] == 0.1
        assert state[StateKey.BATTERY] == 85

    def test_button_bitmask(self):
        phone = PhoneConnection("PHONE0000")
        # MOVE button = bit 0
        phone.update_motion(0, 0, 1, 0, 0, 0, 0x01, 100, 1)
        state = phone.read_state()
        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.PS] is False

        # PS button = bit 1
        phone.update_motion(0, 0, 1, 0, 0, 0, 0x02, 100, 2)
        state = phone.read_state()
        assert state[ButtonKey.MOVE] is False
        assert state[ButtonKey.PS] is True

        # Both buttons
        phone.update_motion(0, 0, 1, 0, 0, 0, 0x03, 100, 3)
        state = phone.read_state()
        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.PS] is True

    def test_set_output_state(self):
        phone = PhoneConnection("PHONE0000")
        phone.set_output_state(255, 128, 0, 64)
        result = phone.get_pending_output()
        assert result is not None
        led, rumble = result
        assert led == {"r": 255, "g": 128, "b": 0}
        assert rumble == 64

    def test_get_pending_output_returns_none_when_unchanged(self):
        phone = PhoneConnection("PHONE0000")
        assert phone.get_pending_output() is None

    def test_get_pending_output_clears_flag(self):
        phone = PhoneConnection("PHONE0000")
        phone.set_output_state(255, 0, 0, 0)
        phone.get_pending_output()
        assert phone.get_pending_output() is None

    def test_read_state_returns_copy(self):
        phone = PhoneConnection("PHONE0000")
        state1 = phone.read_state()
        state2 = phone.read_state()
        assert state1[StateKey.ACCEL] is not state2[StateKey.ACCEL]

    def test_thread_safety(self):
        phone = PhoneConnection("PHONE0000")
        errors = []

        def writer():
            try:
                for i in range(100):
                    phone.update_motion(float(i), 0, 1, 0, 0, 0, 0, 100, i)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    state = phone.read_state()
                    assert StateKey.ACCEL in state
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


class TestMobileAdapterInit:
    def test_adapter_type(self):
        adapter = MobileAdapter()
        assert adapter.adapter_type == "mobile"

    def test_starts_empty(self):
        adapter = MobileAdapter()
        assert adapter.phone_count == 0
        assert adapter.discover() == []


class TestMobileAdapterDiscover:
    def test_discover_returns_registered_phones(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone("Alice")
        serials = adapter.discover()
        assert phone.serial in serials

    def test_discover_after_unregister(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone("Alice")
        adapter.unregister_phone(phone.serial)
        assert adapter.discover() == []


class TestMobileAdapterOpen:
    def test_open_registered_phone(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone()
        assert adapter.open(phone.serial) is True

    def test_open_unknown_returns_false(self):
        adapter = MobileAdapter()
        assert adapter.open("NONEXISTENT") is False


class TestMobileAdapterPoll:
    def test_poll_returns_state_dict(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone("Bob")
        state = adapter.poll(phone.serial)
        assert state is not None
        assert state[StateKey.SERIAL] == phone.serial
        assert StateKey.BATTERY in state
        assert StateKey.ACCEL in state
        assert StateKey.GYRO in state
        assert ButtonKey.MOVE in state
        assert state["connection_type"] == "mobile"

    def test_poll_unknown_returns_none(self):
        adapter = MobileAdapter()
        assert adapter.poll("NONEXISTENT") is None

    def test_poll_reflects_motion_updates(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone()
        phone.update_motion(2.0, 0, 0, 0, 0, 0, 0, 100, 1)
        state = adapter.poll(phone.serial)
        assert state[StateKey.ACCEL][AxisKey.X] == 2.0


class TestMobileAdapterSetOutput:
    def test_set_output_stores_led_and_rumble(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone()
        result = adapter.set_output(phone.serial, 255, 128, 0, 64)
        assert result is True
        output = phone.get_pending_output()
        assert output is not None
        led, rumble = output
        assert led == {"r": 255, "g": 128, "b": 0}
        assert rumble == 64

    def test_set_output_unknown_returns_false(self):
        adapter = MobileAdapter()
        assert adapter.set_output("NONEXISTENT", 0, 0, 0, 0) is False


class TestMobileAdapterClose:
    def test_close_removes_phone(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone()
        adapter.close(phone.serial)
        assert adapter.phone_count == 0

    def test_close_all_clears_everything(self):
        adapter = MobileAdapter()
        adapter.register_phone("A")
        adapter.register_phone("B")
        adapter.close_all()
        assert adapter.phone_count == 0


class TestMobileAdapterRegisterUnregister:
    def test_register_phone_increments_serial(self):
        adapter = MobileAdapter()
        p1 = adapter.register_phone("A")
        p2 = adapter.register_phone("B")
        assert p1.serial == "PHONE0000"
        assert p2.serial == "PHONE0001"
        assert adapter.phone_count == 2

    def test_unregister_phone(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone()
        assert adapter.unregister_phone(phone.serial) is True
        assert adapter.phone_count == 0

    def test_unregister_nonexistent(self):
        adapter = MobileAdapter()
        assert adapter.unregister_phone("NOPE") is False

    def test_get_phone(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone("Test")
        assert adapter.get_phone(phone.serial) is phone
        assert adapter.get_phone("NOPE") is None

    def test_stale_phone_detection(self):
        adapter = MobileAdapter()
        phone = adapter.register_phone()
        # Simulate stale connection (last_update in the past)
        phone._last_update = time.time() - 60.0
        assert phone.last_update < time.time() - 30.0
