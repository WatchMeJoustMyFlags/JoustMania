"""
UC-053 flows through the real flag service, flagd and game coordinator (#1413).

- TC-018: an agent intervention through the flag service: the coordinator
  registers the game (main step 1), two eliminations appended within 100 ms both
  apply exactly once (A4, BR-006), a later configuration push replays nothing,
  the game's scope is dropped at its end (step 7, BR-003) and a write for the
  ended game is refused (A6). The coordinator's "failed read, no replay" step is
  a unit test (services/game_coordinator/tests/test_event_list_reader.py).
- A6 / A5: a serial outside the game and a game that is not registered.
- A10: a coordinator's reconcile drops only its own games.
- A2: a persisted admin value through the API (the menu path is test_admin_flow).

The agent is not run here (it lives behind the `agent` profile): the test sends
the exact requests of its writers (services/agent/actions/writer.go).
"""

import asyncio
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.flag_service import (  # noqa: E402
    FlagServiceError,
    append_event,
    nudge_sync,
    set_player_value,
)
from tests.integration.helpers import (  # noqa: E402
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    setup_mock_controllers,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    RELOAD_SETTLE_SECONDS,
    intervention_env,  # re-exported: flag-service client + suite-wide budget pin
    set_interventions_allowed,
)
from tests.integration.test_parallel_intervention_flow import (  # noqa: E402
    _await_running,
    _intervention_events,
    _player,
    _start_headless_with_retry,
    _wait_for_game_colors,
    _wait_until_dead,
)
from tests.integration.flag_service import variant_value  # noqa: E402

__all__ = ["intervention_env"]

TAG = "flag-service-flows"


def _registered(flag_service, game_id: str) -> bool:
    return any(g["game_id"] == game_id for g in flag_service.games())


async def _serve_full_permission(docker_compose) -> None:
    from openfeature.evaluation_context import EvaluationContext

    set_interventions_allowed("full")
    expected = variant_value("agent", "interventions_allowed", "full")
    with flagd_probe_client(docker_compose, "agent") as client:
        assert await async_poll_until(
            lambda: (
                client.get_string_value(
                    "interventions_allowed", "", EvaluationContext()
                )
                == expected
            ),
        ), "flagd did not serve interventions_allowed=full"


@pytest.mark.asyncio
@pytest.mark.uc("UC-053", flow="main", br=["BR-003"], tc="TC-018")
@pytest.mark.uc("UC-053", flow="A4", br=["BR-006"], tc="TC-018")
@pytest.mark.uc("UC-053", flow="A6", tc="TC-018")
async def test_two_eliminations_through_the_flag_service(
    intervention_env, flag_service, docker_compose, headless_cleanup
):
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    headless_cleanup.add_tag(TAG)
    collector = None
    game_id = ""
    try:
        # Step 1: the host allows every intervention; a game starts with 4 players.
        await _serve_full_permission(docker_compose)
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=TAG
        )
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        headless_cleanup.add_game(game_id)
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # Step 2: the coordinator registered the game with its serials (FR-125).
        game = flag_service.wait_game_registered(game_id)
        assert sorted(game["serials"]) == sorted(serials), game

        # Step 3: two eliminations within 100 ms (one event list, two seqs).
        first, second = serials[0], serials[1]
        started = time.monotonic()
        seq_a = append_event("eliminate_player", game_id, {"serial": first})
        seq_b = append_event("eliminate_player", game_id, {"serial": second})
        assert time.monotonic() - started < 0.1, "the two writes took 100 ms or more"
        assert seq_b == seq_a + 1, (seq_a, seq_b)

        # Step 4: both apply, each exactly once.
        assert await _wait_until_dead(game_client, game_id, first), f"{first} alive"
        assert await _wait_until_dead(game_client, game_id, second), f"{second} alive"
        applied = len(
            _intervention_events(collector, "eliminate_player", blocked="false")
        )
        assert applied == 2, f"{applied} eliminations applied, want 2"

        # Step 5: a new configuration push with the list unchanged replays nothing.
        # Absence assertion: the bounded wait is the test (#894).
        nudge_sync()
        await asyncio.sleep(RELOAD_SETTLE_SECONDS + 1.0)
        assert (
            len(_intervention_events(collector, "eliminate_player", blocked="false"))
            == applied
        ), "an unchanged event list was applied again"
        assert (await _player(game_client, game_id, serials[2])).alive

        # Step 6: a serial outside the game is refused (A5/A6 checks of step 3).
        with pytest.raises(FlagServiceError) as refused:
            set_player_value("shield_seconds", game_id, "NOTINGAME", 5)
        assert (refused.value.status, refused.value.reason) == (
            422,
            "serial_not_in_game",
        )

        # Step 7: the game ends; the flag service drops its scope (BR-003).
        await force_end_game_by_id(game_client, game_id, reason="tc018_end")
        assert await async_poll_until(
            lambda: not _registered(flag_service, game_id), timeout=10.0
        ), "the ended game is still registered"
        view = flag_service.get("interventions", "eliminate_player")
        assert game_id not in (view.get("events") or {}), (
            "the event list outlived its game"
        )

        # Step 8: a late write for the ended game is refused (A6).
        with pytest.raises(FlagServiceError) as late:
            append_event("eliminate_player", game_id, {"serial": serials[2]})
        assert (late.value.status, late.value.reason) == (409, "game_not_live")
    finally:
        if collector is not None:
            await collector.stop()
        if game_id:
            await force_end_game_by_id(game_client, game_id, reason="tc018_cleanup")
        await mock_channel.close()
        await game_channel.close()


