"""Tournament on the base game loop (UC-025 Pre-1, steps 1-4, A4; #1882, #1273, #1938, D-G3, D-G33).

Tournament opens the gameplay stream before its intro (the waiting colours go
over it), drains it during the intro, primes the EMA after the countdown
(UC-030 BR-003) and then reads the stream for the whole game on the base game
loop: the bracket is a state machine stepped on every frame, so the stream is
never read with a timeout (#1882), disconnects reach the bracket (A4) and a
failed stream is reopened (UC-030 BR-006).

The ``run()`` tests use #1912's queue stream (a frame every 10 ms) and never
patch ``asyncio.sleep``: the intro length is a module constant and the
countdown is skipped through the runtime config. The gRPC tests run the game
against a real ``grpc.aio`` server.
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService
from run_fakes import FirstStreamDeadControllerManager
from run_fakes import event_order as _event_order
from run_fakes import name as _name
from test_random_teams_formation import (
    SERIALS,
    QueueControllerManager,
    QueueStream,
    RecordingLog,
    _players,
    _stop,
    _wait_for,
)

from lib.types import GameEvent
from proto import controller_manager_pb2, controller_manager_pb2_grpc, game_coordinator_pb2
from services.game_coordinator.games import base as base_mod
from services.game_coordinator.games import tournament as tournament_mod
from services.game_coordinator.games.tournament import (
    ELIMINATED_COLOR,
    MATCH_COLORS,
    WAITING_COLOR,
    TournamentGame,
    TournamentState,
)
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

INTRO_S = 0.3


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, a short rainbow and a short intro."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(tournament_mod, "TOURNAMENT_INTRO_DURATION_S", INTRO_S),
    ):
        yield config


def _tournament(log, client, *, players=None, match_s=30.0, between_s=0.0, invincibility_s=0.0) -> TournamentGame:
    game = TournamentGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_tournament_run",
        initial_players=players if players is not None else _players(),
        invincibility_seconds=invincibility_s,
    )
    game._match_duration = match_s
    game._time_between_matches = between_s
    return game


def _rgb(color) -> tuple[int, int, int]:
    return (color.r, color.g, color.b)


def _count(log, event_type) -> int:
    return log.events.count_events_of_type(event_type)


# ---------------------------------------------------------------------------
# run(): start order, Pre-1, intro drain, warm-up (step 1)
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-025", flow="main", steps=[1, 2])
@pytest.mark.asyncio
async def test_intro_goes_over_one_stream_in_the_tournament_event_order():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: _count(log, "match_start") == 1)
    await _stop(game, task, client)

    assert len(client.streams) == 1, "one stream for the whole game, never reopened"
    order = _event_order(log)
    expected = [
        GameEvent.GAME_STARTING,
        GameEvent.PLAYERS_INITIALIZED,
        "tournament_intro",
        GameEvent.COUNTDOWN_START,
        GameEvent.COUNTDOWN_END,
        GameEvent.GAME_STARTED,
        "match_start",
    ]
    positions = [order.index(_name(event)) for event in expected]
    assert positions == sorted(positions), order

    config_at = log.writes("config")[0][0]
    intro = log.index_of_event("tournament_intro")
    countdown = log.index_of_event(GameEvent.COUNTDOWN_START)
    assert config_at < intro, "the stream opens before the intro"
    intro_colors = {e.serial: _rgb(e.color) for i, e in log.writes("base_color") if intro < i < countdown}
    assert intro_colors == {s: tuple(WAITING_COLOR) for s in SERIALS}


class SpikeDuringIntroStream(QueueStream):
    """Every controller sends a large spike while ``spiking`` is set (the intro)."""

    spiking = False

    async def _produce(self):
        while True:
            z = 9.0 if self.spiking else 1.0
            controllers = [
                controller_manager_pb2.GameplayData(
                    serial=serial, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=z)
                )
                for serial in SERIALS
            ]
            await self.queue.put(controller_manager_pb2.GameplayDataUpdate(controllers=controllers, timestamp=self.seq))
            self.seq += 1
            await asyncio.sleep(0.01)


class SpikeControllerManager(QueueControllerManager):
    def StreamGameplayData(self):  # noqa: N802 - gRPC stub method name
        stream = SpikeDuringIntroStream(self.log)
        self.streams.append(stream)
        return stream


@pytest.mark.real_game_time
@pytest.mark.uc("UC-025", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_spikes_from_the_intro_never_reach_warmup_or_game_loop():
    log = RecordingLog()
    client = SpikeControllerManager(log)
    game = _tournament(log, client)
    seen: list[int] = []
    marks: dict[str, int] = {}
    real_intro = game._tournament_intro_phase

    async def intro():
        client.streams[0].spiking = True
        await real_intro()
        client.streams[0].spiking = False
        marks["intro_end_seq"] = client.streams[0].seq

    game._tournament_intro_phase = intro
    real_process = game._process_gameplay_update

    async def process(update):
        seen.append(update.timestamp)
        return await real_process(update)

    game._process_gameplay_update = process
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: len(seen) >= 40)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert marks["intro_end_seq"] > 10, "the intro produced frames"
    assert min(seen) >= marks["intro_end_seq"], "an intro frame reached the game loop"
    assert all(p.alive for p in game.players.values()), "an intro spike killed a player"


@pytest.mark.uc("UC-025", flow="main", steps=[1, 2])
@pytest.mark.asyncio
async def test_warmup_runs_after_the_countdown_and_before_the_first_match():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client)
    calls: list[str] = []
    real_warmup = game._warmup_ema

    async def warmup():
        assert _count(log, GameEvent.COUNTDOWN_END) == 1, "warmup before the countdown end"
        assert _count(log, "match_start") == 0, "warmup after the first match start"
        calls.append("warmup")
        await real_warmup()
        calls.append("warmup_done")

    game._warmup_ema = warmup
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: _count(log, "match_start") == 1)
    await _stop(game, task, client)

    assert calls == ["warmup", "warmup_done"]
    # The base game loop's 0.3 s start grace applies on top of the match invincibility (UC-030 BR-003).
    assert all(p.grace_until > 0 for p in game.players.values())


@pytest.mark.uc("UC-025", flow="main", steps=[1], cond=["Pre-1"])
@pytest.mark.asyncio
async def test_one_player_raises_before_the_stream_opens():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client, players=_players()[:1])

    with pytest.raises(ValueError, match="at least 2 players"):
        await asyncio.wait_for(game.run(), timeout=10)

    assert client.streams == []
    order = _event_order(log)
    assert order[0] == _name(GameEvent.GAME_STARTING)
    assert _name(GameEvent.GAME_STARTED) not in order
    assert "tournament_intro" not in order
    assert _name(GameEvent.GAME_ERROR) not in order, "the session publishes game_error once"


@pytest.mark.uc("UC-025", flow="main", steps=[1])
@pytest.mark.asyncio
async def test_stream_dead_before_the_intro_writes_reopens_instead_of_ending_the_game():
    """The intro colour writes on a broken stream are skipped; the drain stop reopens the stream."""
    log = RecordingLog()
    client = FirstStreamDeadControllerManager(log)
    game = _tournament(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: _count(log, "match_start") == 1 or task.done())
    await _stop(game, task, client)

    assert len(client.streams) == 2, "the broken stream is reopened once"
    assert _count(log, "match_start") == 1
    assert _count(log, GameEvent.GAME_ERROR) == 0


@pytest.mark.uc("UC-025", flow="main", steps=[1], br=["BR-002"])
@pytest.mark.asyncio
async def test_run_sets_the_flag_evaluation_context_before_the_stream_opens():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client)
    contexts: list[object] = []
    real_stream = game._start_gameplay_stream

    async def start_stream():
        contexts.append(game._flag_eval_context)
        await real_stream()

    game._start_gameplay_stream = start_stream
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: _count(log, "match_start") == 1)
    await _stop(game, task, client)

    assert contexts and contexts[0] is not None, "no flag evaluation context when the stream opened"
    assert contexts[0].attributes["game_mode"] == "Tournament"
    assert contexts[0].attributes["controller_count"] == len(SERIALS)


@pytest.mark.uc("UC-025", flow="main", steps=[1], br=["BR-003"])
@pytest.mark.asyncio
async def test_force_end_during_the_intro_stops_the_drain_without_a_reopen():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(tournament_mod, "TOURNAMENT_INTRO_DURATION_S", 5.0):
        game = _tournament(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: _count(log, "tournament_intro") == 1)
        drain = game._early_drain_task
        assert drain is not None and not drain.done()
        await _stop(game, task, client)

    assert len(client.streams) == 1
    assert drain.done() and not drain.cancelled()
    assert _count(log, GameEvent.GAME_STARTED) == 0
    assert _count(log, "match_start") == 0
    assert game._game_outcome_attributes()["game.outcome"] == "force_ended"
    assert game.gameplay_stream is None, "the stream is released"


# ---------------------------------------------------------------------------
# run(): the bracket on the base loop, teardown (steps 2-4, BR-002, BR-003)
# ---------------------------------------------------------------------------


@pytest.mark.real_game_time
@pytest.mark.uc("UC-025", flow="main", steps=[4])
@pytest.mark.asyncio
async def test_frames_are_read_during_the_pause_between_matches():
    """The stream is read throughout: frames of the pause are processed, not buffered (D-G33)."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client, match_s=0.1, between_s=0.5)
    in_pause: list[int] = []
    real_process = game._process_gameplay_update

    async def process(update):
        if _count(log, "match_end") == 1 and game.current_match is None:
            in_pause.append(update.timestamp)
        return await real_process(update)

    game._process_gameplay_update = process
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: _count(log, "match_start") == 2)
    await _stop(game, task, client)

    assert len(in_pause) >= 20, f"only {len(in_pause)} frames read during the 0.5 s pause"
    assert in_pause == sorted(in_pause)
    stream = client.streams[0]
    assert stream.queue.qsize() < 5, "frames piled up unread"


