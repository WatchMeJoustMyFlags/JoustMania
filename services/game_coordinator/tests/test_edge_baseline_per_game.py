"""
Edge-triggered interventions fire only in the game that saw them change (#1299).

UC-040 BR-006 / A15: an edge flag value present when a game starts is that
game's consumed baseline. Before the fix the per-game nonce bucket started empty
for any game that began after the coordinator started, so the first unrelated
flag change replayed the previous game's last ``audio_cue`` / ``end_game`` /
``eliminate_player`` into the new game.

The flag client models the real write surface: the agent writes no ``gameId``
targeting, so every live session (real or shadow) resolves the same value.
Telemetry is disabled via conftest; the intervention metric is patched.
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

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}
EDGE_FLAGS = sorted(spec.flag_key for spec in INTERVENTION_SPECS if spec.edge_triggered)
GAME_KINDS = ["real", "shadow"]

# A representative stale / fresh value per edge flag (eliminate/revive carry a
# serial; controller_effect "<serial>:<effect>"; end_game is nonce-only).
_PAYLOAD = {
    "audio_cue": "agent_cue",
    "controller_effect": ":rumble",
    "eliminate_player": "p1",
    "revive_player": "p1",
    "end_game": "",
}


def _edge_value(flag_key: str, nonce: str) -> str:
    return f"{nonce}:{_PAYLOAD[flag_key]}"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class UntargetedFlagClient:
    """flagd stand-in: one value per flag, whatever the gameId (agent writes no
    gameId targeting, decide-contract §11).

    Keys in ``failing`` resolve like the real OpenFeature client does on a flagd
    error: no exception, the DEFAULT value, ``reason=ERROR`` and an error code.
    """

    def __init__(self):
        self.values: dict[str, object] = {}
        self.failing: set[str] = set()

    def get_string_value(self, key, default, _ctx=None):
        if key in self.failing:
            return default
        return str(self.values.get(key, default))

    def get_string_details(self, key, default, _ctx=None):
        if key in self.failing:
            return FlagEvaluationDetails(
                flag_key=key,
                value=default,
                reason=Reason.ERROR,
                error_code=ErrorCode.PROVIDER_NOT_READY,
                error_message="flagd unavailable",
            )
        return FlagEvaluationDetails(flag_key=key, value=self.get_string_value(key, default))

    def get_integer_value(self, key, default, _ctx=None):
        return int(self.values.get(key, default))

    def get_float_value(self, key, default, _ctx=None):
        return float(self.values.get(key, default))

    def get_object_value(self, key, default, _ctx=None):
        return self.values.get(key, default)


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


class FakePlayer:
    def __init__(self, serial):
        self.serial = serial
        self.alive = True
        self.sensitivity_factor = 1.0


class FakeGame:
    """Records the effects the edge handlers apply to a live game."""

    def __init__(self):
        self.players = {"p1": FakePlayer("p1"), "p2": FakePlayer("p2")}
        self.killed: list[str] = []
        self.sounds: list[str] = []

    def get_game_name(self):
        return "Joust Free-for-All"

    async def _kill_player(self, serial, _accel, reason=None):
        self.killed.append(serial)
        self.players[serial].alive = False

    async def _play_sound(self, sound_id, priority=0):
        self.sounds.append(sound_id)


class Harness:
    """A manager plus a mutable live-session list, mimicking the servicer."""

    def __init__(self):
        self.flags = UntargetedFlagClient()
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

    def start_game(self, game_id: str, *, prime: bool = True) -> FakeGame:
        """Admit a game the way the servicer does: prime, then go live."""
        if prime:
            self.mgr.prime_session(game_id)
        game = FakeGame()
        self.views.append(SessionView(game_id=game_id, game=game, publish=self._publisher(game_id)))
        return game

    def end_game(self, game_id: str) -> None:
        self.views = [v for v in self.views if v.game_id != game_id]

    async def flag_change(self) -> None:
        """Any flagd file write: the coordinator re-evaluates every flag."""
        with patch("services.game_coordinator.metrics.interventions_total"):
            await self.mgr.evaluate_all()

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
@pytest.mark.parametrize("flag_key", EDGE_FLAGS)
@pytest.mark.asyncio
async def test_stale_edge_from_previous_game_does_not_fire_in_next_game(flag_key):
    """Game N applies an edge value; game N+1 starts with it still in flagd; an
    unrelated flag change in N+1 must apply nothing (the #1299 replay)."""
    h = Harness()
    h.start_game("game_n")
    h.flags.values[flag_key] = _edge_value(flag_key, "7-abc")
    await h.flag_change()
    assert len(h.applied("game_n", flag_key)) == 1
    h.end_game("game_n")

    h.start_game("game_n1")
    h.flags.values["global_difficulty_factor"] = 1.5  # unrelated write
    await h.flag_change()

    assert h.applied("game_n1", flag_key) == []


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("flag_key", EDGE_FLAGS)
@pytest.mark.asyncio
async def test_fresh_edge_in_next_game_fires_once(flag_key):
    """A new nonce written during game N+1 differs from its baseline: it fires
    exactly once, and re-evaluation does not fire it again."""
    h = Harness()
    h.flags.values[flag_key] = _edge_value(flag_key, "7-abc")  # stale from game N
    h.start_game("game_n1")
    await h.flag_change()
    assert h.applied("game_n1", flag_key) == []

    h.flags.values[flag_key] = _edge_value(flag_key, "8-def")
    await h.flag_change()
    await h.flag_change()

    assert len(h.applied("game_n1", flag_key)) == 1


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_stale_end_game_does_not_end_next_game_but_fresh_one_does():
    """end_game path: the stale nonce never reaches the force-end function; a
    fresh nonce in the new game does, once."""
    h = Harness()
    h.flags.values["end_game"] = "5-old:"
    h.start_game("game_n1")
    h.flags.values["audio_cue"] = "none"  # unrelated write
    await h.flag_change()
    h.end_game_fn.assert_not_awaited()

    h.flags.values["end_game"] = "6-new:"
    await h.flag_change()
    h.end_game_fn.assert_awaited_once_with("agent_intervention")


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_stale_eliminate_player_does_not_kill_in_next_game_but_fresh_one_does():
    """eliminate_player path: no one dies from the stale nonce; a fresh nonce
    in the new game eliminates the target through the game's kill path."""
    h = Harness()
    h.flags.values["eliminate_player"] = "3-old:p1"
    game = h.start_game("game_n1")
    h.flags.values["volume_override"] = 0.3  # unrelated write
    await h.flag_change()
    assert game.killed == []

    h.flags.values["eliminate_player"] = "4-new:p2"
    await h.flag_change()
    assert game.killed == ["p2"]


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_coordinator_restart_does_not_replay_stale_audio_cue(game_kind):
    """The dry-run 13:56:56 case: the coordinator restarts with no live game
    (start() primes nothing), a later game starts and an unrelated flag change
    arrives. The leftover audio_cue must not play."""
    h = Harness()
    h.flags.values["audio_cue"] = "2-n0nce:agent_cue"
    h.mgr._prime_baseline()  # start() with no live sessions
    game = h.start_game(f"game_{game_kind}")
    h.flags.values["tempo_ramp"] = "1.3:8:linear"
    await h.flag_change()

    assert game.sounds == []
    assert h.applied(f"game_{game_kind}", "audio_cue") == []


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_primed_baseline_survives_evaluation_before_game_is_live():
    """A flag change between admission and the game instance being built must
    not prune the primed baseline (the session is not yet in get_sessions)."""
    h = Harness()
    h.flags.values["end_game"] = "5-old:"
    h.mgr.prime_session("game_starting")
    await h.flag_change()  # no live session yet: prune runs
    h.views.append(SessionView(game_id="game_starting", game=FakeGame(), publish=h._publisher("game_starting")))
    await h.flag_change()

    h.end_game_fn.assert_not_awaited()


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_unprimed_live_game_fails_closed():
    """A live game that was never primed treats the current edge values as
    consumed on first evaluation (it never replays them), then fires fresh ones."""
    h = Harness()
    h.flags.values["end_game"] = "5-old:"
    h.start_game("game_unprimed", prime=False)
    await h.flag_change()
    h.end_game_fn.assert_not_awaited()

    h.flags.values["end_game"] = "6-new:"
    await h.flag_change()
    h.end_game_fn.assert_awaited_once()


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
def test_forget_session_drops_primed_baseline():
    """A retired session (even one whose game was never built) leaves no
    pending entry or baseline behind."""
    h = Harness()
    h.mgr.prime_session("game_never_built")
    assert "game_never_built" in h.mgr._pending_primed
    h.mgr.forget_session("game_never_built")
    assert "game_never_built" not in h.mgr._pending_primed
    assert "game_never_built" not in h.mgr._last_nonce


# --------------------------------------------------------------------------- #
# BR-006 fail-closed on flagd read errors (the client never raises; it returns
# the default "" with reason=ERROR)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("flag_key", ["end_game", "eliminate_player"])
@pytest.mark.parametrize("all_flags_failing", [True, False], ids=["flagd-down", "one-flag-error"])
@pytest.mark.asyncio
async def test_prime_during_flagd_error_does_not_replay_stale_value_on_recovery(flag_key, all_flags_failing):
    """Game primed while flagd errors (all flags, or only this one); flagd
    recovers with the stale value; an unrelated change must apply nothing. A
    fresh nonce afterwards still fires."""
    h = Harness()
    h.flags.values[flag_key] = _edge_value(flag_key, "5-stale")
    h.flags.failing = set(EDGE_FLAGS) if all_flags_failing else {flag_key}
    game = h.start_game("game_n1")

    h.flags.failing = set()  # recovery
    h.flags.values["global_difficulty_factor"] = 1.5  # unrelated write
    await h.flag_change()
    assert h.applied("game_n1", flag_key) == []
    h.end_game_fn.assert_not_awaited()
    assert game.killed == []

    h.flags.values[flag_key] = _edge_value(flag_key, "6-fresh")
    await h.flag_change()
    assert len(h.applied("game_n1", flag_key)) == 1


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("flag_key", ["end_game", "eliminate_player"])
@pytest.mark.asyncio
async def test_mid_game_flagd_error_does_not_refire_consumed_nonce(flag_key):
    """A nonce applied mid-game, then a transient flagd error, then recovery
    with the same value: the consumed nonce must not fire again."""
    h = Harness()
    game = h.start_game("game_n1")
    h.flags.values[flag_key] = _edge_value(flag_key, "7-once")
    await h.flag_change()
    assert len(h.applied("game_n1", flag_key)) == 1

    h.flags.failing = {flag_key}
    await h.flag_change()  # error read: baseline must not move to ""
    h.flags.failing = set()
    await h.flag_change()

    assert len(h.applied("game_n1", flag_key)) == 1
    if flag_key == "end_game":
        h.end_game_fn.assert_awaited_once()
    else:
        assert game.killed == ["p1"]


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_unprimed_game_during_flagd_error_fails_closed():
    """The never-primed fallback during a flagd error records unknown, then
    adopts the recovered stale value without firing."""
    h = Harness()
    h.flags.values["end_game"] = "5-old:"
    h.flags.failing = {"end_game"}
    h.start_game("game_unprimed", prime=False)
    await h.flag_change()
    h.flags.failing = set()
    await h.flag_change()
    h.end_game_fn.assert_not_awaited()


# --------------------------------------------------------------------------- #
# BR-006 at the servicer seam: every admitted game is primed, real and shadow
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


async def _start_through_servicer(game_kind: str, stale_values: dict[str, str]):
    """Start a real or shadow game through the servicer with fake flag clients.

    Returns (servicer, flags, end_game_fn, game). The game thread is not run;
    the test installs the built game instance itself.
    """
    servicer = GameCoordinatorServicer()
    mgr = servicer.intervention_manager
    flags = UntargetedFlagClient()
    flags.values.update(stale_values)
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
    game = FakeGame()
    session.current_game = game  # the game thread built the instance
    return servicer, flags, end_game_fn, game


def _assert_fired(flag_key, end_game_fn, game, *, times):
    if flag_key == "end_game":
        assert end_game_fn.await_count == times
    else:
        assert game.killed == (["p2"] if times else [])


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("flag_key", ["end_game", "eliminate_player"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_servicer_start_primes_new_game_so_stale_edge_is_not_replayed(game_kind, flag_key):
    """Starting a real or shadow game through the servicer seeds its baseline:
    a stale value left in flagd is not applied on the next flag change, while
    a fresh value written during the game is."""
    stale = {"end_game": "9-stale:", "eliminate_player": "9-stale:p1"}[flag_key]
    fresh = {"end_game": "10-fresh:", "eliminate_player": "10-fresh:p2"}[flag_key]
    servicer, flags, end_game_fn, game = await _start_through_servicer(game_kind, {flag_key: stale})
    mgr = servicer.intervention_manager
    try:
        with patch("services.game_coordinator.metrics.interventions_total"):
            flags.values["global_difficulty_factor"] = 1.5  # unrelated write
            await mgr.evaluate_all()
            _assert_fired(flag_key, end_game_fn, game, times=0)
            assert game.killed == []

            flags.values[flag_key] = fresh
            await mgr.evaluate_all()
        _assert_fired(flag_key, end_game_fn, game, times=1)
    finally:
        mgr.stop()


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("flag_key", ["end_game", "eliminate_player"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_servicer_primes_at_start_so_first_change_fresh_edge_fires(game_kind, flag_key):
    """The very first flag change in the game is a fresh edge write: it must
    fire. This pins the servicer's start-time prime: without it the never-primed
    fallback would adopt the fresh value as the baseline and swallow it."""
    fresh = {"end_game": "10-fresh:", "eliminate_player": "10-fresh:p2"}[flag_key]
    servicer, flags, end_game_fn, game = await _start_through_servicer(game_kind, {})
    mgr = servicer.intervention_manager
    try:
        flags.values[flag_key] = fresh
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()
        _assert_fired(flag_key, end_game_fn, game, times=1)
    finally:
        mgr.stop()


# --------------------------------------------------------------------------- #
# A session retired WHILE evaluate_all is suspended inside it (handler awaits)
# must not get a fresh, empty baseline for its remaining specs (#1299 review)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_retire_mid_evaluation_does_not_replay_stale_end_game():
    """Shadow S retires while evaluate_all awaits S's eliminate handler; S's
    later end_game spec must not see an empty bucket and fire the stale nonce
    (end_game_fn ends the PRIMARY, so this would end the real game)."""
    h = Harness()
    h.flags.values["end_game"] = "5-old:"
    game = h.start_game("shadow_s")
    original_kill = game._kill_player

    async def kill_and_retire(serial, accel, reason=None):
        h.mgr.forget_session("shadow_s")  # the servicer retires S concurrently
        h.end_game("shadow_s")
        await original_kill(serial, accel, reason)

    game._kill_player = kill_and_retire
    h.flags.values["eliminate_player"] = "4-new:p2"  # fresh write during S
    await h.flag_change()

    assert game.killed == ["p2"]
    h.end_game_fn.assert_not_awaited()
    assert "shadow_s" not in h.mgr._last_nonce
    assert "shadow_s" not in h.mgr._last_state_value


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_retire_mid_evaluation_does_not_apply_later_state_flags():
    """Same race for state flags: once S is forgotten mid-pass, a later state
    flag is not applied to the dead game and no bucket is re-created."""
    h = Harness()
    h.start_game("shadow_s")

    async def tempo_handler_that_retires(_ctx):
        h.mgr.forget_session("shadow_s")
        h.end_game("shadow_s")

    h.mgr.register_handler("music_tempo_override", tempo_handler_that_retires)
    h.flags.values["music_tempo_override"] = 1.3  # first spec in registry order
    h.flags.values["global_difficulty_factor"] = 1.5  # later state spec
    await h.flag_change()

    assert len(h.applied("shadow_s", "music_tempo_override")) == 1
    assert h.applied("shadow_s", "global_difficulty_factor") == []
    assert "shadow_s" not in h.mgr._last_state_value
    assert "shadow_s" not in h.mgr._last_nonce


# --------------------------------------------------------------------------- #
# The servicer forgets a session's baseline on every retire path
# --------------------------------------------------------------------------- #
def _assert_forgotten(mgr, game_id):
    assert game_id not in mgr._pending_primed
    assert game_id not in mgr._last_nonce
    assert game_id not in mgr._last_state_value


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_servicer_retire_forgets_intervention_baseline(game_kind):
    servicer, _flags, _end_game_fn, _game = await _start_through_servicer(game_kind, {"end_game": "1-x:"})
    mgr = servicer.intervention_manager
    try:
        game_id = next(iter(servicer.sessions))
        assert game_id in mgr._pending_primed
        assert game_id in mgr._last_nonce
        servicer._retire_session(game_id)
        _assert_forgotten(mgr, game_id)
    finally:
        mgr.stop()


@pytest.mark.uc("UC-040", flow="A15", br=["BR-006"])
@pytest.mark.asyncio
async def test_servicer_ended_session_sweep_forgets_intervention_baseline():
    """A naturally ENDED session is swept at the next admission; its baseline
    goes with it (covers a game that errored before it was ever built)."""
    servicer, _flags, _end_game_fn, _game = await _start_through_servicer("shadow", {"end_game": "1-x:"})
    mgr = servicer.intervention_manager
    try:
        ended_id = next(iter(servicer.sessions))
        servicer.sessions[ended_id].game_state = game_coordinator_pb2.GameState.ENDED
        with (
            patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"),
            patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None),
        ):
            ok, new_id = await servicer._start_game_from_config(_start_config("shadow"), _MockSpan())
        assert ok, new_id
        assert ended_id not in servicer.sessions
        _assert_forgotten(mgr, ended_id)
        assert new_id in mgr._pending_primed
    finally:
        mgr.stop()
