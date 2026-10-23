"""TC-017 on the service tier (T2): parallel per-mode lifecycle via headless shadow games.

Moved from tests/integration/test_parallel_lifecycle.py (#1563). The batches
run on one stack of real services from source (GAME composition, audio faked).

Parallel per-mode lifecycle coverage via headless shadow games (#826).

Follow-up to #779 / part of #774 (M6). The 14 per-mode lifecycle tests used to
run strictly sequentially through the menu. Now
that the coordinator supports concurrent games (#775/#776) and reserved mock
controllers are invisible to the menu (#777), the per-mode *game logic* coverage
moves here: each mode runs as a headless shadow game, and modes run in batches
of up to 4 concurrently via ``asyncio.gather`` on the one shared stack.

What this file covers (per mode, headless):
- Controllers receive game colors (non-zero LED via GetColor per serial).
- The mode-specific end strategy drives the win condition (reusing the
  game_id-aware ``end_*`` helpers from helpers.py).
- The mode's terminal event (``game_ended``/``game_force_ended``) arrives on the
  collector bound to THAT game_id.
- A natural end publishes exactly one winner event for the mode and a
  ``game_ended`` whose winner field matches it (#1361); a force-end (NonStop)
  publishes ``game_force_ended``, no winner event and ``game_ended`` with no
  winner.

What this file deliberately does NOT cover:
- Menu ready-up, mode selection, and lobby-color *restore* — those are menu
  behavior on the single shared lobby and cannot parallelize. The TC-002
  journey (test_tc002_ffa_game.py) verifies them for JoustFFA; selecting
  JoustTeams through the menu is covered by test_tc005_admin_journey.py.

Why ``asyncio.gather`` and NOT pytest-xdist: the compose fixture was
session-scoped, but each xdist worker gets its own session -> one compose stack
per worker. In-process async batches share the single stack and multiplex over
the already-concurrent data plane (§2.3 of the shadow-games research).

Why reserved controllers are mandatory: the menu service writes lobby LEDs to
every controller it can see; unreserved parallel games would race the LED
assertions. Each mode gets its own reserved/tagged controller set.
"""

import ast
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
    end_traitor_game,
    end_werewolf_game,
    end_zombies_game,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    kill_players_for_team_win,
    kill_players_until_one_remains,
    quiesce_coordinator,
    setup_mock_controllers,
    start_game_headless,
    verify_controllers_have_color,
    wait_until_running,
)
from tests.service.harness import GAME

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = GAME

# =============================================================================
# End strategies (game_id-aware)
# =============================================================================
# Each strategy ends a single headless game identified by game_id, using only
# that game's reserved controller serials. Signature:
#   async def(mock_client, serials, game_client, game_id) -> None

EndStrategy = Callable[[Any, list[str], Any, str], Any]

FORCE_END_REASON = "parallel_lifecycle_end"


async def end_ffa(mock_client, serials, game_client, game_id) -> None:
    """FFA / Random-teams via per-elimination: kill until one remains."""
    await kill_players_until_one_remains(mock_client, serials, delay=0.1, game_client=game_client, game_id=game_id)


async def end_team(mock_client, serials, game_client, game_id) -> None:
    """Team game: eliminate one whole team."""
    await kill_players_for_team_win(mock_client, serials, delay=0.1, game_client=game_client, game_id=game_id)


async def end_swapper(mock_client, serials, game_client, game_id) -> None:
    """Swapper: converge all players onto one team."""
    await end_swapper_game(mock_client, serials, game_client, delay=0.3, game_id=game_id)


async def end_zombies(mock_client, serials, game_client, game_id) -> None:
    """Zombies: convert all humans."""
    await end_zombies_game(mock_client, serials, delay=0.3, game_client=game_client, game_id=game_id)


async def end_werewolf(mock_client, serials, game_client, game_id) -> None:
    """Werewolf: kill all but one (roles random; guarantees a team wipe)."""
    await end_werewolf_game(mock_client, serials, delay=0.3, game_client=game_client, game_id=game_id)


