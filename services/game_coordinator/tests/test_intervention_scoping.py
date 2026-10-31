"""
Interventions act only on the game they were decided for (#1310, #1313).

UC-040 A18 / BR-011: one-shot flags (``end_game``, ``eliminate_player``,
``revive_player``) are per-game event lists served on ``gameId`` (FR-137,
UC-053 A4 / BR-006). Only the game whose list carries the event acts on it,
and ``end_game`` force-ends exactly that session through the servicer's
session-scoped force-end path. Before #1310 a shadow session's evaluation
called a force-end that always resolved the PRIMARY game, so a shadow decision
could end the game humans were playing.

UC-040 A19 / BR-012: per-player grants (``shield_seconds``,
``toughness_boost_seconds``, ``soft_penalty_action``) apply once per decision to
the player they target. Before #1313 shielding B re-granted every player still
in the flag's if-ladder. Grants are game-scoped in the flag service, so a new
game never sees a grant of an earlier game.

Telemetry is disabled via conftest; the intervention metric is patched.
"""

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
    InterventionContext,
    InterventionManager,
    SessionView,
)
from services.game_coordinator.lifecycle_handlers import handle_shield_seconds, register_lifecycle_handlers
from services.game_coordinator.servicer import GameCoordinatorServicer
from services.game_coordinator.tests.conftest import FakeInterventionGame
from services.game_coordinator.tests.event_list_helpers import edge_event, object_details

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}
SPEC_BY_KEY = {spec.flag_key: spec for spec in INTERVENTION_SPECS}
GRANT_FLAGS = ["shield_seconds", "toughness_boost_seconds", "soft_penalty_action"]
# A non-neutral grant value per grant flag.
GRANT_VALUE = {"shield_seconds": 5.0, "toughness_boost_seconds": "5:2.0", "soft_penalty_action": "warn"}
REAL = "game_real000"
SHADOW = "game_shadow00"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class ScopedFlagClient:
    """flagd stand-in that resolves like the real interventions flag set.

    - ``values``: un-targeted value, resolved for any gameId;
    - ``per_game``: a value in one game's scope, ``(flag, game_id) -> value``
      (one-shot flags: the game's event list);
    - ``per_serial``: the per-player if-ladder, ``(flag, serial) -> (variant,
      value)``, resolved for any gameId;
    - ``scoped_serial``: a per-player entry in one game's scope,
      ``(flag, game_id, serial) -> (variant, value)``; ``drop_scope`` removes
      a game's scope like the flag service does when the game ends;
    - ``failing``: flags that resolve like the OpenFeature client on a flagd
      error (no exception, the default, ``reason=ERROR``).
    """

    def __init__(self):
        self.values: dict[str, object] = {}
        self.per_game: dict[tuple[str, str], object] = {}
        self.per_serial: dict[tuple[str, str], tuple[str, object]] = {}
        self.scoped_serial: dict[tuple[str, str, str], tuple[str, object]] = {}
        self.failing: set[str] = set()

    def drop_scope(self, game_id):
        for key in [k for k in self.per_game if k[1] == game_id]:
            del self.per_game[key]
        for key in [k for k in self.scoped_serial if k[1] == game_id]:
            del self.scoped_serial[key]

    def _resolve(self, key, default, ctx):
        serial = getattr(ctx, "targeting_key", None)
        game_id = (getattr(ctx, "attributes", None) or {}).get("gameId")
        if serial and (key, game_id, serial) in self.scoped_serial:
            return self.scoped_serial[(key, game_id, serial)]
        if serial and (key, serial) in self.per_serial:
            return self.per_serial[(key, serial)]
        if (key, game_id) in self.per_game:
            return "game_scoped", self.per_game[(key, game_id)]
        if key in self.values:
            return "active", self.values[key]
        return "none", default

    def _details(self, key, default, ctx, cast):
        if key in self.failing:
            return FlagEvaluationDetails(
                flag_key=key,
                value=default,
                reason=Reason.ERROR,
                error_code=ErrorCode.PROVIDER_NOT_READY,
                error_message="flagd unavailable",
            )
        variant, value = self._resolve(key, default, ctx)
        return FlagEvaluationDetails(flag_key=key, value=cast(value), variant=variant)

    def get_object_details(self, key, default, ctx=None):
        if key in self.failing:
            return self._details(key, default, ctx, dict)
        return object_details(key, self._resolve(key, default, ctx)[1])

    def get_string_details(self, key, default, ctx=None):
        return self._details(key, default, ctx, str)

    def get_float_details(self, key, default, ctx=None):
        return self._details(key, default, ctx, float)

    def get_string_value(self, key, default, ctx=None):
        return self.get_string_details(key, default, ctx).value

    def get_float_value(self, key, default, ctx=None):
        return self.get_float_details(key, default, ctx).value

    def get_integer_value(self, key, default, ctx=None):
        return int(self._details(key, default, ctx, int).value)

    def get_object_value(self, key, default, ctx=None):
        return default


