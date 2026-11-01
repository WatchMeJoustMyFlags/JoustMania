"""
A group shield: one event shields 2 to ``max_targets`` alive players, all or nothing (#2467 PR 3).

UC-040 A47 (applied to every chosen player), A48 (refused as a whole), BR-033 (all or
nothing, the C-034 per-target bounds, the per-game backstop, shadow-first), BR-031 (one
helped cue per covered player), BR-035 (each covered player gets the end grace of a
single grant).

The gate flags are read through ``base.read_bool_flag`` / ``base.read_int_flag``
(patched per test); the shield limits through ``_shield_limits`` (patched). Every test
runs on a fake clock.
"""

import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.feature_flags import GAME_KIND_SHADOW
from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode, Player, ShieldHoldParams
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.teams import SimpleTeamsGame
from services.game_coordinator.intervention_errors import InterventionNotApplied
from services.game_coordinator.interventions import (
    INTERVENTION_SPECS,
    MODE_CAPABILITY_DENY,
    InterventionContext,
    InterventionManager,
    SessionView,
    _edge_target_serial,
    _event_group_seconds,
    _event_payload_string,
    _event_target_serials,
)
from services.game_coordinator.lifecycle_handlers import handle_group_shield, register_lifecycle_handlers

T0 = 1000.0
SECONDS = 5.0
SERIALS = ["p1", "p2", "p3"]
INSTANT = "instant"
TRIGGER = "trigger"
HOLD_MODE = "hold"
# Pool 5 s, budget 15 s, minimum burst 0.5 s, no recharge.
HOLD = ShieldHoldParams(cap=5.0, budget=15.0, initial=0.0, recharge=0.0, min_burst=0.5)
GROUP_SPEC = next(s for s in INTERVENTION_SPECS if s.flag_key == "group_shield")
MAGENTA = (255, 0, 192)


class Clock:
    def __init__(self, t=T0):
        self.t = t


class RecordingStream:
    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)


@pytest.fixture
def clock():
    c = Clock()
    with (
        patch.object(base_module.time, "time", side_effect=lambda: c.t),
        patch.object(game_clock, "now", side_effect=lambda: c.t),
    ):
        yield c


@pytest.fixture
def limits():
    """(max shields, cooldown seconds) returned by ``_shield_limits``."""
    current = {"value": (3, 15)}
    with patch.object(BaseGameMode, "_shield_limits", side_effect=lambda: current["value"]) as read:
        read.current = current
        yield read


@pytest.fixture
def policy():
    """The four group-grant flags as the game reads them; the gate open by default (shadow game)."""
    values = {
        "policy.group_grant.enabled": True,
        "policy.group_grant.real_games": False,
        "policy.group_grant.max_per_game": 1,
        "policy.group_grant.max_targets": 4,
    }
    real_int = base_module.read_int_flag

    def read_bool(_domain, key, default, game_id=None):  # noqa: ARG001
        return values.get(key, default)

    def read_int(domain, key, default, game_id=None):
        if key.startswith("policy.group_grant."):
            return values.get(key, default)
        return real_int(domain, key, default, game_id=game_id)

    with (
        patch.object(base_module, "read_bool_flag", side_effect=read_bool),
        patch.object(base_module, "read_int_flag", side_effect=read_int),
    ):
        yield values


def _make(cls=FFAGame, n=3):
    g = cls(
        controller_manager_client=MockControllerManagerService(num_controllers=n),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=f"test_group_shield_{cls.__name__}",
    )
    g.gameplay_stream = RecordingStream()
    g.players.clear()
    for i in range(n):
        serial = f"p{i + 1}"
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    g._play_sound = AsyncMock()
    g.game_kind = GAME_KIND_SHADOW
    g._latch_shield_end_grace = lambda: None  # end grace fixed per test (BR-035)
    g._shield_end_grace = 0.75
    return g


@pytest.fixture
def game(clock, limits, policy):  # noqa: ARG001 - the fixtures patch base for the test
    return _make()


def set_mode(g, mode):
    """Latch the game to ``mode`` without reading flags."""
    g._shield_hold_latched = True
    if mode == HOLD_MODE:
        g._shield_hold = HOLD
        for player in g.players.values():
            player.hold_energy = 0.0
            player.hold_energy_at = 0.0
        return
    g._shield_hold = None
    g._shield_activation_mode = lambda: mode
    g._read_charge_expiry = lambda: 0.0


