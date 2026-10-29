"""
Shield feedback LED layer (#1301, UC-040 A16 / A17, BR-008 to BR-010).

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
from services.controller_manager.state_builder import StateBuilder

WHITE = (255, 255, 255)
OFF = (0, 0, 0)
PLAYER = (0, 255, 0)
LOBBY = (0, 60, 76)
SERIAL = "MOCK0001"


class TestUC040ToughnessBoostEnum:
    """#1838: the weaker shield variant is GAME_EFFECT_TOUGHNESS_BOOST, wire number unchanged."""

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    def test_enum_name_and_number(self):
        assert controller_manager_pb2.GameEffect.Value("GAME_EFFECT_TOUGHNESS_BOOST") == 15
        assert controller_manager_pb2.GameEffect.Name(15) == "GAME_EFFECT_TOUGHNESS_BOOST"
        retired = "GAME_EFFECT_" + "PARTIAL" + "_SHIELD"
        with pytest.raises(ValueError):
            controller_manager_pb2.GameEffect.Value(retired)


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
    def test_toughness_boost_breathes_between_10_and_50_percent(self):
        """UC-040 BR-009: the toughness boost is a slow dim breathing (0.5 Hz)."""
        assert shield_frame(0.0, 5.0, WHITE, toughness_boost=True) == (26, 26, 26)  # 10 %
        assert shield_frame(1.0, 5.0, WHITE, toughness_boost=True) == (128, 128, 128)  # 50 % at half period
        assert shield_frame(2.0, 5.0, WHITE, toughness_boost=True) == (26, 26, 26)  # back to 10 %
        for t in range(0, 40):
            frame = shield_frame(t / 10, 5.0, WHITE, toughness_boost=True)
            assert 25 <= frame[0] <= 128, (t, frame)

    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    def test_toughness_boost_blinks_at_half_brightness(self):
        """UC-040 BR-009: the toughness boost's last second blinks between 50 % and off."""
        assert shield_frame(4.0, 5.0, WHITE, toughness_boost=True) == (128, 128, 128)
        assert shield_frame(4.125, 5.0, WHITE, toughness_boost=True) == OFF
        assert shield_frame(5.0, 5.0, WHITE, toughness_boost=True) is None


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
    await asyncio.sleep(0)  # let the renderer paint its first frame
    return manager._shield_tasks.get(SERIAL)


