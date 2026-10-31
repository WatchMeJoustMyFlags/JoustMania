"""
Per-player flag lifecycle on the coordinator (#1317, #1323, #1269).

UC-040 A24 / BR-017: when every player's ``player_sensitivity_factor`` or
``player_handicap_factor`` resolves to neutral again, the coordinator resets the
factors in the game. Before #1317 the all-neutral transition dispatched nothing,
so the last factor stayed for the rest of the game. Levels are game-scoped in
the flag service, so a new game never sees a level of an earlier game.

UC-040 A25 / BR-016: the agent prunes a retired game's players from the
per-player flags. A pruned player resolves to the neutral variant, which never
fires a grant.

UC-040 A19 / BR-012: a player the game has not read yet (e.g. revived) has no
baseline entry; it counts as neutral, so its grant or level in this game's
scope applies once. A failed read never fires and never moves a baseline.

Reuses the session harness of ``test_intervention_scoping``.
"""

import sys
from pathlib import Path

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.tests.conftest import FakeInterventionGame
from services.game_coordinator.tests.test_intervention_scoping import (
    GRANT_FLAGS,
    REAL,
    SHADOW,
    SPEC_BY_KEY,
    Harness,
    _grant,
)

LEVEL_ATTR = {"player_sensitivity_factor": "sensitivity_factor", "player_handicap_factor": "handicap_factor"}
LEVEL_FLAGS = list(LEVEL_ATTR)


class LevelHarness(Harness):
    """Harness whose games' players carry the two per-player factors."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        register_difficulty_handlers(self.mgr)

    def start_game(self, game_id, *, real, serials=("p1", "p2")) -> FakeInterventionGame:
        game = super().start_game(game_id, real=real, serials=serials)
        for player in game.players.values():
            player.sensitivity_factor = 1.0
            player.handicap_factor = 1.0
        return game

    def set_level(self, flag_key, serial, value, *, game_id=None):
        """Write a level; with ``game_id`` it lives in that game's scope only."""
        entry = (f"r1_{serial}", value)
        if game_id is None:
            self.flags.per_serial[(flag_key, serial)] = entry
        else:
            self.flags.scoped_serial[(flag_key, game_id, serial)] = entry

    def prune(self, *serials):
        """What the agent's prune does to the flag: drop the players' entries."""
        for key in list(self.flags.per_serial):
            if key[1] in serials:
                del self.flags.per_serial[key]


def factors(game, flag_key) -> dict:
    return {s: getattr(p, LEVEL_ATTR[flag_key]) for s, p in game.players.items()}


# --------------------------------------------------------------------------- #
# A24 / BR-017: levels reset to neutral in the game (#1317)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_level_resets_to_neutral_when_every_player_reverts(flag_key):
    h = LevelHarness()
    game = h.start_game(REAL, real=True)

    h.set_level(flag_key, "p1", 1.5)
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.5, "p2": 1.0}

    h.prune("p1")
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 1.0}

    h.set_level(flag_key, "p2", 0.75)  # a later level still applies
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 0.75}


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_level_reset_is_a_revert_not_an_enforced_intervention():
    """The reset runs even when the level is no longer allowed and records no
    agent_intervention event (it restores a default)."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5)
    await h.flag_change()
    applied = len(h.recorded(REAL, "adjust_player_sensitivity"))

    h.mgr._agent_client.get_string_value = lambda _key, _default, _ctx=None: "none"
    h.prune("p1")
    await h.flag_change()

    assert game.players["p1"].sensitivity_factor == 1.0
    assert len(h.recorded(REAL, "adjust_player_sensitivity")) == applied
    assert h.recorded(REAL, "adjust_player_sensitivity", blocked="true") == []


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_partial_revert_resets_only_the_reverted_player():
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5)
    h.set_level("player_sensitivity_factor", "p2", 0.7)
    await h.flag_change()

    h.prune("p1")
    await h.flag_change()

    assert factors(game, "player_sensitivity_factor") == {"p1": 1.0, "p2": 0.7}


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_never_applied_neutral_levels_dispatch_nothing():
    """A game whose levels were never non-neutral resets nobody: a manual factor
    set by something else (e.g. a test or another lever) is left alone."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    game.players["p1"].sensitivity_factor = 1.3
    h.flags.values["volume_override"] = 0.5  # unrelated write
    await h.flag_change()
    assert game.players["p1"].sensitivity_factor == 1.3