def cues(g):
    """Serials that got the magenta helped cue (UC-040 A42, BR-031)."""
    out = []
    for w in g.gameplay_stream.writes:
        if not w.HasField("game_effect"):
            continue
        effect = w.game_effect
        if (
            effect.effect == controller_manager_pb2.GAME_EFFECT_PULSE
            and (
                effect.color.r,
                effect.color.g,
                effect.color.b,
            )
            == MAGENTA
        ):
            out.append(effect.serial)
    return out


def snapshot(g):
    """Every per-player field a grant writes, plus the game's group count."""
    return (
        {
            s: (
                p.grace_until,
                p.shield_charge_seconds,
                p.hold_energy,
                p.hold_granted_total,
                p.end_grace_until,
            )
            for s, p in g.players.items()
        },
        dict(g._agent_shield_grants),
        dict(g._agent_shield_last_end),
        g._agent_group_grants,
        set(g._hold_granted_pending),
    )


def committed(g, serial, mode):
    p = g.players[serial]
    if mode == HOLD_MODE:
        return p.hold_energy > 0 or p.hold_granted_total > 0
    if mode == TRIGGER:
        return p.shield_charge_seconds > 0
    return p.grace_until > T0


async def refused(g, serials=SERIALS, seconds=SECONDS, **kwargs):
    with pytest.raises(InterventionNotApplied) as exc:
        await g.grant_group_shield(list(serials), seconds, **kwargs)
    return exc.value


# --------------------------------------------------------------------------- #
# A47: applied to every chosen player
# --------------------------------------------------------------------------- #
class TestUC040A47GroupApplied:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033", "BR-031"])
    @pytest.mark.parametrize("mode", [INSTANT, TRIGGER, HOLD_MODE])
    async def test_every_target_is_shielded_with_one_cue_each(self, game, mode):
        set_mode(game, mode)
        assert await game.grant_group_shield(SERIALS, SECONDS) is True
        for serial in SERIALS:
            assert committed(game, serial, mode), f"{serial} not shielded in {mode}"
            assert game._agent_shield_grants[serial] == 1  # each target counts as one shield (BR-022)
            assert game._agent_shield_last_end[serial] == pytest.approx(T0 + SECONDS)
        assert sorted(cues(game)) == SERIALS
        assert game._agent_group_grants == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_trigger_sets_every_charge(self, game):
        set_mode(game, TRIGGER)
        await game.grant_group_shield(SERIALS, SECONDS)
        for serial in SERIALS:
            assert game.players[serial].shield_charge_seconds == SECONDS
            assert game.players[serial].grace_until <= T0  # not protected until activated (A30)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_hold_tops_up_every_pool(self, game):
        set_mode(game, HOLD_MODE)
        await game.grant_group_shield(SERIALS, 3.0)
        for serial in SERIALS:
            assert game.players[serial].hold_energy == pytest.approx(3.0)
        assert game._hold_granted_pending == set(SERIALS)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033", "BR-035"])
    async def test_instant_group_arms_the_end_grace_per_target(self, game):
        """BR-035: a group behaves like N single grants for the shield end grace window."""
        set_mode(game, INSTANT)
        await game.grant_group_shield(SERIALS, SECONDS)
        for serial in SERIALS:
            p = game.players[serial]
            assert p.shield_end_at == pytest.approx(T0 + SECONDS)
            assert p.end_grace_until == pytest.approx(T0 + SECONDS + 0.75)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033", "BR-022"])
    async def test_a_later_single_grant_sees_the_group_cooldown(self, game):
        """Each target counts as one shield, so a single grant right after is refused on the cooldown."""
        set_mode(game, INSTANT)
        await game.grant_group_shield(SERIALS, SECONDS)
        with pytest.raises(InterventionNotApplied) as exc:
            await game.grant_shield("p1", SECONDS)
        assert exc.value.reason == "shield_cooldown"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_instant_kept_target_counts_as_success_and_is_not_counted(self, game):
        set_mode(game, INSTANT)
        game.players["p2"].invincible_until = T0 + 60.0  # admin baseline longer than the shield
        assert await game.grant_group_shield(SERIALS, SECONDS) is True
        assert "p2" not in game._agent_shield_grants
        assert game.players["p2"].grace_until <= T0
        assert game._agent_shield_grants["p1"] == game._agent_shield_grants["p3"] == 1
        assert sorted(cues(game)) == ["p1", "p3"]
        assert game._agent_group_grants == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    @pytest.mark.parametrize("cls", [FFAGame, SimpleTeamsGame, NonstopJoustGame])
    async def test_applies_in_every_mode(self, clock, limits, policy, cls):  # noqa: ARG002
        g = _make(cls, n=4)
        set_mode(g, INSTANT)
        await g.grant_group_shield(["p1", "p2", "p3"], SECONDS)
        assert all(g.players[s].grace_until == pytest.approx(T0 + SECONDS) for s in ("p1", "p2", "p3"))
        assert g.players["p4"].grace_until <= T0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_no_frame_runs_between_the_first_check_and_the_last_commit(self, game):
        """Phases 0 to 2 have no await: a frame scheduled before the grant sees all or nothing."""
        set_mode(game, INSTANT)
        seen = []

        async def frame():
            seen.append([committed(game, s, INSTANT) for s in SERIALS])

        async def yielding_cue(_player):
            await asyncio.sleep(0)  # the first real yield to the loop: a frame can run here

        game._send_shield_help_cue = yielding_cue
        task = asyncio.ensure_future(frame())
        await game.grant_group_shield(SERIALS, SECONDS)
        await task
        assert seen == [[True, True, True]]


