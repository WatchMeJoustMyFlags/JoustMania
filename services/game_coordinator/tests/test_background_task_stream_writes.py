"""LED writes from a mode's background tasks on a dead gameplay call (#2021, #2022).

A background task (Fight Club's round timer, Werewolf's reveal, a Zombie respawn
timer) can write while the gameplay call has failed, for example during the game
loop's reconnect backoff when ``self.gameplay_stream`` is still the dead call.
grpc.aio raises on that write. Before the fix the task died there: Fight Club
seated no next round (stuck until a force-end), Werewolf left the other
werewolves on the human colour. The write is now skipped with a warning and the
task goes on; the reopened stream restores each player's current colour
(UC-030 BR-006). Werewolf also reveals nothing once a side has won (UC-022 A1).
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec

import grpc
import pytest
from opentelemetry.trace import Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import Games
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.fight_club import WAITING_COLOR, FightState
from services.game_coordinator.games.werewolf import HUMAN_COLOR, WEREWOLF_COLOR


class _DeadCallStream:
    """A failed grpc.aio gameplay call whose ``done()`` still says False (the race): every write raises."""

    def __init__(self, error: BaseException | None = None):
        self.error = error or asyncio.InvalidStateError("RPC already finished")
        self.attempts = 0

    def done(self) -> bool:
        return False

    async def write(self, _message):
        self.attempts += 1
        raise self.error


class _LiveStream:
    """A live gameplay call that records the writes."""

    def __init__(self):
        self.messages = []

    def done(self) -> bool:
        return False

    async def write(self, message):
        self.messages.append(message)


def _aio_rpc_error() -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata())


async def _running_game(mode: Games, num_players: int = 4):
    events = EventCollector()
    cm = MockControllerManagerService(num_controllers=num_players)
    game = _GAME_CLASSES[mode](
        controller_manager_client=cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id=f"test_bg_writes_{mode.name.lower()}",
        initial_players=[type("P", (), {"serial": c.serial})() for c in cm.controllers],
    )
    await game._initialize_players_impl(cm.controllers)
    game.state = GameState.RUNNING
    game.start_time = 100.0
    game.running = True
    game._play_sound = AsyncMock()
    return game, events


async def _cancel_round_task(game) -> None:
    task = game.round_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- Fight Club (#2021) -------------------------------------------------------


@pytest.mark.uc("UC-025", flow="A2", steps=[2])
@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.parametrize("error", ["invalid_state", "aio_rpc"])
@pytest.mark.asyncio
async def test_fight_club_timeout_on_a_dead_call_still_seats_the_next_round(error):
    """A round timeout while the call is dead seats the next round (#2021)."""
    game, events = await _running_game(Games.FightClub)
    game.gameplay_stream = _LiveStream()
    await game._start_round()
    await _cancel_round_task(game)
    first_pair = (game.current_defender, game.current_fighter)
    assert None not in first_pair

    game.gameplay_stream = _DeadCallStream(_aio_rpc_error() if error == "aio_rpc" else None)
    try:
        await game._handle_timeout()

        assert game.gameplay_stream.attempts >= 1, "the colour writes were not attempted"
        assert game.current_defender is not None
        assert game.current_fighter is not None
        assert (game.current_defender, game.current_fighter) != first_pair
        assert game.round_number == 2
        assert not game.game_over
        assert [e for e in events.events if e[0] == "round_start"], "the next round was not started"
        assert game.round_task is not None and not game.round_task.done(), "the next round has no timer"
        # The two timed-out combatants wait; the reopened stream restores their colour.
        for serial in first_pair:
            if serial not in (game.current_defender, game.current_fighter):
                assert game.players[serial].state == FightState.IN_LINE
                assert game.players[serial].color == WAITING_COLOR
    finally:
        await _cancel_round_task(game)


@pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-005"])
@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_fight_club_timeout_on_a_dead_call_still_ends_a_decided_game():
    """A timeout that ends the game ends it, with its leader latched, while the call is dead (#2021)."""
    game, _events = await _running_game(Games.FightClub)
    game.gameplay_stream = _LiveStream()
    await game._start_round()
    await _cancel_round_task(game)
    leader = game.current_defender
    game.players[leader].score = 5
    game.round_number = game._min_rounds

    game.gameplay_stream = _DeadCallStream()
    await game._handle_timeout()

    assert game.game_over
    assert game._latched_winner == leader
    assert game.round_number == game._min_rounds, "a next round was seated after the decision"


@pytest.mark.uc("UC-025", flow="A2", steps=[2])
@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_fight_club_kill_on_a_dead_call_still_seats_the_next_round():
    """A round's kill while the call is dead seats the next round with a new timer (#2021)."""
    game, events = await _running_game(Games.FightClub)
    game.gameplay_stream = _LiveStream()
    await game._start_round()
    await _cancel_round_task(game)
    first_timer = game.round_task
    defender, fighter = game.current_defender, game.current_fighter
    assert None not in (defender, fighter)
    game.is_in_grace = lambda _player: False
    game._pause_between_rounds = AsyncMock()

    game.gameplay_stream = _DeadCallStream()
    try:
        await game._kill_player_impl(fighter, 10.0)

        assert game.gameplay_stream.attempts >= 1, "the LED writes were not attempted"
        assert not game.game_over
        assert game.round_number == 2
        assert len([e for e in events.events if e[0] == "round_start"]) == 2, "the next round was not started"
        assert game.current_defender == defender
        assert game.current_fighter not in (None, fighter)
        assert game.round_task is not None and game.round_task is not first_timer
        assert not game.round_task.done(), "the next round has no timer"
    finally:
        await _cancel_round_task(game)


# --- Werewolf (#2022) ---------------------------------------------------------


async def _werewolf_game():
    game, events = await _running_game(Games.Werewolf, num_players=5)
    serials = list(game.players)
    # Two werewolves, so a failed write on the first one must not stop the second.
    game.werewolf_serials = serials[:2]
    for serial in serials:
        game.players[serial].is_werewolf = serial in game.werewolf_serials
        game.players[serial].revealed = False
        game.players[serial].color = HUMAN_COLOR
    game._reveal_time = 0
    return game, events


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_werewolf_reveal_on_a_dead_call_reveals_every_werewolf():
    """A failed write during the reveal does not stop it: every werewolf is revealed (#2022)."""
    game, events = await _werewolf_game()
    stream = _DeadCallStream()
    game.gameplay_stream = stream

    await game._reveal_werewolves()

    assert game.revealed
    assert [e for e in events.events if e[0] == "werewolf_reveal"]
    for serial in game.werewolf_serials:
        assert game.players[serial].revealed, f"werewolf {serial} was not revealed"
        assert game.players[serial].color == WEREWOLF_COLOR
    # A flash and a colour per werewolf were attempted, none raised out of the task.
    assert stream.attempts == 2 * len(game.werewolf_serials)


@pytest.mark.uc("UC-022", flow="A1", steps=[2], br=["BR-006"])
@pytest.mark.asyncio
async def test_werewolf_reveal_after_the_win_reveals_nothing():
    """A reveal timer that fires during the win hold reveals nothing (#2022)."""
    game, events = await _werewolf_game()
    stream = _LiveStream()
    game.gameplay_stream = stream
    assert game._latch_win("humans")

    await game._reveal_werewolves()

    assert not game.revealed
    assert not [e for e in events.events if e[0] == "werewolf_reveal"]
    game._play_sound.assert_not_awaited()
    assert stream.messages == []
    for serial in game.werewolf_serials:
        assert not game.players[serial].revealed
        assert game.players[serial].color == HUMAN_COLOR


class _LatchingStream(_LiveStream):
    """A live call where a side wins during the first reveal write (a kill in the same tick)."""

    def __init__(self, game):
        super().__init__()
        self.game = game

    async def write(self, message):
        await super().write(message)
        if len(self.messages) == 1:
            assert self.game._latch_win("humans")


def _written_serials(messages) -> set[str]:
    serials = set()
    for message in messages:
        if message.HasField("game_effect"):
            serials.add(message.game_effect.serial)
        if message.HasField("base_color"):
            serials.add(message.base_color.serial)
    return serials


def _with_spans(game) -> None:
    for player in game.players.values():
        player.span = create_autospec(Span, instance=True)


def _revealed_span_events(game, serial) -> list:
    return [c for c in game.players[serial].span.add_event.call_args_list if c.args[0] == "werewolf_revealed"]


@pytest.mark.uc("UC-022", flow="A1", steps=[2], br=["BR-006"])
@pytest.mark.asyncio
async def test_werewolf_win_during_the_reveal_stops_it_at_the_next_werewolf():
    """A win latched during the first werewolf's reveal leaves the second unrevealed (#2022)."""
    game, _events = await _werewolf_game()
    _with_spans(game)
    first, second = game.werewolf_serials
    stream = _LatchingStream(game)
    game.gameplay_stream = stream

    await game._reveal_werewolves()

    assert game.players[first].revealed
    assert second not in _written_serials(stream.messages), "the second werewolf was flashed or recoloured"
    assert not game.players[second].revealed
    assert game.players[second].color == HUMAN_COLOR
    assert len(_revealed_span_events(game, first)) == 1
    assert _revealed_span_events(game, second) == [], "an unrevealed werewolf got the werewolf_revealed span event"


@pytest.mark.uc("UC-022", flow="A1", steps=[2], br=["BR-006"])
@pytest.mark.asyncio
async def test_werewolf_win_during_the_reveal_voice_reveals_nobody():
    """A win latched while the reveal voice plays writes no reveal LED and marks no werewolf (#2022)."""
    game, _events = await _werewolf_game()
    _with_spans(game)
    stream = _LiveStream()
    game.gameplay_stream = stream

    async def _win_during_voice(*_args, **_kwargs):
        assert game._latch_win("humans")

    game._play_sound = AsyncMock(side_effect=_win_during_voice)

    await game._reveal_werewolves()

    assert stream.messages == []
    for serial in game.werewolf_serials:
        assert not game.players[serial].revealed
        assert game.players[serial].color == HUMAN_COLOR
        assert _revealed_span_events(game, serial) == []


# --- Zombies (respawn timer) --------------------------------------------------


@pytest.mark.uc("UC-023", flow="main", steps=[4])
@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_zombie_respawn_timer_on_a_dead_call_respawns_without_error():
    """A respawn timer that writes on a dead call still respawns the zombie, and the task ends cleanly."""
    game, _events = await _running_game(Games.Zombies)
    zombie = next(s for s, p in game.players.items() if p.is_zombie)
    game.players[zombie].alive = False
    stream = _DeadCallStream()
    game.gameplay_stream = stream

    await game._respawn_zombie(zombie, 0)

    assert game.players[zombie].alive
    assert stream.attempts == 2
