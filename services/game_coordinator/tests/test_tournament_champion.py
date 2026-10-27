"""Tournament champion and win latch (UC-025 step 5, BR-003, BR-005; #1337, #1986).

The step that decides the final (a kill or the match time limit, or a walkover)
latches the champion and the root-span outcome before any await. The final gets
no match rainbow: the champion's rainbow, the congratulations voice and the
rainbow wait come with the end of the game. ``tournament_champion`` is published
once, only for a latched champion. A force-end before the decision, or a stream
end before it, crowns nobody (``game_ended.champion`` = ``none``).

The ``run()`` tests drive real games with a fake gameplay call that spikes the
first fighter of every undecided match.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService
from run_fakes import event_order
from test_random_teams_formation import RecordingLog, _players
from test_tournament_run import _bracket_game, _frame

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator.games import tournament as tournament_mod
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.tournament import TournamentGame, TournamentState
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

RAINBOW = controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
DEATH = controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, no round pause, a short intro and a short rainbow."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(tournament_mod, "TOURNAMENT_INTRO_DURATION_S", 0.1),
        patch.object(tournament_mod, "ROUND_PAUSE_S", 0.0),
    ):
        yield


class _LethalStream:
    """A gameplay call: a frame every 5 ms for every player; the first fighter of an undecided match spikes.

    ``end_on_final_death``: the controller manager ends the call right after the final
    loser's death effect (#1986): ``done()`` is True, reads end and writes raise.
    ``end_at_first_match``: the call ends (status OK) once the first match started.
    """

    def __init__(self, log, game, *, spike=True, end_on_final_death=False, end_at_first_match=False):
        self.log = log
        self.game = game
        self.spike = spike
        self.end_on_final_death = end_on_final_death
        self.end_at_first_match = end_at_first_match
        self.ended = False
        self.queue: asyncio.Queue = asyncio.Queue()
        self.producer: asyncio.Task | None = None
        self.rejected: list = []

    def done(self) -> bool:
        return self.ended

    def _end(self) -> None:
        self.ended = True
        self.queue.put_nowait(None)

    async def write(self, message):
        if self.ended:
            self.rejected.append(message)
            raise asyncio.InvalidStateError("RPC already finished")
        self.log.entries.append(("write", message))
        if message.HasField("config") and self.producer is None:
            self.producer = asyncio.create_task(self._produce())
        match = self.game.current_match
        if (
            self.end_on_final_death
            and message.HasField("game_effect")
            and message.game_effect.effect == DEATH
            and match is not None
            and match.is_complete
            and self.game._is_final(match)
        ):
            self._end()

    async def _produce(self):
        while not self.ended:
            match = self.game.current_match
            if self.end_at_first_match and match is not None:
                self._end()
                return
            target = match.player1_serial if self.spike and match is not None and not match.is_complete else None
            await self.queue.put(
                controller_manager_pb2.GameplayDataUpdate(
                    controllers=[
                        controller_manager_pb2.GameplayData(
                            serial=s,
                            accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=9.0 if s == target else 1.0),
                        )
                        for s in self.game.players
                    ]
                )
            )
            await asyncio.sleep(0.005)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.ended:
            raise StopAsyncIteration
        update = await self.queue.get()
        if update is None or self.ended:
            raise StopAsyncIteration
        return update


class _LethalControllerManager(MockControllerManagerService):
    def __init__(self, log, **stream_kwargs):
        super().__init__(num_controllers=4, infinite=True)
        self.log = log
        self.stream_kwargs = stream_kwargs
        self.game: TournamentGame | None = None
        self.streams: list[_LethalStream] = []

    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = _LethalStream(self.log, self.game, **self.stream_kwargs)
        self.streams.append(stream)
        return stream


def _game(num_players, **stream_kwargs):
    log = RecordingLog()
    client = _LethalControllerManager(log, **stream_kwargs)
    game = TournamentGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_tournament_champion",
        initial_players=_players()[:num_players],
        invincibility_seconds=0.0,
    )
    game._match_duration = 30.0
    game._time_between_matches = 0.0
    client.game = game
    sounds: list = []
    real_play = game._play_sound

    async def play(sound, *args, **kwargs):
        sounds.append(sound)
        return await real_play(sound, *args, **kwargs)

    game._play_sound = play
    return game, log, client, sounds


async def _run(game, client):
    try:
        await asyncio.wait_for(game.run(), timeout=15)
    finally:
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()


def _events(log, event_type) -> list[dict]:
    return log.events.get_events_of_type(event_type)


def _rainbows(log) -> list[str]:
    return [
        value.game_effect.serial
        for kind, value in log.entries
        if kind == "write" and value.HasField("game_effect") and value.game_effect.effect == RAINBOW
    ]


def _ended(log) -> dict:
    ended = _events(log, GameEvent.GAME_ENDED)
    assert len(ended) == 1, ended
    return ended[0]


# ---------------------------------------------------------------------------
# Step 5: the champion through run()
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-025", flow="main", steps=[3, 4, 5], br=["BR-005"], cond=["Post-S-1"])
@pytest.mark.parametrize("num_players", [2, 3], ids=["two-players", "three-players-bye"])
@pytest.mark.asyncio
async def test_run_crowns_the_champion_once_with_rainbow_and_voice(num_players):
    game, log, client, sounds = _game(num_players)

    await _run(game, client)

    left = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
    assert len(left) == 1
    champion = left[0]
    announced = _events(log, "tournament_champion")
    assert [e["champion"] for e in announced] == [champion], announced
    ended = _ended(log)
    assert ended["champion"] == champion
    assert ended["outcome"] == "completed"
    assert "reason" not in ended
    assert game.game_outcome() == "completed"
    assert game.state == GameState.ENDED, "the game reaches ENDED"

    # One match rainbow per non-final match, then the champion's rainbow only.
    match_ends = _events(log, "match_end")
    assert len(match_ends) == num_players - 1
    assert _rainbows(log) == [e["winner"] for e in match_ends[:-1]] + [champion]
    assert Sound.VOX_CONGRATULATIONS in sounds
    order = event_order(log)
    assert order.index("tournament_champion") > max(i for i, e in enumerate(order) if e == "match_end")
    assert order.index("tournament_champion") < order.index(GameEvent.GAME_ENDED.value)
    assert GameEvent.GAME_ERROR.value not in order
    assert len(client.streams) == 1


@pytest.mark.uc("UC-025", br=["BR-002"])
@pytest.mark.asyncio
async def test_tournament_plays_without_a_tempo_loop():
    """BR-002: no tempo loop by design: the music plays at 1.0 and the slow threshold table applies all game."""
    game, log, client, _sounds = _game(2)

    with (
        patch.object(game, "_music_loop") as music_loop,
        patch.object(game, "_start_game_music", wraps=game._start_game_music) as start_music,
    ):
        await _run(game, client)

    music_loop.assert_not_called()
    assert game.music_loop_task is None
    assert _events(log, "tournament_champion"), "the game did not play to its end"
    # The music starts once at 1.0 and nothing moves the tempo, so the slow table applies to the end.
    start_music.assert_called_once()
    assert game.music_speed == 1.0
    assert game.tempo_intensity == 0.0
    player = next(iter(game.players.values()))
    sens = game.sensitivity.value
    assert game._compute_effective_thresholds(player) == (game.slow_warning[sens], game.slow_max[sens])


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.asyncio
async def test_a_stream_that_ends_as_the_final_is_decided_keeps_the_champion():
    """#1986: the call ends after the final loser's death effect; no write raises, no game_error."""
    game, log, client, sounds = _game(2, end_on_final_death=True)

    await _run(game, client)  # raises on game_error

    stream = client.streams[0]
    assert stream.ended, "the call never ended"
    assert stream.rejected == [], "a write reached the finished call"
    champion = game._champion
    assert champion is not None
    assert [e["champion"] for e in _events(log, "tournament_champion")] == [champion]
    ended = _ended(log)
    assert ended["champion"] == champion
    assert ended["outcome"] == "completed"
    assert Sound.VOX_CONGRATULATIONS in sounds
    assert len(client.streams) == 1, "the finished call was reopened"


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-003"])
@pytest.mark.asyncio
async def test_a_stream_that_ends_before_a_champion_crowns_nobody():
    game, log, client, sounds = _game(2, spike=False, end_at_first_match=True)

    await _run(game, client)

    assert _events(log, "tournament_champion") == []
    ended = _ended(log)
    assert ended["champion"] == "none"
    assert ended["outcome"] == "completed"
    assert _rainbows(log) == []
    assert Sound.VOX_CONGRATULATIONS not in sounds