# --------------------------------------------------------------------------- #
# A48: refused as a whole
# --------------------------------------------------------------------------- #
def _cap(g, serial):
    g._agent_shield_grants[serial] = 3
    g._agent_shield_last_end[serial] = T0 - 1000.0


def _cooling(g, serial):
    g._agent_shield_grants[serial] = 1
    g._agent_shield_last_end[serial] = T0 - 1.0


def _charged(g, serial):
    g.players[serial].shield_charge_seconds = 4.0


def _pool_full(g, serial):
    g.players[serial].hold_energy = HOLD.cap


def _budget_used(g, serial):
    g.players[serial].hold_budget_used = HOLD.budget


def _dead(g, serial):
    g.players[serial].alive = False


REFUSALS = [
    (INSTANT, _dead, "group_refused:target_gone"),
    (TRIGGER, _dead, "group_refused:target_gone"),
    (HOLD_MODE, _dead, "group_refused:target_gone"),
    (INSTANT, _cap, "group_refused:shield_cap_reached"),
    (TRIGGER, _cap, "group_refused:shield_cap_reached"),
    (HOLD_MODE, _cap, "group_refused:shield_cap_reached"),
    (INSTANT, _cooling, "group_refused:shield_cooldown"),
    (TRIGGER, _cooling, "group_refused:shield_cooldown"),
    (HOLD_MODE, _cooling, "group_refused:shield_cooldown"),
    (TRIGGER, _charged, "group_refused:already_charged"),
    (HOLD_MODE, _pool_full, "group_refused:shield_pool_full"),
    (HOLD_MODE, _budget_used, "group_refused:shield_budget_exhausted"),
]


class TestUC040A48GroupRefusedAsAWhole:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    @pytest.mark.parametrize(("mode", "setup", "reason"), REFUSALS)
    async def test_last_target_refused_commits_nothing_for_the_first(self, game, mode, setup, reason):
        """All or nothing: every target is checked before the first one is written."""
        set_mode(game, mode)
        setup(game, "p3")
        before = snapshot(game)
        exc = await refused(game)
        assert exc.reason == reason
        assert exc.refused_serial == "p3"
        assert snapshot(game) == before
        assert cues(game) == []
        assert game.gameplay_stream.writes == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_a_refusal_is_counted_by_reason(self, game):
        set_mode(game, INSTANT)
        _cap(game, "p2")
        with patch("services.game_coordinator.metrics.group_shield_refused_total") as metric:
            await refused(game)
        metric.labels.assert_called_once_with(
            mode=game.get_game_name(), game_kind=GAME_KIND_SHADOW, reason="shield_cap_reached"
        )

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_game_ending_refuses_the_whole_group(self, game):
        set_mode(game, INSTANT)
        game._latched_outcome = "winner"
        before = snapshot(game)
        assert (await refused(game)).reason == "group_refused:game_ending"
        assert snapshot(game) == before

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_low_battery_target_refuses_the_whole_group(self, game):
        set_mode(game, INSTANT)
        before = snapshot(game)
        exc = await refused(game, low_battery=frozenset({"p2"}))
        assert (exc.reason, exc.refused_serial) == ("group_refused:low_battery", "p2")
        assert snapshot(game) == before

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    @pytest.mark.parametrize(
        ("serials", "seconds"),
        [
            (["p1"], SECONDS),  # one player is not a group
            (["p1", "p1"], SECONDS),  # a repeat
            (["p1", ""], SECONDS),  # an empty entry
            (["p1", "p2", "p3"], 0.0),
            (["p1", "p2", "p3"], 11.0),
            (["p1", "p2", "p3"], math.nan),
        ],
    )
    async def test_a_malformed_set_is_refused(self, game, serials, seconds):
        set_mode(game, INSTANT)
        before = snapshot(game)
        assert (await refused(game, serials, seconds)).reason == "group_refused:bad_set"
        assert snapshot(game) == before

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_more_targets_than_max_targets_is_refused(self, clock, limits, policy):  # noqa: ARG002
        g = _make(n=5)
        set_mode(g, INSTANT)
        policy["policy.group_grant.max_targets"] = 4
        assert (await refused(g, ["p1", "p2", "p3", "p4", "p5"])).reason == "group_refused:bad_set"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_unknown_serial_is_target_gone(self, game):
        set_mode(game, INSTANT)
        exc = await refused(game, ["p1", "ghost"])
        assert (exc.reason, exc.refused_serial) == ("group_refused:target_gone", "ghost")


