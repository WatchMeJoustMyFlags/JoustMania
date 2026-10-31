"""
The refusal check of a single shield grant, before any write or await (#2467 PR 2).

UC-040 A31 (``already_charged``), A34 (cap, cooldown), A37 (budget, pool), A42
step 3 (``game_ending``), BR-022 (in trigger mode the cooldown and the count are
recorded when the charge is stored, before its feedback).

``_shield_grant_refusal`` is pure: it returns the reason ``grant_shield`` raises,
in ``grant_shield``'s order, and writes nothing. Every test runs on a fake clock;
the limits come from ``_shield_limits`` (patched per test).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from opentelemetry.trace import Span

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from services.game_coordinator import game_clock
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode, Player, ShieldHoldParams
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.intervention_errors import InterventionNotApplied

T0 = 1000.0
INSTANT = "instant"
TRIGGER = "trigger"
CAP = "shield_cap_reached"
COOLDOWN = "shield_cooldown"
CHARGED = "already_charged"
BUDGET = "shield_budget_exhausted"
POOL = "shield_pool_full"
ENDING = "game_ending"
# Pool 5 s, budget 15 s, minimum burst 0.5 s, no recharge.
HOLD = ShieldHoldParams(cap=5.0, budget=15.0, initial=0.0, recharge=0.0, min_burst=0.5)


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
    """(max shields, cooldown seconds) returned by ``_shield_limits``; tests change it."""
    current = {"value": (3, 15)}
    with patch.object(BaseGameMode, "_shield_limits", side_effect=lambda: current["value"]) as read:
        read.current = current
        yield read


@pytest.fixture
def game(clock, limits):  # noqa: ARG001 - the fixtures patch base for the test
    g = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_shield_grant_refusal",
    )
    g.gameplay_stream = RecordingStream()
    for serial in ("p1", "p2"):
        g.players[serial] = Player(serial=serial, alive=True, color=(0, 255, 0))
    g._play_sound = AsyncMock()
    g._shield_hold = HOLD  # read by the budget and energy helpers in hold mode
    return g


def used_up(game, serial="p1", count=3):
    """The player had ``count`` agent shields, the last one long over (cap only)."""
    game._agent_shield_grants[serial] = count
    game._agent_shield_last_end[serial] = T0 - 1000.0


def cooling(game, serial="p1"):
    """The player's last agent shield ended 1 s ago (cooldown only)."""
    game._agent_shield_grants[serial] = 1
    game._agent_shield_last_end[serial] = T0 - 1.0


def refusal(game, mode=INSTANT, hold=None, serial="p1"):
    return game._shield_grant_refusal(game.players[serial], 5.0, T0, mode, hold)


# --------------------------------------------------------------------------- #
# Each reason, per mode
# --------------------------------------------------------------------------- #
class TestUC040ShieldGrantRefusalReasons:
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    @pytest.mark.parametrize("mode", [INSTANT, TRIGGER])
    def test_a_fresh_player_is_not_refused(self, game, mode):
        assert refusal(game, mode) is None

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    def test_a_fresh_player_is_not_refused_in_hold_mode(self, game):
        game.players["p1"].hold_energy = 1.0
        assert refusal(game, hold=HOLD) is None

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    @pytest.mark.parametrize("mode,hold", [(INSTANT, None), (TRIGGER, None), (INSTANT, HOLD)])
    def test_cap(self, game, mode, hold):
        used_up(game)
        assert refusal(game, mode, hold) == CAP

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    @pytest.mark.parametrize("mode,hold", [(INSTANT, None), (TRIGGER, None), (INSTANT, HOLD)])
    def test_cooldown(self, game, mode, hold):
        cooling(game)
        assert refusal(game, mode, hold) == COOLDOWN

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    def test_cooldown_ends_at_the_shield_end_plus_the_cooldown(self, game):
        cooling(game)  # ended at T0 - 1, cooldown 15 s: refused until T0 + 14
        player = game.players["p1"]
        assert game._shield_grant_refusal(player, 5.0, T0 + 13.9, INSTANT, None) == COOLDOWN
        assert game._shield_grant_refusal(player, 5.0, T0 + 14.0, INSTANT, None) is None

    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020"])
    def test_already_charged_in_trigger_mode(self, game):
        game.players["p1"].shield_charge_seconds = 5.0
        assert refusal(game, TRIGGER) == CHARGED

    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020"])
    def test_a_held_charge_refuses_nothing_outside_trigger_mode(self, game):
        game.players["p1"].shield_charge_seconds = 5.0
        assert refusal(game, INSTANT) is None
        assert refusal(game, TRIGGER, HOLD) is None  # hold mode ignores the activation mode

    @pytest.mark.uc("UC-040", flow="A37", br=["BR-024"])
    def test_budget_exhausted_in_hold_mode(self, game):
        game.players["p1"].hold_budget_used = 14.8  # 0.2 s left, below the 0.5 s burst
        assert refusal(game, hold=HOLD) == BUDGET

    @pytest.mark.uc("UC-040", flow="A37", br=["BR-024"])
    def test_pool_full_in_hold_mode(self, game):
        game.players["p1"].hold_energy = 5.0
        assert refusal(game, hold=HOLD) == POOL

    @pytest.mark.uc("UC-040", flow="A37", br=["BR-024"])
    def test_no_hold_reason_outside_hold_mode(self, game):
        player = game.players["p1"]
        player.hold_budget_used = 15.0
        player.hold_energy = 5.0
        assert refusal(game, INSTANT) is None

    @pytest.mark.uc("UC-040", flow="A42")
    @pytest.mark.parametrize("mode,hold", [(INSTANT, None), (TRIGGER, None), (INSTANT, HOLD)])
    def test_game_ending(self, game, mode, hold):
        game.force_end_reason = "test"
        assert refusal(game, mode, hold) == ENDING