def _spy_run_end(game):
    """Spy the shield feedback end, the shield-span close and the analytics clear, calling through."""
    calls: list[str] = []
    real_clear = base_mod.metrics.clear_session_player_analytics
    real_close = game._close_shield_charge_spans
    real_end_shield = game._end_shield_feedback
    real_end_impl = game._end_game_impl

    def clear(*args, **kwargs):
        calls.append("clear_analytics")
        return real_clear(*args, **kwargs)

    def close(*args, **kwargs):
        calls.append("close_shield_spans")
        return real_close(*args, **kwargs)

    async def end_shield():
        calls.append("end_shield_feedback")
        await real_end_shield()

    async def end_impl():
        calls.append("end_game_impl")
        await real_end_impl()

    return calls, [
        patch.object(base_mod.metrics, "clear_session_player_analytics", side_effect=clear),
        patch.object(game, "_close_shield_charge_spans", side_effect=close),
        patch.object(game, "_end_shield_feedback", side_effect=end_shield),
        patch.object(game, "_end_game_impl", side_effect=end_impl),
    ]


@pytest.mark.uc("UC-025", flow="main", steps=[2, 3], br=["BR-002", "BR-003"])
@pytest.mark.asyncio
async def test_force_end_in_a_match_publishes_no_match_event_and_runs_the_shared_cleanup():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client, match_s=0.2, between_s=0.2)
    calls, patches = _spy_run_end(game)
    for p in patches:
        p.start()
    try:
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: _count(log, "match_end") == 1)
        game.force_end("test_done")
        ended_at = len(log.entries)
        await asyncio.wait_for(task, timeout=10)
    finally:
        for p in patches:
            p.stop()
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()

    late = [value for kind, value in log.entries[ended_at:] if kind == "event"]
    assert not {"match_start", "match_end", "round_start"} & {_name(v) for v in late}, late
    assert "close_shield_spans" in calls, calls
    assert "clear_analytics" in calls, calls
    assert game.force_ended
    assert game.gameplay_stream is None, "the stream is released"
    assert all(p.span is None for p in game.players.values()), "a player span stayed open"
    assert game.match_span is None


