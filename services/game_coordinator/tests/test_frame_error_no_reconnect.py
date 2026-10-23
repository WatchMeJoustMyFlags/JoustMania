"""A frame-processing error ends the game once with game_error, no stream reconnect (#2001).

Before #2001 the base game loop caught every exception from ``_process_gameplay_update``
on its stream-error path: a mode's kill handler that raised made the game reconnect a
healthy gameplay stream three times before it ended with ``game_error``. Only an error
of the gameplay call (a read, or a gRPC error from a write on it) is a stream error now
(UC-030 BR-006); any other error while a frame is processed ends the loop at once and
the run ends with ``game_error`` (UC-020 BR-003): ``run()`` raises the error, the session
publishes ``game_error`` for any error ``run()`` raises (and the base ``run()`` publishes it too).

The runs use #1912's queue stream (a frame every 10 ms, ``client.streams`` counts the
opened calls) and set the reconnect backoff to 10 ms, so the old behaviour fails the
one-stream assertion quickly instead of timing out.
"""

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_random_teams_formation import SERIALS, QueueControllerManager, RecordingLog, _players, _wait_for

from lib.types import GameEvent
from services.game_coordinator.games import fight_club as fight_club_mod
from services.game_coordinator.games import tournament as tournament_mod
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.fight_club import FightClubGame
from services.game_coordinator.games.tournament import TournamentGame
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

WIN_HOLD_LOG = "Win condition met"


@pytest.fixture(autouse=True)
def fast_start():
    """No countdown, a short rainbow and short Fight Club / Tournament intros."""
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with (
        patch.object(get_config_manager(), "get_config", return_value=config),
        patch.object(fight_club_mod, "FC_INTRO_DURATION_S", 0.1),
        patch.object(tournament_mod, "TOURNAMENT_INTRO_DURATION_S", 0.1),
    ):
        yield config


def _ffa(log, client):
    return FFAGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_frame_error_ffa",
        initial_players=_players(),
    )


def _fight_club(log, client):
    return FightClubGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_frame_error_fight_club",
        initial_players=_players(),
    )


def _tournament(log, client):
    game = TournamentGame(
        controller_manager_client=client,
        event_publisher=log.publish,
        audio_client=None,
        game_id="test_frame_error_tournament",
        initial_players=_players(),
        invincibility_seconds=0.0,
    )
    game._match_duration = 30.0
    game._time_between_matches = 0.0
    return game


MODES = {"ffa": _ffa, "fight_club": _fight_club, "tournament": _tournament}


async def _run_until_error(game, client):
    """Run the game to its end; return the error it raised and the reconnects it counted."""
    task = asyncio.create_task(game.run())
    with patch("services.game_coordinator.games.base.metrics.gameplay_stream_reconnects_total") as reconnects:
        try:
            await asyncio.wait_for(task, timeout=10)
            error = None
        except Exception as e:  # noqa: BLE001 - the error under test
            error = e
        finally:
            for stream in client.streams:
                if stream.producer:
                    stream.producer.cancel()
    return error, reconnects.labels.return_value.inc.call_count


def _count_frames(game) -> list[int]:
    """Count the frames the game loop processes (the mode's own override still runs)."""
    frames: list[int] = []
    real_process = game._process_gameplay_update

    async def process(update):
        frames.append(update.timestamp)
        return await real_process(update)

    game._process_gameplay_update = process
    return frames


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.parametrize("mode", list(MODES))
@pytest.mark.asyncio
async def test_a_kill_handler_error_ends_the_game_once_with_game_error(mode):
    """A disconnect in the game loop reaches a kill handler that raises: one stream, game_error, no reconnect."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = MODES[mode](log, client)
    game._RECONNECT_BACKOFF_S = 0.01

    async def failing_kill_handler(_serial, _accel_mag):
        raise RuntimeError("kill handler bug")

    game._kill_player_impl = failing_kill_handler
    frames = _count_frames(game)

    async def disconnect_in_the_loop():
        await _wait_for(lambda: len(frames) >= 1)
        stream = client.streams[-1]
        stream.disconnect_at[stream.seq + 1] = SERIALS[-1]

    trigger = asyncio.create_task(disconnect_in_the_loop())
    error, reconnects = await _run_until_error(game, client)
    trigger.cancel()

    assert isinstance(error, RuntimeError) and "kill handler bug" in str(error), f"unexpected end: {error!r}"
    assert len(client.streams) == 1, "a kill handler error reconnected the healthy gameplay stream"
    assert reconnects == 0
    if mode == "ffa":
        assert log.events.get_events_of_type(GameEvent.GAME_ERROR), "the base run did not publish game_error"


@pytest.mark.uc("UC-025", flow="main", steps=[3])
@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.asyncio
async def test_a_tournament_bracket_error_ends_without_the_win_hold(caplog):
    """A failing bracket step ends the loop at once: no "Win condition met" log, no 1 s hold, game_error."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _tournament(log, client)
    frames = _count_frames(game)

    async def failing_bracket_tick():
        if len(frames) >= 3:
            raise RuntimeError("bracket bug")

    game._bracket_tick = failing_bracket_tick
    with caplog.at_level(logging.INFO, logger="services.game_coordinator.games.base"):
        error, reconnects = await _run_until_error(game, client)

    assert isinstance(error, RuntimeError) and "bracket bug" in str(error), f"unexpected end: {error!r}"
    assert WIN_HOLD_LOG not in caplog.text, "a bracket error was held as a win"
    assert len(client.streams) == 1
    assert reconnects == 0
    assert not log.events.get_events_of_type(GameEvent.GAME_ENDED), "a bracket error ended the game as a natural end"


@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.asyncio
async def test_a_frame_error_after_a_force_end_ends_the_game_as_force_ended(caplog):
    """A kill handler that force-ends the game and then raises: the loop ends quietly, outcome force_ended."""
    log = RecordingLog()
    client = QueueControllerManager(log)
    game = _ffa(log, client)
    game._RECONNECT_BACKOFF_S = 0.01

    async def force_end_then_fail(_serial, _accel_mag):
        game.force_end("admin stop")
        raise RuntimeError("kill handler bug after the stop")

    game._kill_player_impl = force_end_then_fail
    frames = _count_frames(game)

    async def disconnect_in_the_loop():
        await _wait_for(lambda: len(frames) >= 1)
        stream = client.streams[-1]
        stream.disconnect_at[stream.seq + 1] = SERIALS[-1]

    trigger = asyncio.create_task(disconnect_in_the_loop())
    with caplog.at_level(logging.WARNING, logger="services.game_coordinator.games.base"):
        error, reconnects = await _run_until_error(game, client)
    trigger.cancel()

    assert error is None, f"a frame error after a force-end failed the run: {error!r}"
    assert "Frame processing failed after the game stopped" in caplog.text
    assert game._game_outcome_attributes().get("game.outcome") == "force_ended"
    assert not log.events.get_events_of_type(GameEvent.GAME_ERROR), "a stopped game published game_error"
    assert len(client.streams) == 1
    assert reconnects == 0
