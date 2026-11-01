"""Unit tests for the on-device agent-intervention cue (#818).

When the coordinator applies a HARD-class intervention it pulses an LED on the
affected controller(s) so an operator can tell agent activity from a
malfunction. The cue is gated behind the user.json ``agent_intervention_cue``
toggle and must be best-effort (never break the intervention path).
"""

from unittest.mock import AsyncMock

import pytest

from services.game_coordinator.games.base import Player
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    WEIGHT_HARD,
    InterventionManager,
)
from services.game_coordinator.tests.conftest import FakeInterventionGame


class FakeFlagClient:
    def __init__(self, values=None):
        self.values = values or {}

    def get_boolean_value(self, key, default, _ctx=None):
        return bool(self.values.get(key, default))


class FakeStream:
    def __init__(self):
        self.writes = []

    async def write(self, cmd):
        self.writes.append(cmd)


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
@pytest.mark.uc("UC-040", flow="main", steps=[13])
def test_cue_enabled_defaults_true_without_client():
    mgr = _manager(user_values=None)
    assert mgr._cue_enabled() is True


@pytest.mark.uc("UC-040", flow="main", steps=[13])
def test_cue_enabled_reads_user_flag():
    assert _manager({"agent_intervention_cue": True})._cue_enabled() is True
    assert _manager({"agent_intervention_cue": False})._cue_enabled() is False


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_broadcast_cue_pulses_all_alive_players():
    from proto import controller_manager_pb2

    stream = FakeStream()
    game = FakeInterventionGame(
        players={"A": Player("A"), "B": Player("B"), "C": Player("C", alive=False)},
        gameplay_stream=stream,
    )
    mgr = _manager({"agent_intervention_cue": True})

    # end_game is HARD and not player-targeted -> broadcast to alive players.
    await mgr._emit_agent_cue(_spec("end_game"), None, game)

    pulsed = {c.game_effect.serial for c in stream.writes}
    assert pulsed == {"A", "B"}  # C is dead
    assert all(c.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PULSE for c in stream.writes)


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.uc("UC-031", flow="main", br=["BR-002"])
@pytest.mark.asyncio
async def test_cue_pulse_is_magenta():
    """#2426: the #818 cue carries magenta (255,0,192), same as the shield help cue, not the default white."""
    stream = FakeStream()
    game = FakeInterventionGame(players={"A": Player("A")}, gameplay_stream=stream)
    mgr = _manager({"agent_intervention_cue": True})

    await mgr._emit_agent_cue(_spec("end_game"), None, game)

    assert len(stream.writes) == 1
    color = stream.writes[0].game_effect.color
    assert (color.r, color.g, color.b) == (255, 0, 192)


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_player_targeted_cue_pulses_only_target():
    stream = FakeStream()
    game = FakeInterventionGame(players={"A": Player("A"), "B": Player("B")}, gameplay_stream=stream)
    mgr = _manager({"agent_intervention_cue": True})

    # eliminate_player is HARD and player-targeted.
    await mgr._emit_agent_cue(_spec("eliminate_player"), "A", game)

    assert [c.game_effect.serial for c in stream.writes] == ["A"]


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_cue_suppressed_when_muted():
    stream = FakeStream()
    game = FakeInterventionGame(players={"A": Player("A")}, gameplay_stream=stream)
    mgr = _manager({"agent_intervention_cue": False})

    await mgr._emit_agent_cue(_spec("end_game"), None, game)

    assert stream.writes == []


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
@pytest.mark.asyncio
@pytest.mark.parametrize("spec_id, target", [("adjust_global_sensitivity", None), ("eliminate_player", "A")])
async def test_no_cue_once_the_game_is_ending(spec_id, target):
    """#2533: after the win the rainbow owns the winner's LED; an intervention applied then pulses nobody."""
    stream = FakeStream()
    game = FakeInterventionGame(players={"A": Player("A")}, gameplay_stream=stream)
    game.ending = True
    mgr = _manager({"agent_intervention_cue": True})

    await mgr._emit_agent_cue(_spec(spec_id), target, game)
    assert stream.writes == []

    game.ending = False  # the same cue while the game runs pulses (the case above is not vacuous)
    await mgr._emit_agent_cue(_spec(spec_id), target, game)
    assert [c.game_effect.serial for c in stream.writes] == ["A"]


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_the_ending_check_stays_best_effort():
    """#2533 review: a game object without game_ending() skips the cue, it never raises into the apply path."""
    stream = FakeStream()
    game = type("Game", (), {"players": {"A": Player("A")}, "gameplay_stream": stream})()
    mgr = _manager({"agent_intervention_cue": True})

    await mgr._emit_agent_cue(_spec("adjust_global_sensitivity"), None, game)

    assert stream.writes == []


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_cue_no_stream_is_safe():
    game = FakeInterventionGame(players={"A": Player("A")}, gameplay_stream=None)
    mgr = _manager({"agent_intervention_cue": True})
    # Should not raise.
    await mgr._emit_agent_cue(_spec("end_game"), None, game)


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_cue_targeted_dead_serial_no_pulse():
    stream = FakeStream()
    game = FakeInterventionGame(players={"A": Player("A", alive=False)}, gameplay_stream=stream)
    mgr = _manager({"agent_intervention_cue": True})

    await mgr._emit_agent_cue(_spec("eliminate_player"), "A", game)

    assert stream.writes == []


