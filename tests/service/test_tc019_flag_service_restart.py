"""TC-019 on the service tier (T2): the flag service restarts (UC-053 A9, NFR-052; #1413).

Moved from tests/integration/test_flag_service_restart.py (#1588). Each test
gets a ``fresh_stack`` of its own (flag service built from HEAD, flagd,
controller manager on mock controllers, game coordinator; audio is faked) and
restarts its flag plane with the harness: ``restart_flag_service()`` (same
ports and state file), then ``restart_flagd()``, as ``scripts/flags.sh restart``
does.

A planned restart keeps what the host persisted and drops everything else:

- the persisted admin value of ``sensitivity`` is served again, and the
  baseline Werewolf rule still wins over it (the admin value is a
  ``defaultVariant`` substitution, flags.md §13.3);
- a stack overlay and a registered game scope are gone; the epoch changed;
- no write is reported as written while flagd has no sync stream (A7): every
  write during the outage either fails to connect or is refused with 503.

The coordinator's side of A9 (a new non-empty epoch restarts its event count,
a failed read never resets it, so nothing is replayed) is a unit test
(services/game_coordinator/tests/test_event_list_reader.py, tag TC-019).

A second test restarts the flag service while a game runs: the coordinator
notices the new boot epoch and re-registers its live game, so a game-scoped
write is accepted and applied on the first try (UC-053 A9 steps 3 and 4, #1552).
"""

import asyncio
import threading
import time
import uuid

import pytest
from openfeature.evaluation_context import EvaluationContext

from proto import game_coordinator_pb2
from tests.integration.flag_service import set_player_value, variant_value
from tests.integration.helpers import (
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    get_game_client,
    list_games,
    poll_until,
    setup_mock_controllers,
    start_game_headless,
)
from tests.service.harness import GAME

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = GAME

PROBE_SET, PROBE_FLAG = "observability", "flagservice.propagation_probe"
RECOVERY_SECONDS = 60.0
OUTAGE_POLL_SECONDS = 0.2
RUNNING_TIMEOUT_SECONDS = 12.0
# Weighted rate-limit headroom for both layers (agent budget + coordinator
# backstop, flags.md §13.2).
RATE_LIMIT_BUDGET = 50


def _restart_flag_service_probing_writes(stack, flag_service) -> list[int]:
    """Restart the flag service; write the probe flag every 200 ms until it is back.

    Returns the status of each outage write (0 = could not connect). A 200 is
    only allowed with an open sync stream (A7).
    """
    restart = threading.Thread(target=stack.flag_plane.restart_flag_service, daemon=True)
    restart.start()
    statuses = []
    while restart.is_alive():
        try:
            status, _ = flag_service.request(
                "PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": "outage", "writer": "service-test"}
            )
        except OSError:
            status = 0
        statuses.append(status)
        if status == 200:
            try:
                sync_streams = flag_service.state()["sync_streams"]
            except OSError:
                # #1612: the old process accepted the write and then stopped before
                # the state read; only the old process can vanish here (the
                # restarted one keeps running), and it had flagd's stream.
                sync_streams = None
            assert sync_streams is None or sync_streams >= 1, "a write was accepted without a sync stream"
        time.sleep(OUTAGE_POLL_SECONDS)
    restart.join()
    assert stack.flag_plane.flag_service.running, "the flag service did not come back"
    return statuses


def _wait_until_served(stack, flag_service) -> None:
    """A probe write reaches the in-process resolver (the path every service reads)."""
    nonce = uuid.uuid4().hex
    assert poll_until(
        lambda: (
            flag_service.request("PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": nonce, "writer": "service-test"})[0]
            == 200
        ),
        RECOVERY_SECONDS,
    ), "the flag service refused the probe write after the restart"
    with flagd_probe_client(stack, PROBE_SET, resolver="in_process") as probe:
        assert poll_until(lambda: probe.get_string_value(PROBE_FLAG, "", None) == nonce, RECOVERY_SECONDS), (
            "flagd did not serve the probe write after the restart"
        )