async def _spin(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


def _is_shield_frame(color):
    return color != PLAYER and color[0] == color[1] == color[2]


class TestUC040ShieldEffect:
    """UC-040 A16 / A17: the controller manager renders, restores and ends the shield layer."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-008"])
    async def test_shield_renders_schedule_then_restores_player_colour(self, manager, backend):
        """UC-040 A16: full colour first, fade, blink, then the player's base colour."""
        task = await _shield(manager, duration_ms=3000)
        await task

        assert backend.history[0] == WHITE
        assert OFF in backend.history  # blinked
        assert backend.colors[SERIAL] == PLAYER  # restored
        assert not manager.shield_active(SERIAL)
        assert SERIAL not in manager._shield_tasks
        assert manager.current_colors[SERIAL] == PLAYER
        assert backend.effect_active[SERIAL] is False

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    async def test_toughness_boost_is_dimmer_than_full(self, manager, backend):
        """UC-040 BR-009: no toughness boost frame is brighter than 50 %."""
        task = await _shield(manager, effect=controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST, duration_ms=3000)
        await task

        frames = backend.history[:-1]  # last write is the restore
        assert frames and max(f[0] for f in frames) <= 128
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009"])
    @pytest.mark.parametrize(
        ("effect", "kind"),
        [
            (controller_manager_pb2.GAME_EFFECT_SHIELD, "full"),
            (controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST, "toughness_boost"),
        ],
    )
    async def test_render_span_and_blink_event_carry_the_shield_kind(self, manager, monkeypatch, effect, kind):
        """UC-040 A16: shield.kind on the render span and its shield_blink event is full or toughness_boost."""
        spans = []

        class _Span:
            def __init__(self):
                self.attributes = {}
                self.events = []

            def set_attribute(self, key, value):
                self.attributes[key] = value

            def add_event(self, name, attrs=None):
                self.events.append((name, dict(attrs or {})))

        class _Tracer:
            @contextlib.contextmanager
            def start_as_current_span(self, name, context=None):
                span = _Span()
                spans.append((name, span))
                yield span

        monkeypatch.setattr("services.controller_manager.feedback_manager.tracer", _Tracer())
        task = await _shield(manager, effect=effect, duration_ms=3000)
        await task

        (_, span) = next(item for item in spans if item[0] == "controller.shield_effect")
        assert span.attributes["shield.kind"] == kind
        assert ("shield_blink", {"shield.kind": kind}) in span.events

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_extending_restarts_the_fade_from_full(self, manager, backend, clock):
        """UC-040 BR-010: a new SHIELD restarts the layer's schedule at full colour, same renderer."""
        clock.enable_blocking()
        first = await _shield(manager, duration_ms=5000)
        for _ in range(30):  # let the fade advance
            clock.release_all()
            await _spin(3)
        assert backend.colors[SERIAL][0] < 250
        backend.history.clear()

        second = await _shield(manager, duration_ms=8000)
        clock.release_all()
        await _spin(3)

        assert second is first  # one renderer, layer restarted
        assert manager._shield_layers[SERIAL]["full"].total_s == 8.0
        assert max(f[0] for f in backend.history) >= 250
        clock.disable_blocking()
        await manager.end_shield_effect(SERIAL)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_zero_duration_ends_shield_and_restores(self, manager, backend, clock):
        """UC-040 A17: SHIELD with duration 0 (game end) cancels the renderer and restores the base colour."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)

        ok = await manager.handle_game_effect(
            serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=0
        )

        assert ok
        assert task.cancelled()
        assert not manager.shield_active(SERIAL)
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
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_rumble_neither_ends_nor_orphans_the_shield(self, manager, backend, clock):
        """UC-040 BR-010: a rumble (no LED) leaves the shield layer reachable; end_shield_effect still ends it."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)

        assert await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_RUMBLE)

        assert manager.shield_active(SERIAL)
        assert manager._shield_tasks[SERIAL] is task and not task.done()
        assert SERIAL not in manager.active_effects  # no re-typed placeholder left behind
        assert await manager.end_shield_effect(SERIAL) is True
        assert task.cancelled()
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_death_after_rumble_is_not_overwritten_by_the_shield(self, manager, backend):
        """UC-040 A17: shield -> rumble -> death: the death fade ends black, no shield frame after it."""
        await _shield(manager, duration_ms=10_000)
        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_RUMBLE)
        await _spin(5)

        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        death_start = backend.history.index((255, 0, 0))
        fade = manager.active_effects.get(SERIAL)
        if fade is not None:
            await fade.task
        await _spin(20)

        after_death = backend.history[death_start:]
        assert not any(_is_shield_frame(c) and c != OFF for c in after_death), after_death
        assert backend.colors[SERIAL] == OFF
        assert not manager.shield_active(SERIAL)
        assert SERIAL not in manager._shield_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_warning_overlays_then_the_shield_resumes(self, manager, backend):
        """UC-040 BR-010: a PLAYER_WARNING flash overlays the shield, then the shield schedule resumes."""
        await _shield(manager, duration_ms=10_000)
        await _spin(5)

        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING)
        overlay = manager.active_effects[SERIAL].task
        await overlay
        assert manager.shield_active(SERIAL)
        backend.history.clear()
        await _spin(10)

        assert backend.history, "the shield renderer paints again after the overlay"
        assert all(_is_shield_frame(c) or c == OFF for c in backend.history), backend.history
        assert PLAYER not in backend.history  # no restore flash between overlay and shield
        await manager.end_shield_effect(SERIAL)
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-010"])
    async def test_base_colour_change_keeps_the_shield_and_is_restored_after(self, manager, backend):
        """UC-040 BR-010: a new base colour does not end the shield; it is shown when the shield ends."""
        task = await _shield(manager, duration_ms=2000)

        await manager.apply_base_color(SERIAL, LOBBY, label="test")

        assert manager.shield_active(SERIAL) and not task.done()
        assert backend.colors[SERIAL] != LOBBY  # still painting the shield
        await task
        assert backend.colors[SERIAL] == LOBBY

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A16", br=["BR-009", "BR-010"])
    async def test_toughness_boost_resumes_after_the_full_shield_ends(self, manager, backend):
        """UC-040 BR-010: full takes precedence over toughness_boost; the toughness_boost resumes when full ends."""
        task = await _shield(manager, effect=controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST, duration_ms=10_000)
        await _shield(manager, duration_ms=2000)
        assert manager._shield_tasks[SERIAL] is task
        for _ in range(10_000):  # fake time runs out the 2 s full shield
            if "full" not in manager._shield_layers[SERIAL]:
                break
            await asyncio.sleep(0)
        assert "full" not in manager._shield_layers[SERIAL]
        backend.history.clear()
        await _spin(10)

        assert backend.history and max(c[0] for c in backend.history) <= 128  # toughness_boost frames only
        assert set(manager._shield_layers[SERIAL]) == {"toughness_boost"}
        await task
        assert backend.colors[SERIAL] == PLAYER

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_toughness_boost_zero_ends_only_the_toughness_boost_layer(self, manager, clock):
        """UC-040 A17: TOUGHNESS_BOOST 0 ends the toughness_boost layer; a full shield keeps painting."""
        clock.enable_blocking()
        await _shield(manager, effect=controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST, duration_ms=10_000)
        await _shield(manager, duration_ms=10_000)

        await manager.handle_game_effect(
            serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_TOUGHNESS_BOOST, duration_ms=0
        )

        assert set(manager._shield_layers[SERIAL]) == {"full"}
        clock.disable_blocking()
        await manager.end_shield_effect(SERIAL)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_death_during_an_overlay_ends_everything(self, manager, backend):
        """UC-040 A17: shield + warning overlay + death: black at the end, no shield, no task."""
        await _shield(manager, duration_ms=10_000)
        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING)
        overlay = manager.active_effects[SERIAL].task

        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
        fade = manager.active_effects.get(SERIAL)
        if fade is not None:
            await fade.task
        await _spin(20)

        assert overlay.done()
        assert backend.colors[SERIAL] == OFF
        assert not manager.shield_active(SERIAL)
        assert SERIAL not in manager._shield_tasks

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_winner_rainbow_ends_the_shield(self, manager, clock):
        """UC-040 A17: the winner rainbow replaces the shield."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)
        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW)
        assert task.cancelled()
        assert not manager.shield_active(SERIAL)
        clock.disable_blocking()
        rainbow = manager.active_effects.get(SERIAL)
        if rainbow is not None:
            await rainbow.task

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A17", br=["BR-010"])
    async def test_disconnect_cancels_the_shield_task(self, manager, clock):
        """UC-040 A17: a disconnected controller leaves no shield task behind."""
        clock.enable_blocking()
        task = await _shield(manager, duration_ms=10_000)

        manager.tracked_controllers.pop(SERIAL)  # the discovery loop drops it first
        manager.clear_controller(SERIAL)
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert task.cancelled()
        assert SERIAL not in manager._shield_tasks
        assert not manager.shield_active(SERIAL)
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
        cache = StateBuilder(_Monitoring())
        cache.set_led_colors(manager.current_colors)
        info = {"battery": 5, "team": 1}

        clock.enable_blocking()
        await _shield(manager, duration_ms=5000)
        first = cache.build_state(SERIAL, info)
        assert (first.color.r, first.color.g, first.color.b) == WHITE

        await manager.handle_game_effect(serial=SERIAL, effect=controller_manager_pb2.GAME_EFFECT_SHIELD, duration_ms=0)
        restored = cache.build_state(SERIAL, info)
        assert (restored.color.r, restored.color.g, restored.color.b) == PLAYER


class TestUC040HelpCueBeforeTheShield:
    """UC-040 A42 step 2 (#2395): the helped cue pulse, sent just before the shield, shows first."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_pulse_then_shield_shows_the_pulse_first_then_the_shield(self, manager, backend):
        magenta = (255, 0, 192)
        ok = await manager.handle_game_effect(
            serial=SERIAL,
            effect=controller_manager_pb2.GAME_EFFECT_PULSE,
            color=magenta,
            duration_ms=500,
            speed=4,
        )
        assert ok
        pulse = manager.active_effects[SERIAL].task
        await _shield(manager, duration_ms=5000)
        await _spin(5)

        # While the pulse runs the shield layer waits under it: only scaled magenta is painted.
        assert not pulse.done()
        assert backend.history, "the pulse paints"
        assert not any(_is_shield_frame(c) and c != OFF for c in backend.history), backend.history
        assert manager.shield_active(SERIAL)

        await pulse
        pulse_end = len(backend.history)
        await _spin(10)
        after = backend.history[pulse_end:]
        assert after, "the shield renderer paints once the pulse ends"
        assert all(_is_shield_frame(c) or c == OFF for c in after), after
        assert PLAYER not in after  # no restore flash between the cue and the shield
        await manager.end_shield_effect(SERIAL)
