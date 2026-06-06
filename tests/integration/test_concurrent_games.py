"""
Integration tests for concurrent headless games and shadow-vs-menu isolation.

These tests are the payoff of the shadow-games feature (#774, phase 5 / #779).
They exercise capabilities that did not exist before the multi-session game
coordinator (#775/#776) and reserved mock controllers (#777):

- Two headless games running concurrently on one compose stack, each with its
  own reserved controller set and its own game_id, fully isolated.
- A headless (shadow) game running ALONGSIDE a menu-driven game without the
  reserved controllers ever leaking into the menu's lobby view. This is the
  direct regression test for the whole #774 feature.
- Natural-end then immediate restart — the #793 CI bug, where an ENDED session
  pinned the primary slot and the next start was rejected.

The menu remains single-lobby, so the menu-flow portions stay sequential; the
headless games are what run in parallel.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2
from tests.integration.helpers import (
    ButtonStreamObserver,
    GameEventCollector,
    build_start_config,
    force_end_game_by_id,
    get_controller_client,
    get_game_client,
    get_mock_client,
    kill_players_until_one_remains,
    list_games,
    setup_mock_controllers,
    start_game_headless,
    start_game_via_menu,
    wait_for_lobby_colors,
    wait_until_running,
)


def _running_ids(games) -> set[str]:
    """Set of game_ids whose GameInfo.state is RUNNING."""
    return {g.game_id for g in games if g.state == game_coordinator_pb2.RUNNING}


@pytest.mark.asyncio
async def test_two_concurrent_headless_games(docker_compose, headless_cleanup):
    """Two headless games run concurrently and stay fully isolated.

    Asserts:
    - Distinct game_ids.
    - Each collector only sees its own game's events.
    - ListGames shows both RUNNING; GetGameState(game_id) returns each.
    - Ending one (kills) leaves the other running and its event stream intact.
    """
    tag_a = "concurrent-a"
    tag_b = "concurrent-b"
    headless_cleanup.add_tag(tag_a)
    headless_cleanup.add_tag(tag_b)

    serials_a = await setup_mock_controllers(
        docker_compose, count=2, reserved=True, tag=tag_a
    )
    serials_b = await setup_mock_controllers(
        docker_compose, count=4, reserved=True, tag=tag_b
    )
    assert set(serials_a).isdisjoint(serials_b), "reserved sets must be distinct"

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)

    collector_a = None
    collector_b = None
    try:
        # Start two headless games with different modes.
        game_id_a, collector_a = await start_game_headless(
            game_client, build_start_config("JoustFFA", serials_a), timeout=25.0
        )
        headless_cleanup.add_game(game_id_a)

        game_id_b, collector_b = await start_game_headless(
            game_client, build_start_config("JoustTeams", serials_b), timeout=25.0
        )
        headless_cleanup.add_game(game_id_b)

        assert game_id_a != game_id_b, "concurrent games must get distinct game_ids"

        # Both should be RUNNING and enumerable. A session flips to RUNNING
        # when its loop publishes game_started — poll instead of a fixed sleep
        # (a one-shot at 1.0s flaked on CI once the start path grew the #837
        # shadow_policy flag read).
        running = await wait_until_running(game_client, {game_id_a, game_id_b})
        assert game_id_a in running, f"{game_id_a} not RUNNING in {running}"
        assert game_id_b in running, f"{game_id_b} not RUNNING in {running}"

        # GetGameState(game_id) returns each game's own roster.
        state_a = await game_client.GetGameState(
            game_coordinator_pb2.GetGameStateRequest(game_id=game_id_a)
        )
        state_b = await game_client.GetGameState(
            game_coordinator_pb2.GetGameStateRequest(game_id=game_id_b)
        )
        assert state_a.success and state_b.success
        roster_a = {p.serial for p in state_a.game_info.players}
        roster_b = {p.serial for p in state_b.game_info.players}
        assert roster_a == set(serials_a), (
            f"game A roster {roster_a} != {set(serials_a)}"
        )
        assert roster_b == set(serials_b), (
            f"game B roster {roster_b} != {set(serials_b)}"
        )

        # Each collector only saw its own game's events.
        for e in collector_a.get_events():
            if e.game_id:
                assert e.game_id == game_id_a, (
                    f"game A collector saw foreign event {e.game_id}"
                )
        for e in collector_b.get_events():
            if e.game_id:
                assert e.game_id == game_id_b, (
                    f"game B collector saw foreign event {e.game_id}"
                )

        # End game A by killing players (FFA: kill until one remains).
        await kill_players_until_one_remains(
            mock_client,
            serials_a,
            delay=0.2,
            game_client=game_client,
            game_id=game_id_a,
        )
        await collector_a.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )

        # Game B must be unaffected: still RUNNING, no end event, no foreign events.
        await asyncio.sleep(0.5)
        games = await list_games(game_client)
        assert game_id_b in _running_ids(games), (
            "game B should still be RUNNING after A ends"
        )

        b_end_events = [
            e
            for e in collector_b.get_events()
            if e.event_type in ("game_ended", "game_force_ended", "game_error")
        ]
        assert not b_end_events, f"game B saw end events while A ended: {b_end_events}"
        for e in collector_b.get_events():
            if e.game_id:
                assert e.game_id == game_id_b, "game B stream contaminated by game A"

        # End game B too.
        ended = await force_end_game_by_id(game_client, game_id_b)
        assert ended, "force-end of game B should succeed"
        await collector_b.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
    finally:
        if collector_a:
            await collector_a.stop()
        if collector_b:
            await collector_b.stop()
        await mock_channel.close()
        await game_channel.close()


@pytest.mark.asyncio
async def test_shadow_game_isolated_from_menu(
    docker_compose, ensure_game_stopped, headless_cleanup
):
    """A headless shadow game runs alongside a menu game without leaking.

    This is the direct regression test for #774. With a menu-flow (primary)
    game on 4 unreserved mocks AND a headless shadow game on reserved mocks:

    - The reserved controllers never appear to the menu's controller view
      (StreamButtonEvents roster — the exact source the menu reads from).
    - The shadow game's events carry its own game_id and the PRIMARY stream
      never receives them.
    - The menu game runs and ends normally, and the menu restores lobby colors
      on its (unreserved) controllers.
    """
    shadow_tag = "shadow-menu"
    headless_cleanup.add_tag(shadow_tag)

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    controller_client, controller_channel = await get_controller_client(docker_compose)

    # Create the 4 unreserved menu controllers first; add reserved shadow
    # controllers only AFTER the menu game has started so the menu's ready-up
    # flow operates on exactly the 4 it can see.
    menu_serials = await setup_mock_controllers(docker_compose, count=4)

    primary_collector = GameEventCollector(game_client)  # zero-arg: primary stream
    button_observer = ButtonStreamObserver(controller_client)
    shadow_collector = None
    try:
        await primary_collector.start()
        await button_observer.start()

        # 1. Menu-flow game on the 4 unreserved mocks (primary session).
        await start_game_via_menu(
            docker_compose,
            game_mode="JoustFFA",
            timeout=25.0,
            event_collector=primary_collector,
        )
        primary_started = primary_collector.get_events("game_started")
        assert primary_started, "menu game should emit game_started"
        primary_game_id = primary_started[0].game_id

        # 2. Reserved shadow controllers + headless shadow game, concurrently.
        shadow_serials = await setup_mock_controllers(
            docker_compose, count=3, reserved=True, tag=shadow_tag
        )
        assert set(menu_serials).isdisjoint(shadow_serials)
        shadow_game_id, shadow_collector = await start_game_headless(
            game_client, build_start_config("JoustFFA", shadow_serials), timeout=25.0
        )
        headless_cleanup.add_game(shadow_game_id)
        assert shadow_game_id != primary_game_id

        await asyncio.sleep(1.0)

        # 3. Reserved controllers are invisible to the menu's controller view.
        seen_by_menu = button_observer.all_seen_serials()
        for serial in shadow_serials:
            assert serial not in seen_by_menu, (
                f"reserved controller {serial} leaked into button-stream roster "
                f"{sorted(seen_by_menu)}"
            )
        # The unreserved menu controllers DO appear (sanity: the observer works).
        roster = set(button_observer.latest_roster())
        assert set(menu_serials).issubset(roster), (
            f"menu controllers missing from roster {sorted(roster)}"
        )

        # 4. Shadow events carry the shadow game_id; the primary stream never
        # received them.
        for e in shadow_collector.get_events():
            if e.game_id:
                assert e.game_id == shadow_game_id
        for e in primary_collector.get_events():
            if e.game_id:
                assert e.game_id != shadow_game_id, (
                    f"primary stream received shadow event {e.game_id}"
                )

        # 5. Both games are RUNNING.
        games = await list_games(game_client)
        running = _running_ids(games)
        assert primary_game_id in running
        assert shadow_game_id in running

        # 6. End the shadow game; the menu game must be unaffected.
        await force_end_game_by_id(game_client, shadow_game_id)
        await shadow_collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
        await asyncio.sleep(0.5)
        games = await list_games(game_client)
        assert primary_game_id in _running_ids(games), (
            "menu game ended when shadow ended"
        )

        # 7. End the menu game normally and verify lobby colors restore on the
        # menu controllers (the menu's own lifecycle still works). The menu
        # dims lobby colors to ~30% brightness, so assert non-zero (not stuck
        # at the death black) rather than the full-brightness base color —
        # mirroring test_full_game_lifecycle.
        await kill_players_until_one_remains(
            mock_client, menu_serials, delay=0.2, game_client=game_client
        )
        await primary_collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
        await wait_for_lobby_colors(mock_client, menu_serials, timeout=12.0)
    finally:
        await button_observer.stop()
        await primary_collector.stop()
        if shadow_collector:
            await shadow_collector.stop()
        await mock_channel.close()
        await game_channel.close()
        await controller_channel.close()


@pytest.mark.asyncio
async def test_headless_natural_end_then_restart(docker_compose, headless_cleanup):
    """A headless game ends naturally, then a new headless game starts cleanly.

    Regression for the #793 CI bug: a session that reached its natural win
    condition stayed registered as ENDED and pinned the primary slot, so the
    NEXT start was rejected with "Game already in progress". The coordinator now
    lazily retires ENDED sessions on the next start; this proves it end to end.
    """
    tag = "natural-end"
    headless_cleanup.add_tag(tag)

    serials = await setup_mock_controllers(
        docker_compose, count=2, reserved=True, tag=tag
    )

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)

    first_collector = None
    second_collector = None
    try:
        # First game: run to natural end (FFA, kill until one remains).
        game_id_1, first_collector = await start_game_headless(
            game_client, build_start_config("JoustFFA", serials), timeout=25.0
        )
        headless_cleanup.add_game(game_id_1)

        await kill_players_until_one_remains(
            mock_client, serials, delay=0.2, game_client=game_client, game_id=game_id_1
        )
        await first_collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
        await first_collector.stop()
        first_collector = None

        # Immediately start a second headless game — must succeed (the #793 bug
        # rejected this). Reuse the same controllers (they are free now).
        game_id_2, second_collector = await start_game_headless(
            game_client, build_start_config("JoustFFA", serials), timeout=25.0
        )
        headless_cleanup.add_game(game_id_2)

        assert game_id_2 != game_id_1, "restart must get a fresh game_id"

        # Poll: start_game_headless returns on game_starting, so the session
        # may still be STARTING (countdown) here — a one-shot assert raced on
        # CI under parallel load (#897).
        running = await wait_until_running(game_client, {game_id_2})
        assert game_id_2 in running, f"restarted game should be RUNNING, got {running}"

        await force_end_game_by_id(game_client, game_id_2)
        await second_collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=20.0
        )
    finally:
        if first_collector:
            await first_collector.stop()
        if second_collector:
            await second_collector.stop()
        await mock_channel.close()
        await game_channel.close()