# --------------------------------------------------------------------------- #
# BR-017: no carry-over into the next game (#1269 level part)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_next_game_does_not_inherit_levels(flag_key):
    """p1's level lives in game N's scope, which ends with N. Game N+1 (same
    serials) starts neutral, and a level decided in N+1 for p1 applies."""
    h = LevelHarness()
    game_n = h.start_game("game_n", real=True)
    h.set_level(flag_key, "p1", 1.5, game_id="game_n")
    await h.flag_change()
    assert factors(game_n, flag_key)["p1"] == 1.5
    h.end("game_n")

    game_n1 = h.start_game("game_n1", real=True)
    await h.flag_change()
    assert factors(game_n1, flag_key) == {"p1": 1.0, "p2": 1.0}

    h.set_level(flag_key, "p1", 0.7, game_id="game_n1")  # a decision in N+1
    await h.flag_change()
    assert factors(game_n1, flag_key) == {"p1": 0.7, "p2": 1.0}


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_concurrent_shadow_does_not_inherit_the_real_games_level():
    h = LevelHarness()
    real = h.start_game(REAL, real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5, game_id=REAL)
    await h.flag_change()

    shadow = h.start_game(SHADOW, real=False)  # shares serial p1 in this fake
    await h.flag_change()

    assert real.players["p1"].sensitivity_factor == 1.5
    assert shadow.players["p1"].sensitivity_factor == 1.0


# --------------------------------------------------------------------------- #
# A25 / BR-016: a prune never re-fires a grant
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A25", br=["BR-016", "BR-012"])
@pytest.mark.parametrize("flag_key", GRANT_FLAGS)
@pytest.mark.asyncio
async def test_prune_never_refires_a_grant(flag_key):
    """p1 was granted; the agent prunes p1 (neutral variant) and later p2's
    grant arrives. p1 is never granted again, p2 once."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    _grant(h, flag_key, "p1", "1")
    await h.flag_change()

    h.prune("p1")
    await h.flag_change()
    await h.flag_change()
    _grant(h, flag_key, "p2", "2")
    await h.flag_change()

    assert game.grants(flag_key) == ["p1", "p2"]
    blocked = h.recorded(REAL, SPEC_BY_KEY[flag_key].type_id, blocked="true")
    assert blocked == []


@pytest.mark.uc("UC-040", flow="A25", br=["BR-016"])
@pytest.mark.asyncio
async def test_prune_of_a_retired_games_players_touches_no_live_game():
    """Shadow S ended; pruning its serials changes nothing in the live real game:
    no dispatch, no event, factors and grants unchanged."""
    h = LevelHarness()
    real = h.start_game(REAL, real=True, serials=("r1", "r2"))
    h.start_game(SHADOW, real=False, serials=("MOCK0001", "MOCK0002"))
    h.set_level("player_sensitivity_factor", "r1", 1.5)
    h.set_level("player_sensitivity_factor", "MOCK0001", 0.7)
    _grant(h, "shield_seconds", "MOCK0002", "1")
    await h.flag_change()
    events_before = list(h.events.get(REAL, []))

    h.end(SHADOW)
    h.prune("MOCK0001", "MOCK0002")
    await h.flag_change()

    assert h.events.get(REAL, []) == events_before
    assert real.players["r1"].sensitivity_factor == 1.5
    assert real.shields == []


# --------------------------------------------------------------------------- #
# A19 / BR-012: a player the game has not read yet counts as neutral
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.parametrize("flag_key", GRANT_FLAGS)
@pytest.mark.asyncio
async def test_revived_player_gets_its_grant_once(flag_key):
    """p2 is dead on the game's first read, so it has no baseline entry. Once
    revived, its grant in this game's scope applies, once; a new grant again."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    game.players["p2"].alive = False
    _grant(h, flag_key, "p2", "1", game_id=REAL)
    await h.flag_change()
    assert game.grants(flag_key) == []

    game.players["p2"].alive = True  # revived
    await h.flag_change()
    await h.flag_change()
    assert game.grants(flag_key) == ["p2"]

    _grant(h, flag_key, "p2", "2", game_id=REAL)
    await h.flag_change()
    assert game.grants(flag_key) == ["p2", "p2"]


