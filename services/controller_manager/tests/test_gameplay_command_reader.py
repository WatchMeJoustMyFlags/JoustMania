"""
The gameplay stream's command reader never waits on an effect hold, and a closing stream applies the
commands it already received (#2540, UC-020 BR-004, UC-030 step 9).

From the #2538 review: the controller manager applied ``StreamGameplayData`` commands one at a time
and the death effect held the reader for its 300 ms red hold. A backlog that outlived the coordinator's
stream close was dropped by the cancel of the reader in the stream's ``finally``, an end-of-game
WINNER_RAINBOW included. Also from that review: ``play_effect_with_restore`` left a gap between
removing the old effect and registering the new one, in which an effect from the other stream could
take the LED and be orphaned (and a pulse could slip past a starting rainbow).
"""

import asyncio
import contextlib
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_identify_controller import SERIAL, _game_config, servicer  # noqa: F401  (pytest fixture)

from proto import controller_manager_pb2 as pb
from services.controller_manager import feedback_manager as fm_module
from services.controller_manager import servicer as servicer_module
from services.controller_manager import shield_effects as shield_module
from services.controller_manager.effects_base import ActiveEffect, ChargeCues
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.tests.backend_doubles import stub_frame_inputs, stub_method
from services.controller_manager.tests.test_winner_rainbow_overlays import _Backend

LOSER, WINNER = SERIAL, "MOCK0002"
MAGENTA = (255, 0, 192)
LONG_RED_HOLD_MS = 5000  # far beyond every wait below: a reader blocked on it shows at once


class _Context:
    cancelled_flag = False

    def cancelled(self):
        return self.cancelled_flag


def _effect(serial, effect):
    return pb.GameplayStreamControl(game_effect=pb.GameEffectCommand(serial=serial, effect=effect))


def _base_color(serial, rgb):
    r, g, b = rgb
    return pb.GameplayStreamControl(base_color=pb.ControllerColorConfig(serial=serial, color=pb.RGB(r=r, g=g, b=b)))


class _GameStream:
    """A coordinator's game stream: the test feeds control messages and cancels the call (the close)."""

    def __init__(self, svc):
        self.svc = svc
        self.controls: asyncio.Queue = asyncio.Queue()
        self.context = _Context()
        self.task: asyncio.Task | None = None

    async def _requests(self):
        while True:
            yield await self.controls.get()

    async def _consume(self):
        async for _ in self.svc.StreamGameplayData(self._requests(), self.context):
            pass

    async def open(self):
        self.controls.put_nowait(pb.GameplayStreamControl(config=_game_config(LOSER, WINNER)))
        self.task = asyncio.create_task(self._consume())
        for _ in range(100):
            if self.svc._in_game(WINNER):
                return
            await asyncio.sleep(0.005)
        raise AssertionError("the game stream never claimed its roster")

    def send(self, control):
        self.controls.put_nowait(control)

    async def close(self):
        """The coordinator cancels the call: the frame loop sees it on its next frame."""
        self.context.cancelled_flag = True
        await asyncio.wait_for(self.task, timeout=5)


@pytest_asyncio.fixture
async def game(servicer, monkeypatch):  # noqa: F811
    stub_method(
        servicer.state_builder, "frame_inputs", side_effect=lambda serial, _info: stub_frame_inputs(serial=serial)
    )
    servicer.discovery_loop.drain_health_counters.return_value = (0, 0, 0)
    servicer.tracked_controllers[WINNER] = {"name": "P2", "battery": 5}
    monkeypatch.setattr(servicer_module, "STREAM_CLOSE_SHIELD_CLEANUP_DELAY_S", 0.01)
    monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", LONG_RED_HOLD_MS)
    monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 50)
    monkeypatch.setattr(fm_module, "get_winner_rainbow_duration_ms", lambda: 200)

    rainbows: list[tuple[str, float]] = []
    play_rainbow = servicer.feedback_manager._effect_winner_rainbow

    async def record_rainbow(serial, **kwargs):
        rainbows.append((serial, time.monotonic()))
        stream.rainbow_in_game.append(servicer._in_game(serial))
        await play_rainbow(serial=serial, **kwargs)

    servicer.feedback_manager._effect_winner_rainbow = record_rainbow
    stream = _GameStream(servicer)
    stream.rainbows = rainbows
    stream.rainbow_in_game = []
    yield stream
    if stream.task is not None and not stream.task.done():
        stream.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream.task
    for serial in (LOSER, WINNER):
        servicer.feedback_manager.clear_controller(serial)
    if servicer._shield_cleanup_tasks:
        await asyncio.gather(*servicer._shield_cleanup_tasks, return_exceptions=True)


