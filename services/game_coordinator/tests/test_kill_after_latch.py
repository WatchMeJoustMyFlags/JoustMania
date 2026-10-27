"""The result is final once latched (#2019, #2020).

#2020: FFA's win check latches the root-span outcome first and publishes
``game_winner`` / ``game_tie`` only when that outcome is not ``force_ended``
(UC-020 BR-003, UC-027 step 3), the latch-first pattern of the other modes.

#2019: the default kill gate declines every kill once the game's outcome is
latched, at the first and at the second ``_accepts_kill`` check, so a kill after
the win (an ``eliminate_player`` of the winner during the hold or teardown) is
refused with ``eliminate_refused`` and counts nothing (UC-030 step 9, UC-040 A22),
and NonStop's winner, computed from the deaths, cannot flip (UC-026 BR-003).
"""

import sys
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
from opentelemetry import trace

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.base import GameState, Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.random_teams import RandomTeamsGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.games.traitor import TraitorGame
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.games.zombie import ZombieGame
from services.game_coordinator.intervention_errors import InterventionNotApplied
from services.game_coordinator.interventions import (
    BLOCK_ELIMINATE_REFUSED,
    INTERVENTION_SPECS,
    InterventionContext,
)
from services.game_coordinator.lifecycle_handlers import handle_eliminate_player

DEATH = controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
ELIMINATE_SPEC = next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player")
DEFAULT_GATE_MODES = [
    FFAGame,
    SimpleTeamsGame,
    RandomTeamsGame,
    TraitorGame,
    WerewolfGame,
    ZombieGame,
    NonstopJoustGame,
]


class _RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def death_effects(self):
        return [
            m.game_effect.serial for m in self.writes if m.HasField("game_effect") and m.game_effect.effect == DEATH
        ]


def _game(cls, serials=("p1", "p2", "p3")):
    events = EventCollector()
    game = cls(
        controller_manager_client=MockControllerManagerService(num_controllers=len(serials)),
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_kill_after_latch",
    )
    game.gameplay_stream = _RecordingStream()
    game.running = True
    game.sounds = []

    async def record_sound(sound, *_args, **_kwargs):
        game.sounds.append(sound)

    game._play_sound = record_sound
    for serial in serials:
        span = create_autospec(trace.Span, instance=True)
        span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
        game.players[serial] = Player(serial=serial, alive=True, color=(255, 0, 0), span=span)
    return game, events


def _stub_mode_handler(game):
    """The mode's death handler as a plain death: the gate under test runs before it."""

    async def impl(serial, accel_mag):  # noqa: ARG001
        game.players[serial].alive = False

    game._kill_player_impl = impl
    game._sends_death_effect = lambda serial: True  # noqa: ARG005
    game._player_death_event_attributes = lambda serial: {}  # noqa: ARG005


def _eliminate_ctx(game, serial) -> InterventionContext:
    return InterventionContext(
        spec=ELIMINATE_SPEC,
        value=serial,
        payload=serial,
        target_serial=serial,
        game=game,
        objective="balanced",
    )


def _death_events(span):
    return [c for c in span.add_event.call_args_list if c.args and c.args[0] == "player_death"]


def _assert_nothing_counted(game, events, serial, metrics_mock):
    assert game.players[serial].alive is True
    assert game.dead_count == 0
    assert game._elimination_count == 0
    metrics_mock.player_alive.labels.assert_not_called()
    metrics_mock.player_elimination_order.labels.assert_not_called()
    metrics_mock.player_deaths_total.labels.assert_not_called()
    assert Sound.SFX_EXPLOSION not in game.sounds
    assert _death_events(game.players[serial].span) == []
    assert events.get_events_of_type(GameEvent.PLAYER_DEATH) == []
    assert game.gameplay_stream.death_effects() == []


# ---------------------------------------------------------------------------
# #2020: FFA latch-first win check
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_ffa_force_end_during_the_last_kills_explosion_publishes_no_winner():
    """The bug hunt's probe p1: the force-end lands while the lethal kill awaits its explosion."""
    game, events = _game(FFAGame, ("p1", "p2"))

    async def force_end_on_explosion(sound, *_args, **_kwargs):
        if sound == Sound.SFX_EXPLOSION:
            game.force_end("operator")

    game._play_sound = force_end_on_explosion

    with patch("services.game_coordinator.games.base.metrics"):
        # The kill gate closes with the force-end: the kill counts nothing (#2033).
        assert await game._kill_player("p2", 3.0) is False

    assert game.players["p2"].alive is True
    assert not await game._check_win_condition()
    assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
    assert events.get_events_of_type(GameEvent.GAME_TIE) == []
    assert game.game_outcome() == "force_ended"


@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_ffa_force_end_between_the_check_and_the_latch_publishes_no_winner():
    """A cross-thread force-end after the win check started, before the outcome latch (#2002 recipe)."""
    game, events = _game(FFAGame, ("p1", "p2"))
    game.players["p2"].alive = False
    latch = game._latch_game_outcome

    def force_end_then_latch():
        game.force_end("operator")
        latch()

    game._latch_game_outcome = force_end_then_latch

    assert not await game._check_win_condition()
    assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
    assert game.game_outcome() == "force_ended"
    assert game._winner_serial() is None