class AgentStub:
    """Agent domain: every intervention allowed, generous backstop, no battery gate."""

    def __init__(self, battery_threshold=0):
        self.battery_threshold = battery_threshold

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.coordinator_backstop_per_minute":
            return 1000
        if key == "policy.battery_threshold":
            return self.battery_threshold
        return default

    def get_string_value(self, key, default, _ctx=None):
        if key == "interventions_allowed":
            return ",".join(sorted(ALL_TYPES))
        return default

    def get_object_value(self, key, default, _ctx=None):
        return default


class Harness:
    """A manager over a mutable set of live sessions, mimicking the servicer."""

    def __init__(self, battery=None, battery_threshold=0):
        self.flags = ScopedFlagClient()
        self.views: list[SessionView] = []
        self.events: dict[str, list] = {}
        self.end_game_fn = AsyncMock(return_value=(True, ""))
        self.mgr = InterventionManager(
            event_publisher=self._publisher("__primary_bus__"),
            get_sessions=lambda: list(self.views),
            battery_provider=(lambda s: battery.get(s)) if battery is not None else None,
            time_fn=lambda: 1000.0,
            end_game_fn=self.end_game_fn,
        )
        self.mgr._interventions_client = self.flags
        self.mgr._agent_client = AgentStub(battery_threshold)
        self.mgr._rate_limiter.set_budget(1000.0)
        self.mgr.register_ambient_handlers()
        register_lifecycle_handlers(self.mgr)

    def _publisher(self, game_id):
        bucket = self.events.setdefault(game_id, [])

        async def publish(event_type, data):
            bucket.append((event_type, data))

        return publish

    def start_game(self, game_id, *, real, serials=("p1", "p2")) -> FakeInterventionGame:
        """Admit a game like the servicer and let it go live."""
        game = FakeInterventionGame(serials=serials)
        self.views.append(SessionView(game_id=game_id, game=game, publish=self._publisher(game_id), is_real=real))
        return game

    def end(self, game_id):
        """Retire a game like the servicer: forget it and end its flag scope."""
        self.views = [v for v in self.views if v.game_id != game_id]
        self.mgr.forget_session(game_id)
        self.flags.drop_scope(game_id)

    async def flag_change(self):
        with patch("services.game_coordinator.metrics.interventions_total"):
            await self.mgr.evaluate_all()

    def recorded(self, game_id, type_id, *, blocked="false"):
        return [
            d
            for et, d in self.events.get(game_id, [])
            if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id and d["blocked"] == blocked
        ]

    def ended_game_ids(self) -> list[str]:
        return [call.args[0] for call in self.end_game_fn.await_args_list]


# --------------------------------------------------------------------------- #
# #1310 end_game: UC-040 A18 / BR-011 at the manager seam
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_shadow_end_game_ends_the_shadow_and_never_the_real_game():
    """An end_game event in the shadow's list ends only the shadow: the real
    game's list is empty, so it records nothing."""
    h = Harness()
    h.start_game(REAL, real=True)
    h.start_game(SHADOW, real=False)

    h.flags.per_game[("end_game", SHADOW)] = edge_event("end_game")
    await h.flag_change()

    assert h.ended_game_ids() == [SHADOW]
    assert h.recorded(REAL, "end_game") == []
    assert len(h.recorded(SHADOW, "end_game")) == 1


@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_real_end_game_ends_the_real_game_only():
    h = Harness()
    h.start_game(REAL, real=True)
    h.start_game(SHADOW, real=False)

    h.flags.per_game[("end_game", REAL)] = edge_event("end_game")
    await h.flag_change()
    await h.flag_change()  # re-evaluation never applies the same event again

    assert h.ended_game_ids() == [REAL]
    assert h.recorded(SHADOW, "end_game") == []