# --------------------------------------------------------------------------- #
# BR-033: the gate and the per-game backstop
# --------------------------------------------------------------------------- #
class TestUC040BR033Gate:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    @pytest.mark.parametrize("kind", ["primary", "real", "", "unknown"])
    async def test_a_game_other_than_shadow_is_refused_without_real_games(self, game, kind):
        set_mode(game, INSTANT)
        game.game_kind = kind
        before = snapshot(game)
        assert (await refused(game)).reason == "group_refused:real_game"
        assert snapshot(game) == before

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_real_games_on_applies_in_a_real_game(self, game, policy):
        set_mode(game, INSTANT)
        game.game_kind = "primary"
        policy["policy.group_grant.real_games"] = True
        assert await game.grant_group_shield(SERIALS, SECONDS) is True
        assert all(committed(game, s, INSTANT) for s in SERIALS)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    @pytest.mark.parametrize("kind", [GAME_KIND_SHADOW, "primary"])
    async def test_enabled_off_refuses_even_with_real_games_on(self, game, policy, kind):
        set_mode(game, INSTANT)
        game.game_kind = kind
        policy["policy.group_grant.enabled"] = False
        policy["policy.group_grant.real_games"] = True
        assert (await refused(game)).reason == "group_refused:disabled"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_unreadable_flags_read_off(self, clock, limits):  # noqa: ARG002
        """No policy patch: the real reads fail in the unit test, so every flag reads its off default."""
        g = _make()
        set_mode(g, INSTANT)
        assert (await refused(g)).reason == "group_refused:disabled"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_max_per_game_zero_allows_nothing(self, game, policy):
        set_mode(game, INSTANT)
        policy["policy.group_grant.max_per_game"] = 0
        assert (await refused(game)).reason == "group_refused:cap_reached"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_second_group_in_a_game_is_refused_cap_reached(self, game):
        """The per-game backstop: a new event seq does not reset it (the agent's restart would)."""
        set_mode(game, INSTANT)
        game._shield_limits = lambda: (0, 0)  # no per-player limits: only the backstop refuses
        await game.grant_group_shield(["p1", "p2"], SECONDS)
        before = snapshot(game)
        assert (await refused(game, ["p2", "p3"])).reason == "group_refused:cap_reached"
        assert snapshot(game) == before

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_max_per_game_two_allows_a_second_group(self, game, policy):
        set_mode(game, INSTANT)
        game._shield_limits = lambda: (0, 0)
        policy["policy.group_grant.max_per_game"] = 2
        await game.grant_group_shield(["p1", "p2"], SECONDS)
        await game.grant_group_shield(["p2", "p3"], SECONDS)
        assert game._agent_group_grants == 2

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_a_refused_group_does_not_use_the_backstop(self, game):
        set_mode(game, INSTANT)
        _cap(game, "p3")
        await refused(game)
        assert game._agent_group_grants == 0
        await game.grant_group_shield(["p1", "p2"], SECONDS)
        assert game._agent_group_grants == 1


# --------------------------------------------------------------------------- #
# The event, the handler and the manager
# --------------------------------------------------------------------------- #
class _AgentStub:
    def __init__(self, allowed, threshold=20):
        self._allowed = allowed
        self._threshold = threshold

    def get_integer_value(self, key, default, _ctx=None):
        return self._threshold if key == "policy.battery_threshold" else default

    def get_string_value(self, key, default, _ctx=None):
        return ",".join(self._allowed) if key == "interventions_allowed" else default

    def get_object_value(self, _key, default, _ctx=None):
        return default


def _manager(game, allowed, battery=None):
    events = []

    async def publisher(event_type, data):
        events.append((event_type, data))

    mgr = InterventionManager(
        event_publisher=publisher,
        get_game=lambda: game,
        battery_provider=(lambda s: battery.get(s)) if battery is not None else None,
    )
    mgr._agent_client = _AgentStub(allowed)
    register_lifecycle_handlers(mgr)
    return mgr, events


