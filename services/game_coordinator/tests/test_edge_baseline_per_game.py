"""
Edge-triggered interventions fire only in the game that saw them change (#1299).

UC-040 BR-005 / A14: an edge flag value present when a game starts is that
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

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.interventions import (
    _MAX_PENDING_PRIMED_SESSIONS,
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
    gameId targeting, decide-contract §11)."""

    def __init__(self):
        self.values: dict[str, object] = {}

    def get_string_value(self, key, default, _ctx=None):
        return str(self.values.get(key, default))

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
# BR-005 / A14 at the manager seam
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
def test_pending_primed_sessions_are_bounded():
    """Sessions primed but never built (start error) cannot grow without bound."""
    h = Harness()
    for i in range(_MAX_PENDING_PRIMED_SESSIONS + 10):
        h.mgr.prime_session(f"game_{i}")
    assert len(h.mgr._pending_primed) == _MAX_PENDING_PRIMED_SESSIONS
    assert "game_0" not in h.mgr._pending_primed


# --------------------------------------------------------------------------- #
# BR-005 at the servicer seam: every admitted game is primed, real and shadow
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


@pytest.mark.uc("UC-040", flow="A14", br=["BR-005"])
@pytest.mark.parametrize("game_kind", GAME_KINDS)
@pytest.mark.asyncio
async def test_servicer_start_primes_new_game_so_stale_end_game_is_not_replayed(game_kind):
    """Starting a real or shadow game through the servicer seeds its baseline:
    a stale end_game left in flagd does not end it on the next flag change,
    while a fresh end_game written during the game does."""
    servicer = GameCoordinatorServicer()
    mgr = servicer.intervention_manager
    flags = UntargetedFlagClient()
    flags.values["end_game"] = "9-stale:"
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
    session.current_game = FakeGame()  # the game thread built the instance

    with patch("services.game_coordinator.metrics.interventions_total"):
        flags.values["global_difficulty_factor"] = 1.5  # unrelated write
        await mgr.evaluate_all()
        end_game_fn.assert_not_awaited()

        flags.values["end_game"] = "10-fresh:"
        await mgr.evaluate_all()
    end_game_fn.assert_awaited_once_with("agent_intervention")
    mgr.stop()
