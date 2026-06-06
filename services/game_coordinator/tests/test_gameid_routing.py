"""
Unit tests for gameId evaluation-context routing of interventions (#838).

These exercise the per-session evaluation seam added in #838:
- a gameId-targeted intervention applies ONLY to the matching session;
- an un-targeted intervention applies to the PRIMARY only (today's semantics;
  shadow games are untouched);
- per-player (targetingKey=serial) + per-game (gameId) targeting compose in one
  flagd JsonLogic-style rule;
- effects/blocked events are published on the OWNING session's bus.

The fake flag client mimics flagd's IN_PROCESS resolver: it reads the
EvaluationContext (``gameId`` attribute + ``targeting_key``) and applies a
JsonLogic-like targeting rule per flag, exactly as the agent would write into
the ``interventions`` flagd domain. Telemetry is disabled via conftest; the
metric is patched per the repo convention.
"""

import itertools
from unittest.mock import patch

import pytest

from lib.types import GameEvent
from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    InterventionManager,
    SessionView,
)

ALL_TYPES = {spec.type_id for spec in INTERVENTION_SPECS}


# --------------------------------------------------------------------------- #
# Context-aware fakes (mimic flagd IN_PROCESS targeting)
# --------------------------------------------------------------------------- #
def _ctx_attrs(ctx):
    """Return (gameId, targetingKey) from an EvaluationContext (or Nones)."""
    if ctx is None:
        return None, None
    attrs = getattr(ctx, "attributes", None) or {}
    return attrs.get("gameId"), getattr(ctx, "targeting_key", None)


class TargetingFlagClient:
    """In-memory flag client that resolves a per-flag targeting rule.

    ``rules[flag_key]`` is ``(default, [(predicate, value), ...])`` where each
    predicate is ``fn(game_id, targeting_key) -> bool``. The first matching
    predicate wins, else ``default`` — exactly flagd's if-ladder semantics.
    """

    def __init__(self, rules=None):
        self.rules = rules or {}

    def set_rule(self, flag_key, default, branches):
        self.rules[flag_key] = (default, branches)

    def _resolve(self, key, fallback, ctx):
        rule = self.rules.get(key)
        if rule is None:
            return fallback
        default, branches = rule
        game_id, targeting_key = _ctx_attrs(ctx)
        for predicate, value in branches:
            if predicate(game_id, targeting_key):
                return value
        return default

    def get_string_value(self, key, default, ctx=None):
        return str(self._resolve(key, default, ctx))

    def get_integer_value(self, key, default, ctx=None):
        return int(self._resolve(key, default, ctx))

    def get_float_value(self, key, default, ctx=None):
        return float(self._resolve(key, default, ctx))

    def get_object_value(self, key, default, ctx=None):
        return self._resolve(key, default, ctx)


class AgentStub:
    """Agent-domain client: everything allowed, generous budget."""

    def get_integer_value(self, key, default, _ctx=None):
        if key == "policy.max_interventions_per_minute":
            return 50
        if key == "policy.battery_threshold":
            return 20
        return default

    def get_object_value(self, key, default, _ctx=None):
        if key == "interventions_allowed":
            return list(ALL_TYPES)
        return default


class FakePlayer:
    def __init__(self, serial, alive=True):
        self.serial = serial
        self.alive = alive
        self.sensitivity_factor = 1.0


class FakeGame:
    def __init__(self, name="Nonstop Joust", players=None):
        self._name = name
        self.players = players or {}

    def get_game_name(self):
        return self._name


def _mono():
    counter = itertools.count()

    def fn():
        return 1000.0 + next(counter) * 0.0

    return fn


def _spec(flag_key):
    return next(s for s in INTERVENTION_SPECS if s.flag_key == flag_key)


def make_multisession_manager(*, sessions, rules=None):
    """Build a manager wired with a fixed list of SessionViews.

    ``sessions`` is a list of (game_id, game) pairs; each gets its own event
    list (the publisher). Returns (manager, {game_id: events_list}).
    """
    events_by_game: dict[str, list] = {}
    views: list[SessionView] = []

    def make_publisher(gid):
        bucket = events_by_game.setdefault(gid, [])

        async def publisher(event_type, data):
            bucket.append((event_type, data))

        return publisher

    for game_id, game in sessions:
        views.append(SessionView(game_id=game_id, game=game, publish=make_publisher(game_id)))

    mgr = InterventionManager(
        event_publisher=make_publisher("__primary_bus__"),
        get_sessions=lambda: list(views),
        time_fn=_mono(),
    )
    mgr._interventions_client = TargetingFlagClient(rules or {})
    mgr._agent_client = AgentStub()
    mgr._rate_limiter.set_budget(50.0)
    return mgr, events_by_game


def _applied(events, type_id):
    return [
        d for et, d in events if et == GameEvent.AGENT_INTERVENTION and d["type"] == type_id and d["blocked"] == "false"
    ]


