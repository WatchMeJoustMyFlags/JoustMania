"""
Hold-mode shield gauges for the agent (#2096, telemetry.md §6).

UC-040 A37 step 1 (the agent's view of the pool), UC-042 BR-007 (the retro's latched
values and per-player totals): the coordinator publishes the latched mode, every
latched hold value and each player's energy, budget, active flag and totals, and
removes them at game end.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_shield_hold import _make_ffa, arm, clock, events, fill, flags, latch, step  # noqa: F401 - fixtures

from services.game_coordinator import metrics as metrics_module
from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import BaseGameMode


@pytest.fixture(autouse=True)
def _no_shield_limits():
    with patch.object(BaseGameMode, "_shield_limits", return_value=(0, 0)):
        yield


class GaugeSpy:
    """A labelled gauge stand-in that keeps the last value per label set."""

    def __init__(self):
        self.values: dict[tuple, float] = {}
        self.removed: list[tuple] = []

    def labels(self, **labels):
        key = tuple(sorted(labels.items()))
        child = MagicMock()
        child.set.side_effect = lambda v: self.values.__setitem__(key, v)
        return child

    def remove(self, *labels):
        self.removed.append(labels)

    def get(self, **labels):
        return self.values.get(tuple(sorted(labels.items())))


GAUGES = (
    "shield_activation_mode",
    "shield_hold_setting",
    "player_shield_energy_seconds",
    "player_shield_budget_remaining_seconds",
    "player_shield_active",
    "player_shield_stats",
)


@pytest.fixture
def gauges():
    spies = {name: GaugeSpy() for name in GAUGES}
    with patch.multiple(base_module.metrics, **spies):
        yield spies


@pytest.fixture
def hold_game(clock, flags, events, gauges):  # noqa: F811, ARG001 - imported fixtures; gauges patch first
    flags.values["shield.energy_initial_seconds"] = 1.0
    g = _make_ffa(events)
    latch(g, clock)
    with patch.object(g, "_play_sound", new_callable=AsyncMock):
        yield g


GAME = {"game_kind": "primary", "game_id": "test_shield_hold", "experiment_id": "", "arm": ""}


def player_value(gauges, name, serial="p1", **extra):
    return gauges[name].get(serial=serial, game_id="test_shield_hold", **extra)


class TestUC040A37AgentView:
    @pytest.mark.uc("UC-040", flow="A37")
    @pytest.mark.uc("UC-042", br=["BR-007"])
    def test_the_latch_publishes_the_mode_and_every_latched_value(self, hold_game, gauges):
        assert gauges["shield_activation_mode"].get(mode="hold", **GAME) == 1
        settings = gauges["shield_hold_setting"]
        assert settings.get(setting="shield.energy_max_seconds", **GAME) == 5.0
        assert settings.get(setting="shield.energy_budget_seconds", **GAME) == 15.0
        assert settings.get(setting="shield.energy_initial_seconds", **GAME) == 1.0
        assert settings.get(setting="shield.recharge_per_second", **GAME) == 0.0
        assert settings.get(setting="shield.min_burst_seconds", **GAME) == 0.3
        assert settings.get(setting="shield.hold_rumble_intensity", **GAME) == 70.0
        assert settings.get(setting="shield.ending_rumble_seconds", **GAME) == 1.0
        assert settings.get(setting="shield.hold_rumble_max_intensity", **GAME) == 200.0  # #2463
        assert settings.get(setting="shield.hold_led_floor_percent", **GAME) == 20.0

    @pytest.mark.uc("UC-040", flow="A37")
    def test_a_trigger_game_publishes_its_mode_and_no_hold_value(self, clock, flags, events, gauges):  # noqa: F811
        flags.values["shield.activation"] = "trigger"
        g = _make_ffa(events)
        latch(g, clock)
        assert gauges["shield_activation_mode"].get(mode="trigger", **GAME) == 1
        assert gauges["shield_hold_setting"].values == {}

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A37")
    @pytest.mark.uc("UC-042", br=["BR-007"])
    async def test_player_gauges_follow_activation_grant_and_release(self, hold_game, clock, gauges):  # noqa: F811
        g = hold_game
        p1 = g.players["p1"]
        await arm(g, clock)
        await step(g, clock, "p1", True)
        g._set_hold_gauges(p1, clock.t)
        assert player_value(gauges, "player_shield_active") == 1
        assert player_value(gauges, "player_shield_stats", stat="activations") == 1

        assert await g.grant_shield("p1", 2.0)
        g._set_hold_gauges(p1, clock.t)
        assert player_value(gauges, "player_shield_energy_seconds") == pytest.approx(3.0, abs=0.05)
        assert player_value(gauges, "player_shield_stats", stat="granted_seconds") == pytest.approx(2.0)

        for _ in range(60):
            await step(g, clock, "p1", True)
        await step(g, clock, "p1", False)
        g._set_hold_gauges(p1, clock.t)
        used = p1.hold_budget_used
        assert player_value(gauges, "player_shield_active") == 0
        assert player_value(gauges, "player_shield_stats", stat="used_seconds") == pytest.approx(used, abs=1e-3)
        assert player_value(gauges, "player_shield_budget_remaining_seconds") == pytest.approx(15.0 - used, abs=1e-3)
        assert player_value(gauges, "player_shield_energy_seconds") == pytest.approx(3.0 - used, abs=1e-3)
        assert player_value(gauges, "player_shield_stats", stat="recharged_seconds") == pytest.approx(0.0, abs=1e-3)
        assert player_value(gauges, "player_shield_stats", stat="empties") == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A38")
    @pytest.mark.uc("UC-042", br=["BR-007"])
    async def test_recharged_is_what_the_recharge_added(self, clock, flags, events, gauges):  # noqa: F811
        flags.values["shield.recharge_per_second"] = 0.5
        g = _make_ffa(events)
        latch(g, clock)
        clock.t += 4.0
        g._set_hold_gauges(g.players["p1"], clock.t)
        assert player_value(gauges, "player_shield_energy_seconds") == pytest.approx(2.0)
        assert player_value(gauges, "player_shield_stats", stat="recharged_seconds") == pytest.approx(2.0)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A39")
    async def test_running_out_counts_an_empty_and_death_zeroes_the_pool(self, hold_game, clock, gauges):  # noqa: F811
        g = hold_game
        p1 = g.players["p1"]
        fill(g, "p1", 0.5)
        await arm(g, clock)
        for _ in range(40):
            await step(g, clock, "p1", True)
        assert p1.hold_empties == 1
        fill(g, "p1", 2.0)
        p1.alive = False
        g._drop_shield_hold(p1, base_module.HOLD_OUTCOME_DEATH, clock.t)
        assert player_value(gauges, "player_shield_energy_seconds") == 0.0
        assert player_value(gauges, "player_shield_active") == 0
        assert player_value(gauges, "player_shield_stats", stat="empties") == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-040", flow="A36")
    async def test_the_energy_event_sets_the_gauges(self, hold_game, clock, gauges):  # noqa: F811
        fill(hold_game, "p1", 2.0)
        await hold_game._publish_hold_energy("p1", False)
        assert player_value(gauges, "player_shield_energy_seconds") == pytest.approx(2.0)

    @pytest.mark.uc("UC-040", flow="A39")
    def test_game_end_removes_the_game_series(self, hold_game, gauges):
        hold_game._clear_shield_metrics()
        labels = tuple(GAME.values())
        assert ("hold", *labels) in gauges["shield_activation_mode"].removed
        assert ("shield.energy_max_seconds", *labels) in gauges["shield_hold_setting"].removed
        hold_game._clear_shield_metrics()  # idempotent: nothing left to remove
        assert len(gauges["shield_activation_mode"].removed) == 1


@pytest.mark.uc("UC-040", flow="A39")
def test_clear_player_analytics_removes_the_player_shield_gauges():
    spies = {
        name: MagicMock()
        for name in (
            "player_shield_energy_seconds",
            "player_shield_budget_remaining_seconds",
            "player_shield_active",
            "player_shield_stats",
        )
    }
    with patch.multiple(metrics_module, **spies):
        metrics_module.clear_player_analytics("p1", "g1")
    spies["player_shield_energy_seconds"].remove.assert_called_once_with("p1", "g1")
    spies["player_shield_active"].remove.assert_called_once_with("p1", "g1")
    totals = {call.args for call in spies["player_shield_stats"].remove.call_args_list}
    assert totals == {("p1", "g1", t) for t in metrics_module.PLAYER_SHIELD_STATS}
