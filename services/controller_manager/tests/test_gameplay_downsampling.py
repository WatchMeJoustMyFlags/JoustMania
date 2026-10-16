"""UC-030 BR-002: the gameplay stream sends the latest sample per frame, not the frame's maximum (#1362).

The ~1 kHz hardware poll overwrites ``controller_states[serial]`` on every poll
(``DiscoveryLoop``), and each gameplay frame is built from whatever is stored when
the frame is built. So several polls between two frames collapse to the last one:
a spike shorter than a frame can be missed. Drives the real servicer and the real
``StateCache``; only the backend and the discovery loop (hardware) are autospec'd.
"""

from unittest.mock import create_autospec, patch

import pytest

from lib.controller_constants import AxisKey, StateKey
from services.controller_manager.backend import ControllerBackend
from services.controller_manager.discovery_loop import DiscoveryLoop

SERIAL = "AA:BB:CC:DD:EE:01"


def _poll(x: float, y: float, z: float) -> dict:
    """One backend poll result, as DiscoveryLoop stores it."""
    return {StateKey.ACCEL: {AxisKey.X: x, AxisKey.Y: y, AxisKey.Z: z}, StateKey.BATTERY: 80}


@pytest.fixture
def servicer():
    backend = create_autospec(ControllerBackend, instance=True)
    loop = create_autospec(DiscoveryLoop, instance=True)
    loop.drain_health_counters.return_value = (0, 0, 0)
    loop.drain_disconnects.return_value = []
    with (
        patch("services.controller_manager.servicer.create_backend", return_value=backend),
        patch("services.controller_manager.servicer.DiscoveryLoop", return_value=loop),
    ):
        from services.controller_manager.servicer import ControllerManagerServicer

        svc = ControllerManagerServicer()
    svc.tracked_controllers[SERIAL] = {"serial": SERIAL, "battery": 80}
    return svc


@pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
def test_frame_carries_the_latest_poll_not_the_maximum(servicer):
    """UC-030 BR-002: a spike polled between two frames is overwritten by the next poll and never streamed."""
    for sample in (_poll(0.0, 0.0, 1.0), _poll(4.0, 4.0, 4.0), _poll(0.1, 0.0, 1.0)):  # still, spike, still
        servicer.controller_states[SERIAL] = sample  # what each DiscoveryLoop poll does

    update = servicer._build_gameplay_update(None)

    (data,) = update.controllers
    assert (data.accel.x, data.accel.y, data.accel.z) == pytest.approx((0.1, 0.0, 1.0))


@pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
def test_spike_still_stored_at_frame_time_is_streamed(servicer):
    """UC-030 BR-002: the same spike is streamed when it is the latest poll at frame time."""
    servicer.controller_states[SERIAL] = _poll(0.0, 0.0, 1.0)
    servicer._build_gameplay_update(None)

    servicer.controller_states[SERIAL] = _poll(4.0, 4.0, 4.0)
    update = servicer._build_gameplay_update(None)

    (data,) = update.controllers
    assert (data.accel.x, data.accel.y, data.accel.z) == pytest.approx((4.0, 4.0, 4.0))
