"""Fight Club ``run()`` start and teardown (UC-025 A2 step 1, Pre-1; #1273, #1938, D-G3, D-G29).

Fight Club opens the gameplay stream before its intro (the waiting colours go
over it), drains the stream during the intro, primes the EMA after the
countdown (UC-030 BR-003), starts the first round only after the warm-up and
sets the game's flag evaluation context like the base start. Its teardown ends
the shield feedback, closes the stream and runs the shared run-end cleanup.

The run tests use #1912's queue stream, which produces a frame every 10 ms, so
the drain stops by ``break`` and never reopens the stream. They never patch
``game_clock.sleep``: the intro length is a module constant and the countdown is
skipped through the runtime config.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from openfeature import api
from openfeature import transaction_context as tc

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

from lib.feature_flags import set_game_session_kind_context
from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games import base as base_mod
from services.game_coordinator.games import fight_club as fight_club_mod
from services.game_coordinator.games.fight_club import WAITING_COLOR, FightClubGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

INTRO_S = 0.3
INTRO_START = "test_intro_start"
INTRO_END = "test_intro_end"


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, a short rainbow and a short intro."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(fight_club_mod, "FC_INTRO_DURATION_S", INTRO_S),
    ):
        yield config


def _fight_club(log, client, *, players=None) -> FightClubGame:
    game = FightClubGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_fight_club_run",
        initial_players=players if players is not None else _players(),
    )
    # Fight Club publishes no intro events: mark the intro in the log.
    real_intro = game._intro_phase

    async def intro():
        log.entries.append(("event", INTRO_START))
        await real_intro()
        log.entries.append(("event", INTRO_END))

    game._intro_phase = intro
    return game


def _rgb(color) -> tuple[int, int, int]:
    return (color.r, color.g, color.b)


@pytest.mark.uc("UC-025", flow="A2", steps=[1])
@pytest.mark.asyncio
async def test_intro_goes_over_one_stream_in_the_fight_club_event_order():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type("round_start") == 1)
    await _stop(game, task, client)

    assert len(client.streams) == 1, "one stream for the whole game, never reopened"
    order = _event_order(log)
    expected = [
        GameEvent.GAME_STARTING,
        GameEvent.PLAYERS_INITIALIZED,
        INTRO_START,
        INTRO_END,
        GameEvent.COUNTDOWN_START,
        GameEvent.COUNTDOWN_END,
        GameEvent.GAME_STARTED,
        "round_start",
    ]
    positions = [order.index(_name(event)) for event in expected]
    assert positions == sorted(positions), order

    config_at = log.writes("config")[0][0]
    intro_start = log.index_of_event(INTRO_START)
    countdown = log.index_of_event(GameEvent.COUNTDOWN_START)
    assert config_at < intro_start, "the stream opens before the intro"
    intro_colors = {e.serial: _rgb(e.color) for i, e in log.writes("base_color") if intro_start < i < countdown}
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
@pytest.mark.uc("UC-025", flow="A2", steps=[1])
@pytest.mark.asyncio
async def test_spikes_from_the_intro_never_reach_warmup_or_game_loop():
    log = RecordingLog()
    client = SpikeControllerManager(log)
    game = _fight_club(log, client)
    seen: list[int] = []
    marks: dict[str, int] = {}
    real_intro = game._intro_phase

    async def intro():
        client.streams[0].spiking = True
        await real_intro()
        client.streams[0].spiking = False
        marks["intro_end_seq"] = client.streams[0].seq

    game._intro_phase = intro
    real_process = game._process_gameplay_update

    async def process(update):
        seen.append(update.timestamp)
        return await real_process(update)

    game._process_gameplay_update = process
    real_warmup = game._warmup_ema

    async def warmup():
        stream = game.gameplay_stream

        class Spy:
            def __aiter__(self):
                return self

            async def __anext__(self):
                update = await stream.__anext__()
                seen.append(update.timestamp)
                return update

        game.gameplay_stream = Spy()
        try:
            await real_warmup()
        finally:
            game.gameplay_stream = stream

    game._warmup_ema = warmup
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: len(seen) >= 40)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert marks["intro_end_seq"] > 10, "the intro produced frames"
    assert min(seen) >= marks["intro_end_seq"], "an intro frame reached warmup or the game loop"


@pytest.mark.uc("UC-025", flow="A2", steps=[1, 2])
@pytest.mark.asyncio
async def test_warmup_runs_after_the_countdown_and_before_the_first_round():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)
    calls: list[str] = []
    real_warmup = game._warmup_ema

    async def warmup():
        assert log.events.count_events_of_type(GameEvent.COUNTDOWN_END) == 1, "warmup before the countdown end"
        assert log.events.count_events_of_type("round_start") == 0, "warmup after the first round start"
        calls.append("warmup")
        await real_warmup()
        calls.append("warmup_done")

    game._warmup_ema = warmup
    real_round = game._start_round

    async def start_round():
        calls.append("round")
        await real_round()

    game._start_round = start_round
    real_process = game._process_gameplay_update

    async def process(update):
        calls.append("process")
        return await real_process(update)

    game._process_gameplay_update = process
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: calls.count("process") >= 5)
    await _stop(game, task, client)

    assert len(client.streams) == 1
    assert calls[0] == "warmup" and calls.count("warmup") == 1, calls[:5]
    # The first round (and its timer) starts after the warm-up (step 2).
    assert calls.count("round") == 1, calls[:8]
    assert calls.index("warmup_done") < calls.index("round") < calls.index("process"), calls[:5]


@pytest.mark.uc("UC-025", flow="A2", steps=[1])
@pytest.mark.asyncio
async def test_stream_dead_before_the_intro_writes_reopens_instead_of_ending_the_game():
    """The intro colour write errors are logged; the drain stop reopens the stream."""
    log = RecordingLog()
    client = FirstStreamDeadControllerManager(log)
    game = _fight_club(log, client)
    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1 or task.done())
    await _stop(game, task, client)

    assert len(client.streams) == 2, "the broken stream is reopened once"
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 1
    assert log.events.count_events_of_type(GameEvent.GAME_ERROR) == 0
    assert len(log.writes("config")) == 2


@pytest.mark.uc("UC-025", flow="A2", steps=[1], cond=["Pre-1"])
@pytest.mark.asyncio
async def test_one_player_raises_before_the_stream_opens():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client, players=_players()[:1])

    with pytest.raises(ValueError, match="at least 2 players"):
        await asyncio.wait_for(game.run(), timeout=10)

    assert client.streams == []
    order = _event_order(log)
    assert order[0] == _name(GameEvent.GAME_STARTING)
    assert _name(GameEvent.GAME_STARTED) not in order
    assert "round_start" not in order
    assert _name(GameEvent.GAME_ERROR) not in order, "the session publishes game_error once"


@pytest.mark.uc("UC-025", flow="A2", steps=[1], br=["BR-003"])
@pytest.mark.asyncio
async def test_force_end_during_the_intro_stops_the_drain_without_a_reopen():
    log = RecordingLog()
    client = QueueControllerManager(log)
    with patch.object(fight_club_mod, "FC_INTRO_DURATION_S", 5.0):
        game = _fight_club(log, client)
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: any(value == INTRO_START for kind, value in log.entries))
        drain = game._early_drain_task
        assert drain is not None and not drain.done()
        await _stop(game, task, client)

    assert len(client.streams) == 1
    # The stop in run()'s finally ended it; a cancel from cleanup() would cancel the call.
    assert drain.done() and not drain.cancelled()
    assert log.events.count_events_of_type(GameEvent.GAME_STARTED) == 0
    assert log.events.count_events_of_type(GameEvent.COUNTDOWN_START) == 0
    assert log.events.count_events_of_type("round_start") == 0
    assert game.force_ended
    assert game._game_outcome_attributes()["game.outcome"] == "force_ended"
    assert game.gameplay_stream is None, "the stream is released"


@pytest.mark.uc("UC-025", flow="A2", steps=[1, 2], br=["BR-003"])
@pytest.mark.asyncio
async def test_force_end_during_the_warmup_starts_no_round_and_declares_no_winner():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)
    real_warmup = game._warmup_ema

    async def warmup():
        await real_warmup()
        game.running = False  # a force-end that lands while the warm-up runs

    game._warmup_ema = warmup
    task = asyncio.create_task(game.run())
    await _stop(game, task, client)

    assert log.events.count_events_of_type("round_start") == 0
    assert log.events.count_events_of_type(GameEvent.GAME_ENDED) == 0
    assert game._game_outcome_attributes()["game.outcome"] == "force_ended"
    assert game.gameplay_stream is None, "the stream is released"


@pytest.mark.uc("UC-025", flow="A2", steps=[1], br=["BR-002"])
@pytest.mark.asyncio
async def test_shadow_fight_club_adds_the_game_attributes_to_the_session_context():
    """The session set game_kind / experiment already; the start adds game_mode and the rest."""
    previous_propagator = tc._evaluation_transaction_context_propagator
    api.set_transaction_context_propagator(tc.ContextVarsTransactionContextPropagator())
    try:
        await _run_shadow_fight_club_context_check()
    finally:
        api.set_transaction_context_propagator(previous_propagator)


async def _run_shadow_fight_club_context_check():
    log = RecordingLog()
    client = QueueControllerManager(log)
    contexts: dict[str, object] = {}

    async def session():
        set_game_session_kind_context("shadow", experiment_id="exp_1", arm="b")
        game = _fight_club(log, client)
        game.game_kind = base_mod.GAME_KIND_SHADOW
        game.experiment_id, game.arm = "exp_1", "b"
        contexts["before"] = api.get_transaction_context()
        real_stream = game._start_gameplay_stream

        async def start_stream():
            contexts["stream"] = api.get_transaction_context()
            await real_stream()

        game._start_gameplay_stream = start_stream
        real_timer = game._round_timer

        async def timer():
            contexts["round_timer"] = api.get_transaction_context()
            await real_timer()

        game._round_timer = timer
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: "round_timer" in contexts)
        await _stop(game, task, client)
        assert len(client.streams) == 1

    await asyncio.create_task(session())

    before = contexts["before"]
    assert before.attributes["game_kind"] == "shadow"
    assert "game_mode" not in before.attributes and before.targeting_key is None
    for when in ("stream", "round_timer"):
        ctx = contexts[when]
        assert ctx.targeting_key == "Fight Club", when
        assert ctx.attributes["game_mode"] == "Fight Club", when
        assert ctx.attributes["controller_count"] == len(SERIALS), when
        assert ctx.attributes["game_kind"] == "shadow", when
        assert ctx.attributes["experiment_id"] == "exp_1", when


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


@pytest.mark.uc("UC-025", flow="A2", steps=[1], br=["BR-002", "BR-003"])
@pytest.mark.asyncio
async def test_force_end_runs_the_shared_run_end_cleanup():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)
    calls, patches = _spy_run_end(game)
    for p in patches:
        p.start()
    try:
        task = asyncio.create_task(game.run())
        await _wait_for(lambda: log.events.count_events_of_type("round_start") == 1)
        await _stop(game, task, client)
    finally:
        for p in patches:
            p.stop()

    assert "close_shield_spans" in calls, calls
    assert "clear_analytics" in calls, calls
    assert game.force_ended
    assert game.round_task is None or game.round_task.done(), "cleanup() still cancels the round timer"


@pytest.mark.uc("UC-025", flow="A2", steps=[4], br=["BR-003"])
@pytest.mark.asyncio
async def test_natural_end_ends_shield_feedback_closes_the_stream_and_runs_the_shared_cleanup():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)
    calls, patches = _spy_run_end(game)

    async def instant_loop():
        await asyncio.sleep(0.05)

    for p in patches:
        p.start()
    try:
        with patch.object(game, "_game_loop", side_effect=instant_loop):
            await asyncio.wait_for(game.run(), timeout=10)
    finally:
        for p in patches:
            p.stop()
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()

    assert calls.index("end_shield_feedback") < calls.index("end_game_impl"), calls
    assert "close_shield_spans" in calls, calls
    assert "clear_analytics" in calls, calls
    assert game.gameplay_stream is None, "the stream is released after the winner effects"
    assert game._game_outcome_attributes()["game.outcome"] == "completed"
    assert game.round_task is None or game.round_task.done()


def _disconnect_after_warmup(game, serials):
    """A disconnect seen during the intro, the countdown or the warm-up: buffered for the game start."""
    real_warmup = game._warmup_ema

    async def warmup():
        await real_warmup()
        game._warmup_disconnects.extend(serials)

    game._warmup_ema = warmup


@pytest.mark.uc("UC-025", flow="A4", steps=[5])
@pytest.mark.asyncio
async def test_a_disconnect_buffered_before_round_one_leaves_without_a_forfeit():
    """The leaver would be round 1's defender: it leaves before round 1 is seated, nobody scores."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)
    leaver = SERIALS[0]
    _disconnect_after_warmup(game, [leaver])

    task = asyncio.create_task(game.run())
    await _wait_for(lambda: log.events.count_events_of_type("round_start") == 1 or task.done())
    await _stop(game, task, client)

    round_start = log.events.get_events_of_type("round_start")[0]
    assert leaver not in (round_start["defender"], round_start["fighter"])
    assert log.events.count_events_of_type("round_end") == 0
    assert all(p.score == 0 for p in game.players.values())
    assert game.players[leaver].alive is False
    assert leaver not in game.queue
    death_effects = [
        effect
        for _, effect in log.writes("game_effect")
        if effect.serial == leaver and effect.effect == controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
    ]
    assert death_effects == [], "no death effect to a controller that is gone"