@pytest.mark.uc("UC-025", flow="main", steps=[4], br=["BR-002", "BR-003"])
@pytest.mark.asyncio
async def test_finished_bracket_ends_the_game_once_through_the_shared_teardown():
    """Two players: the match times out, the bracket is done, the game ends once (completed)."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client, players=_players()[:2], match_s=0.1)
    calls, patches = _spy_run_end(game)
    for p in patches:
        p.start()
    try:
        await asyncio.wait_for(game.run(), timeout=15)
    finally:
        for p in patches:
            p.stop()
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()

    assert _count(log, "match_start") == 1
    assert _count(log, "match_end") == 1
    assert _count(log, GameEvent.GAME_ENDED) == 1
    assert calls.index("end_shield_feedback") < calls.index("end_game_impl"), calls
    assert "clear_analytics" in calls, calls
    assert game.gameplay_stream is None, "the stream is released after the winner effects"
    assert game._game_outcome_attributes()["game.outcome"] == "completed"
    assert len(client.streams) == 1


@pytest.mark.uc("UC-025", flow="main", steps=[2])
@pytest.mark.asyncio
async def test_a_failing_bracket_step_ends_the_game_with_its_error_and_latches_nothing():
    """No reconnect of the healthy stream and no ``completed`` outcome (game_error, O7)."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client)
    game._start_match = AsyncMock(side_effect=RuntimeError("bracket boom"))
    try:
        with pytest.raises(RuntimeError, match="bracket boom"):
            await asyncio.wait_for(game.run(), timeout=15)
    finally:
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()

    assert len(client.streams) == 1, "the bracket error reconnected the stream"
    assert game._latched_outcome is None, "a bracket failure latched a natural outcome"
    assert not game._outcome_recorded
    assert _count(log, GameEvent.GAME_ENDED) == 0
    assert game.gameplay_stream is None, "the stream is released"