@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"])
@pytest.mark.asyncio
async def test_end_game_for_a_game_that_is_not_live_ends_nothing():
    """Events in the list of a game that is not live never reach a live game."""
    h = Harness()
    h.start_game(REAL, real=True)
    h.start_game(SHADOW, real=False)

    h.flags.per_game[("end_game", "game_gone0000")] = edge_event("end_game")
    await h.flag_change()
    await h.flag_change()

    h.end_game_fn.assert_not_awaited()


# --------------------------------------------------------------------------- #
# #1310 audit: eliminate_player / revive_player act on the evaluated game only
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.parametrize("flag_key", ["eliminate_player", "revive_player"])
@pytest.mark.asyncio
async def test_shadow_scoped_lifecycle_edge_acts_only_on_the_shadow(flag_key):
    """An eliminate/revive event in the shadow's list acts on the shadow's
    player, never on the real game's player with the same serial."""
    h = Harness()
    real = h.start_game(REAL, real=True)
    shadow = h.start_game(SHADOW, real=False)
    if flag_key == "revive_player":
        for game in (real, shadow):
            game.players["p1"].alive = False

    h.flags.per_game[(flag_key, SHADOW)] = edge_event(flag_key, "p1")
    await h.flag_change()

    effect = "killed" if flag_key == "eliminate_player" else "revived"
    assert getattr(shadow, effect) == ["p1"]
    assert getattr(real, effect) == []


@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.parametrize("flag_key", ["eliminate_player", "revive_player"])
@pytest.mark.asyncio
async def test_real_scoped_lifecycle_edge_acts_only_on_the_real_game(flag_key):
    h = Harness()
    real = h.start_game(REAL, real=True)
    shadow = h.start_game(SHADOW, real=False)
    if flag_key == "revive_player":
        for game in (real, shadow):
            game.players["p1"].alive = False

    h.flags.per_game[(flag_key, REAL)] = edge_event(flag_key, "p1")
    await h.flag_change()

    effect = "killed" if flag_key == "eliminate_player" else "revived"
    assert getattr(real, effect) == ["p1"]
    assert getattr(shadow, effect) == []


# --------------------------------------------------------------------------- #
# #1310 at the servicer: end_game ends the evaluated session, never the primary
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


def _start_config(real: bool, serials=("p1", "p2")):
    origin = game_coordinator_pb2.GAME_ORIGIN_MENU if real else game_coordinator_pb2.GAME_ORIGIN_AGENT
    return game_coordinator_pb2.StartGameConfig(
        game_name="FFA",
        players=[game_coordinator_pb2.Player(serial=s) for s in serials],
        sensitivity=2,
        origin=origin,
    )


async def _servicer_with_games(*kinds: bool, flag_values=None, per_serial=None):
    """Start real/shadow games through the servicer with fake flag clients and
    the REAL end_game wiring. Returns (servicer, flags, {game_id: game})."""
    servicer = GameCoordinatorServicer()
    mgr = servicer.intervention_manager
    flags = ScopedFlagClient()
    flags.values.update(flag_values or {})
    flags.per_serial.update(per_serial or {})
    mgr._interventions_client = flags
    mgr._agent_client = AgentStub()
    mgr._rate_limiter.set_budget(1000.0)

    games = {}
    with (
        patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"),
        patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None),
    ):
        for real in kinds:
            ok, game_id = await servicer._start_game_from_config(_start_config(real), _MockSpan())
            assert ok, game_id
            game = FakeInterventionGame()
            servicer.sessions[game_id].current_game = game  # the game thread built it
            games[game_id] = game
    return servicer, flags, games


def _id_of_kind(servicer, real: bool) -> str:
    return next(gid for gid, s in servicer.sessions.items() if s.is_primary == real)


@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.asyncio
@pytest.mark.uc("UC-027", flow="A3", br=["BR-001"])
async def test_servicer_shadow_end_game_ends_shadow_session_not_the_real_game():
    """Through the servicer's real wiring: an end_game in the shadow's list retires
    the shadow session and leaves the real (primary) session running."""
    servicer, flags, _ = await _servicer_with_games(True, False)
    mgr = servicer.intervention_manager
    real_id, shadow_id = _id_of_kind(servicer, True), _id_of_kind(servicer, False)
    try:
        flags.per_game[("end_game", shadow_id)] = edge_event("end_game")
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()

        assert shadow_id not in servicer.sessions
        assert real_id in servicer.sessions
        assert servicer._primary_game_id == real_id
        assert servicer.sessions[real_id].game_state != game_coordinator_pb2.GameState.ENDED
    finally:
        mgr.stop()


