"""Tests for paired Common-Random-Numbers (CRN) RNG seeding (#1003).

These cover the load-bearing per-instance RNG isolation that lets the experiment
registry spawn (experimental, control) shadow-game pairs sharing a seed so the
two arms face an identical bracket order / role assignment / team split — the
only difference being the flag under test.

Invariants exercised:
  * Two equally-seeded game instances produce IDENTICAL RNG-driven assignments.
  * Differently-seeded instances DIVERGE.
  * seed == 0 (real games, unbound shadow games, pre-#1003 callers) -> entropy,
    i.e. random.Random(None) (today's behavior).
  * CONCURRENCY: two same-seed instances whose draws are interleaved do NOT
    perturb each other's sequences (proves the per-instance fix vs. the old
    module-global random bug that would have corrupted a concurrent game).
  * REAL-PROTECTION: the game factory threads rng_seed through; a real session
    is zeroed upstream (servicer) so it gets entropy.
"""

import random

import pytest

from services.game_coordinator.games.random_teams import RandomTeamsGame
from services.game_coordinator.games.tournament import TournamentGame, TournamentPlayer
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.games.zombie import ZombieGame


async def _async_noop(*_args, **_kwargs):
    """Async no-op event publisher for game-init paths that publish events."""


def _make_random_teams(rng_seed: int, num_teams: int = 3) -> RandomTeamsGame:
    return RandomTeamsGame(
        controller_manager_client=None,
        event_publisher=_async_noop,
        audio_client=None,
        game_id=f"rt_{rng_seed}",
        num_teams=num_teams,
        rng_seed=rng_seed,
    )


def _make_tournament(rng_seed: int) -> TournamentGame:
    return TournamentGame(
        controller_manager_client=None,
        event_publisher=None,
        audio_client=None,
        game_id=f"trn_{rng_seed}",
        rng_seed=rng_seed,
    )


# A deliberately UNSORTED serial list: the production code sorts before any RNG
# consumes it, so a paired arm that received its players in a different order
# still aligns. Using a non-sorted list here guards that sort-before-RNG step.
SERIALS = ["mock_07", "mock_01", "mock_11", "mock_03", "mock_09", "mock_05"]


def test_random_teams_same_seed_identical_assignment():
    """Equal seed -> identical team assignment for the same (sorted) serials.

    _assign_random_teams seeds only the team-POOL shuffle; the caller
    (_initialize_players_impl) is responsible for sorting serials before this
    point (covered by test_random_teams_caller_sorts_before_rng). Given the same
    pre-sort order, two equally-seeded games must agree exactly.
    """
    a = _make_random_teams(424242)
    b = _make_random_teams(424242)

    serials = sorted(SERIALS)
    assert a._assign_random_teams(list(serials)) == b._assign_random_teams(list(serials))


def test_random_teams_different_seed_diverges():
    """Different seeds should (very likely) produce a different split."""
    a = _make_random_teams(1)
    b = _make_random_teams(2)

    # Run several player counts so the probability of an accidental full match
    # is negligible; at least one assignment must differ.
    diverged = False
    for n in (4, 6, 8):
        serials = [f"mock_{i:02d}" for i in range(n)]
        if a._assign_random_teams(list(serials)) != b._assign_random_teams(list(serials)):
            diverged = True
            break
    assert diverged, "two distinct seeds produced identical assignments for every size"


def test_random_teams_seed_zero_is_entropy():
    """seed == 0 -> random.Random(None): two such games are independent draws.

    We assert the RNG is NOT pinned to a fixed sequence (i.e. it is seeded from
    entropy). Equality across two seed-0 games is astronomically unlikely over
    several sizes; require at least one divergence.
    """
    a = _make_random_teams(0)
    b = _make_random_teams(0)
    diverged = False
    for n in (6, 8, 10):
        serials = [f"mock_{i:02d}" for i in range(n)]
        if a._assign_random_teams(list(serials)) != b._assign_random_teams(list(serials)):
            diverged = True
            break
    assert diverged, "two seed-0 games matched on every size — RNG appears pinned, not entropy"


@pytest.mark.asyncio
async def test_random_teams_caller_sorts_before_rng():
    """The full init path sorts serials before the RNG, so a seed-matched pair
    aligns even when the controllers arrive in a DIFFERENT order.

    This is the player-alignment gotcha: a shared seed only makes the team-pool
    shuffle the same PERMUTATION; without the pre-sort it would map to different
    players. Two same-seed games fed the same controllers in opposite orders
    must end with the same serial -> team mapping.
    """

    class _Ctrl:
        def __init__(self, serial):
            self.serial = serial

    controllers = [_Ctrl(s) for s in SERIALS]

    a = _make_random_teams(2024, num_teams=3)
    b = _make_random_teams(2024, num_teams=3)

    await a._initialize_players_impl(list(controllers))
    await b._initialize_players_impl(list(reversed(controllers)))

    teams_a = {s: a.players[s].team for s in SERIALS}
    teams_b = {s: b.players[s].team for s in SERIALS}
    assert teams_a == teams_b


