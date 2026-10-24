"""
Identify a controller (UC-004, FR-121, #1430).

The operator presses Identify on a dashboard controller card; the controller
manager flashes that controller white and rumbles it briefly, then restores its
colour. Refused for unknown / disconnected, in-game and reserved controllers.
"""

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
import pytest_asyncio
from opentelemetry import trace
from opentelemetry.trace import Span, Tracer

from lib.clock import FakeClock
from lib.controller_constants import ControllerInfoKey
from proto import controller_manager_pb2
from services.controller_manager.feedback_manager import (
    IDENTIFY_DEFAULT_MS,
    IDENTIFY_MAX_MS,
    IDENTIFY_MIN_MS,
    IDENTIFY_RUMBLE_INTENSITY,
    IDENTIFY_RUMBLE_MS,
    FeedbackManager,
    clamp_identify_duration,
)

WHITE = (255, 255, 255)
OFF = (0, 0, 0)
LOBBY = (0, 60, 76)
NEW_BASE = (0, 0, 200)
SERIAL = "MOCK0001"


class _Backend:
    """Records LED and rumble writes (same interface as the multiplexer backend)."""

    def __init__(self):
        self.colors: dict[str, tuple[int, int, int]] = {}
        self.history: list[tuple[int, int, int]] = []
        self.rumble: list[tuple[str, int]] = []
        self.effect_active: dict[str, bool] = {}

    async def set_led_color(self, serial, r, g, b):
        self.colors[serial] = (r, g, b)
        self.history.append((r, g, b))
        return True

    async def set_rumble(self, serial, intensity):
        self.rumble.append((serial, intensity))
        return True

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active


async def _identify(manager, duration_ms=0):
    ok = await manager.start_identify(SERIAL, duration_ms)
    assert ok
    return manager.active_effects[SERIAL].task


async def _cancel_vibration(manager):
    for task in manager.vibration_tasks.values():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def backend():
    return _Backend()


@pytest.fixture
def clock():
    return FakeClock(start_time=1000.0)


@pytest_asyncio.fixture
async def manager(backend, clock):
    fm = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {"name": "P1"}})
    fm._clock = clock
    fm.base_colors[SERIAL] = LOBBY
    yield fm
    await _cancel_vibration(fm)


