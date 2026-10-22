"""
The base kill path sends the death effect in every mode except a Zombies conversion (#1342).

``BaseGameMode._sends_death_effect`` defaults to True; only Zombies overrides it (a converted
human stays alive and shows the zombie colour). These tests pin the default for the other
modes: the kill's last write for the serial is the base ``GAME_EFFECT_PLAYER_DEATH``, carrying
the player span's trace context (#456).
"""

import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider

test_dir = Path(__file__).parent
project_root = test_dir.parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.games.traitor import TraitorGame
from services.game_coordinator.games.werewolf import WerewolfGame


class RecordingGameplayStream:
    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


@pytest.fixture
def sdk_tracer(monkeypatch):
    """A real SDK tracer, so a player span yields a non-empty traceparent."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    return TracerProvider().get_tracer("test_base_death_effect")


@pytest.mark.uc("UC-020", flow="main", steps=[3])
@pytest.mark.parametrize("game_cls", [FFAGame, SimpleTeamsGame, NonstopJoustGame, TraitorGame, WerewolfGame])
@pytest.mark.asyncio
async def test_kill_ends_with_the_base_death_effect(game_cls, sdk_tracer):
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    game = game_cls(
        controller_manager_client=mock_cm,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=f"test_death_effect_{game_cls.__name__}",
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = RecordingGameplayStream()
    game.running = True
    game._play_sound = async_noop

    serial = next(iter(game.players))
    game.players[serial].span = sdk_tracer.start_span("player_lifecycle")
    assert game._sends_death_effect(serial) is True

    await game._kill_player(serial, 5.0)

    mine = [
        m
        for m in game.gameplay_stream.messages
        if (m.HasField("game_effect") and m.game_effect.serial == serial)
        or (m.HasField("base_color") and m.base_color.serial == serial)
    ]
    assert mine, "the kill wrote nothing for the serial"
    last = mine[-1]
    assert last.HasField("game_effect")
    assert last.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
    assert last.game_effect.trace_parent != ""