async def end_tournament(mock_client, serials, game_client, game_id) -> None:
    """Tournament: drive the single-elimination bracket to a champion.

    State-aware (game_id-scoped): kills one FIGHTING player per match, verified
    against GetGameState, until the terminal event. CI uses 6s match / 1s pause
    flags (ci/game.json) and invincibility=2.0s (start config).
    """
    await end_tournament_game(mock_client, game_client, serials, game_id=game_id, timeout=45.0)


async def end_fight_club(mock_client, serials, game_client, game_id) -> None:
    """FightClub: run rounds until the game ends. CI: invincibility=2.0s, min_rounds=5.

    Follows GetGameState (game_id-scoped) instead of a fixed round count: a tie
    after min_rounds adds a face-off round (UC-025 A2 step 3).
    """
    await end_fight_club_game(
        mock_client,
        serials,
        game_client,
        delay=0.2,
        round_wait=3.5,  # 1.0s inter-round pause + 2.0s invincibility + 0.5 buffer
        game_id=game_id,
        timeout=60.0,
    )


async def end_traitor(mock_client, serials, game_client, game_id) -> None:
    """Traitor: kill all but one (secret teams; the survivor's secret team wins)."""
    await end_traitor_game(mock_client, serials, delay=0.3, game_client=game_client, game_id=game_id)


async def end_force(mock_client, serials, game_client, game_id) -> None:  # noqa: ARG001
    """Modes with no natural end in tests (NonStop, unlimited time): force-end by id."""
    await asyncio.sleep(2.0)  # let the game run briefly
    await force_end_game_by_id(game_client, game_id, reason=FORCE_END_REASON)


# =============================================================================
# Outcome checks (winner event + game_ended winner field, #1361)
# =============================================================================
# Each check gets the game's events (collector bound to its game_id), the
# game's serials and the ``game_ended`` data. Every end strategy leaves the
# LAST serial alive (it is never killed), so that player is always on the
# winning side of a kill-decided mode. GameEvent.data is map<string, string>:
# numbers arrive as their str(), lists and dicts as their Python repr.

# Every event a mode publishes to declare a result; a force-end publishes none.
WINNER_EVENTS = (
    "team_winner",
    "traitor_winner",
    "werewolf_winner",
    "zombie_winner",
    "swapper_winner",
    "tournament_champion",
    "tournament_no_winner",
    "game_winner",
    "game_tie",
)

OutcomeCheck = Callable[[list, list[str], dict[str, str]], None]


def _winner_events(events: list) -> list:
    return [e for e in events if e.event_type in WINNER_EVENTS]


def _single_winner_event(events: list) -> tuple[str, dict[str, str]]:
    """The one result event of the game (the result is latched once)."""
    found = _winner_events(events)
    assert len(found) == 1, f"expected exactly one winner event, got {[(e.event_type, dict(e.data)) for e in found]}"
    return found[0].event_type, dict(found[0].data)


def _check_team_win(events: list, serials: list[str], ended: dict[str, str]) -> None:
    """Teams / Random Teams (UC-021 steps 4, 5): team_winner (or game_tie) matches game_ended.winning_team."""
    kind, data = _single_winner_event(events)
    if kind == "game_tie":
        assert ended["winning_team"] == "none", f"game_tie but game_ended {ended}"
        return
    assert kind == "team_winner", f"unexpected winner event {kind}: {data}"
    assert ended["winning_team"] == data["team"], f"team_winner {data} vs game_ended {ended}"
    assert serials[-1] in ast.literal_eval(data["winning_players"]), f"survivor {serials[-1]} not in {data}"


def _check_traitor_win(events: list, serials: list[str], ended: dict[str, str]) -> None:
    """Traitor (UC-022 step 4): traitor_winner for the survivor's secret team, game_ended.winning_team matches."""
    kind, data = _single_winner_event(events)
    assert kind == "traitor_winner", f"expected traitor_winner, got {kind}: {data}"
    assert ended["winning_team"] == data["team"], f"traitor_winner {data} vs game_ended {ended}"
    assert serials[-1] in ast.literal_eval(data["winners"]), f"survivor {serials[-1]} not in {data}"


