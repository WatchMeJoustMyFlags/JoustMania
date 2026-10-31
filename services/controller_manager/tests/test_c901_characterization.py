"""Characterization tests for the four C901 refactors of #2354.

They pin branches of ``DiscoveryLoop._update_controller_states``,
``HidStreamMixin._stream_loop``, ``validate_archetype`` and
``ControllerManagerServicer.StreamGameplayData`` that no other test reached,
so splitting those functions cannot change what they do.
"""

import asyncio
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_identify_controller import SERIAL, _game_config, servicer  # noqa: F401  (pytest fixture)

from proto import controller_manager_pb2
from services.controller_manager import servicer as servicer_module
from services.controller_manager.discovery import PeriodicRescanTimer
from services.controller_manager.discovery_loop import DiscoveryLoop
from services.controller_manager.event_publisher import EventPublisher
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.monitoring import ControllerMonitoring
from services.controller_manager.multiplexer import motion_patterns as mp
from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter
from services.controller_manager.tests.backend_doubles import autospec_backend, autospec_button_detector
from services.controller_manager.tests.fake_stream_call import FakeStreamCall

DISCOVERY_LOGGER = "services.controller_manager.discovery_loop"
SERVICER_LOGGER = "services.controller_manager.servicer"


# --------------------------------------------------------------------------
# DiscoveryLoop._update_controller_states
# --------------------------------------------------------------------------


def _loop(serials):
    monitoring = create_autospec(ControllerMonitoring, instance=True)
    monitoring.last_battery_check = 0  # instance attribute: not on the class, so not in the autospec
    loop = DiscoveryLoop(
        backend=autospec_backend(),
        tracked_controllers={s: {} for s in serials},
        controller_states={},
        button_detector=autospec_button_detector(),
        feedback_manager=create_autospec(FeedbackManager, instance=True),
        monitoring=monitoring,
        rescan_timer=create_autospec(PeriodicRescanTimer, instance=True),
        paired_serials=[],
        base_colors={},
        event_publisher=create_autospec(EventPublisher, instance=True),
    )
    loop.backend_initialized = True
    loop._cached_serials = list(serials)
    loop.backend.get_controller_state = AsyncMock(side_effect=lambda s: {"serial": s})
    return loop


def _poll_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Polling ")]


