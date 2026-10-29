"""Pins the full per-serial cleanup of DiscoveryLoop on controller disconnect (#2354).

Observed through the public surface (drain / age / quality accessors, the health
window and the metric series removed), so it holds however the loop stores its
per-controller state.
"""

import asyncio
import time
from unittest.mock import create_autospec, patch

import pytest

from lib.otel_metrics import Gauge
from services.controller_manager.backend import ControllerBackend
from services.controller_manager.button_detector import ButtonDetector
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.event_publisher import EventPublisher
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.monitoring import ControllerMonitoring

SERIALS = ("serial_A", "serial_B")


def _frame(serial: str) -> dict:
    return {"serial": serial, "battery": 80, "accel": {"x": 0.0, "y": 0.0, "z": 1.0}}


def _make_loop():
    backend = create_autospec(ControllerBackend, instance=True)
    backend.get_connected_controllers.return_value = list(SERIALS)
    # Polls after the spawn read, per serial: frame, frame, exception (poll error),
    # None (poll drop); every later poll is a fresh frame.
    script = {s: [_frame(s), _frame(s), RuntimeError("usb"), None] for s in SERIALS}
    spawned = set()

    async def get_state(serial):
        if serial not in spawned or serial not in script:
            spawned.add(serial)
            return _frame(serial)
        outcome = script[serial].pop(0) if script[serial] else _frame(serial)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    backend.get_controller_state.side_effect = get_state
    rescan = create_autospec(PeriodicRescanTimer, instance=True)
    rescan.should_force_rescan.return_value = False
    return DiscoveryLoop(
        backend=backend,
        tracked_controllers={},
        controller_states={},
        button_detector=create_autospec(ButtonDetector, instance=True),
        feedback_manager=create_autospec(FeedbackManager, instance=True),
        monitoring=create_autospec(ControllerMonitoring, instance=True),
        rescan_timer=rescan,
        paired_serials=[],
        base_colors={"serial_A": (1, 2, 3)},
        event_publisher=create_autospec(EventPublisher, instance=True),
    ), backend


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A1")
async def test_disconnect_cleans_every_per_serial_record():
    """UC-002 A1: a disconnect drops exactly one controller's health, quality, activity and metric series."""
    loop, backend = _make_loop()
    loop.backend_initialized = True
    await loop._check_for_new_controllers()

    for _ in range(4):  # frame, frame, exception, drop
        await loop._update_controller_states()
        await asyncio.sleep(0.005)
    loop._emit_bluetooth_health(time.time(), time.monotonic())
    assert loop.sample_age_seconds("serial_A", time.time()) is not None
    assert loop.connection_quality("serial_A") is not None

    backend.get_connected_controllers.return_value = ["serial_B"]
    gauges = {
        name: create_autospec(Gauge, instance=True)
        for name in (
            "controller_bluetooth_dropped_events_ratio",
            "controller_bluetooth_movement_update_hz",
            "controller_bluetooth_report_gap_p95_seconds",
            "controller_bluetooth_report_gap_max_seconds",
            "controller_connection_quality",
        )
    }
    with (
        patch.multiple("services.controller_manager.metrics", **gauges),
        patch("services.controller_manager.metrics.remove_controller_info") as remove_info,
    ):
        await loop._check_for_new_controllers()

    # Metric series removed for the departed serial only.
    for gauge in gauges.values():
        gauge.remove.assert_called_once_with("serial_A")
    remove_info.assert_called_once()
    assert remove_info.call_args.args[0] == "serial_A"

    # Tracking, health, quality, activity and the gap window forgot serial_A ...
    assert "serial_A" not in loop.tracked_controllers
    assert "serial_A" not in loop.controller_states
    assert loop.sample_age_seconds("serial_A", time.time()) is None
    assert loop.connection_quality("serial_A") is None
    assert loop.drain_health_counters("serial_A") == (0, 0, 0)
    assert "serial_A" not in loop._gap_window.serials()
    assert loop.drain_disconnects() == ["serial_A"]
    # ... the reconnect colour is kept ...
    assert loop.base_colors == {"serial_A": (1, 2, 3)}
    # ... and serial_B is untouched.
    assert loop.sample_age_seconds("serial_B", time.time()) is not None
    assert loop.connection_quality("serial_B") is not None
    assert loop.drain_health_counters("serial_B") == (1, 1, 0)
    assert "serial_B" in loop._gap_window.serials()
    assert set(loop._compute_health_window(1.0)["per_serial"]) <= {"serial_B"}
    assert loop._compute_health_window(1.0)["active_controllers"] <= 1


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A4")
async def test_reconnect_starts_with_a_clean_record():
    """UC-002 A4: a reconnected controller measures its first gap from its own first frame, not the old session."""
    loop, backend = _make_loop()
    loop.backend_initialized = True
    await loop._check_for_new_controllers()
    await loop._update_controller_states()  # fresh frame for both

    backend.get_connected_controllers.return_value = ["serial_B"]
    await loop._check_for_new_controllers()
    await asyncio.sleep(0.03)

    backend.get_connected_controllers.return_value = list(SERIALS)
    served_before = loop.sample_age_seconds("serial_A", time.time())
    assert served_before is None
    await loop._check_for_new_controllers()
    # The state the loop spawned with is the first frame; no old timestamp may leak in.
    assert loop.sample_age_seconds("serial_A", time.time()) is None
    loop._reset_health_window(time.time())
    await loop._update_controller_states()
    gap = loop._compute_health_window(1.0)["per_serial"]["serial_A"]["max_gap_ms"]
    assert gap == 0.0
