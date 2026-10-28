"""Grafana folders and dashboard uids (#1427 step 6, docs/specs/telemetry.md §6.1).

Dashboards live in one subdirectory per Grafana folder and each subdirectory has
exactly one file provider. Links (runbooks, the dashboard SPA's Metrics-tab embed)
address dashboards by uid, so the uids they name must exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import infra_config as ic
import pytest

GRAFANA = ic.REPO_ROOT / "services" / "grafana"
DASHBOARDS = GRAFANA / "dashboards"
PROVISIONING = GRAFANA / "provisioning" / "dashboards" / "dashboards.yml"
SPA_INDEX = ic.REPO_ROOT / "services" / "dashboard" / "src" / "index.html"
CONTAINER_ROOT = "/var/lib/grafana/dashboards"

EXPECTED_FOLDERS = {
    "Game": {"game-players", "controller-overview"},
    "Agent": {"agent-operations"},
    "Platform": {"joustmania-service-health", "feature-flags"},
    "Talk": {"talk"},
}


@pytest.fixture(scope="module")
def providers():
    return ic.load_yaml(PROVISIONING)["providers"]


def _dashboards() -> dict[Path, dict]:
    return {p: json.loads(p.read_text()) for p in sorted(DASHBOARDS.rglob("*.json"))}


def test_compose_mounts_the_dashboard_tree_where_the_providers_read_it():
    volumes = ic.compose_services()["grafana"]["volumes"]
    assert f"./services/grafana/dashboards:{CONTAINER_ROOT}:ro" in volumes


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_every_dashboard_is_in_exactly_one_provisioned_folder(providers):
    assert not list(DASHBOARDS.glob("*.json")), (
        "a dashboard outside a folder directory is not provisioned"
    )
    by_path = {p["options"]["path"]: p for p in providers}
    assert len(by_path) == len(providers), "two providers read the same directory"
    folders = {}
    for path, dash in _dashboards().items():
        provider = by_path.get(f"{CONTAINER_ROOT}/{path.parent.name}")
        assert provider, (
            f"{path.relative_to(DASHBOARDS)}: no provider reads its directory"
        )
        folders.setdefault(provider["folder"], set()).add(dash["uid"])
    assert folders == EXPECTED_FOLDERS


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_folder_uids_are_stable_and_unique(providers):
    uids = [p["folderUid"] for p in providers]
    assert len(set(uids)) == len(uids)
    assert all(re.fullmatch(r"[a-z0-9-]{1,40}", u) for u in uids)


def test_dashboard_uids_are_unique():
    uids = [d["uid"] for d in _dashboards().values()]
    assert len(set(uids)) == len(uids), uids


def _linked_uids(text: str) -> set[str]:
    return set(re.findall(r"/grafana/d/([A-Za-z0-9_-]+)", text))


def test_spa_embed_and_runbook_links_name_existing_dashboards():
    """The SPA embeds controller-overview by uid; runbooks link dashboards by uid."""
    uids = {d["uid"] for d in _dashboards().values()}
    spa = _linked_uids(SPA_INDEX.read_text())
    assert "controller-overview" in spa
    docs = set()
    for doc in (ic.REPO_ROOT / "docs").rglob("*.md"):
        if "research" in doc.parts:
            continue  # dated research notes keep their historical links
        docs |= _linked_uids(doc.read_text())
    missing = sorted((spa | docs) - uids)
    assert not missing, f"links to dashboards that do not exist: {missing}"


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_talk_dashboard_is_a_single_stage_view():
    talk = next(d for d in _dashboards().values() if d["uid"] == "talk")
    assert not talk["templating"]["list"], "the stage view takes no template variables"
    assert talk["refresh"] in {"5s", "10s", "30s"}
    panels = [p for p in talk["panels"] if p["type"] != "row"]
    assert len(panels) <= 8, "keep the stage view to a few large panels"
    # Grafana 13 cannot search Jaeger 2.21+ (jaeger#7594, #2311): no stage panel uses the Jaeger search.
    assert not any(p["datasource"]["type"] == "jaeger" for p in panels)


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_grafana_home_page_is_an_existing_dashboard():
    """grafana.ini home_page opens the Talk stage view (a missing folder uid shows an error page)."""
    ini = (GRAFANA / "grafana.ini").read_text()
    match = re.search(r"^home_page\s*=\s*(\S+)", ini, re.MULTILINE)
    assert match, "home_page not set"
    uids = {d["uid"] for d in _dashboards().values()}
    assert _linked_uids(match.group(1)) == {"talk"} <= uids


def _game_players() -> dict:
    return next(d for d in _dashboards().values() if d["uid"] == "game-players")


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_player_variable_dedupes_serials_alive_in_two_games():
    """#1471 item 2: a serial alive in two games must not duplicate the join's right-hand side.

    game_player_alive carries (serial, game_id) only, so the query keeps primary games through
    game_active and collapses the result to one series per serial before joining controller_info.
    """
    variable = next(
        v for v in _game_players()["templating"]["list"] if v["name"] == "player"
    )
    query = re.sub(r"\s+", "", variable["query"]["query"])
    assert variable["definition"] == variable["query"]["query"]
    assert "group_left()maxby(serial)(" in query, query
    liveness = 'and on(game_id)(max by(game_id)(game_active{game_kind="primary"})==1)'
    assert re.sub(r"\s+", "", liveness) in query, query
    assert "empty between games" in variable["description"], variable
    assert "game_player_alive{" not in query, (
        "game_player_alive has no game_kind label; filter through game_active"
    )


def _histogram_buckets(metric: str) -> set[float]:
    source = (ic.REPO_ROOT / "services" / "game_coordinator" / "metrics.py").read_text()
    match = re.search(rf'"{metric}".*?buckets=\[([^\]]+)\]', source, flags=re.DOTALL)
    assert match, f"{metric}: no buckets= in metrics.py"
    return {float(b) for b in match.group(1).split(",")}


@pytest.mark.uc("UC-035", flow="main", steps=[6])
def test_movement_zone_pie_uses_fixed_histogram_buckets():
    """#1471 item 3: the pie edges are game_accel_distribution buckets, not perception.zone_* flags."""
    panel = next(
        p
        for p in _game_players()["panels"]
        if p.get("title", "").startswith("Movement Zone Distribution")
    )
    edges = {
        float(le)
        for t in panel["targets"]
        for le in re.findall(r'le="([0-9.]+)"', t["expr"])
    }
    assert edges and edges <= _histogram_buckets("game_accel_distribution"), edges
    assert "fixed" in panel["description"], panel["description"]
    assert "perception.zone_" in panel["description"], (
        "say that the pie does not follow the perception.zone_* flags"
    )