def _check_werewolf_win(events: list, serials: list[str], ended: dict[str, str]) -> None:
    """Werewolf (UC-022 A1 step 4, A2): werewolf_winner (or game_tie) matches game_ended.winner."""
    kind, data = _single_winner_event(events)
    if kind == "game_tie":
        assert ended["winner"] == "none", f"game_tie but game_ended {ended}"
        return
    assert kind == "werewolf_winner", f"unexpected winner event {kind}: {data}"
    assert data["winner"] in ("humans", "werewolves"), data
    assert ended["winner"] == data["winner"], f"werewolf_winner {data} vs game_ended {ended}"
    assert serials[-1] in ast.literal_eval(data["winners"]), f"survivor {serials[-1]} not in {data}"


def _check_zombie_win(events: list, serials: list[str], ended: dict[str, str]) -> None:  # noqa: ARG001
    """Zombies (UC-023 A1 step 1): every human converted, zombie_winner(zombies) and game_ended.winner zombies."""
    kind, data = _single_winner_event(events)
    assert kind == "zombie_winner", f"expected zombie_winner, got {kind}: {data}"
    assert data["winner"] == "zombies", data
    assert ended["winner"] == "zombies", f"game_ended {ended}"


def _check_swapper_win(events: list, serials: list[str], ended: dict[str, str]) -> None:
    """Swapper (UC-024 steps 3, 4): swapper_winner, game_ended.winning_team is its team."""
    kind, data = _single_winner_event(events)
    assert kind == "swapper_winner", f"expected swapper_winner, got {kind}: {data}"
    assert ended["winning_team"] == data["team"], f"swapper_winner {data} vs game_ended {ended}"
    assert set(ast.literal_eval(data["winners"])) <= set(serials), data


def _check_tournament_champion(events: list, serials: list[str], ended: dict[str, str]) -> None:
    """Tournament (UC-025 step 5): tournament_champion, game_ended.champion is the same player."""
    kind, data = _single_winner_event(events)
    assert kind == "tournament_champion", f"expected tournament_champion, got {kind}: {data}"
    assert data["champion"] in serials, data
    assert ended["champion"] == data["champion"], f"tournament_champion {data} vs game_ended {ended}"


def _check_fight_club_winner(events: list, serials: list[str], ended: dict[str, str]) -> None:
    """Fight Club (UC-025 A2 step 4): game_ended.winner is the one top scorer; no other result event."""
    assert not _winner_events(events), [(e.event_type, dict(e.data)) for e in _winner_events(events)]
    winner = ended["winner"]
    assert winner in serials, f"game_ended {ended}"
    scores = ast.literal_eval(ended["final_scores"])
    top = max(scores.values())
    assert scores[winner] == top == int(ended["winner_score"]), f"winner {winner} is not the top scorer: {ended}"
    assert list(scores.values()).count(top) == 1, f"tied top score: {scores}"


def _check_force_end_no_winner(events: list, serials: list[str], ended: dict[str, str]) -> None:  # noqa: ARG001
    """Force-end (UC-026 A1 step 1): no winner event, game_ended winner none with outcome force_ended."""
    assert not _winner_events(events), [(e.event_type, dict(e.data)) for e in _winner_events(events)]
    assert ended.get("winner") == "none", f"game_ended {ended}"
    assert ended["outcome"] == "force_ended", f"game_ended {ended}"
    assert ended.get("reason") == FORCE_END_REASON, f"game_ended {ended}"


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
    check: OutcomeCheck  # winner event + game_ended winner field
    pre_end_delay: float = 0.0  # wait before the end strategy (e.g. formation phase)
    forced: bool = False  # ended by end_force: expect game_force_ended, no winner