# --------------------------------------------------------------------------- #
# The order
# --------------------------------------------------------------------------- #
class TestUC040ShieldGrantRefusalOrder:
    @pytest.mark.uc("UC-040", flow="A42")
    @pytest.mark.parametrize("mode,hold", [(INSTANT, None), (TRIGGER, None), (INSTANT, HOLD)])
    def test_game_ending_comes_first(self, game, mode, hold):
        player = game.players["p1"]
        player.shield_charge_seconds = 5.0
        player.hold_budget_used = 15.0
        used_up(game)
        game.force_end_reason = "test"
        assert refusal(game, mode, hold) == ENDING

    @pytest.mark.uc("UC-040", flow="A31", br=["BR-020", "BR-022"])
    def test_trigger_already_charged_before_the_cap(self, game):
        game.players["p1"].shield_charge_seconds = 5.0
        used_up(game)
        assert refusal(game, TRIGGER) == CHARGED

    @pytest.mark.uc("UC-040", flow="A37", br=["BR-024"])
    def test_hold_budget_before_the_pool(self, game):
        player = game.players["p1"]
        player.hold_budget_used = 14.8
        player.hold_energy = 5.0
        assert refusal(game, hold=HOLD) == BUDGET

    @pytest.mark.uc("UC-040", flow="A37", br=["BR-022", "BR-024"])
    def test_hold_pool_before_the_cap(self, game):
        game.players["p1"].hold_energy = 5.0
        used_up(game)
        assert refusal(game, hold=HOLD) == POOL

    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    @pytest.mark.parametrize("mode,hold", [(INSTANT, None), (TRIGGER, None), (INSTANT, HOLD)])
    def test_cap_before_the_cooldown(self, game, mode, hold):
        used_up(game)
        game._agent_shield_last_end["p1"] = T0 - 1.0
        assert refusal(game, mode, hold) == CAP

    @pytest.mark.uc("UC-040", flow="A37", br=["BR-022"])
    def test_the_limits_are_not_read_when_an_earlier_check_refuses(self, game, limits):
        game.players["p1"].hold_budget_used = 15.0
        assert refusal(game, hold=HOLD) == BUDGET
        game.players["p1"].shield_charge_seconds = 5.0
        assert refusal(game, TRIGGER) == CHARGED
        limits.assert_not_called()


# --------------------------------------------------------------------------- #
# Pure: the check writes nothing
# --------------------------------------------------------------------------- #
class TestUC040ShieldGrantRefusalIsPure:
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    def test_a_refusal_records_nothing(self, game):
        player = game.players["p1"]
        player.span = create_autospec(Span, instance=True)
        cooling(game)
        before = (dict(game._agent_shield_grants), dict(game._agent_shield_last_end), player.grace_until)
        assert refusal(game) == COOLDOWN
        assert (dict(game._agent_shield_grants), dict(game._agent_shield_last_end), player.grace_until) == before
        player.span.add_event.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
    async def test_grant_shield_raises_the_reason_the_check_returns(self, game, limits):
        cooling(game)
        assert refusal(game) == COOLDOWN
        with (
            patch.object(BaseGameMode, "_shield_activation_mode", return_value=INSTANT),
            pytest.raises(InterventionNotApplied) as exc,
        ):
            await game.grant_shield("p1", 5.0)
        assert exc.value.reason == COOLDOWN
        limits.current["value"] = (1, 0)
        assert refusal(game) == CAP


# --------------------------------------------------------------------------- #
# BR-022: trigger mode records the charge before its feedback
# --------------------------------------------------------------------------- #
class TestUC040BR022TriggerRecordsBeforeFeedback:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A30", br=["BR-022"])
    @pytest.mark.parametrize("error", [asyncio.CancelledError, RuntimeError])
    async def test_a_failed_charge_feedback_still_counts_the_charge(self, game, limits, error):
        """The charge is stored, so its count and cooldown hold even if the feedback raises."""
        limits.current["value"] = (3, 15)
        player = game.players["p1"]
        with (
            patch.object(BaseGameMode, "_shield_activation_mode", return_value=TRIGGER),
            patch.object(game, "_read_charge_expiry", return_value=0.0),
            patch.object(game, "_send_shield_charge_layer", side_effect=error("feedback failed")),
            pytest.raises(error),
        ):
            await game.grant_shield("p1", 5.0)
        assert player.shield_charge_seconds == 5.0
        assert game._agent_shield_grants["p1"] == 1
        assert game._agent_shield_last_end["p1"] == T0 + 5.0
