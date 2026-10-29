"""
Shield limits per player per game: the coordinator backstop (#2044).

UC-040 A34 (the coordinator refuses a grant on the cap or the cooldown), BR-022
(count and cooldown definitions, trigger mode counts from the grant), A22 (the
manager records the refusal and refunds the backstop).

Every test runs on a fake clock (``base.time.time``). The limits come from
``_shield_limits`` (patched per test) unless a test is about the flag read.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from opentelemetry.trace import Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent
from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import (
    DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME,
    DEFAULT_SHIELD_COOLDOWN_SECONDS,
    BaseGameMode,
    Player,
    resolve_shield_limit,
)
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.intervention_errors import InterventionNotApplied

T0 = 1000.0
CAP = "shield_cap_reached"
COOLDOWN = "shield_cooldown"


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
    # The game clock drives the deadlines (UC-030 BR-010); the wall clock the wire timestamps.
    with (
        patch.object(base_module.time, "time", side_effect=lambda: c.t),
        patch.object(game_clock, "now", side_effect=lambda: c.t),
    ):
        yield c


@pytest.fixture
def activation():
    """``shield.activation`` for the test (instant unless a test sets trigger)."""
    mode = {"value": "instant"}
    with patch.object(BaseGameMode, "_shield_activation_mode", side_effect=lambda: mode["value"]):
        yield mode


@pytest.fixture
def limits():
    """(max shields, cooldown seconds) returned by ``_shield_limits``; tests change it."""
    current = {"value": (3, 15)}
    with patch.object(BaseGameMode, "_shield_limits", side_effect=lambda: current["value"]):
        yield current


def _make_ffa(game_id="test_shield_limits"):
    events = EventCollector()
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=events.publish,
        audio_client=None,
        game_id=game_id,
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    g._play_sound = AsyncMock()
    return g, events


@pytest.fixture
def game(clock, activation, limits):  # noqa: ARG001 - the fixtures patch base for the test
    g, _events = _make_ffa()
    return g


async def _refused(game, serial, seconds=5.0):
    with pytest.raises(InterventionNotApplied) as exc:
        await game.grant_shield(serial, seconds)
    return exc.value.reason


# --------------------------------------------------------------------------- #
# UC-040 A34 / BR-022: the cap
# --------------------------------------------------------------------------- #
class TestUC040A34Cap:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_three_grants_apply_and_the_fourth_is_refused(self, game, clock, limits):
        """A34 step 1-2: 3 agent shields apply, the 4th raises shield_cap_reached and applies nothing."""
        limits["value"] = (3, 0)
        player = game.players["p1"]
        for _ in range(3):
            assert await game.grant_shield("p1", 5.0) is True
            clock.t += 10.0  # each shield ran out
        grace_before = player.grace_until

        assert await _refused(game, "p1") == CAP
        assert player.grace_until == grace_before
        # The cap is per player: p2 still gets a shield.
        assert await game.grant_shield("p2", 5.0) is True

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_refusal_is_recorded_on_the_player_span(self, game, clock, limits):
        """A34 step 2: span event shield_refused with the reason and the grants used."""
        limits["value"] = (1, 0)
        player = game.players["p1"]
        player.span = create_autospec(Span, instance=True)
        await game.grant_shield("p1", 5.0)
        clock.t += 10.0
        assert await _refused(game, "p1") == CAP

        refused = [
            c.kwargs["attributes"] for c in player.span.add_event.call_args_list if c.args[0] == "shield_refused"
        ]
        assert refused and refused[0]["shield.refusal_reason"] == CAP
        assert refused[0]["shield.grants_used"] == 1
        assert refused[0]["player.serial"] == "p1"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_kept_grant_does_not_count(self, game, clock, limits):
        """BR-022: a kept (no-op) grant moves nothing and does not use a slot."""
        limits["value"] = (1, 0)
        player = game.players["p1"]
        player.grace_until = T0 + 60.0  # a longer grace (e.g. a spawn grace) is already running
        assert await game.grant_shield("p1", 5.0) is True  # kept
        assert player.grace_until == T0 + 60.0

        clock.t += 61.0
        assert await game.grant_shield("p1", 5.0) is True  # the one real shield
        assert player.grace_until == clock.t + 5.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_zero_means_unlimited_and_off(self, game, clock, limits):
        """BR-022: cap 0 = unlimited, cooldown 0 = off (a running shield is extended)."""
        limits["value"] = (0, 0)
        player = game.players["p1"]
        for i in range(10):
            assert await game.grant_shield("p1", 5.0) is True
            assert player.grace_until == clock.t + 5.0, f"grant {i} did not extend"
            clock.t += 1.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_a_new_game_instance_starts_fresh(self, clock, activation, limits):  # noqa: ARG002
        """A34 step 3: the count lives on the game-mode instance; the next game starts at 0."""
        limits["value"] = (1, 0)
        first, _ = _make_ffa("game-1")
        await first.grant_shield("p1", 5.0)
        clock.t += 10.0
        assert await _refused(first, "p1") == CAP

        second, _ = _make_ffa("game-2")
        assert await second.grant_shield("p1", 5.0) is True

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_a_revive_does_not_reset_the_count(self, game, clock, limits):
        """BR-022: keyed by serial, so a player who died and came back keeps the count."""
        limits["value"] = (1, 0)
        await game.grant_shield("p1", 5.0)
        clock.t += 10.0
        game.players["p1"].alive = False  # killed
        assert await game.revive_player("p1") is True
        assert game.players["p1"].alive is True
        assert await _refused(game, "p1") == CAP


# --------------------------------------------------------------------------- #
# UC-040 A34 / BR-022: the cooldown
# --------------------------------------------------------------------------- #
class TestUC040A34Cooldown:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_grant_within_the_cooldown_is_refused(self, game, clock, limits):
        """A34: refused while now < shield end + cooldown; admitted after it."""
        limits["value"] = (0, 15)
        assert await game.grant_shield("p1", 5.0) is True  # ends at T0 + 5
        clock.t = T0 + 5.0 + 15.0 - 0.01
        assert await _refused(game, "p1") == COOLDOWN
        clock.t = T0 + 5.0 + 15.0
        assert await game.grant_shield("p1", 5.0) is True

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_cooldown_refuses_an_extension_of_a_running_shield(self, game, clock, limits):
        """BR-022: with a cooldown set, a running agent shield is never extended."""
        limits["value"] = (0, 15)
        player = game.players["p1"]
        await game.grant_shield("p1", 5.0)
        clock.t += 1.0
        assert await _refused(game, "p1", 10.0) == COOLDOWN
        assert player.grace_until == T0 + 5.0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_cooldown_ignores_other_graces(self, game, clock, limits):
        """BR-022: the cooldown runs from the agent shield's own end, never grace_until."""
        limits["value"] = (0, 15)
        player = game.players["p1"]
        await game.grant_shield("p1", 5.0)  # agent shield ends at T0 + 5
        player.grace_until = T0 + 100.0  # e.g. a long respawn grace set by the mode
        clock.t = T0 + 20.0  # past the agent shield end + cooldown, inside the other grace
        assert await game.grant_shield("p1", 5.0) is True  # kept under the longer grace, not refused

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_cap_is_checked_before_the_cooldown(self, game, clock, limits):  # noqa: ARG002
        """A34 step 1: the cap first, then the cooldown."""
        limits["value"] = (1, 15)
        await game.grant_shield("p1", 5.0)
        assert await _refused(game, "p1") == CAP

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_a_live_cooldown_flip_applies_at_the_next_grant(self, game, clock, limits):
        """flags.md §3: the stored end plus the live value."""
        limits["value"] = (0, 15)
        await game.grant_shield("p1", 5.0)
        clock.t = T0 + 10.0
        assert await _refused(game, "p1") == COOLDOWN
        limits["value"] = (0, 0)
        assert await game.grant_shield("p1", 5.0) is True