class TestUC004IdentifyEffect:
    """UC-004 BR-001 / BR-002: the identify effect in the feedback manager."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-001", "BR-002"], cond=["Post-S-1"])
    async def test_flashes_white_rumbles_then_restores_base_colour(self, manager, backend):
        """UC-004 BR-001: white / off frames and a 150 rumble; BR-002: base colour restored."""
        task = await _identify(manager)
        await task

        frames = backend.history[:-1]
        assert frames[0] == WHITE
        assert set(frames) == {WHITE, OFF}
        assert backend.rumble[0] == (SERIAL, IDENTIFY_RUMBLE_INTENSITY)
        assert SERIAL in manager.vibration_tasks  # rumble stops after IDENTIFY_RUMBLE_MS
        assert backend.colors[SERIAL] == LOBBY
        assert manager.current_colors[SERIAL] == LOBBY
        assert backend.effect_active[SERIAL] is False
        assert SERIAL not in manager.active_effects

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-001"])
    async def test_flashes_at_two_hz_for_the_default_duration(self, manager, backend, clock):
        """UC-004 BR-001: 3000 ms at 2 Hz is 6 white frames (each followed by off)."""
        start = clock.time()
        task = await _identify(manager)
        await task

        assert clock.time() - start == pytest.approx(IDENTIFY_DEFAULT_MS / 1000.0)
        assert backend.history[:-1].count(WHITE) == 6 + 1  # 6 cycles + flash's final frame

    @pytest.mark.uc("UC-004", br=["BR-001"])
    def test_duration_defaults_and_clamps(self):
        """UC-004 BR-001: 0 means 3000 ms; values clamp to 500..10000 ms."""
        assert clamp_identify_duration(0) == IDENTIFY_DEFAULT_MS == 3000
        assert clamp_identify_duration(-5) == IDENTIFY_DEFAULT_MS
        assert clamp_identify_duration(100) == IDENTIFY_MIN_MS == 500
        assert clamp_identify_duration(60_000) == IDENTIFY_MAX_MS == 10_000
        assert clamp_identify_duration(1200) == 1200

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-001"])
    async def test_short_effect_rumbles_for_its_whole_duration(self, manager, backend):
        """UC-004 BR-001: rumble lasts 500 ms, or the whole effect when shorter (never longer)."""
        with patch.object(manager, "set_vibration", AsyncMock(return_value=True)) as vib:
            task = await _identify(manager, duration_ms=500)
            await task
        vib.assert_awaited_once_with(SERIAL, IDENTIFY_RUMBLE_INTENSITY, min(IDENTIFY_RUMBLE_MS, 500))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-002"], cond=["Post-S-1"])
    async def test_base_colour_set_during_identify_is_shown_after_it(self, manager, backend, clock):
        """UC-004 BR-002: a base colour arriving mid-effect is stored and wins at the restore."""
        clock.enable_blocking()
        task = await _identify(manager)
        clock.release_all()
        await asyncio.sleep(0)

        await manager.apply_base_color(SERIAL, NEW_BASE, label="test")
        assert backend.colors[SERIAL] != NEW_BASE  # blocked by the running identify

        clock.disable_blocking()
        clock.release_all()
        await task
        assert backend.colors[SERIAL] == NEW_BASE

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-002"])
    async def test_without_base_colour_restores_colour_shown_before(self, manager, backend):
        """UC-004 BR-002: no base colour -> the colour the LED showed when identify started."""
        manager.base_colors.pop(SERIAL)
        await manager._set_led_color(SERIAL, (12, 34, 56))

        task = await _identify(manager)
        await task
        assert backend.colors[SERIAL] == (12, 34, 56)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A5")
    async def test_pressing_again_restarts_the_effect(self, manager, backend, clock):
        """UC-004 A5: a second identify replaces the running one; the base colour still returns."""
        clock.enable_blocking()
        first = await _identify(manager)
        clock.release_all()
        await asyncio.sleep(0)

        second = await _identify(manager)
        assert first.cancelled() or first.done()
        assert second is not first
        assert manager.active_effects[SERIAL].effect_type == controller_manager_pb2.GAME_EFFECT_IDENTIFY

        clock.disable_blocking()
        clock.release_all()
        await second
        assert backend.colors[SERIAL] == LOBBY
        assert [r for r in backend.rumble if r[1] > 0] == [(SERIAL, IDENTIFY_RUMBLE_INTENSITY)] * 2

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A5")
    async def test_pressing_again_gets_a_full_duration(self, backend, clock):
        """UC-004 A5: the restarted effect runs the whole duration, not what was left of the first."""
        baseline = FeedbackManager(backend=_Backend(), tracked_controllers={SERIAL: {"name": "P1"}})
        baseline_clock = FakeClock(start_time=1000.0)
        baseline._clock = baseline_clock
        baseline.base_colors[SERIAL] = LOBBY
        await (await _identify(baseline))
        full_duration_s = baseline_clock.total_sleep_time
        await _cancel_vibration(baseline)
        assert full_duration_s >= IDENTIFY_DEFAULT_MS / 1000.0

        manager = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {"name": "P1"}})
        manager._clock = clock
        manager.base_colors[SERIAL] = LOBBY
        clock.enable_blocking()
        first = await _identify(manager)
        for _ in range(3):  # the first effect is well into its duration
            clock.release_all()
            await asyncio.sleep(0)
        slept_before_second = clock.total_sleep_time

        second = await _identify(manager)
        assert first.cancelled() or first.done()
        clock.disable_blocking()
        clock.release_all()
        await second
        await _cancel_vibration(manager)

        assert clock.total_sleep_time - slept_before_second == pytest.approx(full_duration_s)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A5", br=["BR-002"])
    async def test_repeat_press_without_base_colour_keeps_the_first_fallback(self, manager, backend, clock):
        """UC-004 A5 / BR-002: a repeat press restores the colour from before the first identify."""
        manager.base_colors.pop(SERIAL)
        await manager._set_led_color(SERIAL, (12, 34, 56))
        clock.enable_blocking()
        await _identify(manager)
        clock.release_all()
        await asyncio.sleep(0)
        assert manager.current_colors[SERIAL] == WHITE  # mid-flash

        second = await _identify(manager)
        clock.disable_blocking()
        clock.release_all()
        await second
        assert backend.colors[SERIAL] == (12, 34, 56)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-004"])
    async def test_identify_is_not_a_stream_game_effect(self, manager, backend):
        """UC-004 BR-004: GAME_EFFECT_IDENTIFY is RPC-only; a GameEffectCommand with it is rejected."""
        ok = await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_IDENTIFY)
        assert ok is False
        assert backend.history == []
        assert backend.rumble == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_countdown_takes_over_a_running_identify(self, manager, backend):
        """UC-004 BR-003: a countdown cancels a running effect; the old task leaves the countdown's state alone."""
        first = await _identify(manager, duration_ms=10_000)
        manager._clock = FakeClock(start_time=0.0)
        manager._clock.enable_blocking()  # keep the identify mid-flash
        await asyncio.sleep(0)

        with patch("services.controller_manager.feedback_manager.asyncio.sleep", AsyncMock()):
            await manager.handle_game_effect(
                serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_COUNTDOWN, duration_ms=10
            )
            assert first.done()
            countdown = manager.active_effects[SERIAL]
            assert countdown.effect_type == controller_manager_pb2.GAME_EFFECT_COUNTDOWN
            await countdown.task
        assert WHITE not in backend.history[backend.history.index((255, 0, 0)) :]


