"""Unit tests for the on-device agent-intervention cue (#818).

When the coordinator applies a HARD-class intervention it pulses an LED on the
affected controller(s) so an operator can tell agent activity from a
malfunction. The cue is gated behind the user.json ``agent_intervention_cue``
toggle and must be best-effort (never break the intervention path).
"""

from unittest.mock import AsyncMock

import pytest

from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    WEIGHT_HARD,
    InterventionManager,
)


class FakeFlagClient:
    def __init__(self, values=None):
        self.values = values or {}

    def get_boolean_value(self, key, default, _ctx=None):
        return bool(self.values.get(key, default))


class FakePlayer:
    def __init__(self, serial, alive=True):
        self.serial = serial
        self.alive = alive


class FakeStream:
    def __init__(self):
        self.writes = []

    async def write(self, cmd):
        self.writes.append(cmd)


class FakeGame:
    def __init__(self, players=None, stream=None):
        self.players = players or {}
        self.gameplay_stream = stream


def _spec(type_id):
    return next(s for s in INTERVENTION_SPECS if s.type_id == type_id)


def _manager(user_values=None):
    mgr = InterventionManager(
        event_publisher=AsyncMock(),
        get_game=lambda: None,
    )
    mgr._user_client = FakeFlagClient(user_values) if user_values is not None else None
    return mgr


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
def test_cue_enabled_defaults_true_without_client():
    mgr = _manager(user_values=None)
    assert mgr._cue_enabled() is True


def test_cue_enabled_reads_user_flag():
    assert _manager({"agent_intervention_cue": True})._cue_enabled() is True
    assert _manager({"agent_intervention_cue": False})._cue_enabled() is False


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_broadcast_cue_pulses_all_alive_players():
    from proto import controller_manager_pb2

    stream = FakeStream()
    game = FakeGame(
        players={"A": FakePlayer("A"), "B": FakePlayer("B"), "C": FakePlayer("C", alive=False)},
        stream=stream,
    )
    mgr = _manager({"agent_intervention_cue": True})

    # end_game is HARD and not player-targeted -> broadcast to alive players.
    await mgr._emit_agent_cue(_spec("end_game"), None, game)

    pulsed = {c.game_effect.serial for c in stream.writes}
    assert pulsed == {"A", "B"}  # C is dead
    assert all(c.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PULSE for c in stream.writes)


@pytest.mark.asyncio
async def test_player_targeted_cue_pulses_only_target():
    stream = FakeStream()
    game = FakeGame(players={"A": FakePlayer("A"), "B": FakePlayer("B")}, stream=stream)
    mgr = _manager({"agent_intervention_cue": True})

    # eliminate_player is HARD and player-targeted.
    await mgr._emit_agent_cue(_spec("eliminate_player"), "A", game)

    assert [c.game_effect.serial for c in stream.writes] == ["A"]


@pytest.mark.asyncio
async def test_cue_suppressed_when_muted():
    stream = FakeStream()
    game = FakeGame(players={"A": FakePlayer("A")}, stream=stream)
    mgr = _manager({"agent_intervention_cue": False})

    await mgr._emit_agent_cue(_spec("end_game"), None, game)

    assert stream.writes == []


@pytest.mark.asyncio
async def test_cue_no_stream_is_safe():
    game = FakeGame(players={"A": FakePlayer("A")}, stream=None)
    mgr = _manager({"agent_intervention_cue": True})
    # Should not raise.
    await mgr._emit_agent_cue(_spec("end_game"), None, game)


@pytest.mark.asyncio
async def test_cue_targeted_dead_serial_no_pulse():
    stream = FakeStream()
    game = FakeGame(players={"A": FakePlayer("A", alive=False)}, stream=stream)
    mgr = _manager({"agent_intervention_cue": True})

    await mgr._emit_agent_cue(_spec("eliminate_player"), "A", game)

    assert stream.writes == []


def test_hard_class_specs_are_the_cued_set():
    """Guard the cue trigger condition: exactly the HARD-weight specs."""
    hard = {s.type_id for s in INTERVENTION_SPECS if s.weight >= WEIGHT_HARD}
    assert hard == {
        "adjust_global_sensitivity",
        "eliminate_player",
        "revive_player",
        "end_game",
    }