# ---------------------------------------------------------------------------
# BR-003 / BR-005: a force-end before or after the decision, in the same tick
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-003", "BR-005"])
@pytest.mark.asyncio
async def test_a_force_end_before_the_final_is_decided_crowns_nobody():
    """The force-end lands during the kill's await, before the handler decides the final."""
    game, log, client, sounds = _game(2)
    real_play = game._play_sound

    async def play(sound, *args, **kwargs):
        if sound == Sound.SFX_EXPLOSION:
            game.force_end("test_before")
        return await real_play(sound, *args, **kwargs)

    game._play_sound = play
    await _run(game, client)

    # The kill gate closes with the force-end: the kill awaiting its explosion is
    # declined before the handler, so the final is never decided (UC-030 step 9, #2033).
    assert game.current_match is not None and not game.current_match.is_complete
    assert game._champion is None
    assert _events(log, "tournament_champion") == []
    ended = _ended(log)
    assert ended["champion"] == "none"
    assert ended["outcome"] == "force_ended"
    assert ended["reason"] == "test_before"
    assert _rainbows(log) == []
    assert Sound.VOX_CONGRATULATIONS not in sounds


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-003", "BR-005"])
@pytest.mark.asyncio
async def test_a_force_end_after_the_final_is_decided_keeps_the_champion():
    """The force-end lands in the same frame, on the final loser's death effect (after the decision)."""
    game, log, client, sounds = _game(2)
    stream_writes: list = []

    real_start = game._start_gameplay_stream

    async def start_stream():
        await real_start()
        stream = game.gameplay_stream
        real_write = stream.write

        async def write(message):
            await real_write(message)
            stream_writes.append(message)
            if message.HasField("game_effect") and message.game_effect.effect == DEATH:
                game.force_end("test_after")

        stream.write = write

    game._start_gameplay_stream = start_stream
    await _run(game, client)

    assert game.force_ended
    champion = game._champion
    assert champion is not None
    assert [e["champion"] for e in _events(log, "tournament_champion")] == [champion]
    ended = _ended(log)
    assert ended["champion"] == champion
    assert ended["outcome"] == "completed"
    assert "reason" not in ended
    assert _rainbows(log) == [champion]
    assert Sound.VOX_CONGRATULATIONS in sounds


