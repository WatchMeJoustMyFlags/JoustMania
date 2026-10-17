"""
TC-019: the flag service restarts (UC-053 A9, NFR-052; #1413).

A planned restart (`scripts/flags.sh restart`: the flag service, then flagd)
keeps what the host persisted and drops everything else:

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
write is accepted and applied again without waiting for the game to end
(UC-053 A9 step 3).

The tests restart two containers of the session stack, so they wait until every
flag set resolves again before they return.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.flag_service import set_player_value  # noqa: E402
from tests.integration.helpers import (  # noqa: E402
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    get_game_client,
    get_mock_client,
    poll_until,
    setup_mock_controllers,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    SUITE_RATE_LIMIT_BUDGET,
    intervention_env,  # binds the flag-service client, pins the rate-limit budget
    set_interventions_allowed,
    set_policy_budget,
)
from tests.integration.test_intervention_smoke import _teardown  # noqa: E402
from tests.integration.test_parallel_intervention_flow import (  # noqa: E402
    _await_running,
    _start_headless_with_retry,
    _wait_for_intervention,
)

__all__ = ["intervention_env"]

FLAG_SERVICE_CONTAINER = "joustmania-flag-service"
FLAGD_CONTAINER = "joustmania-flagd"
PROBE_SET, PROBE_FLAG = "observability", "flagservice.propagation_probe"
RECOVERY_SECONDS = 60.0


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def _healthy() -> bool:
    try:
        return (
            _docker("inspect", "-f", "{{.State.Health.Status}}", FLAG_SERVICE_CONTAINER)
            == "healthy"
        )
    except subprocess.CalledProcessError:
        return False


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.uc("UC-053", flow="A7", tc="TC-019")
@pytest.mark.uc("UC-053", flow="A2", br=["BR-004"], tc="TC-019")
@pytest.mark.nfr("NFR-052")
def test_restart_keeps_admin_settings_and_drops_the_rest(docker_compose, flag_service):
    from openfeature.evaluation_context import EvaluationContext

    plain = EvaluationContext(attributes={"game_kind": "real"})
    werewolf = EvaluationContext(
        attributes={"game_kind": "real", "game_mode": "Werewolf"}
    )
    game_id = f"game_{uuid.uuid4().hex[:12]}"
    try:
        # Step 1: the host's persisted setting, a stage overlay and a live game.
        flag_service.put(
            "game", "sensitivity", variant="slow", persist=True, writer="menu"
        )
        flag_service.put("agent", "decision.backend", variant="claude", writer="stage")
        flag_service._ok(
            "PUT",
            f"/games/{game_id}",
            {
                "coordinator_instance": "itest-restart",
                "game_kind": "shadow",
                "serials": ["MOCK9001"],
            },
        )
        epoch_before = flag_service.state()["epoch"]

        with flagd_probe_client(docker_compose, "game", resolver="rpc") as game:
            # Step 2: flagd serves the admin value; the Werewolf rule still wins.
            assert poll_until(
                lambda: game.get_integer_value("sensitivity", -1, plain) == 1
            ), "flagd did not serve the persisted sensitivity=slow"
            assert game.get_integer_value("sensitivity", -1, werewolf) == 3, (
                "the Werewolf rule lost"
            )

            # Step 3: restart the flag service; writes during the outage are never 200
            # without a sync stream.
            started = time.monotonic()
            _docker("restart", FLAG_SERVICE_CONTAINER)
            outage_statuses = []
            while not _healthy() and time.monotonic() - started < RECOVERY_SECONDS:
                try:
                    status, body = flag_service.request(
                        "PUT",
                        f"/{PROBE_SET}/{PROBE_FLAG}",
                        {"value": "outage", "writer": "integration-test"},
                    )
                except OSError:
                    status, body = 0, {}
                outage_statuses.append(status)
                if status == 200:
                    assert flag_service.state()["sync_streams"] >= 1, (
                        "a write was accepted without a sync stream"
                    )
                time.sleep(0.2)
            assert _healthy(), "the flag service did not become healthy"
            _docker("restart", FLAGD_CONTAINER)

            # Step 4: a write before flagd reconnects is refused with 503, never accepted.
            status, body = flag_service.request(
                "PUT",
                f"/{PROBE_SET}/{PROBE_FLAG}",
                {"value": "early", "writer": "integration-test"},
            )
            if status != 200:
                assert (status, body.get("error")) == (503, "no_sync_stream"), (
                    status,
                    body,
                )

            # Step 5: the new epoch; the admin value is back, the overlay and the game are gone.
            state = flag_service.state()
            assert state["epoch"] and state["epoch"] != epoch_before, state
            assert (
                flag_service.get("game", "sensitivity").get("admin_value") or {}
            ).get("variant") == "slow"
            assert not flag_service.get("agent", "decision.backend").get(
                "stack_overlay"
            )
            assert not any(g["game_id"] == game_id for g in flag_service.games())

            # Step 6: flagd serves the baseline plus the admin value again (NFR-052: 5 s target,
            # bounded here by the flagd restart).
            assert poll_until(
                lambda: game.get_integer_value("sensitivity", -1, plain) == 1,
                RECOVERY_SECONDS,
            ), "flagd did not serve the persisted value after the restart"
            assert game.get_integer_value("sensitivity", -1, werewolf) == 3
            print(
                f"\nTC-019 recovery: {time.monotonic() - started:.1f} s, outage statuses {sorted(set(outage_statuses))}"
            )
    finally:
        flag_service.request("DELETE", "/game/sensitivity?persist=true")
        flag_service.request("DELETE", f"/games/{game_id}")
        _wait_until_served(docker_compose, flag_service)


def _restart_flag_service_and_flagd() -> None:
    started = time.monotonic()
    _docker("restart", FLAG_SERVICE_CONTAINER)
    while not _healthy() and time.monotonic() - started < RECOVERY_SECONDS:
        time.sleep(0.2)
    assert _healthy(), "the flag service did not become healthy"
    _docker("restart", FLAGD_CONTAINER)


def _wait_until_served(docker_compose, flag_service) -> None:
    """The rest of the suite needs every set again: wait for a probe write to be served."""
    nonce = uuid.uuid4().hex
    deadline = time.monotonic() + RECOVERY_SECONDS
    while time.monotonic() < deadline:
        if (
            flag_service.request(
                "PUT",
                f"/{PROBE_SET}/{PROBE_FLAG}",
                {"value": nonce, "writer": "integration-test"},
            )[0]
            == 200
        ):
            break
        time.sleep(0.5)
    with flagd_probe_client(docker_compose, PROBE_SET, resolver="in_process") as probe:
        assert poll_until(
            lambda: probe.get_string_value(PROBE_FLAG, "", None) == nonce,
            RECOVERY_SECONDS,
        ), "flagd did not recover after the restart test"
    flag_service.request("DELETE", f"/{PROBE_SET}/{PROBE_FLAG}")


# How long the coordinator may take to re-register after the service restarted:
# it checks the boot epoch every 2 s and reconciles every 15 s regardless.
REREGISTER_SECONDS = 20.0
_TAG = "tc019-midgame"


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
@pytest.mark.asyncio
async def test_restart_mid_game_reregisters_the_live_game(
    intervention_env, docker_compose, flag_service, headless_cleanup
):
    """A9 step 3: after a restart the live game is registered again, and a
    game-scoped write (what the agent sends) is accepted and applied."""
    from openfeature.evaluation_context import EvaluationContext

    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    headless_cleanup.add_tag(_TAG)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=_TAG
        )
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        headless_cleanup.add_game(game_id)
        await _await_running(game_client, game_id)
        flag_service.wait_game_registered(game_id)

        # Step 1: restart the flag service (and flagd) mid-game; the game scope is gone.
        await asyncio.to_thread(_restart_flag_service_and_flagd)
        await asyncio.to_thread(_wait_until_served, docker_compose, flag_service)

        # Step 2: the coordinator re-registers the game on the new epoch.
        assert await async_poll_until(
            lambda: any(g["game_id"] == game_id for g in flag_service.games()),
            timeout=REREGISTER_SECONDS,
        ), f"the coordinator did not re-register {game_id} after the restart"

        # Step 3: the restart dropped every overlay; set the agent switches again.
        set_policy_budget(SUITE_RATE_LIMIT_BUDGET)
        set_interventions_allowed("full")
        with flagd_probe_client(docker_compose, "agent") as agent_flags:
            assert poll_until(
                lambda: (
                    agent_flags.get_string_value(
                        "interventions_allowed", "", EvaluationContext()
                    )
                    == flag_service.get("agent", "interventions_allowed")[
                        "variant_values"
                    ]["full"]
                ),
                RECOVERY_SECONDS,
            ), "flagd did not serve interventions_allowed=full after the restart"

        # Step 4: a game-scoped write is accepted (no 409) and the game applies it.
        set_player_value("player_sensitivity_factor", game_id, serials[0], 1.5)
        await _wait_for_intervention(
            collector, "adjust_player_sensitivity", blocked="false"
        )
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            _TAG,
            mock_channel,
            game_channel,
        )
        await asyncio.to_thread(_wait_until_served, docker_compose, flag_service)