# --------------------------------------------------------------------------- #
# UC-040 A34 / BR-022: trigger mode counts the charge and paces from the grant
# --------------------------------------------------------------------------- #
class TestUC040A34TriggerMode:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022", "BR-020"])
    async def test_charge_counts_and_already_charged_comes_first(self, game, clock, activation, limits):
        """A charge counts against the cap; a held charge still refuses as already_charged first."""
        activation["value"] = "trigger"
        limits["value"] = (1, 0)
        player = game.players["p1"]
        with patch.object(game, "_read_charge_expiry", return_value=0.0):
            assert await game.grant_shield("p1", 5.0) is True
            assert player.shield_charge_seconds == 5.0
            assert await _refused(game, "p1") == "already_charged"
            game._clear_shield_charge(player)  # the charge ended (used or dropped)
            clock.t += 100.0
            assert await _refused(game, "p1") == CAP

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_cooldown_runs_from_the_grant_not_the_activation(self, game, clock, activation, limits):
        """BR-022: trigger mode, cooldown from charge stored + seconds, whenever it is activated."""
        activation["value"] = "trigger"
        limits["value"] = (0, 15)
        player = game.players["p1"]
        with patch.object(game, "_read_charge_expiry", return_value=0.0):
            await game.grant_shield("p1", 5.0)  # stored at T0, cooldown until T0 + 20
            clock.t = T0 + 12.0
            await game._activate_shield_charge(player, clock.t)  # shield until T0 + 17
            clock.t = T0 + 19.0
            assert await _refused(game, "p1") == COOLDOWN
            clock.t = T0 + 20.0  # from the activation it would still be cooling down until T0 + 32
            assert await game.grant_shield("p1", 5.0) is True
            assert player.shield_charge_seconds == 5.0