class TestUC020CommandReaderDoesNotWaitOnEffects:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", steps=[9])
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_the_death_effect_does_not_delay_the_next_command(self, game):
        """#2540: the command after a death applies at once, not after the red hold."""
        await game.open()
        sent = time.monotonic()
        game.send(_effect(LOSER, pb.GAME_EFFECT_PLAYER_DEATH))
        game.send(_effect(WINNER, pb.GAME_EFFECT_WINNER_RAINBOW))
        for _ in range(100):
            if game.rainbows:
                break
            await asyncio.sleep(0.005)

        assert game.rainbows, "the rainbow waited behind the death effect's red hold"
        assert game.rainbows[0][1] - sent < 0.5 * LONG_RED_HOLD_MS / 1000.0
        # The red hold still runs, as the loser's effect task.
        death = game.svc.feedback_manager.active_effects[LOSER]
        assert death.effect_type == pb.GAME_EFFECT_PLAYER_DEATH and not death.task.done()
        await game.close()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_a_rainbow_queued_behind_a_death_still_plays_when_the_stream_closes(self, game):
        """#2540: the last kill, the win and the stream close in quick succession: the rainbow still plays."""
        await game.open()
        game.send(_effect(LOSER, pb.GAME_EFFECT_PLAYER_DEATH))
        game.send(_effect(WINNER, pb.GAME_EFFECT_WINNER_RAINBOW))
        await asyncio.sleep(0.1)
        await game.close()

        assert [serial for serial, _ in game.rainbows] == [WINNER]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_stream_close_applies_a_received_end_of_game_command(self, game):
        """#2540: a command received while an earlier one is still applying is applied at the close, not dropped."""
        apply_base_color = game.svc.feedback_manager.apply_base_color

        async def slow_apply(serial, color, label=""):
            await asyncio.sleep(0.3)  # a slow backend write
            await apply_base_color(serial, color, label=label)

        game.svc.feedback_manager.apply_base_color = slow_apply
        await game.open()
        game.send(_base_color(LOSER, (0, 0, 0)))
        game.send(_effect(WINNER, pb.GAME_EFFECT_WINNER_RAINBOW))
        await asyncio.sleep(0.05)
        await game.close()  # while the base colour is still applying

        assert [serial for serial, _ in game.rainbows] == [WINNER]
        # #2470 order: the drained command applies while the stream still holds its roster ...
        assert game.rainbow_in_game == [True]
        assert game.svc._game_stream_serials == {}  # ... and the roster is released after the drain

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_the_drain_is_bounded_even_when_a_command_ignores_its_cancel(self, game):
        """#2540: the close goes on after about 1 s (drain bound plus cancel grace), whatever the command does."""

        async def stubborn():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(3)  # swallows the cancel, like a cancel-unsafe await

        apply_task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        started = time.monotonic()
        await game.svc._drain_gameplay_controls(apply_task, asyncio.Queue(), "gameplay_stream_test")
        elapsed = time.monotonic() - started

        bound = servicer_module.STREAM_CLOSE_DRAIN_TIMEOUT_S + servicer_module.STREAM_CLOSE_CANCEL_GRACE_S
        assert elapsed < bound + 0.4, f"the drain took {elapsed:.2f} s"
        apply_task.cancel()
        await asyncio.gather(apply_task, return_exceptions=True)


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(shield_module, "read_charge_cues", lambda: ChargeCues(intensity=0))
    monkeypatch.setattr(fm_module, "get_winner_rainbow_duration_ms", lambda: 300)
    backend = _Backend()
    fm = FeedbackManager(backend=backend, tracked_controllers={WINNER: {"name": "P2"}})
    fm.base_colors[WINNER] = (0, 200, 0)
    yield fm
    fm.clear_controller(WINNER)