# ---------------------------------------------------------------------------
# Frames fed to the base loop's per-frame step: detection filter and disconnects
# ---------------------------------------------------------------------------


class _RecordingStream:
    def __init__(self):
        self.messages: list = []

    async def write(self, message):
        self.messages.append(message)


def _frame(serials=(), spike=(), disconnects=(), names=None):
    names = names or {}
    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[
            controller_manager_pb2.GameplayData(
                serial=s,
                name=names.get(s, ""),
                accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=9.0 if s in spike else 1.0),
            )
            for s in serials
        ],
        disconnects=[controller_manager_pb2.DisconnectEvent(serial=s) for s in disconnects],
    )


async def _bracket_game(num_players=4, invincibility_s=0.0):
    mock_cm = MockControllerManagerService(num_controllers=num_players, death_schedule={}, max_duration=10.0)
    events = EventCollector()
    game = TournamentGame(
        controller_manager_client=mock_cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_tournament_frames",
        invincibility_seconds=invincibility_s,
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game._play_sound = AsyncMock()
    game.running = True
    game.gameplay_stream = _RecordingStream()
    game._time_between_matches = 0.0
    # The first frame starts the first match.
    assert await game._process_gameplay_update(_frame()) is False
    assert game.current_match is not None
    return game, events


@pytest.mark.uc("UC-025", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_a_waiting_player_is_not_checked_but_its_name_is_read():
    game, _ = await _bracket_game()
    match = game.current_match
    waiting = next(s for s in game.players if s not in (match.player1_serial, match.player2_serial))
    game._warn_player = AsyncMock()

    for _ in range(10):
        await game._process_gameplay_update(_frame([waiting], spike=[waiting], names={waiting: "Alice"}))

    player = game.players[waiting]
    assert player.alive and player.tournament_state == TournamentState.WAITING
    assert game.dead_count == 0, "a waiting player's spike reached the death accounting"
    game._warn_player.assert_not_called()
    assert player.name == "Alice", "the waiting player's controller name was not read"


@pytest.mark.uc("UC-025", flow="A4", steps=[1])
@pytest.mark.asyncio
async def test_a_fighter_that_disconnects_during_invincibility_forfeits_the_match():
    game, events = await _bracket_game(invincibility_s=60.0)
    match = game.current_match
    leaver, opponent = match.player1_serial, match.player2_serial

    await game._process_gameplay_update(_frame(disconnects=[leaver]))

    assert match.is_complete and match.winner_serial == opponent
    assert game.players[leaver].tournament_state == TournamentState.ELIMINATED
    assert game.players[leaver].alive is False
    assert game.players[opponent].tournament_state == TournamentState.WAITING
    assert [(e["winner"], e["loser"]) for e in events.get_events_of_type("match_end")] == [(opponent, leaver)]


@pytest.mark.uc("UC-025", flow="A4", steps=[2, 3])
@pytest.mark.asyncio
async def test_a_waiting_player_that_disconnects_gives_its_opponent_a_walkover():
    game, events = await _bracket_game()
    first = game.current_match
    second = next(m for m in game.bracket if m.round_number == 1 and m is not first and not m.is_bye)
    leaver, walkover = second.player1_serial, second.player2_serial

    await game._process_gameplay_update(_frame(disconnects=[leaver]))
    assert game.players[leaver].alive is False
    assert game.players[leaver].tournament_state == TournamentState.ELIMINATED
    assert game.players[leaver].color == ELIMINATED_COLOR

    # The first match ends; the next step skips the second match (walkover) and starts the final.
    with patch.object(tournament_mod, "ROUND_PAUSE_S", 0.0):
        await game._process_gameplay_update(_frame([first.player1_serial], spike=[first.player1_serial]))
        for _ in range(5):
            await game._process_gameplay_update(_frame())

    assert second.is_complete and second.winner_serial == walkover
    starts = [e["match_id"] for e in events.get_events_of_type("match_start")]
    assert second.match_id not in starts, "the walkover match was played"
    final = game.current_match
    assert final is not None and final.round_number == 2
    assert {final.player1_serial, final.player2_serial} == {first.player2_serial, walkover}


@pytest.mark.uc("UC-025", flow="A4", steps=[2])
@pytest.mark.asyncio
async def test_the_winner_of_a_match_decided_in_the_same_frame_that_disconnects_is_out():
    game, events = await _bracket_game()
    match = game.current_match
    loser, winner = match.player1_serial, match.player2_serial

    # The match is decided (the loser's kill), its winner has not advanced yet; then the winner disconnects.
    assert await game._kill_player(loser, 0.0, reason="agent_intervention") is True
    assert match.is_complete and match.winner_serial == winner
    await game._process_gameplay_update(_frame(disconnects=[winner]))

    for serial in (loser, winner):
        assert game.players[serial].alive is False
        assert game.players[serial].tournament_state == TournamentState.ELIMINATED
    assert not events.get_events_of_type("game_error")


@pytest.mark.uc("UC-025", flow="A4", steps=[1, 3])
@pytest.mark.asyncio
async def test_two_players_one_disconnects_ends_the_bracket():
    game, events = await _bracket_game(num_players=2, invincibility_s=60.0)
    leaver = game.current_match.player1_serial

    finished = False
    for frame in range(5):
        update = _frame(disconnects=[leaver]) if frame == 0 else _frame()
        if await game._process_gameplay_update(update):
            finished = True
            break

    assert finished, "the bracket did not end after the only opponent disconnected"
    assert len(events.get_events_of_type("match_end")) == 1


@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-003"])
@pytest.mark.asyncio
async def test_a_force_end_during_the_match_winner_effect_publishes_no_match_end():
    game, events = await _bracket_game()
    match = game.current_match
    real_write = game._write_effect_guarded

    async def write_then_force_end(message, label):
        await real_write(message, label)
        if "winner effect" in label:
            game.force_end("test_done")

    game._write_effect_guarded = write_then_force_end
    await game._process_gameplay_update(_frame([match.player1_serial], spike=[match.player1_serial]))

    assert match.is_complete and game.force_ended
    assert events.get_events_of_type("match_end") == [], "a force-ended match published match_end"


@pytest.mark.uc("UC-025", flow="A4", steps=[2, 3])
@pytest.mark.parametrize("num_players", [6, 8])
@pytest.mark.asyncio
async def test_both_players_of_a_pending_match_disconnect_and_the_bracket_still_finishes(num_players):
    """The emptied match carries an out player forward; its next opponent gets the walkover (#2000)."""
    game, events = await _bracket_game(num_players=num_players)
    pending = next(m for m in game.bracket if m.round_number == 1 and not m.is_bye and m is not game.current_match)
    leavers = [pending.player1_serial, pending.player2_serial]

    finished = False
    with patch.object(tournament_mod, "ROUND_PAUSE_S", 0.0):
        await game._process_gameplay_update(_frame(disconnects=leavers))
        for frame in range(3000):
            match = game.current_match
            if match is not None and not match.is_complete and frame % 3 == 0:
                update = _frame([match.player1_serial], spike=[match.player1_serial])
            else:
                update = _frame()
            if await game._process_gameplay_update(update):
                finished = True
                break

    left = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
    assert finished, f"the bracket stalled with {len(left)} players left"
    assert len(left) == 1
    assert not game._stall_logged
    played = {e[k] for e in events.get_events_of_type("match_start") for k in ("player1", "player2")}
    assert not played & set(leavers), "a disconnected player played a match"


# ---------------------------------------------------------------------------
# Real grpc.aio stream: no read timeout, reopen restores colours, OK end (#1882)
# ---------------------------------------------------------------------------


class _GameplayServer(controller_manager_pb2_grpc.ControllerManagerServiceServicer):
    """A real gameplay stream: frames every 10 ms; ``pause`` stops the frames for a while.

    ``fault`` ("abort" or "end") ends the first call once ``fault_trigger`` is set:
    UNAVAILABLE (a controller-manager restart) or status OK. ``received`` keeps the
    game's messages of every call. ``disconnect`` sends one disconnect event for
    that serial on the first call.
    """

    def __init__(self, serials, fault=None):
        self.serials = serials
        self.fault = fault
        self.fault_trigger = asyncio.Event()
        self.pause_s = 0.0
        self.pause_trigger = asyncio.Event()
        self.paused = asyncio.Event()
        self.calls = 0
        self.disconnect: str | None = None
        self.received: list = []

    async def StreamGameplayData(self, request_iterator, context):  # noqa: N802 - gRPC method name
        self.calls += 1
        call = self.calls

        async def read_client():
            async for message in request_iterator:
                self.received.append((call, message))

        reader = asyncio.create_task(read_client())
        try:
            while True:
                if call == 1 and self.fault and self.fault_trigger.is_set():
                    if self.fault == "abort":
                        await context.abort(grpc.StatusCode.UNAVAILABLE, "controller manager restart")
                    return
                if self.pause_trigger.is_set() and not self.paused.is_set():
                    self.paused.set()
                    await asyncio.sleep(self.pause_s)
                if call == 1 and self.disconnect:
                    serial, self.disconnect = self.disconnect, None
                    yield controller_manager_pb2.GameplayDataUpdate(
                        disconnects=[controller_manager_pb2.DisconnectEvent(serial=serial)]
                    )
                yield controller_manager_pb2.GameplayDataUpdate(
                    controllers=[
                        controller_manager_pb2.GameplayData(
                            serial=s, accel=controller_manager_pb2.Vector3(x=0.0, y=0.0, z=1.0)
                        )
                        for s in self.serials
                    ]
                )
                await asyncio.sleep(0.01)
        finally:
            reader.cancel()


@contextlib.asynccontextmanager
async def _served(server_impl):
    server = grpc.aio.server()
    controller_manager_pb2_grpc.add_ControllerManagerServiceServicer_to_server(server_impl, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"localhost:{port}")
    try:
        yield controller_manager_pb2_grpc.ControllerManagerServiceStub(channel)
    finally:
        await channel.close()
        await server.stop(grace=0)


GRPC_SERIALS = ["p0", "p1", "p2", "p3"]


def _grpc_tournament(stub, log, **kwargs) -> TournamentGame:
    return _tournament(log, stub, players=[game_coordinator_pb2.Player(serial=s) for s in GRPC_SERIALS], **kwargs)


@pytest.mark.uc("UC-025", flow="main", steps=[2, 3])
@pytest.mark.asyncio
async def test_a_frame_gap_in_a_match_keeps_the_match_on_one_call():
    """A 250 ms gap (longer than the old 100 ms read timeout) neither cancels the call nor ends the match."""
    server_impl = _GameplayServer(GRPC_SERIALS)
    server_impl.pause_s = 0.25
    log = RecordingLog()
    async with _served(server_impl) as stub:
        game = _grpc_tournament(stub, log)
        after_gap: list[int] = []
        real_process = game._process_gameplay_update

        async def process(update):
            if server_impl.paused.is_set():
                after_gap.append(1)
                if len(after_gap) >= 20:
                    game.force_end("test_done")
            return await real_process(update)

        game._process_gameplay_update = process
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: _count(log, "match_start") == 1)
        server_impl.pause_trigger.set()
        await asyncio.wait_for(task, timeout=15)

    assert server_impl.calls == 1, "the gap cancelled the gameplay call"
    assert len(after_gap) >= 20, "no frames after the gap"
    assert _count(log, "match_start") == 1
    assert _count(log, "match_end") == 0, "the gap ended the match"
    assert _count(log, GameEvent.GAME_ERROR) == 0


@pytest.mark.uc("UC-025", flow="main", steps=[2])
@pytest.mark.asyncio
async def test_a_stream_that_fails_in_a_match_is_reopened_with_the_match_colours():
    """UC-030 BR-006 reconnect; the new call's config carries each player's current colour (O3).

    Red/blue for the fighters, the waiting colour, and off for a player who left by a disconnect.
    """
    server_impl = _GameplayServer(GRPC_SERIALS, fault="abort")
    log = RecordingLog()
    async with _served(server_impl) as stub:
        game = _grpc_tournament(stub, log)
        game._RECONNECT_BACKOFF_S = 0.05
        frames_on_second_call: list[int] = []
        real_process = game._process_gameplay_update

        async def process(update):
            if server_impl.calls >= 2:
                frames_on_second_call.append(1)
                if len(frames_on_second_call) >= 10:
                    game.force_end("test_done")
            return await real_process(update)

        game._process_gameplay_update = process
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: _count(log, "match_start") == 1)
        match = game.current_match
        leaver = next(s for s in GRPC_SERIALS if s not in (match.player1_serial, match.player2_serial))
        server_impl.disconnect = leaver
        await _wait_for(lambda: game.players[leaver].tournament_state == TournamentState.ELIMINATED)
        server_impl.fault_trigger.set()
        await asyncio.wait_for(task, timeout=15)

    assert server_impl.calls == 2, "the failed call was not reopened exactly once"
    assert _count(log, "match_start") == 1, "the match restarted"
    assert _count(log, GameEvent.GAME_ERROR) == 0
    configs = [m.config for call, m in server_impl.received if call == 2 and m.HasField("config")]
    assert configs, "the reopened call got no config"
    colors = {c.serial: _rgb(c.color) for c in configs[0].colors}
    assert colors[match.player1_serial] == MATCH_COLORS[0]
    assert colors[match.player2_serial] == MATCH_COLORS[1]
    assert colors[leaver] == ELIMINATED_COLOR, "the eliminated player's LED came back on"
    waiting = set(GRPC_SERIALS) - {match.player1_serial, match.player2_serial, leaver}
    assert {colors[s] for s in waiting} == {WAITING_COLOR}


