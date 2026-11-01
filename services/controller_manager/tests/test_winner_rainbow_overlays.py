"""
The winner rainbow plays in full (UC-020 step 6, BR-004, #2533).

Pi session 2026-11-01 (hold mode): a player held the trigger past empty and died, the other
player won, and the winner's rainbow was not visible. The coordinator ends the hold layers
(``SHIELD`` 0) before the rainbow, so the shield cleanup is not what removes it; a short
overlay that lands during the rainbow is: the #818 agent cue (a magenta PULSE to every
alive player, so the winner) or an agent ``send_controller_effect`` flash took the LED over
and restored the base colour. A PULSE or FLASH now yields to a running winner rainbow.
"""

import asyncio
import contextlib
import time

import pytest

from proto import controller_manager_pb2 as pb
from services.controller_manager import feedback_manager as fm_module
from services.controller_manager import shield_effects as shield_module
from services.controller_manager.effects_base import ChargeCues
from services.controller_manager.feedback_manager import FeedbackManager
from services.controller_manager.tests.test_shield_hold_effect import _cues, _state

LOSER, WINNER = "LOSER001", "WINNER01"
LOSER_COLOR, WINNER_COLOR = (200, 0, 0), (0, 200, 0)
MAGENTA = (255, 0, 192)
WHITE = (255, 255, 255)
RAINBOW_MS = 600


class _Backend:
    """Records every LED write with the real time: the effects and the renderer run on the real clock."""

    def __init__(self):
        self.leds: list[tuple[float, str, tuple[int, int, int]]] = []
        self.rumble: dict[str, list[int]] = {}
        self.effect_active: dict[str, bool] = {}

    async def set_led_color(self, serial, r, g, b):
        self.leds.append((time.time(), serial, (r, g, b)))
        return True

    async def set_rumble(self, serial, intensity):
        self.rumble.setdefault(serial, []).append(intensity)
        return True

    def set_effect_active(self, serial, active):
        self.effect_active[serial] = active

    def frames(self, serial, since):
        return [color for t, s, color in self.leds if s == serial and t >= since - 1e-9]


@pytest.fixture
def backend():
    return _Backend()


@pytest.fixture
def manager(backend, monkeypatch):
    monkeypatch.setattr(shield_module, "read_charge_cues", lambda: ChargeCues(intensity=0))
    monkeypatch.setattr(fm_module, "get_winner_rainbow_duration_ms", lambda: RAINBOW_MS)
    # The death effect's red hold and fade run on real time: keep them short.
    monkeypatch.setattr(fm_module, "DEATH_RUMBLE_MS", 50)
    monkeypatch.setattr(fm_module, "DEATH_RED_HOLD_MS", 10)
    monkeypatch.setattr(fm_module, "DEATH_FADE_MS", 20)
    fm = FeedbackManager(backend=backend, tracked_controllers={LOSER: {"name": "P1"}, WINNER: {"name": "P2"}})
    fm.base_colors[LOSER] = LOSER_COLOR
    fm.base_colors[WINNER] = WINNER_COLOR
    yield fm
    for serial in (LOSER, WINNER):
        fm.clear_controller(serial)


async def _effect(manager, serial, effect, duration_ms=0, state=None, color=None):
    assert await manager.handle_game_effect(
        serial=serial, effect=effect, duration_ms=duration_ms, shield_hold=state, color=color
    )


async def _run(seconds):
    """Let the renderers and effects run (real clock: the timings are short)."""
    await asyncio.sleep(seconds)


def _hold(**kwargs):
    # Ramp mode, the shipped default (hold_rumble_max_intensity 200, #2463).
    return _state(cues=_cues(hold_rumble_max_intensity=200, ending_lead_ms=0), **kwargs)


async def _hold_past_empty_then_death(manager, winner_holds):
    """The coordinator's commands up to the win (UC-040 A39): the loser holds past empty and dies."""
    await _effect(manager, LOSER, pb.GAME_EFFECT_SHIELD_ENERGY, 5000, _hold(energy=600))
    await _effect(manager, WINNER, pb.GAME_EFFECT_SHIELD_ENERGY, 5000, _hold(energy=2000))
    await _run(0.2)
    await _effect(manager, LOSER, pb.GAME_EFFECT_SHIELD_HOLD, 600, _hold(energy=600))
    if winner_holds:
        await _effect(manager, WINNER, pb.GAME_EFFECT_SHIELD_HOLD, 2000, _hold(energy=2000))
    await _run(0.6)
    await _effect(manager, LOSER, pb.GAME_EFFECT_SHIELD_HOLD, 0)
    await _effect(manager, LOSER, pb.GAME_EFFECT_SHIELD_ENERGY, 5000, _hold(energy=0, cue=pb.SHIELD_HOLD_CUE_EMPTY))
    await _run(0.05)
    death = asyncio.create_task(_effect(manager, LOSER, pb.GAME_EFFECT_PLAYER_DEATH))
    await _run(0.1)
    await death