@pytest.mark.uc("UC-025", flow="A4", steps=[5, 6])
@pytest.mark.asyncio
async def test_buffered_disconnects_leaving_one_player_end_the_game_before_round_one():
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client, players=_players()[:2])
    _disconnect_after_warmup(game, [SERIALS[0]])

    try:
        await asyncio.wait_for(game.run(), timeout=10)
    finally:
        for stream in client.streams:
            if stream.producer:
                stream.producer.cancel()

    assert log.events.count_events_of_type("round_start") == 0
    assert log.events.count_events_of_type("round_end") == 0
    ended = log.events.get_events_of_type("game_ended")
    assert len(ended) == 1
    assert ended[0]["winner"] == "none"
    assert ended[0]["total_rounds"] == 0
    assert ended[0]["outcome"] == "completed"
    assert game.gameplay_stream is None, "the stream is released"


@pytest.mark.uc("UC-025", flow="A2", steps=[2, 3])
def test_the_spec_defaults():
    """A2 step 2 and 3: 22 s rounds with 4 s invincibility, 10 minimum rounds."""
    game = FightClubGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_fc_defaults",
        initial_players=_players()[:2],
    )
    assert game._invincibility_duration == 4.0
    assert game._round_duration == 22.0
    assert game._min_rounds == 10


@pytest.mark.uc("UC-025", br=["BR-002"])
@pytest.mark.asyncio
async def test_fight_club_plays_without_a_tempo_loop():
    """BR-002: no tempo loop by design: the music plays at 1.0 and the slow threshold table applies all game."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _fight_club(log, client)

    async def instant_loop():
        await asyncio.sleep(0.05)

    with (
        patch.object(game, "_music_loop") as music_loop,
        patch.object(game, "_start_game_music", wraps=game._start_game_music) as start_music,
        patch.object(game, "_game_loop", side_effect=instant_loop),
    ):
        try:
            await asyncio.wait_for(game.run(), timeout=10)
        finally:
            for stream in client.streams:
                if stream.producer:
                    stream.producer.cancel()

    music_loop.assert_not_called()
    assert game.music_loop_task is None
    # The music starts once at 1.0 and nothing moves the tempo, so the slow table applies to the end.
    start_music.assert_called_once()
    assert game.music_speed == 1.0
    assert game.tempo_intensity == 0.0
    player = next(iter(game.players.values()))
    sens = game.sensitivity.value
    assert game._compute_effective_thresholds(player) == (game.slow_warning[sens], game.slow_max[sens])
