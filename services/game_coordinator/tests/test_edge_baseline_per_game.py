"""
One-shot interventions fire only in the game whose event list carries them
(#1299, FR-137).

UC-040 BR-006 / A15, UC-053 A4 / BR-006: one-shot flags are per-game event
lists served on ``gameId``. A game's list dies with its scope when the game
ends, so a new game starts with an empty list and can never replay an earlier
game's ``audio_cue`` / ``end_game`` / ``eliminate_player``. A failed read never
fires, and a session retired while a pass is suspended in it never gets a fresh
bucket that would let later specs fire into it.

The flag client models the flag service: event lists and state values are
rendered per ``gameId``, and ending a game drops its scope. Telemetry is
disabled via conftest; the intervention metric is patched.
"""

import itertools
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from openfeature.exception import ErrorCode
from openfeature.flag_evaluation import FlagEvaluationDetails, Reason

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionManager,
    SessionView,
)
from services.game_coordinator.lifecycle_handlers import register_lifecycle_handlers
from services.game_coordinator.servicer import GameCoordinatorServicer
from services.game_coordinator.tests.conftest import FakeInterventionGame
from services.game_coordinator.tests.event_list_helpers import BASELINE, EDGE_FLAGS, edge_event

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}
GAME_KINDS = ["real", "shadow"]

