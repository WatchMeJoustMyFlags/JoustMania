"""
Shield feedback LED effect (#1301, UC-040 A16 / A17, BR-008 to BR-010).

The schedule is a pure function (``shield_frame``) rendered by the controller
manager's effect loop, so the game coordinator sends one command per grant.
"""

import asyncio
import contextlib

import pytest

from lib.clock import FakeClock
from proto import controller_manager_pb2
from services.controller_manager.effects_base import shield_frame
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.state_cache import StateCache

WHITE = (255, 255, 255)
OFF = (0, 0, 0)
PLAYER = (0, 255, 0)
LOBBY = (0, 60, 76)
SERIAL = "MOCK0001"


class TestUC040ShieldSchedule:
    """UC-040 BR-008 / BR-009: colour at t=0, mid-fade, last-second blink, restore."""

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    def test_full_shield_starts_at_full_colour(self):
        """UC-040 BR-008: t=0 shows the full shield colour."""
        assert shield_frame(0.0, 5.0, WHITE) == WHITE

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    def test_full_shield_fades_linearly_to_30_percent(self):
        """UC-040 BR-008: brightness falls 100% -> 30% over the first T-1 s."""
        # T=5: fade window 4 s; at 2 s halfway -> 1 - 0.7 * 0.5 = 0.65
        assert shield_frame(2.0, 5.0, WHITE) == (166, 166, 166)
        # just before the blink window: about 30 %
        just_before = shield_frame(3.999, 5.0, WHITE)
        assert just_before is not None
        assert 76 <= just_before[0] <= 78
        # monotonic fade
        samples = [shield_frame(t / 10, 5.0, WHITE)[0] for t in range(0, 40)]
        assert samples == sorted(samples, reverse=True)

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    def test_full_shield_blinks_4hz_in_the_last_second(self):
        """UC-040 BR-008: the last second alternates full colour and off at 4 Hz."""
        on_times = [4.0, 4.25, 4.5, 4.75]
        off_times = [4.125, 4.375, 4.625, 4.875]
        assert [shield_frame(t, 5.0, WHITE) for t in on_times] == [WHITE] * 4
        assert [shield_frame(t, 5.0, WHITE) for t in off_times] == [OFF] * 4

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    def test_shield_is_over_at_its_duration(self):
        """UC-040 BR-008: at T the schedule ends (the caller restores the base colour)."""
        assert shield_frame(5.0, 5.0, WHITE) is None
        assert shield_frame(7.0, 5.0, WHITE) is None

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    def test_short_shield_blinks_for_its_whole_duration(self):
        """UC-040 BR-008: a shield of 1 s or less only blinks."""
        assert shield_frame(0.0, 0.8, WHITE) == WHITE
        assert shield_frame(0.15, 0.8, WHITE) == OFF

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    def test_partial_shield_breathes_between_10_and_50_percent(self):
        """UC-040 BR-009: the partial shield is a slow dim breathing (0.5 Hz)."""
        assert shield_frame(0.0, 5.0, WHITE, partial=True) == (26, 26, 26)  # 10 %
        assert shield_frame(1.0, 5.0, WHITE, partial=True) == (128, 128, 128)  # 50 % at half period
        assert shield_frame(2.0, 5.0, WHITE, partial=True) == (26, 26, 26)  # back to 10 %
        for t in range(0, 40):
            frame = shield_frame(t / 10, 5.0, WHITE, partial=True)
            assert 25 <= frame[0] <= 128, (t, frame)

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    def test_partial_shield_blinks_at_half_brightness(self):
        """UC-040 BR-009: the partial shield's last second blinks between 50 % and off."""
        assert shield_frame(4.0, 5.0, WHITE, partial=True) == (128, 128, 128)
        assert shield_frame(4.125, 5.0, WHITE, partial=True) == OFF
        assert shield_frame(5.0, 5.0, WHITE, partial=True) is None


class _Backend:
    """Records LED writes (same interface as the multiplexer backend)."""

    def __init__(self):
        self.colors: dict[str, tuple[int, int, int]] = {}
        self.history: list[tuple[int, int, int]] = []
        self.effect_active: dict[str, bool] = {}

    async def set_led_color(self, serial, r, g, b):
        self.colors[serial] = (r, g, b)
        self.history.append((r, g, b))
        return True

    async def set_rumble(self, serial, intensity):
        return True

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active


@pytest.fixture
def backend():
    return _Backend()


@pytest.fixture
def clock():
    return FakeClock(start_time=1000.0)


@pytest.fixture
def manager(backend, clock):
    fm = FeedbackManager(backend=backend, tracked_controllers={SERIAL: {"name": "P1"}})
    fm._clock = clock
    fm.base_colors[SERIAL] = PLAYER
    return fm


async def _shield(manager, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=3000):
    ok = await manager.handle_game_effect(serial=SERIAL, effect=effect, duration_ms=duration_ms)
    assert ok
    return manager.active_effects[SERIAL].task


