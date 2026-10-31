"""Startup Discover / Open never block the event loop (#2351, UC-002 A7).

``MultiplexerBackend.initialize()`` used to call each adapter's blocking
``discover()`` / ``open()`` on the event loop: against a wedged rust-hid each
call waits its 5 s deadline, so the loop froze and the server's 10 s startup
wait (``DiscoveryLoop.wait_initialized``) could not fire. These tests hang a
startup Discover and check that the loop stays free and the timeouts fire.
"""

import asyncio
import threading
import time
from unittest.mock import create_autospec

import pytest

from services.controller_manager.multiplexer import multiplexer_backend
from services.controller_manager.multiplexer.mock_adapter import MockAdapter
from services.controller_manager.multiplexer.multiplexer_backend import MultiplexerBackend

HANG_S = 2.0  # how long the hanging Discover blocks; the timer thread releases it so a mutation fails, not hangs
FAST_S = 1.0  # well under HANG_S: anything that waited for the hang takes longer
HUNG_SERIAL = "AA:BB:CC:DD:EE:09"


class HangingAdapter(MockAdapter):
    """A rust-hid that does not answer: ``discover()`` blocks until ``release`` (at most HANG_S)."""

    def __init__(self):
        super().__init__(num_controllers=0)
        self.release = threading.Event()
        self.done = threading.Event()  # the hung startup pass has opened its controller
        threading.Timer(HANG_S, self.release.set).start()

    @property
    def adapter_type(self) -> str:
        return "rust"

    def discover(self, force=False, exclude_serials=None):  # noqa: ARG002
        if not self.done.is_set():
            self.release.wait(timeout=HANG_S * 2)
        return [HUNG_SERIAL]

    def open(self, serial: str) -> bool:
        result = super().open(serial)
        self.done.set()
        return result


def _make_loop(backend):
    from services.controller_manager.button_detector import ButtonDetector
    from services.controller_manager.discovery import PeriodicRescanTimer
    from services.controller_manager.discovery_loop import DiscoveryLoop
    from services.controller_manager.event_publisher import EventPublisher
    from services.controller_manager.feedback_manager import FeedbackManager
    from services.controller_manager.monitoring import ControllerMonitoring

    monitoring = create_autospec(ControllerMonitoring, instance=True)
    monitoring.last_battery_check = 0.0
    rescan = create_autospec(PeriodicRescanTimer, instance=True)
    rescan.should_force_rescan.return_value = False
    return DiscoveryLoop(
        backend=backend,
        tracked_controllers={},
        controller_states={},
        button_detector=create_autospec(ButtonDetector, instance=True),
        feedback_manager=create_autospec(FeedbackManager, instance=True),
        monitoring=monitoring,
        rescan_timer=rescan,
        paired_serials=[],
        base_colors={},
        event_publisher=create_autospec(EventPublisher, instance=True),
    )


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A7")
async def test_a_hanging_startup_discover_lets_the_startup_timeout_fire():
    """The startup wait gives up on time while Discover hangs: the event loop was never blocked."""
    hanging = HangingAdapter()
    loop = _make_loop(MultiplexerBackend(adapters=[hanging]))
    try:
        loop.start()
        started = time.monotonic()
        initialized = await loop.wait_initialized(timeout_seconds=0.3)
        elapsed = time.monotonic() - started

        assert elapsed < FAST_S, f"the startup wait took {elapsed:.2f}s: Discover blocked the event loop"
        assert initialized is False
    finally:
        hanging.release.set()
        loop.stop()
        await loop.wait_stopped(timeout_seconds=2.0)


@pytest.mark.asyncio
@pytest.mark.uc("UC-002", flow="A7")
async def test_an_adapter_whose_startup_discover_hangs_starts_with_no_controllers(monkeypatch):
    """Past INIT_ADAPTER_TIMEOUT_S the backend starts without the hung adapter's controllers; others are routed."""
    monkeypatch.setattr(multiplexer_backend, "INIT_ADAPTER_TIMEOUT_S", 0.2, raising=False)
    hanging = HangingAdapter()
    healthy = MockAdapter(num_controllers=1)
    backend = MultiplexerBackend(adapters=[hanging, healthy])
    try:
        started = time.monotonic()
        initialized = await backend.initialize()
        elapsed = time.monotonic() - started

        assert elapsed < FAST_S, f"initialize() waited {elapsed:.2f}s for the hung Discover"
        assert initialized is True  # like a failed Discover: discovery retries later
        assert set(backend._serial_to_adapter.values()) == {healthy}

        # The hung pass finishes later in its worker thread: it installs no route.
        hanging.release.set()
        assert await asyncio.to_thread(hanging.done.wait, HANG_S)
        await asyncio.sleep(0.05)
        assert HUNG_SERIAL not in backend._serial_to_adapter
    finally:
        hanging.release.set()
        await backend.shutdown()