@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.asyncio
async def test_ffa_win_is_latched_before_its_publish_and_not_published_again():
    """A force-end landing during the game_winner publish keeps the winner; a later check publishes nothing."""
    holder: dict = {}
    events = EventCollector()

    async def publish(event_type, data):
        await events.publish(event_type, data)
        if event_type == GameEvent.GAME_WINNER:
            holder["game"].force_end("operator")

    game, _ = _game(FFAGame, ("p1", "p2"))
    game.event_publisher = publish
    holder["game"] = game
    game.players["p2"].alive = False

    assert await game._check_win_condition()
    assert await game._check_win_condition()  # latched: not published again
    assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "p1"}]
    assert game.game_outcome() == "winner"
    assert game._winner_serial() == "p1"


@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.asyncio
async def test_ffa_tie_is_latched_before_its_publish():
    game, events = _game(FFAGame, ("p1", "p2"))
    for player in game.players.values():
        player.alive = False

    assert await game._check_win_condition()
    assert game._latched_outcome == {"game.outcome": "tie"}
    assert len(events.get_events_of_type(GameEvent.GAME_TIE)) == 1


# ---------------------------------------------------------------------------
# #2019: no kill after the outcome latch (default kill gate)
# ---------------------------------------------------------------------------


@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.asyncio
async def test_eliminate_of_the_ffa_winner_after_game_winner_is_refused():
    """The bug hunt's probe p2: the winner is not 'killed' during the hold."""
    game, events = _game(FFAGame, ("p1", "p2"))
    game.players["p2"].alive = False
    assert await game._check_win_condition()
    events.clear()

    with patch("services.game_coordinator.games.base.metrics") as m, pytest.raises(InterventionNotApplied) as exc:
        await handle_eliminate_player(_eliminate_ctx(game, "p1"))

    assert exc.value.reason == BLOCK_ELIMINATE_REFUSED
    _assert_nothing_counted(game, events, "p1", m)
    assert game._winner_serial() == "p1"


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.parametrize("game_cls", DEFAULT_GATE_MODES, ids=lambda c: c.__name__)
@pytest.mark.asyncio
async def test_default_gate_declines_a_kill_after_the_latch(game_cls):
    """First check: a kill entering after the latch is declined before any bookkeeping."""
    game, events = _game(game_cls)
    _stub_mode_handler(game)
    game._latch_game_outcome()
    game.players["p1"].shield_charge_seconds = 5.0

    with patch("services.game_coordinator.games.base.metrics") as m:
        assert await game._kill_player("p1", 3.0) is False

    assert game.players["p1"].shield_charge_seconds == 5.0  # declined ahead of the charge discard
    _assert_nothing_counted(game, events, "p1", m)


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.parametrize("game_cls", DEFAULT_GATE_MODES, ids=lambda c: c.__name__)
@pytest.mark.asyncio
async def test_default_gate_declines_a_kill_whose_explosion_spans_the_latch(game_cls):
    """Second check: the outcome latches while the kill awaits its explosion."""
    game, events = _game(game_cls)
    _stub_mode_handler(game)

    async def latch_on_explosion(sound, *_args, **_kwargs):
        game.sounds.append(sound)
        if sound == Sound.SFX_EXPLOSION:
            game._latch_game_outcome()

    game._play_sound = latch_on_explosion

    with patch("services.game_coordinator.games.base.metrics") as m:
        assert await game._kill_player("p1", 3.0) is False

    game.sounds.remove(Sound.SFX_EXPLOSION)  # the explosion has played (UC-030 step 9)
    _assert_nothing_counted(game, events, "p1", m)


@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.asyncio
async def test_kills_before_the_latch_are_unchanged():
    game, events = _game(FFAGame)

    with patch("services.game_coordinator.games.base.metrics"):
        assert await game._kill_player("p1", 3.0) is True

    assert game.players["p1"].alive is False
    assert game.dead_count == 1
    assert game.gameplay_stream.death_effects() == ["p1"]


@pytest.mark.uc("UC-026", br=["BR-003"])
@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.asyncio
async def test_nonstop_deaths_after_the_time_limit_latch_do_not_flip_the_winner():
    """The bug hunt's probe p3: eliminations during the end-game step add no death."""
    events = EventCollector()
    client = MockControllerManagerService(num_controllers=3)
    game = NonstopJoustGame(
        controller_manager_client=client,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_nonstop_latch",
    )

    async def silent(*_args, **_kwargs):
        return None

    game._play_sound = silent
    await game._initialize_players_impl(client.controllers)
    leader, second, third = list(game.players)
    game.players[leader].deaths = 0
    game.players[second].deaths = 1
    game.players[third].deaths = 4
    game.gameplay_stream = _RecordingStream()
    game.running = True
    game.state = GameState.RUNNING
    game.start_time = time.time()
    game._time_limit_reached = True
    game._latch_game_outcome()  # the loop latches at the time limit (UC-026 step 3)

    with patch("services.game_coordinator.games.base.metrics"):
        for _ in range(3):
            with pytest.raises(InterventionNotApplied) as exc:
                await handle_eliminate_player(_eliminate_ctx(game, leader))
            assert exc.value.reason == BLOCK_ELIMINATE_REFUSED

    assert game.players[leader].deaths == 0
    game.running = False  # skip the winner hold
    await game._end_game_impl()

    ended = events.get_events_of_type(GameEvent.GAME_ENDED)
    assert ended[-1]["winner"] == leader
