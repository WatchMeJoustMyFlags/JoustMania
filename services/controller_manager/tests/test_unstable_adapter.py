"""
Unit tests for UnstableAdapter — a deterministically-degraded decorator.

Verifies the M3 fitness thresholds are reliably violated:
  * fresh-frame update rate < 10 Hz
  * dropped-event ratio > 2%
  * gap between fresh frames > 50 ms
plus lifecycle delegation and the routable adapter_type.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from services.controller_manager.multiplexer.unstable_adapter import UnstableAdapter


class FakeClock:
    """Injectable monotonic clock for deterministic throttle tests."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _inner(poll_returns=None):
    inner = MagicMock()
    inner.adapter_type = "python"
    inner.inner = None
    if poll_returns is None:
        inner.poll = MagicMock(side_effect=lambda s: {"serial": s, "seq": inner.poll.call_count})
    else:
        inner.poll = MagicMock(return_value=poll_returns)
    inner.discover = MagicMock(return_value=["AA:AA"])
    inner.open = MagicMock(return_value=True)
    inner.set_output = MagicMock(return_value=True)
    inner.close = MagicMock()
    inner.close_all = MagicMock()
    inner.get_adapter_for_serial = MagicMock(return_value="hci0")
    return inner


class TestAdapterType:
    def test_adapter_type_is_unstable_not_transparent(self):
        adapter = UnstableAdapter(_inner())
        # Unlike ChaosAdapter, this is NOT transparent — routing must see it.
        assert adapter.adapter_type == "unstable"

    def test_exposes_inner(self):
        inner = _inner()
        adapter = UnstableAdapter(inner)
        assert adapter.inner is inner

    def test_rejects_drop_every_below_two(self):
        with pytest.raises(ValueError, match="drop_every"):
            UnstableAdapter(_inner(), drop_every=1)


class TestDegradation:
    def test_fresh_update_rate_below_10hz(self):
        """Driving poll() at 100 Hz wall-clock yields < 10 fresh frames/sec."""
        clock = FakeClock()
        inner = _inner()
        adapter = UnstableAdapter(inner, throttle_seconds=0.12, drop_every=10, time_source=clock)

        fresh = 0
        # Simulate 1 second at 100 Hz (10 ms steps).
        for _ in range(100):
            inner.poll.reset_mock()
            adapter.poll("AA:AA")
            if inner.poll.called:  # a fresh inner poll happened this tick
                fresh += 1
            clock.advance(0.010)

        # 1 s / 0.12 s window -> at most ~9 fresh frames, strictly < 10 Hz.
        assert fresh < 10

    def test_gap_between_fresh_frames_exceeds_50ms(self):
        """Time between consecutive fresh inner polls is always > 50 ms."""
        clock = FakeClock()
        inner = _inner()
        adapter = UnstableAdapter(inner, throttle_seconds=0.12, drop_every=10, time_source=clock)

        fresh_times = []
        for _ in range(200):
            inner.poll.reset_mock()
            adapter.poll("AA:AA")
            if inner.poll.called:
                fresh_times.append(clock.t)
            clock.advance(0.005)  # 5 ms steps

        gaps = [b - a for a, b in zip(fresh_times, fresh_times[1:], strict=False)]
        assert gaps  # we observed multiple fresh frames
        assert all(g > 0.050 for g in gaps), gaps

    def test_drop_ratio_above_2_percent(self):
        """Across fresh polls, the dropped (None) ratio exceeds 2%."""
        clock = FakeClock()
        inner = _inner()
        adapter = UnstableAdapter(inner, throttle_seconds=0.12, drop_every=10, time_source=clock)

        fresh = 0
        drops = 0
        for _ in range(500):
            inner.poll.reset_mock()
            result = adapter.poll("AA:AA")
            if inner.poll.called:
                fresh += 1
                if result is None:
                    drops += 1
            clock.advance(0.130)  # always past the throttle window -> every poll is fresh

        assert fresh > 0
        ratio = drops / fresh
        assert ratio > 0.02, ratio
        # drop_every=10 -> exactly 10%
        assert abs(ratio - 0.10) < 0.001, ratio

    def test_stale_replay_within_throttle_window(self):
        """Within a throttle window, the last served frame is replayed."""
        clock = FakeClock()
        inner = _inner()
        adapter = UnstableAdapter(inner, throttle_seconds=0.12, time_source=clock)

        first = adapter.poll("AA:AA")
        inner.poll.reset_mock()
        # Advance less than the throttle window.
        clock.advance(0.05)
        replay = adapter.poll("AA:AA")

        inner.poll.assert_not_called()  # no fresh inner poll
        assert replay == first

    def test_poll_never_blocks(self):
        """poll() must not call sleep — degradation is throttle/drop only."""
        import time as time_module

        clock = FakeClock()
        adapter = UnstableAdapter(_inner(), time_source=clock)
        start = time_module.perf_counter()
        for _ in range(50):
            adapter.poll("AA:AA")
            clock.advance(0.2)
        elapsed = time_module.perf_counter() - start
        assert elapsed < 0.5  # generous; real impl is microseconds


class TestDelegation:
    def test_discover_delegates(self):
        inner = _inner()
        adapter = UnstableAdapter(inner)
        assert adapter.discover(force=True, exclude_serials=["X"]) == ["AA:AA"]
        inner.discover.assert_called_once_with(force=True, verify_only=False, exclude_serials=["X"])

    def test_open_delegates(self):
        inner = _inner()
        adapter = UnstableAdapter(inner)
        assert adapter.open("AA:AA") is True
        inner.open.assert_called_once_with("AA:AA")

    def test_set_output_delegates(self):
        inner = _inner()
        adapter = UnstableAdapter(inner)
        assert adapter.set_output("AA:AA", 1, 2, 3, 4) is True
        inner.set_output.assert_called_once_with("AA:AA", 1, 2, 3, 4)

    def test_get_adapter_for_serial_delegates(self):
        inner = _inner()
        adapter = UnstableAdapter(inner)
        assert adapter.get_adapter_for_serial("AA:AA") == "hci0"

    def test_close_delegates_and_clears_state(self):
        clock = FakeClock()
        inner = _inner()
        adapter = UnstableAdapter(inner, time_source=clock)
        adapter.poll("AA:AA")  # seed throttle state
        adapter.close("AA:AA")
        inner.close.assert_called_once_with("AA:AA")
        assert "AA:AA" not in adapter._last_fresh_time

    def test_close_all_delegates_and_clears_state(self):
        clock = FakeClock()
        inner = _inner()
        adapter = UnstableAdapter(inner, time_source=clock)
        adapter.poll("AA:AA")
        adapter.close_all()
        inner.close_all.assert_called_once()
        assert adapter._last_fresh_time == {}

    def test_unknown_attr_delegates_to_inner(self):
        inner = _inner()
        inner.some_extra = "value"
        adapter = UnstableAdapter(inner)
        assert adapter.some_extra == "value"