# --------------------------------------------------------------------------- #
# UC-040 BR-022: the flag read
# --------------------------------------------------------------------------- #
class TestUC040BR022FlagRead:
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    def test_flag_values_are_read_from_the_agent_set(self):
        """_shield_limits reads both literal keys from the agent set with the game id."""
        g, _ = _make_ffa("g-read")
        values = {"policy.max_shields_per_player_per_game": 6, "policy.shield_cooldown_seconds": 8}
        calls = []

        def fake_read(domain, key, _default, game_id=None):
            calls.append((domain, key, game_id))
            return values[key]

        with patch.object(base_module, "read_int_flag", side_effect=fake_read):
            assert g._shield_limits() == (6, 8)
        assert {(d, gid) for d, _k, gid in calls} == {("agent", "g-read")}

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    def test_a_read_error_gives_the_defaults(self):
        """BR-022: an unreadable flag set keeps the limits on (3 / 15 s)."""
        g, _ = _make_ffa()
        with patch("lib.feature_flags.init_flag_domain", side_effect=RuntimeError("flagd down")):
            assert g._shield_limits() == (3, 15)
        assert (DEFAULT_MAX_SHIELDS_PER_PLAYER_PER_GAME, DEFAULT_SHIELD_COOLDOWN_SECONDS) == (3, 15)

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    def test_a_negative_value_gives_the_default(self):
        g, _ = _make_ffa()
        with patch.object(base_module, "read_int_flag", return_value=-1):
            assert g._shield_limits() == (3, 15)

    @pytest.mark.parametrize(("value", "expected"), [(0, 0), (6, 6), (-1, 3), (True, 3), (2.5, 3), ("4", 3), (None, 3)])
    def test_resolve_shield_limit(self, value, expected):
        assert resolve_shield_limit(value, 3) == expected


# --------------------------------------------------------------------------- #
# UC-040 A22 / A34: the manager records the refusal and refunds the backstop
# --------------------------------------------------------------------------- #
class TestUC040A34Manager:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    @pytest.mark.uc("UC-040", flow="A22")
    async def test_handler_lets_the_refusal_reach_the_manager(self, game, clock, limits):
        """A34: handle_shield_seconds lets shield_cap_reached reach the manager."""
        from services.game_coordinator.interventions import INTERVENTION_SPECS, InterventionContext
        from services.game_coordinator.lifecycle_handlers import handle_shield_seconds

        limits["value"] = (1, 0)
        spec = next(s for s in INTERVENTION_SPECS if s.flag_key == "shield_seconds")
        ctx = InterventionContext(spec=spec, value=5.0, payload="", target_serial="p1", game=game, objective="balanced")
        with patch("services.game_coordinator.lifecycle_handlers._grant_values", return_value={"p1": 5.0}):
            await handle_shield_seconds(ctx, MagicMock())
            clock.t += 10.0
            with pytest.raises(InterventionNotApplied) as exc:
                await handle_shield_seconds(ctx, MagicMock())
        assert exc.value.reason == CAP

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.parametrize(("limit", "reason"), [((1, 0), CAP), ((0, 15), COOLDOWN)])
    async def test_manager_records_the_reason_and_refunds_the_backstop(self, game, limits, limit, reason):
        """A34 step 2 / A22: blocked=true with the reason; the backstop reservation is refunded."""
        from services.game_coordinator.tests.event_list_helpers import edge_event
        from services.game_coordinator.tests.test_interventions import make_manager

        limits["value"] = limit
        await game.grant_shield("p1", 5.0)  # the slot or the cooldown is now taken

        mgr, published = make_manager(interventions={"audio_cue": edge_event("audio_cue", "beep")})

        async def grant_again(_ctx):
            await game.grant_shield("p1", 5.0)

        mgr.register_handler("audio_cue", grant_again)
        with patch("services.game_coordinator.metrics.interventions_total"):
            await mgr.evaluate_all()

        rows = [d for et, d in published if et == GameEvent.AGENT_INTERVENTION]
        assert rows[0]["blocked"] == "true"
        assert rows[0]["block_reason"] == reason
        assert mgr._rate_limiter.current_weight() == 0.0
