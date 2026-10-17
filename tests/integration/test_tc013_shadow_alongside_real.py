"""
TC-013: Shadow game alongside a real FFA game (docs/test_cases/TC-013-shadow-game-alongside-real-game.md).

One journey on one stack: a real menu game R1 on four unreserved mock
controllers and a headless shadow game S1 on four reserved ones run together.
Interventions and force-ends addressed to one game must never reach the other:

- lobby isolation (reserved controllers never reach the menu's roster) and
  per-game event streams and rosters (steps 1-3);
- gameId-scoped ``eliminate_player`` (exactly once, the same nonce served again
  does not re-apply) and gameId+serial-scoped ``player_sensitivity_factor``
  reach only S1; an un-targeted eliminate still reaches the real game (4-5);
- an ``end_game`` addressed ``<nonce>:S1`` ends S1 only (6-7);
- with ``shadow_policy=block`` a real start preempts a live shadow game and no
  new shadow game may start while it runs (8).

The agent does not run here: flag writes reproduce its writer's file mutations
(services/agent/actions/writer.go) in place, and flagd's file watch serves them.
``interventions_allowed`` and the rate limiter are global, so this test runs
alone (no gather).
"""

import asyncio
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from openfeature.evaluation_context import EvaluationContext  # noqa: E402

from lib.colors import Colors  # noqa: E402
from proto import game_coordinator_pb2  # noqa: E402
from tests.integration.helpers import (  # noqa: E402
    ButtonStreamObserver,
    GameEventCollector,
    active_flag_file,
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    get_controller_client,
    get_controller_color,
    get_game_client,
    get_mock_client,
    kill_players_until_one_remains,
    poll_until,
    setup_mock_controllers,
    start_game_headless,
    start_game_via_menu,
    wait_flag_file_restored,
    wait_for_lobby_colors,
    wait_until_running,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    APPLY_TIMEOUT_SECONDS,
    INTERVENTIONS_PATH,
    RELOAD_SETTLE_SECONDS,
    flag_files,  # snapshot/restore interventions.json + agent.json, pins the rate-limit budget
    set_interventions_allowed,
)

# Keep the re-exported fixture referenced so linters don't flag it as unused.
__all__ = ["flag_files"]

GAME_PATH = active_flag_file("game")
SHADOW_TAG = "tc013-shadow"
_TERMINAL_EVENTS = ("game_ended", "game_force_ended", "game_error")
# The menu's CONNECTED colour for FFA: orange at 30 % (services/menu/utils/led.py).
FFA_LOBBY_COLOR = tuple(int(c * 0.3) for c in Colors.Orange.value)
_LOBBY_TOLERANCE = 2
_SENSITIVITY_FACTOR = 1.5  # the "handicap" variant value of player_sensitivity_factor
_SECOND_FACTOR = (
    0.7  # the "protect" value; the marker that flagd served the re-sent nonce
)


# =============================================================================
# Flag-file writes (in place: a rename over a watched bind mount breaks the watch)
# =============================================================================


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    with open(path, "w") as fh:
        fh.write(json.dumps(doc, indent=2) + "\n")


def _game_rule(
    game_id: str, match: str, neutral: str, serial: str | None = None
) -> dict:
    cond: dict = {"===": [{"var": "gameId"}, game_id]}
    if serial:
        cond = {"and": [cond, {"===": [{"var": "targetingKey"}, serial]}]}
    return {"if": [cond, match, neutral]}


def write_edge_for_game(flag_key: str, game_id: str, value: str) -> None:
    """Edge value served only to ``game_id`` (#838); every other game reads the neutral ``none``."""
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"]["active"] = value
    flag["targeting"] = _game_rule(game_id, "active", "none")
    flag["defaultVariant"] = "none"
    # Toggled so serving the SAME value again still changes the file and reloads flagd.
    flag["_reload_marker"] = int(flag.get("_reload_marker", 0)) + 1
    _dump_in_place(INTERVENTIONS_PATH, doc)


def write_edge_untargeted(flag_key: str, value: str) -> None:
    """Edge value served to EVERY live game (no gameId rule)."""
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"]["active"] = value
    flag.pop("targeting", None)
    flag["defaultVariant"] = "active"
    _dump_in_place(INTERVENTIONS_PATH, doc)


