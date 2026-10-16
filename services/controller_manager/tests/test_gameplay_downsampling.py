"""UC-030 BR-002: the gameplay stream sends the latest sample per frame, not the frame's maximum (#1362).

The hardware poll (``DiscoveryLoop._update_controller_states``) stores each
poll's state, and each gameplay frame is built from what is stored when the
frame is built. So several polls between two frames collapse to the last one:
a spike shorter than a frame can be missed. Drives the real servicer, the real
``DiscoveryLoop`` poll and the real ``StateCache``; only the hardware backend is
autospec'd (its ``get_controller_state`` returns the scripted polls).
"""

from unittest.mock import create_autospec, patch

import pytest

from lib.controller_constants import AxisKey, StateKey
from services.controller_manager.backend import ControllerBackend

SERIAL = "AA:BB:CC:DD:EE:01"


def _poll(x: float, y: float, z: float) -> dict:
    """One backend poll result (a fresh dict per poll, as the adapters return)."""
    return {StateKey.ACCEL: {AxisKey.X: x, AxisKey.Y: y, AxisKey.Z: z}, StateKey.BATTERY: 80}


@pytest.fixture
def servicer():
    backend = create_autospec(ControllerBackend, instance=True)
    with patch("services.controller_manager.servicer.create_backend", return_value=backend):
        from services.controller_manager.servicer import ControllerManagerServicer

        svc = ControllerManagerServicer()
    svc.tracked_controllers[SERIAL] = {"serial": SERIAL, "battery": 80}
    svc.discovery_loop.backend_initialized = True
    return svc


async def _hardware_polls(servicer, *polls: dict) -> None:
    """Run the real poll step once per scripted hardware sample."""
    servicer.backend.get_controller_state.side_effect = list(polls)
    for _ in polls:
        await servicer.discovery_loop._update_controller_states()


def _accel(update) -> tuple[float, float, float]:
    (data,) = update.controllers
    return (data.accel.x, data.accel.y, data.accel.z)


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
async def test_frame_carries_the_latest_poll_not_the_maximum(servicer):
    """UC-030 BR-002: a spike polled between two frames is replaced by the next poll and never streamed."""
    await _hardware_polls(servicer, _poll(0.0, 0.0, 1.0), _poll(4.0, 4.0, 4.0), _poll(0.1, 0.0, 1.0))

    update = servicer._build_gameplay_update(None)

    assert _accel(update) == pytest.approx((0.1, 0.0, 1.0))


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[2], br=["BR-002"])
async def test_spike_that_is_the_latest_poll_is_streamed(servicer):
    """UC-030 BR-002: the same spike is streamed when it is the latest poll at frame time."""
    await _hardware_polls(servicer, _poll(0.0, 0.0, 1.0))
    servicer._build_gameplay_update(None)

    await _hardware_polls(servicer, _poll(0.1, 0.0, 1.0), _poll(4.0, 4.0, 4.0))
    update = servicer._build_gameplay_update(None)

    assert _accel(update) == pytest.approx((4.0, 4.0, 4.0))