@pytest.mark.uc("UC-053", flow="A9", cond=["Pre-1"], tc="TC-019")
@pytest.mark.uc("UC-053", flow="A2", br=["BR-004"], tc="TC-019")
@pytest.mark.nfr("NFR-052")
def test_restart_keeps_admin_settings_and_drops_the_rest(fresh_stack, flag_service):
    stack = fresh_stack
    plain = EvaluationContext(attributes={"game_kind": "real"})
    werewolf = EvaluationContext(attributes={"game_kind": "real", "game_mode": "Werewolf"})
    game_id = f"game_{uuid.uuid4().hex[:12]}"

    # Step 1: the host's persisted setting, a stage overlay and a live game.
    flag_service.put("game", "sensitivity", variant="slow", persist=True, writer="menu")
    flag_service.put("agent", "decision.backend", variant="claude", writer="stage")
    flag_service._ok(
        "PUT",
        f"/games/{game_id}",
        {"coordinator_instance": "service-restart", "game_kind": "shadow", "serials": ["MOCK9001"]},
    )
    epoch_before = flag_service.state()["epoch"]

    with flagd_probe_client(stack, "game", resolver="rpc") as game:
        # Step 2: flagd serves the admin value; the Werewolf rule still wins.
        assert poll_until(lambda: game.get_integer_value("sensitivity", -1, plain) == 1), (
            "flagd did not serve the persisted sensitivity=slow"
        )
        assert game.get_integer_value("sensitivity", -1, werewolf) == 3, "the Werewolf rule lost"

        # Step 3: restart the flag service; writes during the outage are never 200
        # without a sync stream.
        started = time.monotonic()
        outage_statuses = _restart_flag_service_probing_writes(stack, flag_service)

        # Step 4: a write before flagd reconnects is refused with 503, never accepted.
        status, body = flag_service.request(
            "PUT", f"/{PROBE_SET}/{PROBE_FLAG}", {"value": "early", "writer": "service-test"}
        )
        if status != 200:
            assert (status, body.get("error")) == (503, "no_sync_stream"), (status, body)
        stack.flag_plane.restart_flagd()

        # Step 5: the new epoch; the admin value is back, the overlay and the game are gone.
        state = flag_service.state()
        assert state["epoch"] and state["epoch"] != epoch_before, state
        assert (flag_service.get("game", "sensitivity").get("admin_value") or {}).get("variant") == "slow"
        assert not flag_service.get("agent", "decision.backend").get("stack_overlay")
        assert not any(g["game_id"] == game_id for g in flag_service.games())

        # Step 6: flagd serves the baseline plus the admin value again (NFR-052: 5 s target,
        # bounded here by the flagd restart).
        assert poll_until(lambda: game.get_integer_value("sensitivity", -1, plain) == 1, RECOVERY_SECONDS), (
            "flagd did not serve the persisted value after the restart"
        )
        assert game.get_integer_value("sensitivity", -1, werewolf) == 3
        print(f"\nTC-019 recovery: {time.monotonic() - started:.1f} s, outage statuses {sorted(set(outage_statuses))}")


LEVEL_FLAG, LEVEL_TYPE = "player_sensitivity_factor", "adjust_player_sensitivity"
LEVEL_VALUE = 1.5


async def _write_level_once_and_expect_applied(collector, game_id: str, serial: str):
    """Write a player level once; the coordinator's first decision on it is an apply (#1552).

    The fresh flagd probe in step 3 proves flagd serves ``interventions_allowed=full``,
    not that the coordinator does. After the flagd restart the coordinator's
    per-domain sync streams reconnect one by one after gRPC backoff (seconds), in
    no fixed order. When the ``interventions`` stream delivers this write before
    the ``agent`` stream delivers the allow-list, the coordinator defers it (UC-053
    A9 step 4): it neither applies nor blocks it until the allow-list it holds is
    at least as new, then checks it against that one. So one write is enough
    (TC-019 step 7, no repeat), and the first ``agent_intervention`` event for it
    must be ``blocked=false``: a ``not_allowed`` block means the coordinator
    decided against the stale pre-restart allow-list, the bug #1552 fixed.
    """
    seen = len(collector.get_events("agent_intervention"))
    # Accepted, never 409: the game is registered again (A9 step 3).
    set_player_value(LEVEL_FLAG, game_id, serial, LEVEL_VALUE)
    event = await _next_intervention(collector, LEVEL_TYPE, after=seen, wait_seconds=RECOVERY_SECONDS)
    assert event.data.get("blocked") == "false", dict(event.data)
    return event