def _set_player_factor(doc: dict, game_id: str, serial: str, value: float) -> None:
    flag = doc["flags"]["player_sensitivity_factor"]
    name = "g_" + uuid.uuid5(uuid.NAMESPACE_OID, f"{game_id}\x00{serial}").hex
    flag["variants"][name] = value
    flag.setdefault("_branches", {})[name] = {"gameId": game_id, "serial": serial}
    flag["targeting"] = _game_rule(game_id, name, "default", serial=serial)
    flag["defaultVariant"] = "default"


def write_player_factor_for_game(game_id: str, serial: str, value: float) -> None:
    """``player_sensitivity_factor`` for one (gameId, serial); neutral ``default`` elsewhere.

    The ``_branches`` registry lets the flag_files fixture wait until flagd
    serves the restored baseline for exactly this scope.
    """
    doc = _load(INTERVENTIONS_PATH)
    _set_player_factor(doc, game_id, serial, value)
    _dump_in_place(INTERVENTIONS_PATH, doc)


def resend_edge_with_factor(
    game_id: str, edge_value: str, serial: str, factor: float
) -> None:
    """ONE file write: serve the same ``eliminate_player`` value to ``game_id`` again
    and change ``serial``'s factor. Once the new factor is observed, flagd has
    served the file that carries the repeated nonce, so the absence window that
    follows is not vacuous."""
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"]["eliminate_player"]
    flag["variants"]["active"] = edge_value
    flag["targeting"] = _game_rule(game_id, "active", "none")
    flag["defaultVariant"] = "none"
    flag["_reload_marker"] = int(flag.get("_reload_marker", 0)) + 1
    _set_player_factor(doc, game_id, serial, factor)
    _dump_in_place(INTERVENTIONS_PATH, doc)


def set_shadow_policy(policy: str) -> None:
    doc = _load(GAME_PATH)
    doc["flags"]["shadow_policy"]["defaultVariant"] = policy
    _dump_in_place(GAME_PATH, doc)


@pytest.fixture
def game_flag_file(docker_compose):
    """Snapshot game.json; restore it byte-for-byte and wait until flagd serves it."""
    with open(GAME_PATH) as fh:
        backup = fh.read()
    yield
    with open(GAME_PATH) as fh:
        mutated = fh.read()

    def _restore():
        with open(GAME_PATH, "w") as fh:
            fh.write(backup)

    _restore()
    wait_flag_file_restored(docker_compose, "game", backup, mutated, _restore)


# =============================================================================
# Observation helpers (all scoped to a game id)
# =============================================================================


def _interventions(
    collector: GameEventCollector, game_id: str, type_id: str, *, blocked=None
) -> list:
    return [
        e
        for e in collector.get_events("agent_intervention")
        if e.game_id == game_id
        and e.data.get("type") == type_id
        and (blocked is None or e.data.get("blocked") == blocked)
    ]


def _events(collector: GameEventCollector, game_id: str, event_type: str) -> list:
    return [e for e in collector.get_events(event_type) if e.game_id == game_id]


async def _players(game_client, game_id: str) -> dict:
    resp = await game_client.GetGameState(
        game_coordinator_pb2.GetGameStateRequest(game_id=game_id)
    )
    assert resp.success, f"GetGameState({game_id}) failed: {resp.error}"
    return {p.serial: p for p in resp.game_info.players}


async def _until(predicate, what: str, timeout: float = APPLY_TIMEOUT_SECONDS) -> None:
    assert await async_poll_until(predicate, timeout=timeout), what


async def _running_ids(game_client) -> set[str]:
    return await wait_until_running(game_client, set(), timeout=0)