@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.asyncio
@pytest.mark.uc("UC-027", flow="A3", br=["BR-001"])
async def test_servicer_real_end_game_ends_the_real_game():
    servicer, flags, _ = await _servicer_with_games(True, False)
    mgr = servicer.intervention_manager
    real_id, shadow_id = _id_of_kind(servicer, True), _id_of_kind(servicer, False)
    try:
        flags.per_game[("end_game", real_id)] = edge_event("end_game")
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()

        assert real_id not in servicer.sessions
        assert servicer._primary_game_id is None
        assert shadow_id in servicer.sessions
    finally:
        mgr.stop()


@pytest.mark.uc("UC-040", flow="A18", br=["BR-011"])
@pytest.mark.asyncio
@pytest.mark.uc("UC-027", flow="A1", br=["BR-001"], cond=["Post-F-1"])
async def test_force_end_session_never_falls_back_to_the_primary():
    """``_force_end_session`` with an unknown non-empty id is a no-op: it must
    not resolve "the current game"."""
    servicer, _, _ = await _servicer_with_games(True)
    real_id = _id_of_kind(servicer, True)
    try:
        ok, error = await servicer._force_end_session("game_unknown00", "agent_intervention")
        assert (ok, error) == (False, "Unknown game_id: game_unknown00")
        assert real_id in servicer.sessions
        assert servicer.sessions[real_id].game_state != game_coordinator_pb2.GameState.ENDED
    finally:
        servicer.intervention_manager.stop()


# --------------------------------------------------------------------------- #
# #1313 per-player grants: UC-040 A19 / BR-012
# --------------------------------------------------------------------------- #
def _grant(h_or_flags, flag_key, serial, write_id, value=None, *, game_id=None):
    """Write a grant for ``serial``. The flag service generates a fresh opaque
    ``r<epoch>_<n>`` variant per write; ``write_id`` stands in for ``<n>``. With
    ``game_id`` the grant lives in that game's scope only."""
    flags = getattr(h_or_flags, "flags", h_or_flags)
    entry = (f"r1_{write_id}", GRANT_VALUE[flag_key] if value is None else value)
    if game_id is None:
        flags.per_serial[(flag_key, serial)] = entry
    else:
        flags.scoped_serial[(flag_key, game_id, serial)] = entry


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.parametrize("flag_key", GRANT_FLAGS)
@pytest.mark.asyncio
async def test_granting_b_does_not_regrant_a(flag_key):
    """A was granted earlier; granting B applies to B only (the #1313 fan-out
    re-granted every serial still in the ladder)."""
    h = Harness()
    game = h.start_game(REAL, real=True)

    _grant(h, flag_key, "p1", "1")
    await h.flag_change()
    assert game.grants(flag_key) == ["p1"]

    _grant(h, flag_key, "p2", "2")
    await h.flag_change()
    await h.flag_change()  # unrelated re-evaluation

    assert game.grants(flag_key) == ["p1", "p2"]
    targets = [d["target"] for d in h.recorded(REAL, SPEC_BY_KEY[flag_key].type_id)]
    assert targets == ["p1", "p2"]


@pytest.mark.uc("UC-053", flow="main", steps=[6])
@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.parametrize("flag_key", GRANT_FLAGS)
@pytest.mark.asyncio
async def test_new_game_does_not_reapply_previous_games_grants(flag_key):
    """Game N granted p1 in its scope; the scope ends with game N. Game N+1
    (same serial) grants nobody on re-evaluation; a fresh grant still applies."""
    h = Harness()
    game_n = h.start_game("game_n", real=True)
    _grant(h, flag_key, "p1", "1", game_id="game_n")
    await h.flag_change()
    assert game_n.grants(flag_key) == ["p1"]
    h.end("game_n")

    game_n1 = h.start_game("game_n1", real=True)
    await h.flag_change()
    assert game_n1.grants(flag_key) == []

    _grant(h, flag_key, "p1", "2", game_id="game_n1")  # a new decision for p1 in N+1
    await h.flag_change()
    await h.flag_change()
    assert game_n1.grants(flag_key) == ["p1"]


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_same_value_regrant_is_a_new_decision():
    """Two decisions for p1 with the same seconds are two grants: the fresh
    variant per decision is the change, not the value."""
    h = Harness()
    game = h.start_game(REAL, real=True)

    _grant(h, "shield_seconds", "p1", "1", 5.0)
    await h.flag_change()
    _grant(h, "shield_seconds", "p1", "2", 5.0)
    await h.flag_change()

    assert game.shields == [("p1", 5.0), ("p1", 5.0)]