@pytest.mark.uc("UC-040", flow="main", steps=[13])
def test_hard_class_specs_are_the_cued_set():
    """Guard the cue trigger condition: exactly the HARD-weight specs."""
    hard = {s.type_id for s in INTERVENTION_SPECS if s.weight >= WEIGHT_HARD}
    assert hard == {
        "adjust_global_sensitivity",
        "eliminate_player",
        "revive_player",
        "end_game",
    }


class _RecordingAudio:
    """Stands in for ``audio_pb2_grpc.AudioServiceStub``: records PlaySound requests."""

    def __init__(self):
        self.sounds: list[str] = []

    async def PlaySound(self, request):  # noqa: N802 - gRPC stub name
        self.sounds.append(request.file_path)


@pytest.mark.asyncio
@pytest.mark.uc("UC-031", flow="main", br=["BR-002"])
async def test_intervention_cue_is_an_led_pulse_not_a_sound():
    """UC-031 BR-002: agent_intervention_cue pulses the LEDs of a real game and plays no sound."""
    from proto import controller_manager_pb2
    from services.game_coordinator.games.base import Player
    from services.game_coordinator.games.ffa import FFAGame
    from services.game_coordinator.tests.conftest import MockControllerManagerService, async_noop

    audio = _RecordingAudio()
    stream = FakeStream()
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=async_noop,
        audio_client=audio,
        game_id="test_cue_br002",
    )
    game.gameplay_stream = stream
    for serial in ("A", "B"):
        game.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    mgr = _manager({"agent_intervention_cue": True})

    await mgr._emit_agent_cue(_spec("end_game"), None, game)

    assert {c.game_effect.serial for c in stream.writes} == {"A", "B"}
    assert all(c.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PULSE for c in stream.writes)
    assert audio.sounds == []


@pytest.mark.uc("UC-020", steps=[6], br=["BR-004"])
@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.asyncio
async def test_a_real_game_is_ending_from_the_win_on():
    """UC-020 BR-004 (#2533): game_ending() turns True with the latched win, before the rainbow is sent."""
    from services.game_coordinator.games.base import GameState
    from services.game_coordinator.games.ffa import FFAGame
    from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_cue_game_ending",
    )
    game.players = {"A": Player("A"), "B": Player("B")}
    game.state = GameState.RUNNING
    assert game.game_ending() is False
    game.players["A"].alive = False
    assert await game._check_win_condition() is True

    assert game.game_ending() is True
