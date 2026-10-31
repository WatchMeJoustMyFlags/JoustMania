"""
Shadow games and the simulated trigger policy (#2096 PR-7a).

UC-040 BR-019: a shadow game honours ``shield.activation`` trigger and hold only
when its pattern controllers press the trigger (``pattern_trigger_policy``,
UC-046 BR-018); otherwise it stays instant. UC-035 BR-008: a shadow hold game
publishes no ``player_shield_energy`` event (the agent's gauges are still set).
UC-030 BR-005: a policy press held from the lobby or warm-up is not a press.
"""

import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode, Player
from services.game_coordinator.games.ffa import FFAGame

T0 = 1000.0
DT = 1.0 / 30.0


class Clock:
    def __init__(self):
        self.t = T0


class Flags:
    def __init__(self, activation):
        self.values = {
            "shield.activation": activation,
            "shield.charge_expiry_seconds": 20.0,
            "shield.energy_max_seconds": 5.0,
            "shield.energy_budget_seconds": 15.0,
            "shield.energy_initial_seconds": 2.0,
            "shield.recharge_per_second": 0.0,
            "shield.min_burst_seconds": 0.3,
        }
        self.reads: list[str] = []

    def read(self, _set, key, default, **_kw):
        self.reads.append(key)
        return self.values.get(key, default)


class RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)


@pytest.fixture(autouse=True)
def _no_shield_limits():
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


@pytest.fixture
def clock():
    c = Clock()
    # The game clock drives the deadlines (UC-030 BR-010); the wall clock the wire timestamps.
    with (
        patch.object(base_module.time, "time", side_effect=lambda: c.t),
        patch.object(game_clock, "now", side_effect=lambda: c.t),
    ):
        yield c


@pytest.fixture
def events():
    return EventCollector()


@contextlib.contextmanager
def patched(flags):
    with (
        patch.object(base_module, "read_string_flag", side_effect=flags.read),
        patch.object(base_module, "read_float_flag", side_effect=flags.read),
    ):
        yield


def _game(events, *, kind="shadow", policy=False):
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_shadow_policy",
    )
    g.game_kind = kind
    g.pattern_trigger_policy = policy
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    return g


async def _step(game, clock, serial, trigger):
    clock.t += DT
    await game._process_controller_state(
        controller_manager_pb2.GameplayData(
            serial=serial, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0), trigger_pressed=trigger
        )
    )


class TestUC040BR019ShadowPolicy:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019"])
    @pytest.mark.parametrize("activation", ["trigger", "hold"])
    async def test_shadow_game_without_policy_stays_instant(self, clock, events, activation):
        """BR-019: no pattern_trigger_policy -> instant whatever the flag; the flag is not even read."""
        flags = Flags(activation)
        with patched(flags):
            g = _game(events, policy=False)
            assert g._latch_shield_hold() is None
            assert g._shield_activation_mode() == "instant"
            with patch.object(g, "_play_sound", new_callable=AsyncMock):
                assert await g.grant_shield("p1", 4.0) is True
        assert g.players["p1"].grace_until == pytest.approx(clock.t + 4.0)
        assert g.players["p1"].shield_charge_seconds == 0.0
        assert "shield.activation" not in flags.reads

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019"])
    async def test_shadow_game_with_policy_honours_trigger(self, clock, events):
        """BR-019: with pattern_trigger_policy a trigger read makes the grant a charge."""
        flags = Flags("trigger")
        with patched(flags):
            g = _game(events, policy=True)
            assert g._latch_shield_hold() is None
            assert g._shield_activation_mode() == "trigger"
            with patch.object(g, "_play_sound", new_callable=AsyncMock):
                assert await g.grant_shield("p1", 4.0) is True
        assert g.players["p1"].shield_charge_seconds == 4.0
        assert g.players["p1"].grace_until < clock.t

    @pytest.mark.uc("UC-040", flow="A36", br=["BR-019", "BR-027"])
    def test_shadow_game_with_policy_honours_hold(self, clock, events):  # noqa: ARG002 - clock patches time
        """BR-019 / BR-027: with pattern_trigger_policy a hold read latches the game to hold."""
        flags = Flags("hold")
        with patched(flags):
            g = _game(events, policy=True)
            hold = g._latch_shield_hold()
        assert hold is not None
        assert g._shield_activation_mode() == "hold"

    @pytest.mark.uc("UC-040", flow="A36", br=["BR-019"])
    def test_policy_attribute_changes_nothing_in_another_mode(self, clock, events):  # noqa: ARG002
        """BR-019: the allow-list still applies; only Free-for-All honours trigger or hold."""
        flags = Flags("hold")
        with patched(flags):
            g = _game(events, policy=True)
            g.SHIELD_TRIGGER_ALLOWED = False
            assert g._latch_shield_hold() is None
            assert g._shield_activation_mode() == "instant"


class TestUC035BR008ShadowEnergy:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-035", flow="main", br=["BR-008"])
    @pytest.mark.parametrize(("kind", "published"), [("shadow", 0), ("primary", 1)])
    async def test_shadow_hold_game_publishes_no_energy_event(self, clock, events, kind, published):
        """UC-035 BR-008: a shadow hold game publishes no player_shield_energy; its agent gauges are still set."""
        flags = Flags("hold")
        with patched(flags):
            g = _game(events, kind=kind, policy=True)
            assert g._latch_shield_hold() is not None
            g._start_shield_hold_clock(clock.t)
            with patch.object(g, "_set_hold_gauges") as gauges:
                await g._publish_hold_energy("p1", False)
        assert len(events.get_events_of_type(GameEvent.PLAYER_SHIELD_ENERGY)) == published
        gauges.assert_called_once()


class TestUC030BR005PolicyPressFromTheLobby:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="A6", br=["BR-005"])
    @pytest.mark.uc("UC-046", flow="main", br=["BR-018"])
    async def test_window_held_from_the_lobby_is_not_a_press(self, clock, events):
        """A policy window that is on when the game starts (or that continues across a stream
        reconnect, which keeps the coordinator's edge state) is no press; release, then press is."""
        flags = Flags("trigger")
        with patched(flags):
            g = _game(events, policy=True)
            g._latch_shield_hold()
            with patch.object(g, "_play_sound", new_callable=AsyncMock):
                await g.grant_shield("p1", 4.0)
                for _ in range(6):
                    await _step(g, clock, "p1", True)
                assert g.players["p1"].shield_charge_seconds == 4.0
                await _step(g, clock, "p1", False)
                await _step(g, clock, "p1", True)
        assert g.players["p1"].shield_charge_seconds == 0.0
        assert g.players["p1"].grace_until > clock.t
