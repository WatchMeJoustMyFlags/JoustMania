"""
Per-player flag lifecycle on the coordinator (#1317, #1323, #1269).

UC-040 A24 / BR-017: when every player's ``player_sensitivity_factor`` or
``player_handicap_factor`` resolves to neutral again, the coordinator resets the
factors in the game. Before #1317 the all-neutral transition dispatched nothing,
so the last factor stayed for the rest of the game. A level present when a game
starts belongs to an earlier game and is never applied in the new one.

UC-040 A25 / BR-016: the agent prunes a retired game's players from the
per-player flags. A pruned player resolves to the neutral variant, which never
fires a grant.

UC-040 A19 / BR-012: once a game's grant baseline is primed, a player with no
baseline entry adopts its grant without firing (#1323).

Reuses the session harness of ``test_intervention_scoping``.
"""

import sys
from pathlib import Path

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))

from services.game_coordinator.difficulty_handlers import register_difficulty_handlers
from services.game_coordinator.interventions import SessionView
from services.game_coordinator.tests.test_intervention_scoping import (
    GRANT_FLAGS,
    REAL,
    SHADOW,
    SPEC_BY_KEY,
    FakeGame,
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

    def start_game(self, game_id, *, real, serials=("p1", "p2")) -> FakeGame:
        game = super().start_game(game_id, real=real, serials=serials)
        for player in game.players.values():
            player.sensitivity_factor = 1.0
            player.handicap_factor = 1.0
        return game

    def set_level(self, flag_key, serial, value):
        self.flags.per_serial[(flag_key, serial)] = (f"agent_{serial}", value)

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
    h.flags.values["audio_cue"] = "2-b:agent_cue"  # unrelated write
    await h.flag_change()
    assert game.players["p1"].sensitivity_factor == 1.3


# --------------------------------------------------------------------------- #
# BR-017: no carry-over into the next game (#1269 level part)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_next_game_does_not_inherit_levels(flag_key):
    """p1's level from game N is still in the flag when N+1 starts (the agent's
    prune has not landed yet). N+1 starts neutral, keeps p1 neutral when p2 gets
    a level, and the late prune changes nothing in N+1."""
    h = LevelHarness()
    game_n = h.start_game("game_n", real=True)
    h.set_level(flag_key, "p1", 1.5)
    await h.flag_change()
    assert factors(game_n, flag_key)["p1"] == 1.5
    h.end("game_n")

    game_n1 = h.start_game("game_n1", real=True)
    h.flags.values["audio_cue"] = "2-b:agent_cue"  # unrelated write
    await h.flag_change()
    assert factors(game_n1, flag_key) == {"p1": 1.0, "p2": 1.0}

    h.set_level(flag_key, "p2", 1.25)  # a decision in N+1 for another player
    await h.flag_change()
    assert factors(game_n1, flag_key) == {"p1": 1.0, "p2": 1.25}

    h.prune("p1")  # the agent's game-end prune for N lands late
    await h.flag_change()
    assert factors(game_n1, flag_key) == {"p1": 1.0, "p2": 1.25}


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_new_decision_for_a_leftover_player_applies():
    """A level decided in N+1 for p1 (whose N entry was left over) applies."""
    h = LevelHarness()
    h.start_game("game_n", real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5)
    await h.flag_change()
    h.end("game_n")

    game = h.start_game("game_n1", real=True)
    h.set_level("player_sensitivity_factor", "p1", 0.7)
    await h.flag_change()
    assert game.players["p1"].sensitivity_factor == 0.7


@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.asyncio
async def test_concurrent_shadow_does_not_inherit_the_real_games_level():
    h = LevelHarness()
    real = h.start_game(REAL, real=True)
    h.set_level("player_sensitivity_factor", "p1", 1.5)
    await h.flag_change()

    shadow = h.start_game(SHADOW, real=False)  # shares serial p1 in this fake
    h.flags.values["audio_cue"] = "2-b:agent_cue"
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
    _grant(h, flag_key, "p1", "n1")
    await h.flag_change()

    h.prune("p1")
    await h.flag_change()
    await h.flag_change()
    _grant(h, flag_key, "p2", "n2")
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
    _grant(h, "shield_seconds", "MOCK0002", "n1")
    await h.flag_change()
    events_before = list(h.events.get(REAL, []))

    h.end(SHADOW)
    h.prune("MOCK0001", "MOCK0002")
    await h.flag_change()

    assert h.events.get(REAL, []) == events_before
    assert real.players["r1"].sensitivity_factor == 1.5
    assert real.shields == []


# --------------------------------------------------------------------------- #
# A19 / BR-012: a missing serial in a primed baseline is adopted (#1323)
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.parametrize("flag_key", GRANT_FLAGS)
@pytest.mark.asyncio
async def test_revived_player_missing_from_the_fallback_prime_is_not_granted(flag_key):
    """The fallback prime records alive players only. p2 was dead then; its
    leftover grant must not fire when it is revived. A fresh grant does."""
    h = LevelHarness()
    game = FakeGame(("p1", "p2"))
    game.players["p2"].alive = False
    _grant(h, flag_key, "p2", "old")  # left over from an earlier game
    h.views.append(SessionView(game_id=REAL, game=game, publish=h._publisher(REAL), is_real=True))
    await h.flag_change()  # never primed: fallback prime (alive players only)

    game.players["p2"].alive = True  # revived
    h.flags.values["audio_cue"] = "2-b:agent_cue"  # unrelated write
    await h.flag_change()
    assert game.grants(flag_key) == []

    _grant(h, flag_key, "p2", "new")
    await h.flag_change()
    assert game.grants(flag_key) == ["p2"]


# --------------------------------------------------------------------------- #
# BR-012: two grants within one flagd poll collapse to the later one
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A19", br=["BR-012"])
@pytest.mark.asyncio
async def test_two_grants_within_one_poll_apply_the_later_once():
    h = LevelHarness()
    game = h.start_game(REAL, real=True)
    _grant(h, "shield_seconds", "p1", "n1", 10.0)
    _grant(h, "shield_seconds", "p1", "n2", 3.0)  # same poll: flagd serves only this
    await h.flag_change()
    await h.flag_change()
    assert game.shields == [("p1", 3.0)]


# --------------------------------------------------------------------------- #
# BR-017: the level baseline fails closed on flagd errors
# --------------------------------------------------------------------------- #
@pytest.mark.uc("UC-040", flow="A24", br=["BR-017"])
@pytest.mark.parametrize("flag_key", LEVEL_FLAGS)
@pytest.mark.asyncio
async def test_level_read_error_at_game_start_never_applies_the_leftover(flag_key):
    """flagd errors while the game is primed: the getter would return the
    neutral default and make p1's leftover level look new on recovery. The
    error is recorded as unknown, so the leftover is adopted, not applied."""
    h = LevelHarness()
    h.set_level(flag_key, "p1", 1.5)  # left over from an earlier game
    h.flags.failing = {flag_key}
    game = h.start_game(REAL, real=True)
    await h.flag_change()

    h.flags.failing = set()
    h.flags.values["audio_cue"] = "2-b:agent_cue"  # unrelated write
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 1.0}

    h.set_level(flag_key, "p1", 0.75)  # a decision in this game applies
    await h.flag_change()
    assert factors(game, flag_key)["p1"] == 0.75


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
async def test_revived_player_missing_from_the_level_baseline_adopts_without_applying(flag_key):
    """The fallback prime records alive players only, so the primed level
    bucket has no entry for the dead p2. When p2 is revived its leftover level
    is adopted as the baseline, not applied; a later level for p2 applies."""
    h = LevelHarness()
    game = FakeGame(("p1", "p2"))
    for player in game.players.values():
        player.sensitivity_factor = 1.0
        player.handicap_factor = 1.0
    game.players["p2"].alive = False
    h.set_level(flag_key, "p2", 1.5)  # left over from an earlier game
    h.views.append(SessionView(game_id=REAL, game=game, publish=h._publisher(REAL), is_real=True))
    await h.flag_change()  # never primed: fallback prime (alive players only)
    assert "p2" not in h.mgr._state_bucket(REAL)[flag_key]

    game.players["p2"].alive = True  # revived
    h.flags.values["audio_cue"] = "2-b:agent_cue"  # unrelated write
    await h.flag_change()
    assert factors(game, flag_key) == {"p1": 1.0, "p2": 1.0}
    assert h.recorded(REAL, SPEC_BY_KEY[flag_key].type_id) == []

    h.set_level(flag_key, "p2", 0.75)
    await h.flag_change()
    assert factors(game, flag_key)["p2"] == 0.75


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
