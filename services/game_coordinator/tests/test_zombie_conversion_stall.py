"""UC-023 step 3 (#2023): a conversion does not stall the game loop.

The kill handler runs inline in the game loop. The 0.5 s between a converted
human's death flash and its zombie colour runs in a tracked task, so the kill
path returns at once and the loop keeps processing frames.
"""

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest
import pytest_asyncio

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, async_noop

from proto import controller_manager_pb2
from services.game_coordinator.games.zombie import ZOMBIE_COLOR, ZombieGame

DEATH = ("effect", controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH)
GREY = ("base", tuple(ZOMBIE_COLOR))
CONVERSION_DELAY = 0.5


class RecordingGameplayStream:
    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class DeadGameplayStream(RecordingGameplayStream):
    """A call that ended under the zombie-colour write (UC-030 BR-006)."""

    async def write(self, message):
        if message.HasField("base_color"):
            raise asyncio.InvalidStateError("call finished")
        await super().write(message)


def _writes_for(stream, serial):
    out = []
    for m in stream.messages:
        if m.HasField("game_effect") and m.game_effect.serial == serial:
            out.append(("effect", m.game_effect.effect))
        elif m.HasField("base_color") and m.base_color.serial == serial:
            c = m.base_color.color
            out.append(("base", (c.r, c.g, c.b)))
    return out


@pytest_asyncio.fixture
async def game():
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    events = EventCollector()
    g = ZombieGame(
        controller_manager_client=mock_cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_zombie_conversion_stall",
    )
    await g._initialize_players_impl(mock_cm.controllers)
    g.gameplay_stream = RecordingGameplayStream()
    g.running = True
    g._play_sound = async_noop
    g.events = events
    yield g
    await g.cleanup()


@pytest.fixture
def held_delay(monkeypatch):
    """Hold the conversion delay until ``release`` is set; other sleeps run normally.

    Bounded, so a kill path that awaits the delay inline fails the test instead of
    hanging it.
    """
    real_sleep = asyncio.sleep
    release = asyncio.Event()

    async def sleep(delay, *args, **kwargs):
        if delay == CONVERSION_DELAY:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(release.wait(), timeout=2.0)
            return None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr("services.game_coordinator.game_clock.sleep", sleep)
    return release


@pytest.mark.uc("UC-023", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_conversion_kill_returns_before_the_zombie_colour_delay(game, held_delay):
    human = game.human_serials[0]
    kill = asyncio.create_task(game._kill_player(human, 3.0))
    done, _ = await asyncio.wait({kill}, timeout=1.0)

    assert kill in done, "the kill handler waited for the zombie-colour delay inside the game loop"
    assert kill.result() is True
    assert game.players[human].is_zombie and game.players[human].alive
    assert len(game.events.get_events_of_type("human_converted")) == 1
    assert _writes_for(game.gameplay_stream, human) == [DEATH]

    held_delay.set()
    await game._conversion_colour_tasks[human]
    assert _writes_for(game.gameplay_stream, human) == [DEATH, GREY]
    assert human not in game._conversion_colour_tasks


@pytest.mark.uc("UC-023", flow="main", steps=[3, 4])
@pytest.mark.asyncio
async def test_zombie_killed_during_the_delay_stays_dark(game, held_delay):
    human = game.human_serials[0]
    game.death_grace_period = 0.0
    await game._kill_player(human, 3.0)
    game.players[human].grace_until = 0.0
    task = game._conversion_colour_tasks[human]

    await game._kill_player(human, 5.0)
    held_delay.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert task.cancelled(), "the zombie kill must drop the pending conversion colour"
    assert human not in game._conversion_colour_tasks
    writes = _writes_for(game.gameplay_stream, human)
    assert GREY not in writes
    assert writes[-1] == DEATH


@pytest.mark.uc("UC-023", flow="main", steps=[3, 4])
@pytest.mark.asyncio
async def test_zombie_respawned_during_the_delay_gets_no_second_colour(game, held_delay):
    human = game.human_serials[0]
    game.death_grace_period = 0.0
    await game._kill_player(human, 3.0)
    game.players[human].grace_until = 0.0
    task = game._conversion_colour_tasks[human]
    await game._kill_player(human, 5.0)

    before_respawn = len(_writes_for(game.gameplay_stream, human))
    assert await game._respawn_player(human) is True
    respawn_writes = _writes_for(game.gameplay_stream, human)[before_respawn:]
    held_delay.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Only the respawn's own grey and flash: the conversion's write never follows.
    assert _writes_for(game.gameplay_stream, human)[before_respawn:] == respawn_writes
    assert respawn_writes.count(GREY) == 1


@pytest.mark.uc("UC-023", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_cleanup_cancels_the_pending_zombie_colour(game, held_delay):
    stream = game.gameplay_stream
    human = game.human_serials[0]
    await game._kill_player(human, 3.0)
    task = game._conversion_colour_tasks[human]

    await game.cleanup()
    held_delay.set()
    await asyncio.sleep(0)

    assert task.cancelled(), "cleanup() left the conversion colour task running"
    assert game._conversion_colour_tasks == {}
    assert _writes_for(stream, human) == [DEATH]


@pytest.mark.uc("UC-023", flow="main", steps=[3, 5])
@pytest.mark.asyncio
@pytest.mark.real_durations
async def test_winning_conversion_turns_grey_before_the_winner_rainbow(game):
    # Real clock: the conversion delay (0.5 s) ends inside the 1 s post-win hold
    # (UC-020 BR-003), so the last convert shows grey, then the rainbow.
    game._wait_for_rainbow_effect = async_noop
    humans = list(game.human_serials)
    for human in humans:
        await game._kill_player(human, 3.0)
    assert await game._check_win_condition() is True

    await game._hold_win_display()
    await game._end_game_impl()

    rainbow = ("effect", controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW)
    for human in humans:
        assert _writes_for(game.gameplay_stream, human) == [DEATH, GREY, rainbow]


@pytest.mark.uc("UC-023", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_force_end_during_the_delay_sets_no_colour(game, held_delay):
    # The game stops before teardown runs cleanup() (which would cancel the task
    # and drop the stream), so the delay can end while the game is not running.
    stream = game.gameplay_stream
    human = game.human_serials[0]
    await game._kill_player(human, 3.0)
    task = game._conversion_colour_tasks[human]

    game.running = False
    held_delay.set()
    await task

    assert _writes_for(stream, human) == [DEATH]
    assert human not in game._conversion_colour_tasks


@pytest.mark.uc("UC-023", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_zombie_colour_on_a_dead_stream_is_skipped(game, held_delay):
    game.gameplay_stream = DeadGameplayStream()
    human = game.human_serials[0]
    await game._kill_player(human, 3.0)
    task = game._conversion_colour_tasks[human]

    held_delay.set()
    await task

    assert task.exception() is None
    assert _writes_for(game.gameplay_stream, human) == [DEATH]