class TestUC040ShieldEffect:
    """UC-040 A16 / A17: the controller manager renders, restores and cancels the shield."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    async def test_shield_renders_schedule_then_restores_player_colour(self, manager, backend):
        """UC-040 A16: full colour first, fade, blink, then the player's base colour."""
        task = await _shield(manager, duration_ms=3000)
        await task

        assert backend.history[0] == WHITE
        assert OFF in backend.history  # blinked
        assert backend.colors[SERIAL] == PLAYER  # restored
        assert SERIAL not in manager.active_effects
        assert SERIAL not in manager._shield_tasks
        assert manager.current_colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    async def test_partial_shield_is_dimmer_than_full(self, manager, backend):
        """UC-040 BR-009: no partial-shield frame is brighter than 50 %."""
        task = await _shield(manager, effect=controller_manager_pb2.GAME_EFFECT_PARTIAL_SHIELD, duration_ms=3000)
        await task

        frames = backend.history[:-1]  # last write is the restore
        assert frames and max(f[0] for f in frames) <= 128
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_extending_restarts_the_fade_from_full(self, manager, backend, clock):
        """UC-040 BR-010: a new SHIELD command replaces the running one and starts at full colour."""
        clock.enable_blocking()
        first = await _shield(manager, duration_ms=5000)
        for _ in range(5):
            await asyncio.sleep(0)
        clock.release_one()  # let the first effect advance and dim
        await asyncio.sleep(0)
        backend.history.clear()

        second = await _shield(manager, duration_ms=8000)
        await asyncio.sleep(0)

        assert first.cancelled() or first.done()
        assert manager.active_effects[SERIAL].task is second
        assert manager._shield_tasks[SERIAL] is second
        assert WHITE in backend.history  # restarted at full brightness
        clock.disable_blocking()
        await second

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_zero_duration_ends_shield_and_restores(self, manager, backend, clock):
        """UC-040 A17: SHIELD with duration 0 (game end) cancels the task and restores the base colour."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)
        await asyncio.sleep(0)

        ok = await manager.handle_game_effect(
            serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=0
        )

        assert ok
        assert task.cancelled()
        assert SERIAL not in manager.active_effects
        assert SERIAL not in manager._shield_tasks
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_zero_duration_leaves_other_effects_alone(self, manager, backend, clock):
        """UC-040 A17: ending a shield never cancels a different effect (e.g. the death fade)."""
        clock.enable_blocking()
        await manager.play_effect_with_restore(SERIAL, "fade_out", (255, 0, 0), 700, 1, OFF)
        other = manager.active_effects[SERIAL].task
        await asyncio.sleep(0)

        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=0)

        assert not other.done()
        assert manager.active_effects[SERIAL].task is other
        clock.disable_blocking()
        await other

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_death_effect_replaces_the_shield(self, manager, backend, clock):
        """UC-040 A17: the death effect cancels the shield task; nothing is leaked."""
        clock.enable_blocking()
        shield_task = await _shield(manager, duration_ms=10_000)
        await asyncio.sleep(0)

        death = asyncio.create_task(
            manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        clock.disable_blocking()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(death, timeout=3)

        assert shield_task.cancelled()
        assert SERIAL not in manager._shield_tasks
        fade = manager.active_effects.get(SERIAL)
        if fade is not None:
            await fade.task
        assert backend.colors[SERIAL] == OFF  # dead: black, not the shield colour

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_new_base_colour_ends_the_shield(self, manager, backend, clock):
        """UC-040 A17: a new base colour (lobby reset) cancels the shield and shows at once."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)
        await asyncio.sleep(0)

        await manager.apply_base_color(SERIAL, LOBBY, label="test")

        assert task.cancelled()
        assert backend.colors[SERIAL] == LOBBY
        assert SERIAL not in manager._shield_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_disconnect_cancels_the_shield_task(self, manager, clock):
        """UC-040 A17: a disconnected controller leaves no shield task behind."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)
        await asyncio.sleep(0)

        manager.tracked_controllers.pop(SERIAL)  # the discovery loop drops it first
        manager.clear_controller(SERIAL)
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert SERIAL not in manager._shield_tasks
        assert SERIAL not in manager.current_colors


class _Monitoring:
    def get_rssi(self, _serial):
        return 0


class TestUC040LiveLedColourInStream:
    """UC-040 A16 step 4: the gameplay stream carries the live LED colour for the dashboard."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    async def test_stream_state_follows_the_shield_frames(self, manager, clock):
        """UC-040 A16: ControllerState.color is the colour the LED shows right now."""
        cache = StateCache(_Monitoring())
        cache.set_led_colors(manager.current_colors)
        info = {"battery": 5, "team": 1}

        clock.enable_blocking()
        await _shield(manager, duration_ms=5000)
        await asyncio.sleep(0)
        first = cache.build_or_get_cached_state(SERIAL, info)
        assert (first.color.r, first.color.g, first.color.b) == WHITE

        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=0)
        restored = cache.build_or_get_cached_state(SERIAL, info)
        assert (restored.color.r, restored.color.g, restored.color.b) == PLAYER
