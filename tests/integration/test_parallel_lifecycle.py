"""
Parallel per-mode lifecycle coverage via headless shadow games (#826).

Follow-up to #779 / part of #774 (M6). The 14 per-mode lifecycle tests used to
run strictly sequentially through the menu (``test_full_game_lifecycle``). Now
that the coordinator supports concurrent games (#775/#776) and reserved mock
controllers are invisible to the menu (#777), the per-mode *game logic* coverage
moves here: each mode runs as a headless shadow game, and modes run in batches
of up to 4 concurrently via ``asyncio.gather`` on the one shared compose stack.

What this file covers (per mode, headless):
- Controllers receive game colors (non-zero LED via GetColor per serial).
- The mode-specific end strategy drives the win condition (reusing the
  game_id-aware ``end_*`` helpers from helpers.py).
- The mode's terminal event (``game_ended``/``game_force_ended``) arrives on the
  collector bound to THAT game_id.

What this file deliberately does NOT cover (still in test_full_game_lifecycle):
- Menu ready-up, mode selection, and lobby-color *restore* — those are menu
  behavior on the single shared lobby and cannot parallelize. A thin sequential
  menu-flow set (JoustFFA + JoustTeams) keeps verifying them.

Why ``asyncio.gather`` and NOT pytest-xdist: the ``docker_compose`` fixture is
session-scoped, but each xdist worker gets its own session -> one compose stack
per worker. In-process async batches share the single stack and multiplex over
the already-concurrent data plane (§2.3 of the shadow-games research).

Why reserved controllers are mandatory: the menu service writes lobby LEDs to
every controller it can see; unreserved parallel games would race the LED
assertions. Each mode gets its own reserved/tagged controller set.
"""

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import (
    build_start_config,
    describe_game_state,
    end_fight_club_game,
    end_swapper_game,
    end_tournament_game,
    end_werewolf_game,
    end_zombies_game,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    kill_players_for_team_win,
    kill_players_until_one_remains,
    setup_mock_controllers,
    start_game_headless,
    verify_controllers_have_color,
)

# =============================================================================
# End strategies (game_id-aware)
# =============================================================================
# Each strategy ends a single headless game identified by game_id, using only
# that game's reserved controller serials. Signature:
#   async def(mock_client, serials, game_client, game_id) -> None

EndStrategy = Callable[[Any, list[str], Any, str], Any]


async def end_ffa(mock_client, serials, game_client, game_id) -> None:
    """FFA / Random-teams via per-elimination: kill until one remains."""
    await kill_players_until_one_remains(
        mock_client, serials, delay=0.1, game_client=game_client, game_id=game_id
    )


async def end_team(mock_client, serials, game_client, game_id) -> None:
    """Team game: eliminate one whole team."""
    await kill_players_for_team_win(
        mock_client, serials, delay=0.1, game_client=game_client, game_id=game_id
    )


async def end_swapper(mock_client, serials, game_client, game_id) -> None:
    """Swapper: converge all players onto one team."""
    await end_swapper_game(
        mock_client, serials, game_client, delay=0.3, game_id=game_id
    )


async def end_zombies(mock_client, serials, game_client, game_id) -> None:
    """Zombies: convert all humans."""
    await end_zombies_game(
        mock_client, serials, delay=0.3, game_client=game_client, game_id=game_id
    )


async def end_werewolf(mock_client, serials, game_client, game_id) -> None:
    """Werewolf: kill all but one (roles random; guarantees a team wipe)."""
    await end_werewolf_game(
        mock_client, serials, delay=0.3, game_client=game_client, game_id=game_id
    )


async def end_tournament(mock_client, serials, game_client, game_id) -> None:
    """Tournament: run the bracket. CI invincibility=2.0s (set in start config)."""
    # Pure SimulateDeath-driven (no per-game state query needed): game_id-agnostic.
    await end_tournament_game(
        mock_client, serials, delay=0.2, invincibility_wait=2.5
    )