@pytest.mark.uc("UC-025", flow="main", steps=[2, 4])
@pytest.mark.asyncio
async def test_a_stream_that_ends_ok_in_a_match_ends_the_game_once():
    """No match restart and no game_error: the base loop ends on an OK end (UC-030 BR-006)."""
    server_impl = _GameplayServer(GRPC_SERIALS, fault="end")
    log = RecordingLog()
    async with _served(server_impl) as stub:
        game = _grpc_tournament(stub, log)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: _count(log, "match_start") == 1)
        server_impl.fault_trigger.set()
        await asyncio.wait_for(task, timeout=15)

    assert server_impl.calls == 1
    assert _count(log, "match_start") == 1, "the match restarted after the stream end"
    assert _count(log, GameEvent.GAME_ENDED) == 1
    assert _count(log, GameEvent.GAME_ERROR) == 0


@pytest.mark.uc("UC-025", flow="main", steps=[2, 4])
@pytest.mark.real_durations
def test_the_spec_defaults():
    """Step 2 and 4: 4 s invincibility, 2 s round pause, 5 s between matches; A1: 22 s match limit."""
    game = TournamentGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_tournament_defaults",
        initial_players=_players()[:2],
    )
    assert game._invincibility_duration == 4.0
    assert tournament_mod.ROUND_PAUSE_S == 2.0
    assert game._time_between_matches == 5.0
    assert game._match_duration == 22.0
