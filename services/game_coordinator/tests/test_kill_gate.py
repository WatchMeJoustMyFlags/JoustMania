"""The base kill gate: a mode can decline a kill before any bookkeeping (#1383).

``BaseGameMode._accepts_kill`` is checked by ``_kill_player`` with its other guards.
A declined kill gets no ``dead_count``, ``player_alive`` gauge, elimination order,
``player_deaths_total``, explosion, ``player_death`` span event or death effect, and
``_kill_player`` returns False. Spec: UC-030 step 9.
"""

import sys
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
from opentelemetry import trace

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import MockControllerManagerService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame


class _RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def death_effects(self):
        return [
            m.game_effect.serial
            for m in self.writes
            if m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
        ]


class _GatedFFA(FFAGame):
    """FFA whose kill gate declines every kill while ``accept`` is False."""

    accept = False

    def _accepts_kill(self, serial: str) -> bool:  # noqa: ARG002
        return self.accept


def _game(cls=FFAGame):
    game = cls(
        controller_manager_client=MockControllerManagerService(num_controllers=3),
        event_publisher=async_noop,
        audio_client=None,
        game_id="test_kill_gate",
    )
    game.gameplay_stream = _RecordingStream()
    game.running = True
    game.sounds = []

    async def record_sound(sound, *_args, **_kwargs):
        game.sounds.append(sound)

    game._play_sound = record_sound
    for serial in ("p1", "p2", "p3"):
        span = create_autospec(trace.Span, instance=True)
        span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
        game.players[serial] = Player(serial=serial, alive=True, color=(255, 0, 0), span=span)
    return game


def _death_events(span):
    return [c for c in span.add_event.call_args_list if c.args and c.args[0] == "player_death"]


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_declined_kill_does_no_bookkeeping():
    game = _game(_GatedFFA)
    span = game.players["p1"].span
    impl_calls = []

    async def impl(serial, accel_mag):  # noqa: ARG001
        impl_calls.append(serial)

    game._kill_player_impl = impl
    game.players["p1"].shield_charge_seconds = 5.0

    with patch("services.game_coordinator.games.base.metrics") as m:
        result = await game._kill_player("p1", 3.0)

    assert result is False
    # the gate sits ahead of the shield-charge discard: a declined kill keeps the charge
    assert game.players["p1"].shield_charge_seconds == 5.0
    assert impl_calls == []
    assert game.players["p1"].alive is True
    assert game.dead_count == 0
    assert game._elimination_count == 0
    assert "p1" not in game._kills_in_progress
    m.player_alive.labels.assert_not_called()
    m.player_elimination_order.labels.assert_not_called()
    m.player_deaths_total.labels.assert_not_called()
    assert game.sounds == []
    assert _death_events(span) == []
    assert game.gameplay_stream.death_effects() == []


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_accepted_kill_is_unchanged_in_ffa():
    game = _game()
    span = game.players["p1"].span

    with patch("services.game_coordinator.games.base.metrics") as m:
        result = await game._kill_player("p1", 3.0)

    assert result is True
    assert game.players["p1"].alive is False
    assert game.dead_count == 1
    assert game._elimination_count == 1
    m.player_alive.labels.return_value.set.assert_called_with(0)
    m.player_elimination_order.labels.return_value.set.assert_called_with(1)
    m.player_deaths_total.labels.return_value.inc.assert_called_once()
    assert len(game.sounds) == 1
    assert len(_death_events(span)) == 1
    assert game.gameplay_stream.death_effects() == ["p1"]


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_ignored_kill_returns_false():
    game = _game()
    game.players["p2"].alive = False

    assert await game._kill_player("ghost", 3.0) is False
    assert await game._kill_player("p2", 3.0) is False
    assert game.dead_count == 0