# --------------------------------------------------------------------------- #
# BR-012: two grants within one flagd poll collapse to the later one
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_two_grants_within_one_poll_apply_the_later_once():
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    _grant(h, "shield_seconds", "p1", "1", 10.0)
    _grant(h, "shield_seconds", "p1", "2", 3.0)  # same poll: flagd serves only this
    await h.flag_change()
    await h.flag_change()
    assert game.shields == [("p1", 3.0)]


# --------------------------------------------------------------------------- #
# BR-017: the level baseline fails closed on flagd errors
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_level_read_error_at_game_start_never_fires(flag_key):
    """flagd errors on the game's first reads: the getter would return the
    neutral default. The failed read applies nothing and moves no baseline; on
    recovery the game's level applies, once."""
    h = LevelHarness()
    h.set_level(flag_key, "p1", 1.5, game_id=REAL)
    h.flags.failing = {flag_key}
    game = h.start_game(REAL, real=True)
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 1.0}
    assert h.mgr._state_bucket(REAL)[flag_key] == {}

    h.flags.failing = set()
    await h.flag_change()
    await h.flag_change()
    assert factors(game, flag_key)["p1"] == 1.5
    assert len(h.recorded(REAL, SPEC_BY_KEY[flag_key].type_id)) == 1


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_level_read_error_mid_game_keeps_the_baseline():
    """An error read mid-game neither resets nor moves the baseline."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5)
    await h.flag_change()

    h.flags.failing = {"player_sensitivity_factor"}
    await h.flag_change()
    assert game.players["p1"].sensitivity_factor == 1.5
    h.flags.failing = set()
    await h.flag_change()
    assert game.players["p1"].sensitivity_factor == 1.5
    assert len(h.recorded(REAL, "adjust_player_sensitivity")) == 1


# --------------------------------------------------------------------------- #
# A24: a move to neutral is a revert, never blocked by the chain
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("blocker", ["kill_switch", "budget_exhausted"])
@pytest.mark.asyncio
async def test_partial_revert_to_neutral_bypasses_a_blocked_chain(blocker):
    """p1 reverts while p2 keeps its level and the chain blocks everything:
    p1 is still reset (the baseline moved, so a blocked dispatch would leave
    p1's factor stuck for the rest of the game)."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5)
    h.set_level("player_sensitivity_factor", "p2", 0.7)
    await h.flag_change()

    agent = h.mgr._agent_client
    if blocker == "kill_switch":
        agent.get_string_value = lambda _key, _default, _ctx=None: "none"
    else:
        original = agent.get_integer_value
        agent.get_integer_value = lambda key, default, _ctx=None: (
            0 if key == "policy.coordinator_backstop_per_minute" else original(key, default, _ctx)
        )
    h.prune("p1")
    await h.flag_change()

    assert factors(game, "player_sensitivity_factor") == {"p1": 1.0, "p2": 0.7}
    assert h.recorded(REAL, "adjust_player_sensitivity", blocked="true") == []


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_revived_player_gets_its_level(flag_key):
    """p2 is dead on the game's first read, so the level baseline has no entry
    for it. Once revived, its level in this game's scope applies, once."""
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    game.players["p2"].alive = False
    h.set_level(flag_key, "p2", 1.5, game_id=REAL)
    await h.flag_change()
    assert "p2" not in h.mgr._state_bucket(REAL)[flag_key]

    game.players["p2"].alive = True  # revived
    await h.flag_change()
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 1.5}
    assert len(h.recorded(REAL, SPEC_BY_KEY[flag_key].type_id)) == 1


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_low_battery_player_gets_no_new_level(flag_key):
    """#798 battery guard on levels: a new level for a controller below
    policy.battery_threshold is neither dispatched nor applied; the others
    still get theirs."""
    battery = {"p1": 5.0, "p2": 90.0}
    h = LevelHarness(battery=battery, battery_threshold=20)
    game = h.start_game(REAL, real=True)

    h.set_level(flag_key, "p1", 1.5)
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 1.0}
    assert h.recorded(REAL, SPEC_BY_KEY[flag_key].type_id) == []

    h.set_level(flag_key, "p2", 0.75)
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 0.75}
