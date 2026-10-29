"""Poll cadence is isolated from discovery, LED and health work (#1447, NFR-002).

The 100 Hz state poll feeds death detection (NFR-004). Discovery (USB/HID
enumeration in a worker thread), the 20 Hz LED batch (blocking USB writes in a
worker thread) and the ~1 Hz bluetooth-health emission used to be awaited
inline in the poll loop, so any slow step delayed the next poll by its full
duration. These tests run the real loop (``DiscoveryLoop.start``) against a
backend whose discovery and LED calls block, and measure the gaps between polls.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

SERIAL = "AA:BB:CC:DD:EE:01"
SLOW_STEP_SECONDS = 0.25
# Generous for a loaded CI runner, yet well below SLOW_STEP_SECONDS.
MAX_POLL_GAP_SECONDS = 0.12


class SlowBackend:
    """Backend whose discovery and LED batch block their worker thread."""

    def __init__(self, discovery_delay: float = 0.0, led_delay: float = 0.0):
        self.discovery_delay = discovery_delay
        self.led_delay = led_delay
        self.poll_times: list[float] = []
        self.led_batches = 0
        self.discovery_calls = 0
        self._lock = threading.Lock()

    async def initialize(self) -> bool:
        return True

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        with self._lock:
            self.discovery_calls += 1
            first = self.discovery_calls == 1
        # The first discovery is fast so the controller gets tracked; every
        # later one is slow.
        if not first:
            time.sleep(self.discovery_delay)
        return [SERIAL]

    async def get_controller_state(self, serial: str) -> dict:
        self.poll_times.append(time.monotonic())
        return {"serial": serial, "battery": 5, "accel": {"x": 0.0, "y": 0.0, "z": 1.0}}

    def update_all_leds(self) -> int:
        time.sleep(self.led_delay)
        self.led_batches += 1
        return 1


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


async def _run_for(loop, seconds: float) -> None:
    loop.start()
    assert await loop.wait_initialized(timeout_seconds=2.0)
    await asyncio.sleep(seconds)
    loop.stop()
    await loop.wait_stopped(timeout_seconds=2.0)


def _steady_gaps(backend) -> list[float]:
    """Gaps between consecutive polls after the controller is tracked."""
    times = backend.poll_times[6:]  # skip the spawn retries / first polls
    return [b - a for a, b in zip(times, times[1:], strict=False)]


@pytest.fixture(autouse=True)
def _no_tracer():
    with patch("services.controller_manager.discovery_loop.tracer"):
        yield


@pytest.mark.nfr("NFR-002")
class TestPollCadenceIsolation:
    @pytest.mark.asyncio
    async def test_slow_discovery_does_not_delay_the_poll(self):
        backend = SlowBackend(discovery_delay=SLOW_STEP_SECONDS)
        loop = _make_loop(backend)
        loop._discovery_interval = 0.3  # several slow discoveries in the run

        await _run_for(loop, 1.2)

        assert backend.discovery_calls >= 3, "discovery must keep running"
        gaps = _steady_gaps(backend)
        assert len(gaps) > 50, f"poll barely ran: {len(gaps)} gaps"
        assert max(gaps) < MAX_POLL_GAP_SECONDS, f"poll stalled {max(gaps) * 1000:.0f} ms behind discovery"

    @pytest.mark.asyncio
    async def test_slow_led_batch_does_not_delay_the_poll(self):
        backend = SlowBackend(led_delay=SLOW_STEP_SECONDS)
        loop = _make_loop(backend)

        await _run_for(loop, 1.2)

        assert backend.led_batches >= 2, "LED batches must keep running"
        gaps = _steady_gaps(backend)
        assert len(gaps) > 50, f"poll barely ran: {len(gaps)} gaps"
        assert max(gaps) < MAX_POLL_GAP_SECONDS, f"poll stalled {max(gaps) * 1000:.0f} ms behind the LED batch"

    @pytest.mark.asyncio
    async def test_slow_discovery_does_not_delay_led_batches(self):
        """A death LED waiting in the batch is flushed while discovery is slow."""
        backend = SlowBackend(discovery_delay=SLOW_STEP_SECONDS)
        loop = _make_loop(backend)
        loop._discovery_interval = 0.3

        await _run_for(loop, 1.0)

        # 20 Hz for 1 s is ~20 batches; inline with a 250 ms discovery it was ~5.
        assert backend.led_batches >= 12, f"only {backend.led_batches} LED batches in 1 s"

    @pytest.mark.asyncio
    async def test_health_is_emitted_off_the_poll_loop(self):
        backend = SlowBackend()
        loop = _make_loop(backend)
        loop._health_interval = 0.2
        emitted: list[float] = []
        loop._emit_bluetooth_health = lambda _now, now_mono=None: emitted.append(now_mono)

        await _run_for(loop, 0.9)

        assert len(emitted) >= 3, f"health emitted {len(emitted)} times"

    @pytest.mark.asyncio
    async def test_stop_cancels_every_background_task(self):
        backend = SlowBackend(discovery_delay=0.05, led_delay=0.01)
        loop = _make_loop(backend)

        await _run_for(loop, 0.3)

        assert loop._task is not None and loop._task.done()
        assert loop._side_tasks, "side tasks were started"
        assert all(task.done() for task in loop._side_tasks)
        polls_after_stop = len(backend.poll_times)
        await asyncio.sleep(0.05)
        assert len(backend.poll_times) == polls_after_stop


@pytest.mark.nfr("NFR-002")
class TestLoopLateness:
    @pytest.mark.asyncio
    async def test_poll_lateness_is_observed(self):
        backend = SlowBackend()
        loop = _make_loop(backend)
        with patch("services.controller_manager.discovery_loop.metrics") as metrics:
            await _run_for(loop, 0.3)
        observed = [c.args[0] for c in metrics.poll_loop_lateness_seconds.observe.call_args_list]
        assert len(observed) > 10
        assert all(value >= 0.0 for value in observed)

    def test_event_gap_excludes_loop_stall(self):
        """A 60 ms gap that spans 40 ms of loop stall is a 20 ms link gap."""
        loop = _make_loop(SlowBackend())
        loop._record_fresh_frame("AA", 0.0, stall_total=0.0)
        loop._record_fresh_frame("AA", 0.06, stall_total=0.04)
        assert loop._record("AA").hw_max_gap_ms == pytest.approx(20.0)
        # Input lag still uses the real pick-up time.
        assert loop.sample_age_seconds("AA", 0.07) == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_controller_spawned_during_a_poll_is_polled():
    """The serial cache must not freeze without a controller discovered mid-poll.

    With discovery running concurrently, the poll can rebuild the serial cache
    while ``_spawn_controller_process`` awaits its first state, before the new
    serial is tracked.
    """
    backend = SlowBackend()
    loop = _make_loop(backend)
    loop.backend_initialized = True
    loop._cached_serials = None

    original_get_state = backend.get_controller_state
    rebuilt = asyncio.Event()

    async def get_state_rebuilding_cache(serial):
        # Simulate the poll loop running while spawn awaits the first state.
        if not rebuilt.is_set():
            rebuilt.set()
            await loop._update_controller_states()  # rebuilds the cache: empty
        return await original_get_state(serial)

    backend.get_controller_state = get_state_rebuilding_cache
    await loop._spawn_controller_process(SERIAL)
    backend.get_controller_state = original_get_state

    backend.poll_times.clear()
    await loop._update_controller_states()
    assert backend.poll_times, "newly tracked controller is never polled"


@pytest.mark.nfr("NFR-002")
class TestSideTaskResilience:
    @pytest.mark.asyncio
    async def test_step_that_raises_once_is_logged_and_keeps_its_schedule(self, caplog):
        loop = _make_loop(SlowBackend())
        calls: list[float] = []

        async def flaky_step():
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise RuntimeError("boom")

        caplog.set_level("ERROR", logger="services.controller_manager.discovery_loop")
        task = asyncio.create_task(loop._run_periodic("flaky", 0.02, flaky_step))
        await asyncio.sleep(0.2)
        loop.running = False
        await asyncio.wait_for(task, timeout=1.0)

        assert len(calls) >= 5, "schedule stopped after the failure"
        errors = [r for r in caplog.records if "flaky step error" in r.getMessage()]
        assert len(errors) == 1
        assert errors[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_a_step_that_keeps_failing_is_logged_at_most_once_per_interval(self, caplog):
        loop = _make_loop(SlowBackend())
        calls = 0

        async def failing_step():
            nonlocal calls
            calls += 1
            raise RuntimeError("still broken")

        caplog.set_level("ERROR", logger="services.controller_manager.discovery_loop")
        task = asyncio.create_task(loop._run_periodic("led", 0.005, failing_step))
        await asyncio.sleep(0.2)
        loop.running = False
        await asyncio.wait_for(task, timeout=1.0)

        assert calls >= 10
        errors = [r for r in caplog.records if "led step error" in r.getMessage()]
        assert len(errors) == 1, f"{len(errors)} error lines for {calls} failures"