class TestUpdateControllerStatesCharacterization:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    async def test_uninitialized_backend_polls_nothing(self):
        """Before the backend is up the 100 Hz tick returns without a poll."""
        loop = _loop(["AA"])
        loop.backend_initialized = False

        await loop._update_controller_states()

        loop.backend.get_controller_state.assert_not_awaited()
        assert loop.controller_states == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    async def test_periodic_log_counts_active_idle_and_adapters(self, caplog):
        """The 5 s log names the polled count, active / idle split and adapters, and does not repeat inside 5 s."""
        loop = _loop(["AA", "BB", "CC"])
        loop._record("BB").last_activity_time = time.time() - 10 * loop._idle_threshold_seconds - 1
        loop._record("CC").last_activity_time = time.time()
        loop.backend.get_adapter_type.side_effect = {"AA": "rust", "BB": "python", "CC": "rust"}.get

        with caplog.at_level(logging.INFO, logger=DISCOVERY_LOGGER):
            await loop._update_controller_states()
            await loop._update_controller_states()

        assert _poll_logs(caplog) == ["Polling 3 controllers at 100Hz, active=2, idle=1, adapters=python:1+rust:2"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    async def test_periodic_log_without_adapter_types(self, caplog):
        """A backend without adapters reports "" for each serial: no adapters suffix."""
        loop = _loop(["AA"])
        loop.backend.get_adapter_type.return_value = ""

        with caplog.at_level(logging.INFO, logger=DISCOVERY_LOGGER):
            await loop._update_controller_states()

        assert _poll_logs(caplog) == ["Polling 1 controllers at 100Hz, active=1, idle=0"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    async def test_controller_removed_during_polling_is_skipped(self):
        """A serial removed while the batch was polled gets no state and no health counts."""
        loop = _loop(["AA", "BB"])
        loop.backend.get_adapter_type.return_value = ""

        async def poll(serial):
            if serial == "BB":
                loop.tracked_controllers.pop("AA")
            return {"serial": serial}

        loop.backend.get_controller_state = AsyncMock(side_effect=poll)

        await loop._update_controller_states()

        assert "AA" not in loop.controller_states
        assert "BB" in loop.controller_states
        assert loop._record("AA").hw_polls == 0
        assert loop._record("BB").hw_polls == 1


# --------------------------------------------------------------------------
# HidStreamMixin._stream_loop
# --------------------------------------------------------------------------


def _bare_hid():
    adapter = RustServiceAdapter.__new__(RustServiceAdapter)
    adapter._init_stream_state()
    adapter._channel = create_autospec(grpc.Channel, instance=True)
    adapter._stub = MagicMock(spec_set=["StreamIO"])  # the stub's attributes are set per instance: no class to autospec
    adapter._latest_data = {}
    adapter._last_state = {}
    adapter._trigger_latch = set()
    adapter._data_lock = threading.Lock()
    adapter._closed = set()
    adapter._command_queue = queue.Queue(maxsize=256)
    adapter._adapter_names = {}
    return adapter


class _ReplacedCall(FakeStreamCall):
    """Ends at once, after another call has taken the adapter's ``_stream_call`` slot."""

    def __init__(self, adapter, other):
        super().__init__(())
        self._adapter = adapter
        self._other = other

    def __next__(self):
        self._adapter._stream_call = self._other
        raise StopIteration


class TestStreamLoopCharacterization:
    @pytest.mark.nfr("NFR-002")
    def test_end_keeps_a_call_slot_it_does_not_own(self):
        """The reader's cleanup clears ``_stream_call`` only when it still holds its own call."""
        adapter = _bare_hid()
        other = FakeStreamCall()
        call = _ReplacedCall(adapter, other)
        adapter._stub.StreamIO.return_value = call
        adapter._stream_running = True

        adapter._stream_loop()

        assert call.cancelled
        assert not other.cancelled
        assert adapter._stream_call is other
        assert adapter._stream_running is False

    def test_stream_stopped_while_opening_cancels_the_call_and_reads_nothing(self):
        """A stop that lands while StreamIO is being opened cancels that call; none of its frames is read."""
        adapter = _bare_hid()
        sensor = MagicMock()
        sensor.serial = SERIAL
        call = FakeStreamCall([sensor])

        def open_then_stop(_commands):
            adapter._stream_running = False  # the stop lands between the open and the registration
            return call

        adapter._stub.StreamIO.side_effect = open_then_stop
        adapter._stream_running = True

        adapter._stream_loop()

        assert call.cancelled
        assert adapter._latest_data == {}
        assert adapter._stream_call is None


# --------------------------------------------------------------------------
# validate_archetype
# --------------------------------------------------------------------------


def _params(**overrides):
    p = dict.fromkeys(mp.PARAM_FIELDS, 0.0)
    p.update(players=4, stream_rate_hz=30, base=1.0, period_s=2.0)
    p.update(overrides)
    return p


class TestValidateArchetypeCharacterization:
    @pytest.mark.uc("UC-046", flow="A2", br=["BR-003"])
    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            (
                {"amp": 9.5, "d_amp": 0.3},
                [f"slot {i} amp {v} g outside 0.0..10.0" for i, v in ((2, "10.1000"), (3, "10.4000"))],
            ),
            ({"d_amp": -0.1}, [f"slot {i} amp {-0.1 * i:.4f} g outside 0.0..10.0" for i in (1, 2, 3)]),
        ],
    )
    def test_slot_amp_outside_the_g_range(self, overrides, expected):
        """BR-003: a per-slot amplitude outside MIN_G..MAX_G is rejected with one rule per slot."""
        arch, errors = mp.validate_archetype("unit_test", _params(**overrides))
        assert arch is None
        assert errors == expected


# --------------------------------------------------------------------------
# StreamGameplayData
# --------------------------------------------------------------------------


class _Context:
    def __init__(self):
        self.cancelled_flag = False

    def cancelled(self):
        return self.cancelled_flag

    async def abort(self, code, details):
        raise AssertionError(f"unexpected abort {code}: {details}")


@pytest.fixture
def stream_servicer(servicer):  # noqa: F811
    servicer.state_builder.build_state = MagicMock(
        side_effect=lambda serial, _info: controller_manager_pb2.ControllerState(serial=serial)
    )
    servicer.discovery_loop.drain_health_counters.return_value = (0, 0, 0)
    return servicer


async def _run_frames(svc, requests, frames):
    """Run the stream for ``frames`` frames, letting the reader task run between them."""
    context = _Context()
    seen = 0
    async for _ in svc.StreamGameplayData(requests, context):
        seen += 1
        await asyncio.sleep(0)
        if seen >= frames:
            context.cancelled_flag = True


