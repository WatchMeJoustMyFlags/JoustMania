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

The test restarts two containers of the session stack, so it waits until every
flag set resolves again before it returns.
"""

import os
import subprocess
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import flagd_probe_client, poll_until  # noqa: E402

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
        # The rest of the suite needs every set again: wait for a probe write to be served.
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
        with flagd_probe_client(
            docker_compose, PROBE_SET, resolver="in_process"
        ) as probe:
            assert poll_until(
                lambda: probe.get_string_value(PROBE_FLAG, "", None) == nonce,
                RECOVERY_SECONDS,
            ), "flagd did not recover after the restart test"
        flag_service.request("DELETE", f"/{PROBE_SET}/{PROBE_FLAG}")