# Per-mode specs. JoustFFA runs through the menu in the TC-002 journey
# (test_tc002_ffa_game.py), so it is intentionally absent here.
_SPECS = {
    # Teams: 4 players on alternating teams (build_start_config); killing the
    # first three wipes team 0. Moved here from the menu-flow lifecycle test.
    "JoustTeams": ModeSpec("JoustTeams", 4, end_team, 15, _check_team_win),
    # RandomTeams runs a 5s team-formation phase (TEAM_FORMATION_DURATION in
    # random_teams.py) before the game proper — kills during it are swallowed,
    # so wait it out before driving the win condition (caught in CI).
    "JoustRandomTeams": ModeSpec("JoustRandomTeams", 4, end_team, 20, _check_team_win, pre_end_delay=6.5),
    # Swapper's death-swap end sequence (delay=0.3/swap) can exceed 15s under
    # 4-way parallel load on a loaded CI runner; 25s also timed out on CI
    # (#897), so give the post-convergence end path generous headroom.
    "Swapper": ModeSpec("Swapper", 4, end_swapper, 40, _check_swapper_win),
    "Zombies": ModeSpec("Zombies", 4, end_zombies, 15, _check_zombie_win),
    # NonStop has no time limit in the start config (unlimited, UC-026 A1): it
    # is the force-end case, asserting the force-end = no winner pattern.
    "NonStop": ModeSpec("NonStop", 2, end_force, 15, _check_force_end_no_winner, forced=True),
    # Traitor is driven to a win (#1361): kill all but one, the survivor's
    # secret team wins.
    "Traitor": ModeSpec("Traitor", 4, end_traitor, 15, _check_traitor_win),
    "Werewolf": ModeSpec("Werewolf", 4, end_werewolf, 20, _check_werewolf_win),
    # Tournament: full single-elimination bracket to a champion (#903). Two
    # root causes fixed there: (1) numeric calibration flags were read with the
    # wrong typed getter so the CI match/pause variants silently fell back to
    # defaults (lib/feature_flags); (2) kill-decided matches skipped finalize so
    # the winner never advanced and the bracket stalled (tournament.py). The
    # state-aware end strategy then drives it to completion. Measured ~18-26s of
    # bracket play locally; 45s terminal-event budget covers parallel CI load.
    "Tournament": ModeSpec("Tournament", 4, end_tournament, 45, _check_tournament_champion),
    "FightClub": ModeSpec("FightClub", 4, end_fight_club, 30, _check_fight_club_winner),
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
    pytest.param(["JoustTeams"], id="teams-batch"),
]

_TERMINAL_EVENTS = ["game_ended", "game_force_ended", "game_error"]


async def _start_headless_with_retry(game_client, start_config, attempts: int = 4, backoff: float = 2.0):
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


async def _wait_for_game_colors(mock_client, serials: list[str], timeout: float = 10.0) -> None:  # noqa: ASYNC109
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


async def _run_mode_headless(stack, spec: ModeSpec, tag: str) -> None:
    """Run one mode end-to-end as a headless shadow game.

    Reserves its own controller set (distinct tag), starts headless, verifies
    game colors, runs the mode's end strategy, and awaits the terminal event on
    the collector bound to this game's game_id. Cleans up its game + reserved
    controllers even on failure. Raises with the mode name on any failure so a
    gathered batch surfaces exactly which mode broke.
    """
    from tests.integration.helpers import remove_reserved_controllers

    mock_client, mock_channel = await get_mock_client(stack)
    game_client, game_channel = await get_game_client(stack)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(stack, count=spec.players, reserved=True, tag=tag)

        # Start headless: bypasses the lobby; returns the coordinator-assigned
        # game_id plus a collector already filtered to that session. Retries
        # transient cap rejections (sibling games in their ENDING window).
        game_id, collector = await _start_headless_with_retry(game_client, build_start_config(spec.mode, serials))

        # The session must actually be RUNNING before the end strategy may
        # query it: start_game_headless returns on the first start event (can
        # be game_starting, #897), and the state-driven end strategies treat
        # "not RUNNING" from GetGameState as "game already ended" and silently
        # return — leaving a live game with no kills and no terminal event.
        # That was the recurring [Swapper] fast-batch failure: under load the
        # strategy's first query landed inside the stretched STARTING window,
        # zero SimulateDeath calls ever fired, and the batch timed out on a
        # healthy 2v2 RUNNING game (#894).
        running = await wait_until_running(game_client, {game_id}, timeout=20.0)
        assert game_id in running, f"{spec.mode} game {game_id} never reached RUNNING (running: {sorted(running)})"

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
            terminal = await collector.wait_for_any_event(_TERMINAL_EVENTS, timeout=spec.timeout)
        except TimeoutError as exc:
            state = await describe_game_state(game_client, game_id)
            raise TimeoutError(f"{exc} (session state: {state})") from exc
        # ``game_error`` stays in the awaited set so a crash fails FAST, but it
        # is never a success: NonStop's startup crash (GameplayStreamConfig
        # ``serials`` regression) hid behind game_error counting as terminal.
        assert terminal.event_type != "game_error", f"game errored instead of ending: {dict(terminal.data)}"

        # The result (#1361). A natural end: game_ended, never a force-end. A
        # force-end: game_force_ended, then the mode's own game_ended (it may
        # arrive before or after the session's game_force_ended).
        if spec.forced:
            await collector.wait_for_event("game_force_ended", timeout=spec.timeout)
            ended = await collector.wait_for_event("game_ended", timeout=spec.timeout)
        else:
            assert terminal.event_type == "game_ended", f"expected a natural end, got {terminal.event_type}"
            ended = terminal
            assert not collector.get_events("game_force_ended"), "a natural end was force-ended"
            assert ended.data["outcome"] not in ("force_ended", "error"), dict(ended.data)
        spec.check(collector.get_events(), serials, dict(ended.data))
    except Exception as exc:  # noqa: BLE001 - re-raise tagged with the mode name
        raise AssertionError(f"[{spec.mode}] headless lifecycle failed: {exc}") from exc
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="parallel_cleanup")
        await remove_reserved_controllers(stack, tag)
        await mock_channel.close()
        await game_channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("modes", _BATCHES)