async def end_fight_club(mock_client, serials, game_client, game_id) -> None:
    """FightClub: run minimum rounds to a winner. CI: invincibility=2.0s, min_rounds=5."""
    # Pure SimulateDeath-driven (queued players ignored): game_id-agnostic.
    await end_fight_club_game(
        mock_client,
        serials,
        game_client,
        delay=0.2,
        round_wait=3.5,  # 1.0s inter-round pause + 2.0s invincibility + 0.5 buffer
        rounds=6,  # CI min_rounds=5 + 1 to ensure a clear winner
    )


async def end_force(mock_client, serials, game_client, game_id) -> None:
    """Modes with no natural end in tests (NonStop, Traitor): force-end by id."""
    await asyncio.sleep(2.0)  # let the game run briefly
    await force_end_game_by_id(game_client, game_id, reason="parallel_lifecycle_end")


# =============================================================================
# Per-mode spec + duration-grouped batches
# =============================================================================


@dataclass(frozen=True)
class ModeSpec:
    """One game mode's headless lifecycle parameters."""

    mode: str
    players: int
    end: EndStrategy
    timeout: int  # seconds to await the terminal event after the end strategy
    pre_end_delay: float = 0.0  # wait before the end strategy (e.g. formation phase)


# Per-mode specs. JoustFFA + JoustTeams stay in the sequential menu-flow set
# (test_full_game_lifecycle) to keep menu ready-up / lobby-restore coverage, so
# they are intentionally absent here.
_SPECS = {
    # RandomTeams runs a 5s team-formation phase (TEAM_FORMATION_DURATION in
    # random_teams.py) before the game proper — kills during it are swallowed,
    # so wait it out before driving the win condition (caught in CI).
    "JoustRandomTeams": ModeSpec("JoustRandomTeams", 4, end_team, 20, pre_end_delay=6.5),
    # Swapper's death-swap end sequence (delay=0.3/swap) can exceed 15s under
    # 4-way parallel load on a loaded CI runner; 25s also timed out on CI
    # (#897), so give the post-convergence end path generous headroom.
    "Swapper": ModeSpec("Swapper", 4, end_swapper, 40),
    "Zombies": ModeSpec("Zombies", 4, end_zombies, 15),
    "NonStop": ModeSpec("NonStop", 2, end_force, 15),
    "Traitor": ModeSpec("Traitor", 4, end_force, 15),
    "Werewolf": ModeSpec("Werewolf", 4, end_werewolf, 20),
    "Tournament": ModeSpec("Tournament", 4, end_tournament, 30),
    "FightClub": ModeSpec("FightClub", 4, end_fight_club, 30),
}

# Duration-grouped batches (size <= GAME_MAX_CONCURRENT_GAMES=4). Wall time of a
# batch == the slowest mode in it, so the slow modes (Tournament, FightClub,
# Werewolf) are pooled into ONE batch rather than each anchoring a separate one.
# A fast mode (Zombies) fills the 4th slot of the slow batch for free.
_BATCHES = [
    pytest.param(
        ["Tournament", "FightClub", "Werewolf", "Zombies"],
        id="slow-batch",
    ),
    pytest.param(
        ["JoustRandomTeams", "Swapper", "NonStop", "Traitor"],
        id="fast-batch",
    ),
]

_TERMINAL_EVENTS = ["game_ended", "game_force_ended", "game_error"]


async def _start_headless_with_retry(
    game_client, start_config, attempts: int = 4, backoff: float = 2.0
):
    """Start headless, retrying brief "Game already in progress" windows.

    Within a batch, a sibling's force-ended game may still be in its ENDING
    window when this start hits the cap check; the lazy sweep frees the slot
    moments later, so a short retry is correct (and a persistent rejection
    still fails after ``attempts``).
    """
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return await start_game_headless(game_client, start_config, timeout=25.0)
        except Exception as exc:
            if "Game already in progress" not in str(exc):
                raise
            last_exc = exc
            await asyncio.sleep(backoff)
    raise last_exc