async def _game_end(manager):
    """Teardown: SHIELD 0 to every player that had a hold command, then the winner rainbow (UC-020 step 6)."""
    await _effect(manager, LOSER, pb.GAME_EFFECT_SHIELD, 0)
    await _effect(manager, WINNER, pb.GAME_EFFECT_SHIELD, 0)
    await _effect(manager, WINNER, pb.GAME_EFFECT_WINNER_RAINBOW)


def _rainbow_frames(frames):
    return [c for c in frames if c not in (WINNER_COLOR, MAGENTA, WHITE, (0, 0, 0))]


class TestUC020WinnerRainbowAfterHoldDeath:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    @pytest.mark.uc("UC-040", flow="A39", br=["BR-026"])
    @pytest.mark.parametrize("winner_holds", [False, True])
    @pytest.mark.parametrize("overlay", [pb.GAME_EFFECT_PULSE, pb.GAME_EFFECT_FLASH])
    async def test_an_agent_overlay_during_the_rainbow_does_not_cut_it(self, manager, backend, winner_holds, overlay):
        """#2533: hold past empty, death, win; an agent cue during the rainbow leaves it running to its end.

        The #2462 / #2470 cleanup still holds on both controllers: no layer, no hold rumble.
        """
        await _hold_past_empty_then_death(manager, winner_holds)
        await _game_end(manager)
        rainbow_start = time.time()
        await _run(0.3)
        await _effect(manager, WINNER, overlay, color=MAGENTA)  # the #818 agent cue lands mid-rainbow
        await _run(RAINBOW_MS / 1000.0)  # the rest of the rainbow and its restore

        frames = backend.frames(WINNER, rainbow_start)
        assert MAGENTA not in frames
        assert WHITE not in frames
        # 20 Hz for the whole duration, then the base colour once it ends.
        assert len(_rainbow_frames(frames)) >= int(RAINBOW_MS / 50) - 1
        assert frames[-1] == WINNER_COLOR
        assert WINNER_COLOR not in frames[1:-1]  # the base colour shows only before and after the rainbow
        assert backend.effect_active[WINNER] is False
        for serial in (LOSER, WINNER):
            assert not manager.shield_active(serial)
            assert serial not in manager._background_rumble
            assert serial not in manager._hold_cue_tasks and serial not in manager._hold_ending_tasks
        assert backend.rumble[LOSER][-1] == 0
        assert backend.rumble.get(WINNER, [0])[-1] == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    @pytest.mark.parametrize("overlay", [pb.GAME_EFFECT_PULSE, pb.GAME_EFFECT_FLASH])
    async def test_an_overlay_after_the_rainbow_plays(self, manager, backend, overlay):
        """UC-020 BR-004: only a running rainbow is protected; once it ended a pulse or flash shows again."""
        await _effect(manager, WINNER, pb.GAME_EFFECT_WINNER_RAINBOW)
        await _run(RAINBOW_MS / 1000.0 + 0.1)
        since = time.time()
        await _effect(manager, WINNER, overlay, color=MAGENTA, duration_ms=200)
        await _run(0.1)

        assert any(c != WINNER_COLOR and c != (0, 0, 0) for c in backend.frames(WINNER, since))
        effect = manager.active_effects.get(WINNER)
        if effect is not None:
            effect.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await effect.task

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
    async def test_the_death_effect_still_replaces_a_rainbow(self, manager, backend):
        """UC-020 BR-004: only the short overlays yield; a lifecycle effect such as the death still takes over."""
        await _effect(manager, WINNER, pb.GAME_EFFECT_WINNER_RAINBOW)
        await _run(0.2)
        death = asyncio.create_task(_effect(manager, WINNER, pb.GAME_EFFECT_PLAYER_DEATH))
        await _run(0.1)
        await death
        await _run(0.2)

        assert (255, 0, 0) in backend.frames(WINNER, 0.0)
        assert backend.leds[-1][2] == (0, 0, 0)