# --------------------------------------------------------------------------- #
# Edge-triggered intervention scoped by gameId
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gameid_targeted_edge_applies_only_to_matching_session():
    """An eliminate_player rule targeting gameId=shadow fires ONLY for shadow;
    the primary (and its bus) is provably untouched."""
    primary_game = FakeGame(players={"p1": FakePlayer("p1")})
    shadow_game = FakeGame(players={"s1": FakePlayer("s1")})
    mgr, events = make_multisession_manager(
        sessions=[("game_primary", primary_game), ("game_shadow", shadow_game)],
    )
    # {"if": [{"==": [{"var":"gameId"}, "game_shadow"]}, "7:s1", ""]}
    mgr._interventions_client.set_rule(
        "eliminate_player",
        "",  # default: no-op for everyone else
        [(lambda gid, _tk: gid == "game_shadow", "7:s1")],
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    # Shadow received the elimination; primary did not.
    assert len(_applied(events["game_shadow"], "eliminate_player")) == 1
    assert _applied(events["game_primary"], "eliminate_player") == []
    # And it targeted the shadow's serial.
    assert events["game_shadow"][-1][1]["target"] == "s1"


@pytest.mark.asyncio
async def test_untargeted_intervention_applies_to_primary_only():
    """An un-targeted state flag (no gameId rule) moves the primary and leaves
    every shadow at the default no-op — today's least-surprise semantics."""
    primary_game = FakeGame()
    shadow_game = FakeGame()
    mgr, events = make_multisession_manager(
        sessions=[("game_primary", primary_game), ("game_shadow", shadow_game)],
    )
    # Model #838's "un-targeted = primary only" contract: the agent scopes its
    # experiment to the primary's gameId; the neutral default (1.0) leaves every
    # shadow a no-op. (An agent that wants a shadow experiment writes a
    # shadow-gameId rule instead — see the gameId-targeted tests above.)
    mgr._interventions_client.set_rule(
        "global_difficulty_factor",
        1.0,  # neutral default -> shadow gets no-op
        [(lambda gid, _tk: gid == "game_primary", 1.5)],
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    assert len(_applied(events["game_primary"], "adjust_global_difficulty")) == 1
    assert _applied(events["game_shadow"], "adjust_global_difficulty") == []


@pytest.mark.asyncio
async def test_per_player_and_per_game_compose_in_one_rule():
    """player_sensitivity_factor with a rule matching BOTH gameId==shadow AND
    targetingKey==s1 resolves the experiment value only for that serial in that
    game; the same serial name in the primary game is unaffected."""
    primary_game = FakeGame(players={"s1": FakePlayer("s1")})
    shadow_game = FakeGame(players={"s1": FakePlayer("s1"), "s2": FakePlayer("s2")})
    mgr, events = make_multisession_manager(
        sessions=[("game_primary", primary_game), ("game_shadow", shadow_game)],
    )
    # The real per-player handler writes sensitivity_factor on the live game.
    register_difficulty_handlers(mgr)
    # Compose: gameId == game_shadow AND targetingKey == s1 -> 1.7, else neutral.
    mgr._interventions_client.set_rule(
        "player_sensitivity_factor",
        1.0,
        [(lambda gid, tk: gid == "game_shadow" and tk == "s1", 1.7)],
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    # The composed rule fired exactly once — for the shadow game.
    assert len(_applied(events["game_shadow"], "adjust_player_sensitivity")) == 1
    assert _applied(events["game_primary"], "adjust_player_sensitivity") == []
    # Only s1 in the shadow got the experiment factor; s2 stayed neutral; the
    # primary's like-named s1 stayed neutral.
    assert shadow_game.players["s1"].sensitivity_factor == pytest.approx(1.7)
    assert shadow_game.players["s2"].sensitivity_factor == pytest.approx(1.0)
    assert primary_game.players["s1"].sensitivity_factor == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_change_detection_is_per_session_independent():
    """Two shadows targeted by the same edge flag each fire once; re-evaluating
    does not re-fire either (per-(game_id,flag) nonce baseline)."""
    a = FakeGame(players={"a1": FakePlayer("a1")})
    b = FakeGame(players={"b1": FakePlayer("b1")})
    mgr, events = make_multisession_manager(sessions=[("game_a", a), ("game_b", b)])
    mgr._interventions_client.set_rule(
        "eliminate_player",
        "",
        [
            (lambda gid, _tk: gid == "game_a", "3:a1"),
            (lambda gid, _tk: gid == "game_b", "3:b1"),
        ],
    )
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()
        await mgr.evaluate_all()  # same nonce per game -> no re-fire

    assert len(_applied(events["game_a"], "eliminate_player")) == 1
    assert len(_applied(events["game_b"], "eliminate_player")) == 1


@pytest.mark.asyncio
async def test_legacy_get_game_path_unchanged():
    """A manager wired with the legacy get_game callback still evaluates a single
    primary-only session (empty game_id) and publishes on the primary bus."""
    game = FakeGame()
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(event_publisher=publisher, get_game=lambda: game, time_fn=_mono())
    mgr._interventions_client = TargetingFlagClient(
        {"global_difficulty_factor": (1.0, [(lambda gid, _tk: gid is None, 1.4)])}
    )
    mgr._agent_client = AgentStub()
    mgr._rate_limiter.set_budget(50.0)
    with patch("services.game_coordinator.metrics.interventions_total"):
        await mgr.evaluate_all()

    # gid is None (no gameId attribute) for the synthesized primary view, so the
    # rule that matches gid is None fires -> proves the legacy path adds no gameId.
    assert len(_applied(events, "adjust_global_difficulty")) == 1