async def _wait_for_game_colors(mock_client, serials: list[str], timeout: float = 10.0) -> None:
    """Poll until every controller shows a non-zero LED.

    Under 4-way parallel load the feedback stream can lag (and modes with a
    team-formation phase set colors late); the previous one-shot check after a
    fixed 0.5s sleep was flaky in CI.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await verify_controllers_have_color(mock_client, serials)
            return
        except AssertionError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.5)


async def _run_mode_headless(docker_compose, spec: ModeSpec, tag: str) -> None:
    """Run one mode end-to-end as a headless shadow game.

    Reserves its own controller set (distinct tag), starts headless, verifies
    game colors, runs the mode's end strategy, and awaits the terminal event on
    the collector bound to this game's game_id. Cleans up its game + reserved
    controllers even on failure. Raises with the mode name on any failure so a
    gathered batch surfaces exactly which mode broke.
    """
    from tests.integration.helpers import remove_reserved_controllers

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=spec.players, reserved=True, tag=tag
        )

        # Start headless: bypasses the lobby; returns the coordinator-assigned
        # game_id plus a collector already filtered to that session. Retries
        # transient cap rejections (sibling games in their ENDING window).
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config(spec.mode, serials)
        )

        # Controllers must get game colors (non-zero LED, per serial via
        # GetColor); polled, since parallel load can delay the feedback stream.
        await _wait_for_game_colors(mock_client, serials)

        # Drive the mode's win condition on THIS game only (after any
        # mode-specific warm-up phase, e.g. RandomTeams team formation).
        if spec.pre_end_delay:
            await asyncio.sleep(spec.pre_end_delay)
        await spec.end(mock_client, serials, game_client, game_id)

        # The mode's terminal event must arrive for this game_id. (Force-end
        # strategies already triggered it; awaiting it is still correct.)
        # On timeout, attach the session's live state so the failure says
        # WHICH way it broke: still RUNNING = the game never reached its win
        # condition (frame starvation / end strategy bug); ENDED/ENDING = the
        # game finished but the event never reached the collector; absent =
        # session retired without the collector seeing the event (#897).
        try:
            await collector.wait_for_any_event(_TERMINAL_EVENTS, timeout=spec.timeout)
        except TimeoutError as exc:
            state = await describe_game_state(game_client, game_id)
            raise TimeoutError(f"{exc} (session state: {state})") from exc
    except Exception as exc:  # noqa: BLE001 - re-raise tagged with the mode name
        raise AssertionError(f"[{spec.mode}] headless lifecycle failed: {exc}") from exc
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_cleanup")
        await remove_reserved_controllers(docker_compose, tag)
        await mock_channel.close()
        await game_channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", _BATCHES)
async def test_parallel_mode_lifecycle(docker_compose, headless_cleanup, modes):
    """Run a duration-grouped batch of game modes concurrently, headless.

    Each mode in the batch:
    1. reserves its own tagged controller set (distinct serials per mode),
    2. starts a headless game (its own game_id),
    3. verifies controllers got game colors,
    4. runs the mode-specific end strategy (game_id-aware),
    5. awaits its terminal event on the collector bound to that game_id.

    The batch runs all modes via ``asyncio.gather(..., return_exceptions=True)``
    so a single mode's failure doesn't mask the others; failures are re-raised
    together, each tagged with its mode name.
    """
    specs = [_SPECS[m] for m in modes]

    # Coordinator idleness (no leftover live sessions eating cap slots) is
    # enforced by the autouse ``coordinator_idle`` fixture in conftest.

    # Register tags up front so the fixture sweeps reserved controllers even if a
    # coroutine dies before its own finally-block cleanup runs.
    tags = []
    for spec in specs:
        tag = f"parallel-{spec.mode.lower()}"
        headless_cleanup.add_tag(tag)
        tags.append(tag)

    results = await asyncio.gather(
        *(
            _run_mode_headless(docker_compose, spec, tag)
            for spec, tag in zip(specs, tags)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        messages = "; ".join(str(f) for f in failures)
        raise AssertionError(f"{len(failures)}/{len(specs)} modes failed: {messages}")