async def _next_intervention(collector, type_id: str, *, after: int, wait_seconds: float):
    """The first ``agent_intervention`` of ``type_id`` past the first ``after`` events."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        for event in collector.get_events("agent_intervention")[after:]:
            if event.data.get("type") == type_id:
                return event
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"no new agent_intervention(type={type_id}) within {wait_seconds}s; saw: "
        f"{[dict(e.data) for e in collector.get_events('agent_intervention')]}"
    )


async def _await_running(game_client, game_id: str) -> None:
    async def running() -> bool:
        return any(
            g.game_id == game_id and g.state == game_coordinator_pb2.RUNNING for g in await list_games(game_client)
        )

    assert await async_poll_until(running, timeout=RUNNING_TIMEOUT_SECONDS), (
        f"game {game_id} never reached RUNNING within {RUNNING_TIMEOUT_SECONDS}s"
    )


def _serve_full_permission(stack, flag_service) -> None:
    for flag_key in ("policy.max_interventions_per_minute", "policy.coordinator_backstop_per_minute"):
        flag_service.put("agent", flag_key, value=RATE_LIMIT_BUDGET)
    flag_service.put("agent", "interventions_allowed", variant="full")
    expected = variant_value("agent", "interventions_allowed", "full")
    with flagd_probe_client(stack, "agent") as agent_flags:
        assert poll_until(
            lambda: agent_flags.get_string_value("interventions_allowed", "", EvaluationContext()) == expected,
            RECOVERY_SECONDS,
        ), "flagd did not serve interventions_allowed=full after the restart"


# How long the coordinator may take to re-register after the service restarted:
# it checks the boot epoch every 2 s and reconciles every 15 s regardless.
REREGISTER_SECONDS = 20.0
_TAG = "tc019-midgame"


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_restart_mid_game_reregisters_the_live_game(fresh_stack, flag_service, headless_cleanup):
    """A9 step 3: after a restart the live game is registered again, and a
    game-scoped write (what the agent sends) is accepted and applied on the
    first write, never blocked against the pre-restart allow-list (A9 step 4)."""
    stack = fresh_stack
    game_client, game_channel = await get_game_client(stack)
    headless_cleanup.add_tag(_TAG)
    collector = None
    try:
        serials = await setup_mock_controllers(stack, count=4, reserved=True, tag=_TAG)
        game_id, collector = await start_game_headless(
            game_client, build_start_config("JoustFFA", serials), timeout=25.0
        )
        headless_cleanup.add_game(game_id)
        await _await_running(game_client, game_id)
        flag_service.wait_game_registered(game_id)

        # Step 1: restart the flag service (and flagd) mid-game; the game scope is gone.
        await asyncio.to_thread(stack.flag_plane.restart_flag_service)
        await asyncio.to_thread(stack.flag_plane.restart_flagd)
        await asyncio.to_thread(_wait_until_served, stack, flag_service)

        # Step 2: the coordinator re-registers the game on the new epoch.
        assert await async_poll_until(
            lambda: any(g["game_id"] == game_id for g in flag_service.games()),
            timeout=REREGISTER_SECONDS,
        ), f"the coordinator did not re-register {game_id} after the restart"

        # Step 3: the restart dropped every overlay; set the agent switches again.
        await asyncio.to_thread(_serve_full_permission, stack, flag_service)

        # Step 4: a game-scoped write is accepted (no 409) and the game applies it.
        await _write_level_once_and_expect_applied(collector, game_id, serials[0])
    finally:
        if collector is not None:
            await collector.stop()
        await game_channel.close()