# ---------------------------------------------------------------------------
# Servicer RPC
# ---------------------------------------------------------------------------


class TestUC004PulseNeverCutsShortAnIdentify:
    """UC-004 BR-005 / UC-015 BR-015 (#2105): the lobby mode pulse yields to identify and battery display."""

    PINK = (255, 105, 180)

    async def _mode_change(self, manager):
        # What the menu sends on a mode change: the new lobby base colour, then the pulse.
        await manager.apply_base_color(SERIAL, NEW_BASE, label="test")
        return await manager.handle_game_effect(
            SERIAL, controller_manager_pb2.GAME_EFFECT_PULSE, color=self.PINK, duration_ms=600, speed=6
        )

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-005", "BR-002"])
    @pytest.mark.uc("UC-015", br=["BR-015"])
    async def test_pulse_during_identify_is_skipped_and_the_new_base_shows_after(self, manager, backend, clock):
        clock.enable_blocking()
        task = await _identify(manager)
        clock.release_all()
        await asyncio.sleep(0)

        await self._mode_change(manager)
        active = manager.active_effects[SERIAL]
        assert active.task is task
        assert active.effect_type == controller_manager_pb2.GAME_EFFECT_IDENTIFY

        clock.disable_blocking()
        clock.release_all()
        await task
        assert self.PINK not in backend.history
        assert backend.colors[SERIAL] == NEW_BASE

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-005"])
    @pytest.mark.uc("UC-015", br=["BR-015"])
    async def test_pulse_during_battery_display_is_skipped(self, manager, backend, clock):
        clock.enable_blocking()
        await manager.handle_game_effect(SERIAL, controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY)
        task = manager.active_effects[SERIAL].task
        clock.release_all()
        await asyncio.sleep(0)

        await self._mode_change(manager)
        assert manager.active_effects[SERIAL].task is task
        assert manager.active_effects[SERIAL].effect_type == controller_manager_pb2.GAME_EFFECT_SHOW_BATTERY

        clock.disable_blocking()
        clock.release_all()
        await task
        assert self.PINK not in backend.history
        assert backend.colors[SERIAL] == NEW_BASE

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-015", br=["BR-015"])
    async def test_pulse_on_an_idle_controller_still_plays(self, manager, backend):
        await self._mode_change(manager)
        await manager.active_effects[SERIAL].task
        assert self.PINK in backend.history
        assert backend.colors[SERIAL] == NEW_BASE


class _ServicerBackend(_Backend):
    def initialize(self):
        pass

    def discover(self):
        return []

    def cleanup(self):
        pass


@pytest.fixture
def servicer():
    with (
        patch("services.controller_manager.servicer.create_backend") as mock_create_backend,
        patch("services.controller_manager.servicer.DiscoveryLoop") as mock_discovery_loop,
    ):
        mock_create_backend.return_value = _ServicerBackend()
        from services.controller_manager.discovery_loop import DiscoveryLoop
        from services.controller_manager.servicer import ControllerManagerServicer

        mock_discovery_loop.return_value = create_autospec(DiscoveryLoop, instance=True)
        mock_discovery_loop.return_value.connection_quality.return_value = None  # UC-035 BR-005: no window yet

        svc = ControllerManagerServicer()
        svc.feedback_manager._clock = FakeClock(start_time=1000.0)
        svc.tracked_controllers[SERIAL] = {ControllerInfoKey.NAME: "P1", ControllerInfoKey.BATTERY: 5}
        svc.feedback_manager.base_colors[SERIAL] = LOBBY
        yield svc