@pytest.mark.uc("UC-053", flow="main", steps=[6])
@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_grant_baseline_is_per_game_for_concurrent_games():
    """A shadow starting later has its own scope: p1's grant decided for the
    real game is not applied to the shadow's p1."""
    h = Harness()
    real = h.start_game(REAL, real=True)
    _grant(h, "shield_seconds", "p1", "1", game_id=REAL)
    await h.flag_change()

    shadow = h.start_game(SHADOW, real=False)
    await h.flag_change()

    assert real.grants("shield_seconds") == ["p1"]
    assert shadow.grants("shield_seconds") == []


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_low_battery_grant_is_blocked_per_serial_and_consumed():
    """The battery guard applies per grant (target = serial); a blocked grant
    is consumed, so it does not fire later when the battery reads fine."""
    battery = {"p1": 5.0, "p2": 90.0}
    h = Harness(battery=battery, battery_threshold=20)
    game = h.start_game(REAL, real=True)

    _grant(h, "shield_seconds", "p1", "1")
    _grant(h, "shield_seconds", "p2", "2")
    await h.flag_change()
    battery["p1"] = 90.0
    h.flags.values["volume_override"] = 0.5  # unrelated write
    await h.flag_change()

    assert game.grants("shield_seconds") == ["p2"]
    blocked = h.recorded(REAL, "grant_shield", blocked="true")
    assert [(d["target"], d["block_reason"]) for d in blocked] == [("p1", "low_battery")]


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_grant_handler_applies_only_the_dispatched_serial():
    """The shield handler grants ``ctx.target_serial`` only, even while the
    ladder still holds other players' grants."""
    h = Harness()
    game = FakeInterventionGame()
    _grant(h, "shield_seconds", "p1", "1")
    _grant(h, "shield_seconds", "p2", "2", 3.0)
    ctx = InterventionContext(
        spec=SPEC_BY_KEY["shield_seconds"],
        value=3.0,
        payload="",
        target_serial="p2",
        game=game,
        objective="balanced",
        game_id=REAL,
    )
    await handle_shield_seconds(ctx, h.mgr)
    assert game.shields == [("p2", 3.0)]


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_grant_read_error_at_game_start_never_fires():
    """flagd errors on the game's first reads: the getter would return the
    neutral default; the failed read fires nothing and moves no baseline. On
    recovery the game's grant applies, once."""
    h = Harness()
    _grant(h, "shield_seconds", "p1", "1")
    h.flags.failing = {"shield_seconds"}
    game = h.start_game(REAL, real=True)
    await h.flag_change()
    assert game.grants("shield_seconds") == []
    assert "p1" not in h.mgr._state_bucket(REAL)["shield_seconds"]

    h.flags.failing = set()
    await h.flag_change()
    await h.flag_change()
    assert game.grants("shield_seconds") == ["p1"]


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_grant_read_error_mid_game_keeps_the_baseline():
    """An error read never moves a serial's baseline: recovery to the same
    grant does not re-fire, and a grant written during the outage fires once."""
    h = Harness()
    game = h.start_game(REAL, real=True)
    _grant(h, "shield_seconds", "p1", "1")
    await h.flag_change()

    h.flags.failing = {"shield_seconds"}
    await h.flag_change()
    h.flags.failing = set()
    await h.flag_change()
    assert game.grants("shield_seconds") == ["p1"]

    h.flags.failing = {"shield_seconds"}
    await h.flag_change()
    _grant(h, "shield_seconds", "p1", "2")  # a new decision while flagd errored
    h.flags.failing = set()
    await h.flag_change()
    await h.flag_change()
    assert game.grants("shield_seconds") == ["p1", "p1"]


@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_servicer_started_game_applies_its_grants_once():
    """A game started through the servicer needs no seeding: its first grant
    applies on the next flag change, once."""
    servicer, flags, games = await _servicer_with_games(True)
    mgr = servicer.intervention_manager
    (game,) = games.values()
    try:
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()
        assert game.grants("shield_seconds") == []

        flags.per_serial[("shield_seconds", "p2")] = ("r1_1", 5.0)
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()
            await mgr.evaluate_all()
        assert game.grants("shield_seconds") == ["p2"]
    finally:
        mgr.stop()