class TestStreamGameplayDataCharacterization:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    async def test_control_messages_dispatch_to_their_handlers(self, stream_servicer):
        """Config sets the rate and filter, a filter update replaces the filter, colour and effect commands
        reach their handlers and leave the filter alone."""
        svc = stream_servicer
        color = controller_manager_pb2.ControllerColorConfig(serial=SERIAL, color=controller_manager_pb2.RGB(g=9))
        effect = controller_manager_pb2.GameEffectCommand(serial=SERIAL, effect=1)
        done = asyncio.Event()

        async def requests():
            yield controller_manager_pb2.GameplayStreamControl(config=_game_config(SERIAL))
            yield controller_manager_pb2.GameplayStreamControl(
                filter_update=controller_manager_pb2.FilterUpdate(serials=["OTHER"])
            )
            yield controller_manager_pb2.GameplayStreamControl(base_color=color)
            yield controller_manager_pb2.GameplayStreamControl(game_effect=effect)
            done.set()
            await asyncio.Event().wait()

        filters = []
        real_build = svc._build_gameplay_update

        def build(current_filter, subscriber_id=""):
            filters.append(None if current_filter is None else set(current_filter))
            return real_build(current_filter, subscriber_id)

        with (
            patch.object(svc, "_build_gameplay_update", side_effect=build),
            patch.object(svc, "_process_base_color_command", new_callable=AsyncMock) as base_color,
            patch.object(svc, "_process_game_effect_command", new_callable=AsyncMock) as game_effect,
        ):
            context = _Context()
            frames_after_done = 0
            async for _ in svc.StreamGameplayData(requests(), context):
                if done.is_set():
                    frames_after_done += 1
                await asyncio.sleep(0)
                if frames_after_done >= 1 or len(filters) > 200:
                    context.cancelled_flag = True

        base_color.assert_awaited_once()
        assert base_color.await_args.args[0] == color
        assert base_color.await_args.args[2] == "GameplayStream"
        game_effect.assert_awaited_once()
        assert game_effect.await_args.args[0] == effect
        subscriber_id = base_color.await_args.args[1]
        assert game_effect.await_args.args[1] == subscriber_id
        assert filters[-1] == {"OTHER"}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    @pytest.mark.parametrize("hz", [10, 60])
    async def test_config_rate_reaches_the_frame_loop(self, stream_servicer, hz):
        """A config's update_frequency_hz is the rate the frame loop asks _stream_rate_hz for."""
        svc = stream_servicer
        config = controller_manager_pb2.GameplayStreamConfig(update_frequency_hz=hz)

        async def requests():
            yield controller_manager_pb2.GameplayStreamControl(config=config)
            await asyncio.Event().wait()

        with (
            patch.object(servicer_module, "_frequency_override", None),
            patch.object(servicer_module, "_stream_rate_hz", wraps=servicer_module._stream_rate_hz) as rate,
        ):
            await _run_frames(svc, requests(), frames=5)

        assert rate.call_args.args[0] == hz

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", steps=[2])
    async def test_reader_error_is_logged_and_frames_continue(self, stream_servicer, caplog):
        """A failing request iterator is logged; the frame loop keeps streaming."""

        async def requests():
            raise RuntimeError("reader broke")
            yield  # pragma: no cover

        with caplog.at_level(logging.ERROR, logger=SERVICER_LOGGER):
            await _run_frames(stream_servicer, requests(), frames=3)

        assert any("Error reading client updates: reader broke" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-046", flow="A10", br=["BR-008"])
    async def test_pattern_rate_pin_and_overrun(self, stream_servicer, caplog):
        """FR-103: a pattern stream logs the pinned rate once; an overrun frame counts per pattern stream."""
        svc = stream_servicer

        async def requests():
            await asyncio.Event().wait()
            yield  # pragma: no cover

        real_build = svc._build_gameplay_update

        def slow_build(current_filter, subscriber_id=""):
            time.sleep(0.1)  # longer than one 15 Hz frame: an overrun
            return real_build(current_filter, subscriber_id)

        with (
            patch.object(svc, "_pattern_stream_rate", return_value=15),
            patch.object(svc, "_build_gameplay_update", side_effect=slow_build),
            patch.object(svc.backend, "record_pattern_overrun", autospec=True) as overrun,
            caplog.at_level(logging.INFO, logger=SERVICER_LOGGER),
        ):
            await _run_frames(svc, requests(), frames=2)

        pinned = [r.getMessage() for r in caplog.records if "Pattern stream rate pinned" in r.getMessage()]
        assert len(pinned) == 1
        assert pinned[0].endswith("Pattern stream rate pinned: 30 -> 15 Hz")
        assert overrun.call_count >= 1
