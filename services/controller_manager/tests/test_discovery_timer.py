"""
Tests for PeriodicRescanTimer, including force_next_rescan().
"""

import sys
import time
from pathlib import Path

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from services.controller_manager.discovery import PeriodicRescanTimer


class TestPeriodicRescanTimer:
    def test_first_call_returns_true(self):
        timer = PeriodicRescanTimer(interval=5.0)
        assert timer.should_force_rescan() is True

    def test_immediate_second_call_returns_false(self):
        timer = PeriodicRescanTimer(interval=5.0)
        timer.should_force_rescan()  # first call
        assert timer.should_force_rescan() is False

    def test_returns_true_after_interval(self):
        timer = PeriodicRescanTimer(interval=1.0)
        timer.should_force_rescan()
        # Simulate time passing
        timer._last_rescan_time = time.time() - 2.0
        assert timer.should_force_rescan() is True

    def test_reset_prevents_immediate_rescan(self):
        timer = PeriodicRescanTimer(interval=5.0)
        timer.should_force_rescan()  # consume initial
        timer._last_rescan_time = 0.0  # would normally trigger
        timer.reset()
        assert timer.should_force_rescan() is False


class TestForceNextRescan:
    def test_force_next_rescan_triggers_on_next_call(self):
        """After force_next_rescan(), should_force_rescan() returns True."""
        timer = PeriodicRescanTimer(interval=5.0)
        timer.should_force_rescan()  # consume initial
        assert timer.should_force_rescan() is False  # verify normal behavior

        timer.force_next_rescan()
        assert timer.should_force_rescan() is True

    def test_force_next_rescan_resets_after_trigger(self):
        """Second call after force returns False (normal interval resumes)."""
        timer = PeriodicRescanTimer(interval=5.0)
        timer.should_force_rescan()  # consume initial

        timer.force_next_rescan()
        timer.should_force_rescan()  # consume the forced one
        assert timer.should_force_rescan() is False

    def test_force_next_rescan_sets_last_time_to_zero(self):
        """Implementation detail: _last_rescan_time is set to 0.0."""
        timer = PeriodicRescanTimer(interval=5.0)
        timer.should_force_rescan()
        assert timer._last_rescan_time > 0

        timer.force_next_rescan()
        assert timer._last_rescan_time == 0.0

    def test_multiple_force_calls_idempotent(self):
        """Calling force_next_rescan multiple times has the same effect as once."""
        timer = PeriodicRescanTimer(interval=5.0)
        timer.should_force_rescan()  # consume initial

        timer.force_next_rescan()
        timer.force_next_rescan()
        timer.force_next_rescan()
        assert timer.should_force_rescan() is True
        assert timer.should_force_rescan() is False  # only one trigger
