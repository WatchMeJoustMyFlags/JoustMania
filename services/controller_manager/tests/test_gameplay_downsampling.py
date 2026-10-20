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


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[2])
@pytest.mark.nfr("NFR-002")
async def test_poll_loop_runs_every_10_ms_and_keeps_only_the_latest_state(servicer):
    """UC-030 step 2: the real poll loop (``DiscoveryLoop._poll_loop``) polls on a fixed 100 Hz
    schedule and stores each poll's state, so the next gameplay frame carries the latest one.

    The clock is simulated: the loop's ``time.monotonic`` and ``asyncio.sleep`` are replaced so
    each poll costs no time and every sleep advances the clock by the requested amount.
    """
    import time
    from types import SimpleNamespace

    from services.controller_manager import discovery_loop as discovery_loop_module

    loop = servicer.discovery_loop
    polls = [_poll(0.0, 0.0, 1.0), _poll(4.0, 4.0, 4.0), _poll(0.2, 0.0, 1.0)]
    servicer.backend.get_controller_state.side_effect = polls
    clock = {"now": 100.0}
    poll_times: list[float] = []
    sleeps: list[float] = []
    real_update = loop._update_controller_states

    async def timed_update():
        poll_times.append(clock["now"])
        await real_update()
        if len(poll_times) == len(polls):
            loop.running = False

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    loop.running = True
    with (
        patch.object(loop, "_update_controller_states", side_effect=timed_update),
        patch.object(discovery_loop_module, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time)),
        patch.object(discovery_loop_module.asyncio, "sleep", side_effect=fake_sleep),
        patch.object(discovery_loop_module, "metrics"),
    ):
        await loop._poll_loop()

    assert loop._poll_interval == pytest.approx(0.010)
    assert [round(b - a, 6) for a, b in zip(poll_times, poll_times[1:], strict=False)] == [0.01, 0.01]
    assert sleeps == pytest.approx([0.010] * len(polls))
    # Only the latest poll is kept: the 4 g spike in the middle never reaches the frame.
    assert _accel(servicer._build_gameplay_update(None)) == pytest.approx((0.2, 0.0, 1.0))