# ---------------------------------------------------------------------------
# A4: a final won by a walkover; the both-out walkover carries no round
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-025", flow="A4", steps=[2], br=["BR-005"])
@pytest.mark.asyncio
async def test_a_final_won_by_a_walkover_crowns_the_player_left():
    """3 players: the bye player disconnects while waiting; the round-1 winner is champion."""
    game, events = await _bracket_game(num_players=3)
    first = game.current_match
    bye_player = next(s for s in game.players if s not in (first.player1_serial, first.player2_serial))

    await game._process_gameplay_update(_frame(disconnects=[bye_player]))
    finished = False
    for frame in range(50):
        update = _frame([first.player1_serial], spike=[first.player1_serial]) if frame == 0 else _frame()
        if await game._process_gameplay_update(update):
            finished = True
            break

    assert finished
    assert [e["champion"] for e in events.get_events_of_type("tournament_champion")] == [first.player2_serial]
    assert game._champion == first.player2_serial
    assert game.game_outcome() == "completed"


@pytest.mark.uc("UC-025", flow="A4", steps=[2])
@pytest.mark.asyncio
async def test_an_out_player_carried_by_a_both_out_walkover_keeps_its_round():
    """The both-out walkover carries an out player forward; its round (span final_round) stays."""
    game, _ = await _bracket_game(num_players=4)
    pending = next(m for m in game.bracket if m.round_number == 1 and m is not game.current_match)
    for serial in (pending.player1_serial, pending.player2_serial):
        game.players[serial].tournament_state = TournamentState.ELIMINATED
        game.players[serial].alive = False

    game._resolve_walkovers()

    assert pending.is_complete and pending.winner_serial is None
    final = next(m for m in game.bracket if m.round_number == 2)
    assert {final.player1_serial, final.player2_serial} & {pending.player1_serial, pending.player2_serial}
    assert [game.players[s].round_number for s in (pending.player1_serial, pending.player2_serial)] == [1, 1]