async def _slow_to_cancel(gate: asyncio.Event):
    """An effect whose cancellation takes a while (its finally awaits), so its replacement waits for it."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await gate.wait()
        raise


class TestUC020EffectTakeOverHasNoGap:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_a_pulse_from_the_other_stream_cannot_slip_in_while_the_rainbow_takes_over(self, manager):
        """#2540: while the rainbow waits for the old effect to end, the LED is already the rainbow's.

        A pulse that lands in that window yields to the rainbow (#2538) instead of taking the LED
        and being orphaned by the rainbow's registration.
        """
        gate = asyncio.Event()
        old = asyncio.create_task(_slow_to_cancel(gate))
        manager.active_effects[WINNER] = ActiveEffect(task=old, effect_type=pb.GAME_EFFECT_FLASH)
        await asyncio.sleep(0)

        rainbow = asyncio.create_task(manager.handle_game_effect(WINNER, pb.GAME_EFFECT_WINNER_RAINBOW))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not rainbow.done(), "the rainbow must still be waiting for the old effect to end"
        pulses = []
        play_pulse = manager._game_effect_pulse

        async def record_pulse(**kwargs):
            pulses.append(kwargs["serial"])
            await play_pulse(**kwargs)

        manager._game_effect_pulse = record_pulse
        assert await manager.handle_game_effect(WINNER, pb.GAME_EFFECT_PULSE, color=MAGENTA)
        gate.set()
        assert await rainbow
        await asyncio.sleep(0.1)

        assert pulses == [], "the pulse took the LED from the starting rainbow"
        running = manager.active_effects[WINNER]
        assert running.effect_type == pb.GAME_EFFECT_WINNER_RAINBOW and not running.task.done()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_an_effect_started_in_the_take_over_window_is_never_orphaned(self, manager):
        """#2540: two effects started back to back from two streams leave one LED owner, the newer one."""
        gate = asyncio.Event()
        old = asyncio.create_task(_slow_to_cancel(gate))
        manager.active_effects[WINNER] = ActiveEffect(task=old, effect_type=pb.GAME_EFFECT_FLASH)
        await asyncio.sleep(0)
        live_before = set(asyncio.all_tasks())

        first = asyncio.create_task(
            manager.play_effect_with_restore(WINNER, "flash", (255, 255, 255), 500, 5, (0, 200, 0))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        await manager.play_effect_with_restore(WINNER, "flash", MAGENTA, 500, 5, (0, 200, 0))
        gate.set()
        await first
        await asyncio.sleep(0.05)

        owner = manager.active_effects[WINNER].task
        effect_tasks = {t for t in asyncio.all_tasks() - live_before if not t.done() and t is not first}
        effect_tasks.discard(asyncio.current_task())
        assert effect_tasks == {owner}, "an effect task runs that no active_effects entry owns"
        assert manager.backend.frames(WINNER, 0.0)[-1] in (MAGENTA, (0, 0, 0))  # the newer flash paints


class TestUC030DeathIsShownInFull:
    @pytest.fixture
    def dying(self, manager, monkeypatch):
        monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 50)
        monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 150)
        monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 50)
        return manager

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", steps=[9])
    @pytest.mark.parametrize(
        "follow_up",
        [pb.GAME_EFFECT_PULSE, pb.GAME_EFFECT_FLASH, pb.GAME_EFFECT_SHOW_BATTERY, pb.GAME_EFFECT_PLAYER_WARNING],
    )
    async def test_an_effect_right_behind_the_death_does_not_hide_the_red(self, dying, follow_up):
        """#2540: a Zombie conversion plus the agent cue (a pulse), or a warning on the next frame, right
        behind the death: the red is painted at once and the death plays in full; the follow-up yields."""
        since = time.time()
        assert await dying.handle_game_effect(WINNER, pb.GAME_EFFECT_PLAYER_DEATH)
        await dying.handle_game_effect(WINNER, follow_up, color=MAGENTA)

        death = dying.active_effects[WINNER]
        assert death.effect_type == pb.GAME_EFFECT_PLAYER_DEATH and not death.task.done()
        await death.task
        frames = dying.backend.frames(WINNER, since)
        assert frames[0] == (255, 0, 0)
        assert MAGENTA not in frames and (255, 255, 255) not in frames
        assert frames[-1] == (0, 0, 0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", steps=[9])
    async def test_the_death_still_gives_way_to_the_winner_rainbow(self, dying):
        """UC-020 BR-004: only overlays and the warning yield; a lifecycle effect such as the rainbow takes over."""
        assert await dying.handle_game_effect(WINNER, pb.GAME_EFFECT_PLAYER_DEATH)
        assert await dying.handle_game_effect(WINNER, pb.GAME_EFFECT_WINNER_RAINBOW)
        assert dying.active_effects[WINNER].effect_type == pb.GAME_EFFECT_WINNER_RAINBOW


class TestLowBatteryDoesNotHoldTheReader:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_low_battery_returns_at_once_and_restores_the_base_colour(self, manager):
        """#2540: the 600 ms low-battery flash runs as the effect task and restores the base colour."""
        since = time.time()
        started = time.monotonic()
        assert await manager.handle_game_effect(WINNER, pb.GAME_EFFECT_LOW_BATTERY)
        assert time.monotonic() - started < 0.3

        await manager.active_effects[WINNER].task
        frames = manager.backend.frames(WINNER, since)
        assert (255, 0, 0) in frames
        assert frames[-1] == (0, 200, 0)
        assert WINNER not in manager.active_effects


class TestStartEffectIsCancelSafe:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_a_cancel_while_waiting_for_the_lock_leaves_no_claim(self, manager):
        """#2540: a caller cancelled before its effect starts leaves no owner that would block the base colour."""
        gate = asyncio.Event()
        old = asyncio.create_task(_slow_to_cancel(gate))
        manager.active_effects[WINNER] = ActiveEffect(task=old, effect_type=pb.GAME_EFFECT_FLASH)
        await asyncio.sleep(0)
        starter = asyncio.create_task(
            manager.play_effect_with_restore(WINNER, "flash", (255, 255, 255), 500, 5, (0, 200, 0))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        await manager.effect_lock.acquire()
        gate.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert old.done() and not starter.done()  # the starter now waits for the lock
        starter.cancel()
        manager.effect_lock.release()
        with pytest.raises(asyncio.CancelledError):
            await starter

        assert WINNER not in manager.active_effects
        assert manager.backend.effect_active[WINNER] is False
        await manager.apply_base_color(WINNER, (1, 2, 3))
        assert manager.backend.leds[-1][1:] == (WINNER, (1, 2, 3))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_a_cancel_while_the_old_effect_ends_is_not_swallowed(self, manager):
        """#2540: the drain's cancel stops the command; it is not taken for the old effect's own cancel."""
        gate = asyncio.Event()
        old = asyncio.create_task(_slow_to_cancel(gate))
        manager.active_effects[WINNER] = ActiveEffect(task=old, effect_type=pb.GAME_EFFECT_FLASH)
        await asyncio.sleep(0)
        starter = asyncio.create_task(
            manager.play_effect_with_restore(WINNER, "flash", (255, 255, 255), 500, 5, (0, 200, 0))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not starter.done()  # waiting for the old effect to end
        starter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starter

        assert WINNER not in manager.active_effects
