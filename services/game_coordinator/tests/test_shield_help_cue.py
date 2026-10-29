"""The "helped" cue of an agent shield (#2395, UC-040 A42, BR-031).

An applied ``grant_shield`` (the agent's grant: the only caller is the
``shield_seconds`` intervention handler) pulses the helped controller magenta
(``GAME_EFFECT_PULSE``, 500 ms at speed 4) BEFORE the shield's own LED command,
in all three activation modes: instant (``GAME_EFFECT_SHIELD``), trigger
(``GAME_EFFECT_SHIELD_CHARGE``) and hold (``GAME_EFFECT_SHIELD_ENERGY``). A
grant that applies nothing (refused, kept, dead player) sends no cue, and a
toughness boost or a player's own hold activation never sends it.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.colors import Colors
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.shields import (
    SHIELD_HELP_CUE_DURATION_MS,
    SHIELD_HELP_CUE_SPEED,
)
from services.game_coordinator.intervention_errors import InterventionNotApplied

PULSE = controller_manager_pb2.GAME_EFFECT_PULSE
SHIELD = controller_manager_pb2.GAME_EFFECT_SHIELD
SHIELD_CHARGE = controller_manager_pb2.GAME_EFFECT_SHIELD_CHARGE
SHIELD_ENERGY = controller_manager_pb2.GAME_EFFECT_SHIELD_ENERGY
T0 = 1000.0


class RecordingStream:
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail

    async def write(self, message):
        if self.fail:
            raise RuntimeError("stream closed")
        self.writes.append(message)

    def commands(self):
        return [m.game_effect for m in self.writes if m.HasField("game_effect")]

    def effects(self):
        return [(c.serial, c.effect) for c in self.commands()]


class Flags:
    def __init__(self, activation):
        self.values = {
            "shield.activation": activation,
            "shield.charge_expiry_seconds": 20.0,
            "shield.energy_max_seconds": 5.0,
            "shield.energy_budget_seconds": 15.0,
            "shield.energy_initial_seconds": 0.0,
            "shield.recharge_per_second": 0.0,
            "shield.min_burst_seconds": 0.3,
        }

    def read(self, _set, key, default, **_kw):
        return self.values.get(key, default)


def _make_game(activation, limits=(0, 0)):
    flags = Flags(activation)
    patches = [
        patch.object(BaseGameMode, "_shield_limits", return_value=limits),
        patch.object(base_module, "read_string_flag", side_effect=flags.read),
        patch.object(base_module, "read_float_flag", side_effect=flags.read),
        patch.object(game_clock, "now", return_value=T0),
    ]
    for p in patches:
        p.start()
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=f"test_shield_help_cue_{activation}",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    g._play_sound = AsyncMock()
    return g, patches


@pytest.fixture
def make_game():
    started = []

    def factory(activation, limits=(0, 0)):
        g, patches = _make_game(activation, limits)
        started.extend(patches)
        return g

    yield factory
    for p in reversed(started):
        p.stop()


def _assert_help_cue(command, serial):
    assert (command.serial, command.effect) == (serial, PULSE)
    assert (command.color.r, command.color.g, command.color.b) == Colors.Magenta.value
    assert command.duration_ms == SHIELD_HELP_CUE_DURATION_MS
    assert command.speed == SHIELD_HELP_CUE_SPEED


class TestUC040A42HelpCue:
    """A42: an applied agent shield pulses the helped controller before the shield colour."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_instant_grant_pulses_before_the_shield_layer(self, make_game):
        game = make_game("instant")
        assert await game.grant_shield("p1", 5.0) is True

        commands = game.gameplay_stream.commands()
        assert [(c.serial, c.effect) for c in commands] == [("p1", PULSE), ("p1", SHIELD)]
        _assert_help_cue(commands[0], "p1")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_trigger_grant_pulses_before_the_charge_layer(self, make_game):
        game = make_game("trigger")
        assert await game.grant_shield("p1", 5.0) is True

        commands = game.gameplay_stream.commands()
        assert [(c.serial, c.effect) for c in commands] == [("p1", PULSE), ("p1", SHIELD_CHARGE)]
        _assert_help_cue(commands[0], "p1")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_hold_grant_pulses_before_the_energy_layer(self, make_game):
        game = make_game("hold")
        game._latch_shield_hold()
        game._start_shield_hold_clock(T0)
        assert await game.grant_shield("p1", 2.0) is True
        # The hold feedback task sends the new energy after the grant returns.
        task = game._hold_feedback_task
        for _ in range(5):
            if any(e == ("p1", SHIELD_ENERGY) for e in game.gameplay_stream.effects()):
                break
            await asyncio.sleep(0)
        game._cancel_hold_feedback()
        assert task is not None

        effects = [e for e in game.gameplay_stream.effects() if e[0] == "p1"]
        assert effects[0] == ("p1", PULSE)
        assert ("p1", SHIELD_ENERGY) in effects[1:]
        _assert_help_cue(game.gameplay_stream.commands()[0], "p1")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_extension_of_a_running_shield_pulses_again(self, make_game):
        """Cooldown 0 (extend-not-shorten): each applied extension is a grant, so it is cued."""
        game = make_game("instant")
        await game.grant_shield("p1", 3.0)
        await game.grant_shield("p1", 8.0)

        assert game.gameplay_stream.effects() == [("p1", PULSE), ("p1", SHIELD), ("p1", PULSE), ("p1", SHIELD)]


class TestUC040A42NoCueWithoutAGrant:
    """A42 step 3: a grant that applies nothing, and every other shield path, sends no cue."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031", "BR-022"])
    async def test_refused_grant_sends_no_cue(self, make_game):
        game = make_game("instant", limits=(1, 0))
        await game.grant_shield("p1", 3.0)
        game.gameplay_stream.writes.clear()
        with pytest.raises(InterventionNotApplied):
            await game.grant_shield("p1", 8.0)
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031", "BR-020"])
    async def test_already_charged_sends_no_cue(self, make_game):
        game = make_game("trigger")
        await game.grant_shield("p1", 3.0)
        game.gameplay_stream.writes.clear()
        with pytest.raises(InterventionNotApplied):
            await game.grant_shield("p1", 3.0)
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_kept_grant_sends_no_cue(self, make_game):
        """A shorter grant inside a running longer shield adds nothing (kept): no cue, no layer."""
        game = make_game("instant")
        await game.grant_shield("p1", 8.0)
        game.gameplay_stream.writes.clear()
        assert await game.grant_shield("p1", 2.0) is True
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_dead_player_gets_no_cue(self, make_game):
        game = make_game("instant")
        game.players["p1"].alive = False
        assert await game.grant_shield("p1", 5.0) is False
        assert game.gameplay_stream.effects() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_toughness_boost_sends_no_cue(self, make_game):
        game = make_game("instant")
        assert await game.grant_toughness_boost("p1", 5.0) is True
        assert PULSE not in [e for _, e in game.gameplay_stream.effects()]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_failing_stream_never_fails_the_grant(self, make_game):
        """The cue is best-effort: a closed stream still grants the protection."""
        game = make_game("instant")
        game.gameplay_stream = RecordingStream(fail=True)
        assert await game.grant_shield("p1", 5.0) is True
        assert game.players["p1"].grace_until == pytest.approx(T0 + 5.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A42", br=["BR-031"])
    async def test_no_stream_still_grants(self, make_game):
        game = make_game("instant")
        game.gameplay_stream = None
        assert await game.grant_shield("p1", 5.0) is True
