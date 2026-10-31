"""
A (re)opened gameplay stream never lights a dead player and ends its feedback (#2471).

UC-030 BR-006 (stream lifecycle), step 9 (the death effect; the LED stays off):
- the stream config sends a dead player's colour as off, never its game colour;
- a death whose ``PLAYER_DEATH`` found no stream gets it on the next stream, once;
- every other dead player gets ``GAME_EFFECT_SHIELD`` 0, which ends leftover layers.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame

DEATH = controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
SHIELD = controller_manager_pb2.GAME_EFFECT_SHIELD
GREEN = (0, 255, 0)


class RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def config_colors(self):
        return {
            c.serial: (c.color.r, c.color.g, c.color.b)
            for m in self.writes
            if m.HasField("config")
            for c in m.config.colors
        }

    def effects(self):
        return [
            (m.game_effect.serial, m.game_effect.effect, m.game_effect.duration_ms)
            for m in self.writes
            if m.HasField("game_effect")
        ]


@pytest.fixture
def game():
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_stream_reopen_dead",
    )
    g.running = True
    g.start_time = game_clock.now()
    for serial in ("p1", "p2", "p3"):
        g.players[serial] = Player(serial=serial, alive=True, color=GREEN)
    streams: list[RecordingStream] = []

    def new_stream():
        streams.append(RecordingStream())
        return streams[-1]

    g.controller_client = MagicMock()
    g.controller_client.StreamGameplayData.side_effect = new_stream
    g.streams = streams
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


class TestUC030BR006ReopenDeadPlayers:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
    async def test_reopen_config_sends_a_dead_player_off(self, game):
        """The config lights only living players: a dead one gets (0, 0, 0), never its game colour."""
        game.players["p2"].alive = False
        await game._create_gameplay_stream()

        assert game.streams[0].config_colors() == {"p1": GREEN, "p2": (0, 0, 0), "p3": GREEN}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
    async def test_a_death_without_a_stream_is_sent_on_the_next_stream_once(self, game):
        """A kill while no stream is open sends PLAYER_DEATH on the next stream, then SHIELD 0 on later ones."""
        game.gameplay_stream = None
        assert await game._run_kill("p2", game.players["p2"], 0.0, "test") is True
        assert game.players["p2"].alive is False

        await game._create_gameplay_stream()
        await game._create_gameplay_stream()

        assert game.streams[0].effects() == [("p2", DEATH, 0)]
        assert game.streams[1].effects() == [("p2", SHIELD, 0)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
    async def test_reopen_ends_a_dead_players_layers_and_leaves_the_living_alone(self, game):
        """Each reopen sends SHIELD 0 to every dead player (its death was delivered) and nothing to the living."""
        game.players["p1"].alive = False
        game.players["p3"].alive = False
        await game._create_gameplay_stream()

        assert sorted(game.streams[0].effects()) == [("p1", SHIELD, 0), ("p3", SHIELD, 0)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-030", flow="main", br=["BR-006"])
    async def test_a_delivered_death_is_not_sent_again(self, game):
        """A kill on an open stream sends PLAYER_DEATH there; the reopen sends only SHIELD 0."""
        game.gameplay_stream = RecordingStream()
        assert await game._run_kill("p2", game.players["p2"], 0.0, "test") is True
        assert game.gameplay_stream.effects() == [("p2", DEATH, 0)]

        await game._create_gameplay_stream()

        assert game.streams[0].effects() == [("p2", SHIELD, 0)]