async def _rpc(servicer, serial=SERIAL, duration_ms=0):
    request = controller_manager_pb2.IdentifyControllerRequest(serial=serial, duration_ms=duration_ms)
    return await servicer.IdentifyController(request, None)


async def _drain(servicer):
    effect = servicer.feedback_manager.active_effects.get(SERIAL)
    if effect is not None:
        await effect.task
    await _cancel_vibration(servicer.feedback_manager)


def _game_config(*serials):
    return controller_manager_pb2.GameplayStreamConfig(
        update_frequency_hz=60,
        colors=[
            controller_manager_pb2.ControllerColorConfig(serial=s, color=controller_manager_pb2.RGB(r=255))
            for s in serials
        ],
    )


class TestUC004IdentifyControllerRpc:
    """UC-004 main scenario and alternative flows through the IdentifyController RPC."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", steps=[3, 4, 6], cond=["Post-S-2"])
    async def test_connected_lobby_controller_is_identified(self, servicer):
        """UC-004 main: started, white flash + rumble, colour restored, metric counted."""
        backend = servicer.backend
        with patch("services.controller_manager.servicer.metrics.controller_identify_requests_total") as counter:
            response = await _rpc(servicer)
            await _drain(servicer)

        assert response.success is True
        assert response.outcome == "started"
        assert response.duration_ms == IDENTIFY_DEFAULT_MS
        assert WHITE in backend.history
        assert backend.rumble[0] == (SERIAL, IDENTIFY_RUMBLE_INTENSITY)
        assert backend.colors[SERIAL] == LOBBY
        counter.labels.assert_called_once_with(outcome="started")
        counter.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", steps=[4], cond=["Post-S-2"])
    async def test_records_controller_identify_span(self, servicer):
        """UC-004 step 4: one controller.identify span with serial, outcome and clamped duration."""
        span = create_autospec(Span, instance=True)
        tracer = create_autospec(Tracer, instance=True)
        tracer.start_as_current_span.return_value = MagicMock(
            __enter__=MagicMock(return_value=span), __exit__=MagicMock(return_value=False)
        )
        with patch("services.controller_manager.servicer.tracer", tracer):
            await _rpc(servicer, duration_ms=60_000)
            await _drain(servicer)

        tracer.start_as_current_span.assert_called_once_with("controller.identify")
        span.set_attribute.assert_any_call("controller.serial", SERIAL)
        span.set_attribute.assert_any_call("identify.outcome", "started")
        span.set_attribute.assert_any_call("effect.duration_ms", IDENTIFY_MAX_MS)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A1", cond=["Post-F-1"])
    async def test_unknown_serial_is_not_connected(self, servicer):
        """UC-004 A1: an unknown serial is refused; nothing is written."""
        with patch("services.controller_manager.servicer.metrics.controller_identify_requests_total") as counter:
            response = await _rpc(servicer, serial="UNKNOWN99")

        assert response.success is False
        assert response.outcome == "not_connected"
        assert response.error
        assert servicer.backend.history == []
        assert servicer.backend.rumble == []
        counter.labels.assert_called_once_with(outcome="not_connected")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A1", cond=["Pre-1"])
    async def test_disconnected_controller_is_not_connected(self, servicer):
        """UC-004 A1: a controller that disconnected (no longer tracked) is refused."""
        servicer.tracked_controllers.pop(SERIAL)

        response = await _rpc(servicer)

        assert response.success is False
        assert response.outcome == "not_connected"
        assert servicer.backend.history == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_player_of_open_game_stream_is_refused(self, servicer):
        """UC-004 A2 / BR-003: refused while listed in a game stream, allowed after it closes."""
        span = trace.INVALID_SPAN
        await servicer._process_gameplay_config(_game_config(SERIAL, "MOCK0002"), "game_stream_1", span)
        servicer.backend.history.clear()

        response = await _rpc(servicer)
        assert response.success is False
        assert response.outcome == "in_game"
        assert servicer.backend.history == []
        assert servicer.backend.rumble == []

        servicer._release_game_stream("game_stream_1")
        response = await _rpc(servicer)
        await _drain(servicer)
        assert response.outcome == "started"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_dead_player_is_still_in_game(self, servicer):
        """UC-004 BR-003: a dead player (filtered out of the stream) is still in the game."""
        span = trace.INVALID_SPAN
        await servicer._process_gameplay_config(_game_config(SERIAL), "game_stream_1", span)
        servicer._process_filter_update(controller_manager_pb2.FilterUpdate(serials=["MOCK0002"]), "x", span, None)

        response = await _rpc(servicer)
        assert response.outcome == "in_game"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_dashboard_stream_does_not_count_as_a_game(self, servicer):
        """UC-004 BR-003: an unfiltered stream (no player colours, the dashboard) is not a game."""
        config = controller_manager_pb2.GameplayStreamConfig(update_frequency_hz=10)
        await servicer._process_gameplay_config(config, "dashboard_stream", trace.INVALID_SPAN)

        response = await _rpc(servicer)
        await _drain(servicer)
        assert response.outcome == "started"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A3", br=["BR-004"], cond=["Post-F-1"])
    async def test_reserved_mock_controller_is_refused(self, servicer):
        """UC-004 A3 / BR-004: a reserved (shadow / pattern) mock controller is refused."""
        servicer.tracked_controllers["MOCK0900"] = {ControllerInfoKey.RESERVED: True}

        response = await _rpc(servicer, serial="MOCK0900")

        assert response.success is False
        assert response.outcome == "reserved"
        assert servicer.backend.history == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A4")
    async def test_mock_controller_streams_the_flash_to_the_dashboard(self, servicer):
        """UC-004 A4: the LED colour streamed to the dashboard (current_colors) shows white."""
        fm = servicer.feedback_manager
        clock = fm._clock
        clock.enable_blocking()
        response = await _rpc(servicer)
        await asyncio.sleep(0)

        assert response.outcome == "started"
        assert fm.current_colors[SERIAL] == WHITE

        clock.disable_blocking()
        clock.release_all()
        await _drain(servicer)
        assert fm.current_colors[SERIAL] == LOBBY

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A6")
    async def test_empty_serial_is_invalid(self, servicer):
        """UC-004 A6: no serial -> outcome invalid."""
        response = await _rpc(servicer, serial="")

        assert response.success is False
        assert response.outcome == "invalid"
        assert servicer.backend.history == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_game_claiming_controller_cancels_running_identify(self, servicer):
        """UC-004 BR-003: game config mid-identify -> identify and rumble end, no white after it, countdown intact."""
        fm = servicer.feedback_manager
        backend = servicer.backend
        fm._clock.enable_blocking()
        response = await _rpc(servicer, duration_ms=10_000)
        assert response.outcome == "started"
        await asyncio.sleep(0)
        identify_task = fm.active_effects[SERIAL].task
        assert backend.colors[SERIAL] == WHITE

        await servicer._process_gameplay_config(_game_config(SERIAL), "game_stream_1", trace.INVALID_SPAN)
        after_config = len(backend.history)
        assert identify_task.done()
        assert backend.rumble[-1] == (SERIAL, 0)  # rumble stopped
        assert SERIAL not in fm.vibration_tasks

        fm._clock.disable_blocking()
        fm._clock.release_all()
        with patch("services.controller_manager.feedback_manager.asyncio.sleep", AsyncMock()):
            await fm.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_COUNTDOWN)
            await fm.active_effects[SERIAL].task
        frames = backend.history[after_config:]
        assert WHITE not in frames
        assert frames[:3] == [(255, 0, 0), (255, 255, 0), (0, 255, 0)]  # red, yellow, green
        assert frames[-1] == (255, 0, 0)  # the game's base colour

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_closing_the_game_stream_releases_the_roster(self, servicer):
        """UC-004 BR-003: StreamGameplayData's close path forgets the game's players."""
        servicer.state_builder.build_state = MagicMock(
            side_effect=lambda serial, _info: controller_manager_pb2.ControllerState(serial=serial)
        )
        servicer.discovery_loop.drain_health_counters.return_value = (0, 0, 0)
        config = controller_manager_pb2.GameplayStreamControl(config=_game_config(SERIAL))

        async def requests():
            yield config
            await asyncio.Event().wait()

        class _Context:
            cancelled_flag = False

            def cancelled(self):
                return self.cancelled_flag

        context = _Context()
        seen_in_game = False
        async for _ in servicer.StreamGameplayData(requests(), context):
            seen_in_game = seen_in_game or servicer._in_game(SERIAL)
            if seen_in_game:
                context.cancelled_flag = True

        assert seen_in_game
        assert servicer._game_stream_serials == {}
        assert (await _rpc(servicer)).outcome == "started"
        await _drain(servicer)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", flow="A2", br=["BR-003"])
    async def test_game_claiming_controller_while_identify_starts_wins(self, servicer):
        """UC-004 BR-003: a game config landing while the identify is starting ends it (outcome in_game)."""
        fm = servicer.feedback_manager
        real_set_vibration = fm.set_vibration

        async def vibration_then_game_claims(serial, intensity, duration_ms):
            ok = await real_set_vibration(serial, intensity, duration_ms)
            # The entry is still the IDENTIFY placeholder here.
            assert fm.active_effects[serial].effect_type == controller_manager_pb2.GAME_EFFECT_IDENTIFY
            await servicer._process_gameplay_config(_game_config(SERIAL), "game_stream_1", trace.INVALID_SPAN)
            return ok

        with patch.object(fm, "set_vibration", side_effect=vibration_then_game_claims):
            response = await _rpc(servicer)

        assert response.outcome == "in_game"
        assert response.success is False
        assert SERIAL not in fm.active_effects or fm.active_effects[SERIAL].task.done()
        assert servicer.backend.rumble[-1] == (SERIAL, 0)
        assert servicer.backend.colors[SERIAL] == (255, 0, 0)  # the game's colour

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-003"])
    async def test_cancel_identify_ends_a_starting_identify(self, servicer):
        """UC-004 BR-003: the IDENTIFY placeholder (identify still starting) counts as running."""
        from services.controller_manager.effects_base import ActiveEffect

        fm = servicer.feedback_manager
        placeholder = asyncio.get_running_loop().create_future()
        placeholder.set_result(None)
        fm.active_effects[SERIAL] = ActiveEffect(
            task=placeholder, effect_type=controller_manager_pb2.GAME_EFFECT_IDENTIFY
        )

        assert await fm.cancel_identify(SERIAL) is True
        assert SERIAL not in fm.active_effects

    @pytest.mark.uc("UC-004", br=["BR-002"])
    def test_clear_controller_forgets_identify_fallback(self, servicer):
        """UC-004 BR-002: a disconnect drops the captured identify colour."""
        fm = servicer.feedback_manager
        fm._identify_fallback[SERIAL] = (1, 2, 3)
        fm.clear_controller(SERIAL)
        assert SERIAL not in fm._identify_fallback


_REPO = Path(__file__).resolve().parents[3]


class TestUC004IdentifyIsOperatorOnly:
    """UC-004 BR-004: no flag gates Identify and the agent has no way to call it."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-004", br=["BR-004"])
    async def test_identify_reads_no_flag(self, servicer):
        """The RPC starts without consulting any flag client, so no flag value can gate it."""
        with patch("lib.feature_flags.get_flag_client", side_effect=AssertionError("identify read a flag")) as getter:
            response = await _rpc(servicer)
            await _drain(servicer)

        assert response.outcome == "started"
        getter.assert_not_called()

    @pytest.mark.uc("UC-004", br=["BR-004"])
    def test_no_flag_exists_for_identify(self):
        """The agent acts only through flagd flags; none of them names or controls Identify."""
        flag_files = sorted((_REPO / "services/flagd").glob("*.json"))
        assert flag_files
        for flag_file in flag_files:
            flags = json.loads(flag_file.read_text()).get("flags", {})
            assert not [key for key in flags if "identify" in key.lower()], flag_file.name

    @pytest.mark.uc("UC-004", br=["BR-004"])
    def test_agent_does_not_call_identify_controller(self):
        """Only the dashboard path (connect-proxy, dashboard) refers to the RPC; the agent's code never does."""
        agent_sources = [path for path in (_REPO / "services/agent").rglob("*.go") if "/gen/" not in path.as_posix()]
        assert agent_sources
        offenders = [str(p) for p in agent_sources if "IdentifyController" in p.read_text()]
        assert offenders == []