@pytest.mark.uc("UC-053", flow="A6")
def test_write_for_an_unregistered_game_is_refused(flag_service):
    game_id = f"game_{uuid.uuid4().hex[:12]}"
    status, body = flag_service.request(
        "PUT",
        "/interventions/global_difficulty_factor",
        {"value": 1.2, "scope": {"game_id": game_id}, "writer": "integration-test"},
    )
    assert (status, body.get("error")) == (409, "game_not_live"), body


@pytest.mark.uc("UC-053", flow="A10")
def test_coordinator_reconcile_drops_only_its_own_games(flag_service):
    run = uuid.uuid4().hex[:8]
    mine, gone = f"game_{run}mine", f"game_{run}gone"
    other = f"game_{run}othr"
    instance, other_instance = f"itest-{run}-a", f"itest-{run}-b"
    try:
        for game_id, inst in (
            (mine, instance),
            (gone, instance),
            (other, other_instance),
        ):
            flag_service._ok(
                "PUT",
                f"/games/{game_id}",
                {
                    "coordinator_instance": inst,
                    "game_kind": "shadow",
                    "serials": ["MOCK9001"],
                },
            )
        # Step 1: the coordinator restarted with only `mine` still live.
        result = flag_service._ok(
            "PUT",
            f"/coordinators/{instance}/games",
            {
                "games": [
                    {
                        "game_id": mine,
                        "coordinator_instance": instance,
                        "game_kind": "shadow",
                        "serials": ["MOCK9001"],
                    }
                ]
            },
        )
        # Step 2: only its other game is dropped; another instance's game stays.
        assert result["dropped"] == [gone], result
        assert _registered(flag_service, mine)
        assert not _registered(flag_service, gone)
        assert _registered(flag_service, other)
    finally:
        for game_id in (mine, gone, other):
            flag_service.request("DELETE", f"/games/{game_id}")


@pytest.mark.uc("UC-053", flow="A2", br=["BR-004"])
def test_admin_value_is_persisted_and_cleared(flag_service):
    try:
        # Step 1: a persisted admin write (registry: num_teams persist).
        result = flag_service._ok(
            "PUT",
            "/game/num_teams",
            {"variant": "four", "persist": True, "writer": "menu"},
        )
        assert result.get("persisted") is True, result
        view = flag_service.get("game", "num_teams")
        assert view["effective_default"] == "four"
        assert (view.get("admin_value") or {}).get("variant") == "four", view
        assert flag_service.state()["persisted_overlays"] >= 1
        # Step 2: a TTL never persists (BR-004).
        status, body = flag_service.request(
            "PUT",
            "/game/num_teams",
            {"variant": "five", "persist": True, "ttl_seconds": 30, "writer": "menu"},
        )
        assert (status, body.get("error")) == (400, "malformed"), body
    finally:
        flag_service.request("DELETE", "/game/num_teams?persist=true")
    view = flag_service.get("game", "num_teams")
    assert (
        not view.get("admin_value")
        and view["effective_default"] == view["baseline_default"]
    )