def _published(events):
    return [d for et, d in events if et == GameEvent.AGENT_INTERVENTION]


class TestUC040A47GroupEvent:
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    def test_payload_round_trip(self):
        payload = _event_payload_string(GROUP_SPEC, {"serials": "p1,p2,p3", "seconds": "5"})
        assert _event_target_serials(GROUP_SPEC, payload) == SERIALS
        assert _event_group_seconds(payload) == 5.0
        assert _edge_target_serial(GROUP_SPEC, payload) is None  # not player-targeted
        assert _event_group_seconds(_event_payload_string(GROUP_SPEC, {"serials": "p1,p2"})) is None

    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    def test_spec_is_a_medium_one_shot_allowed_in_every_mode(self):
        assert GROUP_SPEC.type_id == "grant_group_shield"
        assert GROUP_SPEC.edge_triggered and not GROUP_SPEC.player_targeted
        assert all("grant_group_shield" not in deny for deny in MODE_CAPABILITY_DENY.values())

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
    async def test_dispatch_applies_once_and_records_one_intervention(self, game):
        set_mode(game, INSTANT)
        mgr, events = _manager(game, ["grant_group_shield"])
        session = SessionView(game_id="", game=game, publish=mgr._publish)
        payload = _event_payload_string(GROUP_SPEC, {"serials": "p1,p2,p3", "seconds": "5"})
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr._enforce_and_dispatch(GROUP_SPEC, payload, payload, None, session)
        assert all(committed(game, s, INSTANT) for s in SERIALS)
        published = _published(events)
        assert len(published) == 1
        assert published[0]["type"] == "grant_group_shield"
        assert published[0]["blocked"] == "false"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_refused_group_is_blocked_and_refunds_the_backstop(self, game):
        set_mode(game, INSTANT)
        _cap(game, "p3")
        mgr, events = _manager(game, ["grant_group_shield"])
        session = SessionView(game_id="", game=game, publish=mgr._publish)
        payload = _event_payload_string(GROUP_SPEC, {"serials": "p1,p2,p3", "seconds": "5"})
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr._enforce_and_dispatch(GROUP_SPEC, payload, payload, None, session)
        assert not committed(game, "p1", INSTANT)
        published = _published(events)
        assert published[0]["blocked"] == "true"
        assert published[0]["block_reason"] == "group_refused:shield_cap_reached"
        assert mgr._rate_limiter.current_weight() == pytest.approx(0.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_not_in_the_allow_list_is_not_allowed(self, game):
        """No interventions_allowed variant names grant_group_shield yet (PR 4)."""
        set_mode(game, INSTANT)
        mgr, events = _manager(game, ["grant_shield"])
        session = SessionView(game_id="", game=game, publish=mgr._publish)
        payload = _event_payload_string(GROUP_SPEC, {"serials": "p1,p2,p3", "seconds": "5"})
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr._enforce_and_dispatch(GROUP_SPEC, payload, payload, None, session)
        assert not any(committed(game, s, INSTANT) for s in SERIALS)
        assert _published(events)[0]["block_reason"] == "not_allowed"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_handler_passes_low_battery_serials(self, game):
        set_mode(game, INSTANT)
        mgr, _ = _manager(game, ["grant_group_shield"], battery={"p1": 80.0, "p2": 5.0, "p3": 90.0})
        payload = _event_payload_string(GROUP_SPEC, {"serials": "p1,p2,p3", "seconds": "5"})
        ctx = InterventionContext(
            spec=GROUP_SPEC, value=payload, payload=payload, target_serial=None, game=game, objective=""
        )
        with pytest.raises(InterventionNotApplied) as exc:
            await handle_group_shield(ctx, mgr)
        assert (exc.value.reason, exc.value.refused_serial) == ("group_refused:low_battery", "p2")
        assert not any(committed(game, s, INSTANT) for s in SERIALS)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A48", br=["BR-033"])
    async def test_handler_with_disabled_gate_reads_disabled_for_a_malformed_payload(self, game, policy):
        set_mode(game, INSTANT)
        policy["policy.group_grant.enabled"] = False
        mgr, _ = _manager(game, ["grant_group_shield"])
        ctx = InterventionContext(spec=GROUP_SPEC, value="|", payload="|", target_serial=None, game=game, objective="")
        with pytest.raises(InterventionNotApplied) as exc:
            await handle_group_shield(ctx, mgr)
        assert exc.value.reason == "group_refused:disabled"