# Steps limited to what is asserted: start, RUNNING, game colours, no game_error,
# and the result: each kill-decided mode's winner event and the matching winner
# field of game_ended; NonStop's force-end declares no winner (#1361).
# Mode-specific deaths stay unit-only (TC-017 step 7); one parametrised run
# carries every tag, so a failure in one mode fails every tag.
@pytest.mark.uc("UC-021", flow="main", steps=[1, 2, 4, 5], tc="TC-017")
@pytest.mark.uc("UC-022", flow="main", steps=[1, 4], tc="TC-017")
@pytest.mark.uc("UC-022", flow="A1", steps=[4], tc="TC-017")
@pytest.mark.uc("UC-023", flow="main", steps=[1], tc="TC-017")
@pytest.mark.uc("UC-023", flow="A1", steps=[1], tc="TC-017")
@pytest.mark.uc("UC-024", flow="main", steps=[1, 3, 4], tc="TC-017")
@pytest.mark.uc("UC-025", flow="main", steps=[1, 5], tc="TC-017")
@pytest.mark.uc("UC-025", flow="A2", steps=[4], tc="TC-017")
@pytest.mark.uc("UC-026", flow="main", steps=[1], tc="TC-017")
@pytest.mark.uc("UC-026", flow="A1", steps=[1], tc="TC-017")
async def test_parallel_mode_lifecycle(service_stack, headless_cleanup, modes):
    """Run a duration-grouped batch of game modes concurrently, headless.

    Each mode in the batch:
    1. reserves its own tagged controller set (distinct serials per mode),
    2. starts a headless game (its own game_id),
    3. verifies controllers got game colors,
    4. runs the mode-specific end strategy (game_id-aware),
    5. awaits its terminal event on the collector bound to that game_id,
    6. checks the result: the mode's winner event and the matching winner field
       of ``game_ended``, or, for a force-end, ``game_force_ended`` and no winner.

    The batch runs all modes via ``asyncio.gather(..., return_exceptions=True)``
    so a single mode's failure doesn't mask the others; failures are re-raised
    together, each tagged with its mode name.
    """
    specs = [_SPECS[m] for m in modes]

    # The module's stack is shared by the three batches: no leftover live
    # session may eat a cap slot (the integration suite's autouse
    # ``coordinator_idle`` has no service-tier twin).
    game_client, game_channel = await get_game_client(service_stack)
    try:
        await quiesce_coordinator(game_client, context="parallel batch start")
    finally:
        await game_channel.close()

    # Register tags up front so the fixture sweeps reserved controllers even if a
    # coroutine dies before its own finally-block cleanup runs.
    tags = []
    for spec in specs:
        tag = f"parallel-{spec.mode.lower()}"
        headless_cleanup.add_tag(tag)
        tags.append(tag)

    results = await asyncio.gather(
        *(_run_mode_headless(service_stack, spec, tag) for spec, tag in zip(specs, tags, strict=True)),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        messages = "; ".join(str(f) for f in failures)
        raise AssertionError(f"{len(failures)}/{len(specs)} modes failed: {messages}")
