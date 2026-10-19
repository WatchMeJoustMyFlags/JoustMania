"""
UC-053 flows through the real flag service, flagd and game coordinator (#1413).

- TC-018 (the agent intervention journey) moved to the service tier:
  tests/service/test_tc018_flag_service_eliminations.py (#1524).
- A6 / A5: a game that is not registered.
- A10: a coordinator's reconcile drops only its own games.
- A2: a persisted admin value through the API (the menu path is test_tc005_admin_journey).
"""

import uuid

import pytest

from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE


def _registered(flag_service, game_id: str) -> bool:
    return any(g["game_id"] == game_id for g in flag_service.games())


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
@pytest.mark.usefixtures("fresh_stack")
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
    assert not view.get("admin_value") and view["effective_default"] == view["baseline_default"]