# A representative payload per one-shot flag (eliminate/revive carry a serial;
# controller_effect "<serial>:<effect>"; end_game carries none).
_PAYLOAD = {
    "audio_cue": "agent_cue",
    "controller_effect": ":rumble",
    "eliminate_player": "p1",
    "revive_player": "p1",
    "end_game": "",
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class GameScopedFlagClient:
    """flagd stand-in for the flag service's per-game rendering.

    ``scoped[(flag, game_id)]`` is the value in one game's scope (an event list
    for one-shot flags, a plain value for state flags); ``end(game_id)`` drops
    the scope like the flag service does when the game ends. Keys in
    ``failing`` resolve like the OpenFeature client on a flagd error: no
    exception, the default, ``reason=ERROR`` and an error code.
    """

    def __init__(self):
        self.scoped: dict[tuple[str, str], object] = {}
        self.failing: set[str] = set()

    def end(self, game_id):
        for key in [k for k in self.scoped if k[1] == game_id]:
            del self.scoped[key]

    def _lookup(self, key, default, ctx):
        game_id = (getattr(ctx, "attributes", None) or {}).get("gameId", "")
        return self.scoped.get((key, game_id), default)

    def get_object_details(self, key, default, ctx=None):
        if key in self.failing:
            return FlagEvaluationDetails(
                flag_key=key,
                value=default,
                reason=Reason.ERROR,
                error_code=ErrorCode.PROVIDER_NOT_READY,
                error_message="flagd unavailable",
            )
        return FlagEvaluationDetails(flag_key=key, value=self._lookup(key, BASELINE, ctx))

    def get_string_details(self, key, default, ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=str(self._lookup(key, default, ctx)), variant="none")

    def get_float_details(self, key, default, ctx=None):
        return FlagEvaluationDetails(flag_key=key, value=float(self._lookup(key, default, ctx)), variant="none")

    def get_string_value(self, key, default, ctx=None):
        return str(self._lookup(key, default, ctx))

    def get_integer_value(self, key, default, ctx=None):
        return int(self._lookup(key, default, ctx))

    def get_float_value(self, key, default, ctx=None):
        return float(self._lookup(key, default, ctx))

    def get_object_value(self, key, default, ctx=None):
        return self._lookup(key, default, ctx)


class AgentStub:
    """Agent domain: every intervention allowed, generous backstop."""

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.coordinator_backstop_per_minute":
            return 1000
        if key == "policy.battery_threshold":
            return 0
        return default

    def get_float_value(self, key, default, _ctx=None):
        return default

    def get_string_value(self, key, default, _ctx=None):
        if key == "interventions_allowed":
            return ",".join(sorted(ALL_TYPES))
        return default

    def get_object_value(self, key, default, _ctx=None):
        return default


class Harness:
    """A manager plus a mutable live-session list, mimicking the servicer."""

    def __init__(self):
        self.flags = GameScopedFlagClient()
        self.views: list[SessionView] = []
        self.events: dict[str, list] = {}
        self.end_game_fn = AsyncMock(return_value=(True, ""))
        counter = itertools.count()
        self.mgr = InterventionManager(
            event_publisher=self._publisher("__primary_bus__"),
            get_sessions=lambda: list(self.views),
            time_fn=lambda: 1000.0 + next(counter) * 0.0,
            end_game_fn=self.end_game_fn,
        )
        self.mgr._interventions_client = self.flags
        self.mgr._agent_client = AgentStub()
        self.mgr._rate_limiter.set_budget(1000.0)
        self.mgr.register_ambient_handlers()
        register_lifecycle_handlers(self.mgr)

    def _publisher(self, game_id):
        bucket = self.events.setdefault(game_id, [])

        async def publish(event_type, data):
            bucket.append((event_type, data))

        return publish

    def start_game(self, game_id: str, *, real: bool = True) -> FakeInterventionGame:
        game = FakeInterventionGame()
        self.views.append(SessionView(game_id=game_id, game=game, publish=self._publisher(game_id), is_real=real))
        return game

    def end_game(self, game_id: str) -> None:
        """Retire a game like the servicer: forget it and end its flag scope."""
        self.views = [v for v in self.views if v.game_id != game_id]
        self.mgr.forget_session(game_id)
        self.flags.end(game_id)

    async def flag_change(self) -> None:
        """Any flag write: the coordinator re-evaluates every flag."""
        with patch("services.game_coordinator.metrics.interventions_total"):
            await self.mgr.evaluate_all()

    def published(self, game_id: str, flag_key: str) -> list[dict]:
        """Every agent_intervention of the flag's type published for the game, blocked or not."""
        type_id = next(s.type_id for s in INTERVENTION_SPECS if s.flag_key == flag_key)
        return [
            d for et, d in self.events.get(game_id, []) if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id
        ]

    def applied(self, game_id: str, flag_key: str) -> list[dict]:
        type_id = next(s.type_id for s in INTERVENTION_SPECS if s.flag_key == flag_key)
        return [
            d
            for et, d in self.events.get(game_id, [])
            if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id and d["blocked"] == "false"
        ]


# --------------------------------------------------------------------------- #
# BR-006 / A15 at the manager seam
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.parametrize("flag_key", EDGE_FLAGS)
@pytest.mark.asyncio
async def test_event_of_an_ended_game_is_not_replayed_in_the_next_game(flag_key):
    """Game N applies an event; game N+1 starts after N's scope ended. A flag
    change in N+1 applies nothing; an event written for N+1 applies once."""
    h = Harness()
    h.start_game("game_n")
    h.flags.scoped[(flag_key, "game_n")] = edge_event(flag_key, _PAYLOAD[flag_key])
    await h.flag_change()
    assert len(h.applied("game_n", flag_key)) == 1
    h.end_game("game_n")

    h.start_game("game_n1")
    h.flags.scoped[("global_difficulty_factor", "game_n1")] = 1.5  # unrelated write
    await h.flag_change()
    # A15 step 2: nothing applied and no agent_intervention published at all.
    assert h.published("game_n1", flag_key) == []

    h.flags.scoped[(flag_key, "game_n1")] = edge_event(flag_key, _PAYLOAD[flag_key])
    await h.flag_change()
    await h.flag_change()
    assert len(h.applied("game_n1", flag_key)) == 1


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.parametrize("flag_key", ["end_game", "eliminate_player"])
@pytest.mark.parametrize("all_flags_failing", [True, False], ids=["flagd-down", "one-flag-error"])
@pytest.mark.asyncio
async def test_read_error_on_the_games_first_read_never_fires(flag_key, all_flags_failing):
    """flagd errors on the game's first read (all flags, or only this one): the
    error default is never taken for an event and nothing fires. On recovery
    the game's own event applies, once."""
    h = Harness()
    game = h.start_game("game_n1")
    h.flags.scoped[(flag_key, "game_n1")] = edge_event(flag_key, "p1")
    h.flags.failing = set(EDGE_FLAGS) if all_flags_failing else {flag_key}
    await h.flag_change()
    assert h.published("game_n1", flag_key) == []
    h.end_game_fn.assert_not_awaited()
    assert game.killed == []
    assert flag_key not in h.mgr._last_event["game_n1"]

    h.flags.failing = set()  # recovery
    await h.flag_change()
    await h.flag_change()
    assert len(h.applied("game_n1", flag_key)) == 1


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
def test_forget_session_drops_all_per_game_state():
    """A retired session leaves no event position, baseline or dispatcher."""
    h = Harness()
    h.mgr._last_event["game_x"] = {"end_game": ("e1", 3)}
    h.mgr._state_bucket("game_x")["volume_override"] = 0.5
    h.mgr._dispatcher_for(SessionView(game_id="game_x", game=None, publish=h._publisher("game_x")))
    h.mgr.forget_session("game_x")
    assert "game_x" not in h.mgr._last_event
    assert "game_x" not in h.mgr._last_state_value
    assert "game_x" not in h.mgr._dispatchers


# --------------------------------------------------------------------------- #
# A session retired WHILE evaluate_all is suspended inside it (handler awaits)
# must not get a fresh, empty bucket for its remaining specs (#1299 review)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_retire_mid_evaluation_does_not_fire_later_end_game():
    """Shadow S retires while evaluate_all awaits S's eliminate handler; S's
    later end_game spec must not fire into the retired game."""
    h = Harness()
    game = h.start_game("shadow_s", real=False)
    h.flags.scoped[("end_game", "shadow_s")] = edge_event("end_game")
    original_kill = game._kill_player

    async def kill_and_retire(serial, accel, reason=None):
        h.mgr.forget_session("shadow_s")  # the servicer retires S concurrently
        h.views = [v for v in h.views if v.game_id != "shadow_s"]
        await original_kill(serial, accel, reason)

    game._kill_player = kill_and_retire
    h.flags.scoped[("eliminate_player", "shadow_s")] = edge_event("eliminate_player", "p2")
    await h.flag_change()

    assert game.killed == ["p2"]
    h.end_game_fn.assert_not_awaited()
    assert "shadow_s" not in h.mgr._last_event
    assert "shadow_s" not in h.mgr._last_state_value


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_retire_mid_evaluation_does_not_apply_later_state_flags():
    """Same race for state flags: once S is forgotten mid-pass, a later state
    flag is not applied to the dead game and no bucket is re-created."""
    h = Harness()
    h.start_game("shadow_s", real=False)

    async def tempo_handler_that_retires(_ctx):
        h.mgr.forget_session("shadow_s")
        h.views = [v for v in h.views if v.game_id != "shadow_s"]

    h.mgr.register_handler("music_tempo_override", tempo_handler_that_retires)
    h.flags.scoped[("music_tempo_override", "shadow_s")] = 1.3  # first spec in registry order
    h.flags.scoped[("global_difficulty_factor", "shadow_s")] = 1.5  # later state spec
    await h.flag_change()

    assert len(h.applied("shadow_s", "music_tempo_override")) == 1
    assert h.applied("shadow_s", "global_difficulty_factor") == []
    assert "shadow_s" not in h.mgr._last_state_value
    assert "shadow_s" not in h.mgr._last_event


# --------------------------------------------------------------------------- #
# BR-006 at the servicer seam: real and shadow games alike
# --------------------------------------------------------------------------- #
class _MockSpanContext:
    trace_id = 0
    span_id = 0


class _MockSpan:
    def set_attribute(self, _key, _value):
        pass

    def add_event(self, _name, attributes=None):
        pass

    def get_span_context(self):
        return _MockSpanContext()


def _start_config(game_kind: str):
    origin = game_coordinator_pb2.GAME_ORIGIN_MENU if game_kind == "real" else game_coordinator_pb2.GAME_ORIGIN_AGENT
    return game_coordinator_pb2.StartGameConfig(
        game_name="FFA",
        players=[game_coordinator_pb2.Player(serial=s) for s in ("p1", "p2")],
        sensitivity=2,
        origin=origin,
    )


async def _start_through_servicer(game_kind: str):
    """Start a real or shadow game through the servicer with fake flag clients.

    Returns (servicer, flags, end_game_fn, game_id, game). The game thread is
    not run; the test installs the built game instance itself.
    """
    servicer = GameCoordinatorServicer()
    mgr = servicer.intervention_manager
    flags = GameScopedFlagClient()
    mgr._interventions_client = flags
    mgr._agent_client = AgentStub()
    mgr._rate_limiter.set_budget(1000.0)
    end_game_fn = AsyncMock(return_value=(True, ""))
    mgr._end_game_fn = end_game_fn

    with (
        patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"),
        patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None),
    ):
        ok, game_id = await servicer._start_game_from_config(_start_config(game_kind), _MockSpan())
    assert ok, game_id
    session = servicer.sessions[game_id]
    assert session.game_kind == ("primary" if game_kind == "real" else "shadow")
    game = FakeInterventionGame()
    session.current_game = game  # the game thread built the instance
    return servicer, flags, end_game_fn, game_id, game


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.parametrize("flag_key", ["end_game", "eliminate_player"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_servicer_started_game_applies_its_first_event_once(game_kind, flag_key):
    """The very first flag change in a game started through the servicer is an
    event in the game's list: it applies, once, with no seeding at start."""
    servicer, flags, end_game_fn, game_id, game = await _start_through_servicer(game_kind)
    mgr = servicer.intervention_manager
    try:
        flags.scoped[(flag_key, game_id)] = edge_event(flag_key, "p2")
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()
            await mgr.evaluate_all()
        if flag_key == "end_game":
            end_game_fn.assert_awaited_once_with(game_id, "agent_intervention")
        else:
            assert game.killed == ["p2"]
    finally:
        mgr.stop()


# --------------------------------------------------------------------------- #
# The servicer forgets a session's per-game state on every retire path
# --------------------------------------------------------------------------- #
def _assert_forgotten(mgr, game_id):
    assert game_id not in mgr._last_event
    assert game_id not in mgr._last_state_value
    assert game_id not in mgr._dispatchers


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_servicer_retire_forgets_intervention_state(game_kind):
    servicer, _flags, _end_game_fn, game_id, _game = await _start_through_servicer(game_kind)
    mgr = servicer.intervention_manager
    try:
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()
        assert game_id in mgr._last_event
        servicer._retire_session(game_id)
        _assert_forgotten(mgr, game_id)
    finally:
        mgr.stop()


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_servicer_ended_session_sweep_forgets_intervention_state():
    """A naturally ENDED session is swept at the next admission; its per-game
    state goes with it."""
    servicer, _flags, _end_game_fn, ended_id, _game = await _start_through_servicer("shadow")
    mgr = servicer.intervention_manager
    try:
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()
        assert ended_id in mgr._last_event
        servicer.sessions[ended_id].game_state = game_coordinator_pb2.GameState.ENDED
        with (
            patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"),
            patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None),
        ):
            ok, new_id = await servicer._start_game_from_config(_start_config("shadow"), _MockSpan())
        assert ok, new_id
        assert ended_id not in servicer.sessions
        _assert_forgotten(mgr, ended_id)
    finally:
        mgr.stop()