def _bracket_positions(game: TournamentGame, serials: list[str]) -> dict[str, int]:
    """Populate players and run the seeded bracket shuffle; return serial->slot."""
    game.players = {s: TournamentPlayer(serial=s) for s in serials}
    game._generate_bracket(len(serials))
    return {s: game.players[s].bracket_position for s in serials}


def test_tournament_same_seed_identical_bracket():
    """Equal seed -> identical bracket seeding, robust to input ordering."""
    a = _make_tournament(99)
    b = _make_tournament(99)
    pos_a = _bracket_positions(a, list(SERIALS))
    pos_b = _bracket_positions(b, list(reversed(SERIALS)))
    assert pos_a == pos_b


def test_tournament_different_seed_diverges():
    a = _make_tournament(100)
    b = _make_tournament(101)
    pos_a = _bracket_positions(a, list(SERIALS))
    pos_b = _bracket_positions(b, list(SERIALS))
    assert pos_a != pos_b


def test_werewolf_zombie_same_seed_identical_roles():
    """Role-assignment modes: same seed -> same chosen players."""

    class _Ctrl:
        def __init__(self, serial):
            self.serial = serial

    controllers = [_Ctrl(s) for s in SERIALS]

    def werewolves(seed):
        g = WerewolfGame(
            controller_manager_client=None,
            event_publisher=None,
            audio_client=None,
            game_id=f"ww_{seed}",
            rng_seed=seed,
        )
        # Reproduce the seeded selection without the async player init/IO.
        serials = sorted(c.serial for c in controllers)
        n = max(1, int(len(serials) * g.werewolf_fraction))
        return set(g._rng.sample(serials, n))

    def zombies(seed):
        g = ZombieGame(
            controller_manager_client=None,
            event_publisher=None,
            audio_client=None,
            game_id=f"zb_{seed}",
            rng_seed=seed,
        )
        serials = sorted(c.serial for c in controllers)
        n = min(g.initial_zombies, len(serials) - 1)
        return set(g._rng.sample(serials, n))

    assert werewolves(7) == werewolves(7)
    assert zombies(7) == zombies(7)


def test_concurrency_interleaved_draws_do_not_perturb():
    """Two same-seed instances drawing INTERLEAVED keep independent sequences.

    This is the regression guard for the original module-global random bug: with
    a shared global RNG, interleaving game A's and game B's draws would consume
    from the same stream, so neither would match a same-seed game run alone.
    With per-instance RNG, an interleaved run is byte-identical to a solo run.
    """
    a = _make_random_teams(31337)
    b = _make_random_teams(31337)

    # Reference: a full solo sequence from a third, identically-seeded instance.
    solo = _make_random_teams(31337)
    sizes = [6, 4, 8, 6]
    expected = [solo._assign_random_teams([f"m_{i:02d}" for i in range(n)]) for n in sizes]

    # Now interleave a's and b's draws; each must still match the solo sequence.
    got_a, got_b = [], []
    for n in sizes:
        serials = [f"m_{i:02d}" for i in range(n)]
        got_a.append(a._assign_random_teams(list(serials)))
        got_b.append(b._assign_random_teams(list(serials)))

    assert got_a == expected
    assert got_b == expected


def test_factory_threads_rng_seed_and_zero_is_entropy():
    """GameFactory.create_game threads rng_seed into the per-instance RNG.

    A non-zero seed yields a deterministic _rng; seed 0 yields an entropy RNG
    (random.Random with no fixed state). This is the real-protection contract:
    the servicer passes 0 for a real session, so a real game gets entropy.
    """
    from services.game_coordinator.game_factory import GameFactory

    seeded = GameFactory.create_game(
        game_name="JoustFFA",
        controller_manager_client=None,
        event_publisher=None,
        audio_client=None,
        game_id="ffa_seeded",
        initial_players=[],
        rng_seed=555,
    )
    seeded_twin = GameFactory.create_game(
        game_name="JoustFFA",
        controller_manager_client=None,
        event_publisher=None,
        audio_client=None,
        game_id="ffa_seeded_twin",
        initial_players=[],
        rng_seed=555,
    )
    # Same seed -> identical raw RNG stream.
    assert [seeded._rng.random() for _ in range(8)] == [seeded_twin._rng.random() for _ in range(8)]

    # seed 0 (real games) -> a Random instance not pinned to the seeded stream.
    real = GameFactory.create_game(
        game_name="JoustFFA",
        controller_manager_client=None,
        event_publisher=None,
        audio_client=None,
        game_id="ffa_real",
        initial_players=[],
        rng_seed=0,
    )
    assert isinstance(real._rng, random.Random)
    pinned = random.Random(555)
    assert [real._rng.random() for _ in range(8)] != [pinned.random() for _ in range(8)]