# =============================================================================
# The journey
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="main", steps=[1, 3, 4, 5, 7, 8], tc="TC-013")
@pytest.mark.uc("UC-003", flow="A2", tc="TC-013")
@pytest.mark.uc("UC-020", flow="main", steps=[5, 7], tc="TC-013")
@pytest.mark.uc("UC-020", flow="A3", tc="TC-013")
@pytest.mark.uc("UC-040", flow="A18", tc="TC-013")
@pytest.mark.uc("UC-027", flow="A3", tc="TC-013")
@pytest.mark.uc("UC-043", flow="A2", tc="TC-013")
async def test_tc013_shadow_game_alongside_real_game(
    flag_files, game_flag_file, docker_compose, ensure_game_stopped, headless_cleanup
):
    """TC-013: interventions and force-ends addressed to one game never reach the other."""
    set_interventions_allowed(
        "full"
    )  # eliminate_player, player_sensitivity_factor and end_game pass the allow-list
    headless_cleanup.add_tag(SHADOW_TAG)

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    controller_client, controller_channel = await get_controller_client(docker_compose)

    menu_serials = await setup_mock_controllers(docker_compose, count=4)
    primary = GameEventCollector(game_client)  # unfiltered primary stream
    buttons = ButtonStreamObserver(controller_client)
    shadow = None
    shadow2 = None
    primary2 = None
    try:
        await primary.start()
        await buttons.start()

        # Step 1: Start the real game. Players ready up; the menu starts R1.
        await start_game_via_menu(
            docker_compose, game_mode="JoustFFA", timeout=25.0, event_collector=primary
        )
        r1 = primary.get_events("game_started")[0].game_id
        assert r1, "Step 1: game_started carries no game id"

        # Step 2: Start a shadow game on four reserved controllers, added only
        # now so the menu's ready-up saw exactly the four lobby controllers.
        shadow_serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=SHADOW_TAG
        )
        assert set(menu_serials).isdisjoint(shadow_serials), (
            "Step 2: reserved serials overlap the lobby"
        )
        s1, shadow = await start_game_headless(
            game_client, build_start_config("JoustFFA", shadow_serials), timeout=25.0
        )
        headless_cleanup.add_game(s1)
        assert s1 != r1, "Step 2: shadow game got the real game's id"
        running = await wait_until_running(game_client, {r1, s1}, timeout=15.0)
        assert {r1, s1} <= running, f"Step 2: not both RUNNING: {running}"

        # Step 3: Verify lobby isolation. The menu's controller roster holds only
        # the lobby controllers; each game's roster is its own; no S1 event on R1's stream.
        await asyncio.sleep(
            RELOAD_SETTLE_SECONDS
        )  # absence window for a leaked roster entry
        leaked = set(shadow_serials) & buttons.all_seen_serials()
        assert not leaked, (
            f"Step 3: reserved controllers {sorted(leaked)} reached the menu's button stream"
        )
        roster = set(buttons.latest_roster())
        assert roster == set(menu_serials), (
            f"Step 3: the menu's roster must hold only the lobby controllers {sorted(menu_serials)}, got {sorted(roster)}"
        )
        assert set(await _players(game_client, r1)) == set(menu_serials), (
            "Step 3: R1 roster is not the lobby"
        )
        assert set(await _players(game_client, s1)) == set(shadow_serials), (
            "Step 3: S1 roster is not the reserved set"
        )
        assert not [e for e in primary.get_events() if e.game_id == s1], (
            "Step 3: S1 event on R1's stream"
        )

        # Step 4: Scoped interventions. eliminate_player scoped to S1 for one
        # shadow player; player_sensitivity_factor scoped to S1 + another serial.
        victim, factor_target, factor_other = (
            shadow_serials[0],
            shadow_serials[1],
            shadow_serials[2],
        )
        bystander = menu_serials[
            0
        ]  # the like-positioned real player that must stay alive
        players = await _players(game_client, s1)
        assert players[victim].alive, "Step 4: shadow victim not alive before the write"
        assert abs(players[factor_target].sensitivity_factor - 1.0) <= 0.01, (
            "Step 4: shadow factor not neutral"
        )
        assert (await _players(game_client, r1))[bystander].alive, (
            "Step 4: real bystander not alive"
        )

        eliminate_value = f"{uuid.uuid4().hex}:{victim}"
        write_edge_for_game("eliminate_player", s1, eliminate_value)
        write_player_factor_for_game(s1, factor_target, _SENSITIVITY_FACTOR)

        # Step 5: Verify only the shadow game. S1: one unblocked eliminate, the
        # victim dies with exactly one player_death; only the targeted player's
        # factor changes. R1: no agent_intervention, nobody dead, factors neutral.
        await _until(
            lambda: _interventions(shadow, s1, "eliminate_player", blocked="false"),
            f"Step 5: no unblocked eliminate_player on S1: {[dict(e.data) for e in shadow.get_events('agent_intervention')]}",
        )

        async def _victim_dead() -> bool:
            return not (await _players(game_client, s1))[victim].alive

        await _until(_victim_dead, "Step 5: the S1 victim was not eliminated")

        async def _factor_applied() -> bool:
            p = await _players(game_client, s1)
            return (
                abs(p[factor_target].sensitivity_factor - _SENSITIVITY_FACTOR) <= 0.01
            )

        await _until(
            _factor_applied,
            f"Step 5: {factor_target} factor never reached {_SENSITIVITY_FACTOR}",
        )
        await _until(
            lambda: _interventions(
                shadow, s1, "adjust_player_sensitivity", blocked="false"
            ),
            "Step 5: no unblocked adjust_player_sensitivity on S1",
        )
        assert (
            abs(
                (await _players(game_client, s1))[factor_other].sensitivity_factor - 1.0
            )
            <= 0.01
        ), f"Step 5: un-targeted shadow player {factor_other} factor changed"

        # Exactly once: flagd may emit several change events per write, and the
        # same nonce served again must not re-apply. The same write also moves
        # the factor to 0.7: once that is observed, flagd is serving the file
        # with the repeated nonce, so the absence check below is not vacuous.
        resend_edge_with_factor(s1, eliminate_value, factor_target, _SECOND_FACTOR)

        async def _second_factor_applied() -> bool:
            p = await _players(game_client, s1)
            return abs(p[factor_target].sensitivity_factor - _SECOND_FACTOR) <= 0.01

        await _until(
            _second_factor_applied,
            f"Step 5: flagd never served the re-sent nonce (factor stayed off {_SECOND_FACTOR})",
        )
        await asyncio.sleep(
            RELOAD_SETTLE_SECONDS
        )  # absence window: no re-apply may follow
        assert len(_interventions(shadow, s1, "eliminate_player")) == 1, (
            f"Step 5: eliminate applied more than once: {[dict(e.data) for e in _interventions(shadow, s1, 'eliminate_player')]}"
        )
        deaths = [e.data["serial"] for e in _events(shadow, s1, "player_death")]
        assert deaths == [victim], (
            f"Step 5: S1 player_death events {deaths}, expected exactly [{victim}]"
        )

        assert not [
            e for e in primary.get_events("agent_intervention") if e.game_id == r1
        ], "Step 5: R1 received a shadow-scoped intervention"
        real_players = await _players(game_client, r1)
        assert all(p.alive for p in real_players.values()), (
            "Step 5: a real player died from a shadow-scoped write"
        )
        assert all(
            abs(p.sensitivity_factor - 1.0) <= 0.01 for p in real_players.values()
        ), (
            f"Step 5: a real player's factor changed: { {s: p.sensitivity_factor for s, p in real_players.items()} }"
        )

        # Step 5 (un-targeted): the same flag without a gameId rule still reaches
        # the real game (UC-020 A3 on R1).
        write_edge_untargeted("eliminate_player", f"{uuid.uuid4().hex}:{bystander}")
        await _until(
            lambda: _interventions(primary, r1, "eliminate_player", blocked="false"),
            "Step 5: un-targeted eliminate did not reach R1",
        )

        async def _bystander_dead() -> bool:
            return not (await _players(game_client, r1))[bystander].alive

        await _until(
            _bystander_dead,
            "Step 5: un-targeted eliminate did not kill the real bystander",
        )

        # Step 6: Addressed end_game. Served to EVERY game (no gameId rule), but
        # the value names S1, so only S1 force-ends, with reason agent_intervention.
        write_edge_untargeted("end_game", f"tc013{uuid.uuid4().hex[:8]}:{s1}")
        await _until(
            lambda: _events(shadow, s1, "game_force_ended"),
            "Step 6: S1 was not force-ended by the addressed end_game",
            timeout=20.0,
        )
        force_ended = _events(shadow, s1, "game_force_ended")
        assert force_ended[0].data["reason"] == "agent_intervention", (
            f"Step 6: {dict(force_ended[0].data)}"
        )
        # The unblocked end_game event is recorded after the handler ran.
        await _until(
            lambda: _interventions(shadow, s1, "end_game", blocked="false"),
            f"Step 6: no unblocked agent_intervention end_game on S1: "
            f"{[dict(e.data) for e in shadow.get_events('agent_intervention')]}",
        )

        # Step 7: Verify the real game runs on: R1 RUNNING, no game_force_ended
        # and no end_game event on R1; S1 has exactly one game_force_ended.
        await asyncio.sleep(
            RELOAD_SETTLE_SECONDS
        )  # absence window for a stray force-end on R1
        assert r1 in await _running_ids(game_client), (
            "Step 7: R1 stopped running after S1's end_game"
        )
        assert not _events(primary, r1, "game_force_ended"), (
            "Step 7: R1 was force-ended by S1's end_game"
        )
        assert not _interventions(primary, r1, "end_game"), (
            "Step 7: R1 published an end_game addressed to S1"
        )
        assert len(_events(shadow, s1, "game_force_ended")) == 1, (
            "Step 7: S1 force-ended more than once"
        )
        assert not _events(shadow, s1, "game_winner"), (
            "Step 7: a force-ended S1 declared a winner"
        )

        # Validation 1: every agent_intervention / game_force_ended carries the
        # id of the game it was addressed to, on that game's stream.
        for event_type in ("agent_intervention", "game_force_ended"):
            assert all(e.game_id == s1 for e in shadow.get_events(event_type)), (
                f"Validation 1: {event_type} without S1's id on S1's stream"
            )
            assert all(e.game_id == r1 for e in primary.get_events(event_type)), (
                f"Validation 1: {event_type} without R1's id on R1's stream: "
                f"{[(e.game_id, dict(e.data)) for e in primary.get_events(event_type)]}"
            )

        # Validation 2: R1 ends; the menu restores the lobby colour of the lobby
        # controllers only (the reserved controllers never get it).
        await kill_players_until_one_remains(
            mock_client, menu_serials, delay=0.2, game_client=game_client
        )
        await _until(
            lambda: _events(primary, r1, "game_ended"),
            "Validation 2: R1 never ended",
            timeout=20.0,
        )
        await wait_for_lobby_colors(
            mock_client,
            menu_serials,
            expected_color=FFA_LOBBY_COLOR,
            tolerance=_LOBBY_TOLERANCE,
            timeout=12.0,
        )
        for serial in shadow_serials:
            color = await get_controller_color(mock_client, serial)
            assert any(
                abs(a - b) > _LOBBY_TOLERANCE for a, b in zip(color, FFA_LOBBY_COLOR)
            ), (
                f"Validation 2: reserved controller {serial} shows the lobby colour {color}"
            )

        # Step 8: Real game preempts shadow, under shadow_policy=block.
        set_shadow_policy("block")
        with flagd_probe_client(docker_compose, "game") as probe:
            assert poll_until(
                lambda: probe.get_string_value("shadow_policy", "", EvaluationContext())
                == "block",
                rewrite=lambda: set_shadow_policy("block"),
            ), "Step 8: flagd did not serve shadow_policy=block"

        # With no real game live, a shadow game may start even under block.
        s2, shadow2 = await start_game_headless(
            game_client, build_start_config("JoustFFA", shadow_serials), timeout=25.0
        )
        headless_cleanup.add_game(s2)
        assert s2 in await wait_until_running(game_client, {s2}, timeout=15.0), (
            "Step 8: S2 never RUNNING"
        )

        primary2 = GameEventCollector(
            game_client
        )  # fresh: R1's game_started is on the first one
        await primary2.start()
        await start_game_via_menu(
            docker_compose, game_mode="JoustFFA", timeout=25.0, event_collector=primary2
        )
        r2 = primary2.get_events("game_started")[0].game_id
        assert r2 not in (r1, s2), f"Step 8: the second real game reused an id: {r2}"

        await _until(
            lambda: _events(shadow2, s2, "game_force_ended"),
            "Step 8: the real start did not preempt S2",
            timeout=20.0,
        )
        preempted = _events(shadow2, s2, "game_force_ended")
        assert [e.data["reason"] for e in preempted] == ["preempted_by_real_game"], (
            f"Step 8: S2 force-end {[dict(e.data) for e in preempted]}"
        )
        # Released without a winner: nothing a fitness sample could be taken from.
        assert not _events(shadow2, s2, "game_winner"), (
            "Step 8: preempted S2 declared a winner"
        )
        for ended in _events(shadow2, s2, "game_ended"):
            assert ended.data.get("outcome") == "force_ended", (
                f"Step 8: S2 game_ended {dict(ended.data)}"
            )
        assert r2 in await _running_ids(game_client), (
            "Step 8: R2 not RUNNING after preempting S2"
        )

        # Under block, no new shadow game starts while R2 runs.
        with pytest.raises(RuntimeError, match="blocked while a real game is running"):
            await start_game_headless(
                game_client,
                build_start_config("JoustFFA", shadow_serials),
                timeout=15.0,
            )
        assert r2 in await _running_ids(game_client), (
            "Step 8: the rejected shadow start disturbed R2"
        )
    finally:
        await buttons.stop()
        for collector in (primary, primary2, shadow, shadow2):
            if collector is not None:
                await collector.stop()
        await mock_channel.close()
        await game_channel.close()
        await controller_channel.close()