# ---------------------------------------------------------------------------
# BR-003 / BR-005: a force-end inside the latch window; the champion after the latch
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-003", "BR-005"])
@pytest.mark.asyncio
async def test_a_force_end_inside_the_latch_window_crowns_nobody():
    """A force-end from the servicer thread lands between the running check and the outcome latch.

    The champion is then neither announced nor named in ``game_ended``: a champion
    appears in both or in neither.
    """
    game, log, client, sounds = _game(2)
    real_latch = type(game)._latch_game_outcome
    fired: list = []

    def latch():
        # The first latch while the final is being decided: the force-end lands just before it.
        match = game.current_match
        if not fired and match is not None and match.is_complete and game._is_final(match):
            fired.append(True)
            game.force_end("latch_window")
        real_latch(game)

    game._latch_game_outcome = latch
    await _run(game, client)

    assert fired, "the latch window was never hit"
    assert game._champion is None
    assert _events(log, "tournament_champion") == []
    ended = _ended(log)
    assert ended["champion"] == "none"
    assert ended["outcome"] == "force_ended"
    assert ended["reason"] == "latch_window"
    assert _rainbows(log) == []
    assert Sound.VOX_CONGRATULATIONS not in sounds


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.asyncio
async def test_the_latched_champion_ignores_a_later_disconnect_and_eliminate():
    """After the final is decided, a disconnect or an eliminate of the champion changes nothing."""
    game, _ = await _bracket_game(num_players=2)
    final = game.current_match
    loser, champion = final.player1_serial, final.player2_serial

    assert await game._process_gameplay_update(_frame([loser], spike=[loser])) is True
    assert game._champion == champion

    await game._handle_disconnect(champion)
    await game._kill_player(champion, 0.0, reason="agent_intervention")

    player = game.players[champion]
    assert player.alive
    assert player.tournament_state == TournamentState.CHAMPION
    deaths = [
        m.game_effect.serial
        for m in game.gameplay_stream.messages
        if m.HasField("game_effect") and m.game_effect.effect == DEATH
    ]
    assert champion not in deaths, deaths


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.asyncio
async def test_a_final_decided_outside_a_frame_is_finished_at_the_end():
    """An intervention decides the final outside a frame: the end of the game finishes it once."""
    game, events = await _bracket_game(num_players=2)
    final = game.current_match
    loser, champion = final.player1_serial, final.player2_serial

    assert await game._kill_player(loser, 0.0, reason="agent_intervention") is True
    assert game._champion == champion
    assert events.get_events_of_type("tournament_champion") == []

    await game._end_game_impl()

    order = [et for et, _ in events.events]
    assert order.count("match_end") == 1, order
    assert order.count("tournament_champion") == 1, order
    assert order.index("match_end") < order.index("tournament_champion") < order.index(GameEvent.GAME_ENDED.value)
    assert [e["champion"] for e in events.get_events_of_type("tournament_champion")] == [champion]
    assert events.get_events_of_type(GameEvent.GAME_ENDED)[0]["champion"] == champion


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-003"])
@pytest.mark.asyncio
async def test_a_force_ended_outcome_names_no_champion_in_game_ended():
    """The end of the game names no champion once the outcome reads ``force_ended``.

    A bracket error keeps the outcome unlatched, so a champion decided afterwards and
    a later force-end leave ``_champion`` set with a ``force_ended`` outcome.
    """
    game, events = await _bracket_game(num_players=2)
    game._bracket_error = RuntimeError("bracket step failed")
    final = game.current_match
    loser, champion = final.player1_serial, final.player2_serial
    assert await game._kill_player(loser, 0.0, reason="agent_intervention") is True
    assert game._champion == champion
    game.force_end("after_error")

    await game._end_game_impl()

    ended = events.get_events_of_type(GameEvent.GAME_ENDED)
    assert len(ended) == 1, ended
    assert ended[0]["champion"] == "none"
    assert ended[0]["outcome"] == "force_ended"
    assert events.get_events_of_type("tournament_champion") == []
