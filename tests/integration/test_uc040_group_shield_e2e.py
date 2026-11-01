"""
UC-040 A47 / A48: a group shield event against a shadow game, end to end (#2467 PR 3, PR 4).

The real stack on reserved mock controllers: a headless (shadow) FFA game with three
players, the group-grant gate open through flag-service overlays (``enabled`` on,
``max_per_game`` 1), and ``group_shield`` events written through the flag service's
HTTP API exactly like the agent's writer (services/agent/actions/writer.go).

What this proves:

- the flag service accepts the event row (``serial_set``, ``float 1 10``) and refuses a
  one-serial set;
- the global allow-list stays the outer gate: under ``full`` (no group lever) the event
  is dispatched once as ``grant_group_shield`` and blocked ``not_allowed``, nobody is
  shielded and the per-game group count does not move (UC-040 A48 step 4, A22);
- under the opt-in ``shadow_group`` variant (#2467 PR 4) the next event applies: one
  unblocked ``agent_intervention`` and one ``player_shielded`` per player of the set
  (UC-040 A47). The CI game set resolves ``shield.activation`` to ``instant``.

``interventions_allowed`` and the rate limiter are global, so this test runs alone.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from openfeature.evaluation_context import EvaluationContext

from tests.integration.flag_service import FlagServiceError, append_event
from tests.integration.helpers import (
    APPLY_TIMEOUT_SECONDS,
    RELOAD_SETTLE_SECONDS,
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    get_game_client,
    intervention_env,  # binds the flag-service client, pins the rate-limit budget
    poll_until,
    set_interventions_allowed,
    setup_mock_controllers,
    start_game_headless,
    wait_until_running,
)

__all__ = ["intervention_env"]

TAG = "uc040-group-shield"


def _group_interventions(collector, game_id: str) -> list:
    return [
        e
        for e in collector.get_events("agent_intervention")
        if e.game_id == game_id and e.data.get("type") == "grant_group_shield"
    ]


def _serve_group_gate(docker_compose, flag_service) -> None:
    """Open the group-grant gate for shadow games; wait until flagd serves it."""
    flag_service.put("agent", "policy.group_grant.enabled", variant="on")
    flag_service.put("agent", "policy.group_grant.max_per_game", variant="default")
    set_interventions_allowed("full")  # every coordinator type but the group lever
    with flagd_probe_client(docker_compose, "agent") as client:
        ctx = EvaluationContext()
        assert poll_until(
            lambda: (
                client.get_boolean_value("policy.group_grant.enabled", False, ctx)
                is True
                and client.get_integer_value("policy.group_grant.max_per_game", -1, ctx)
                == 1
                and "grant_group_shield"
                not in client.get_string_value("interventions_allowed", "", ctx)
            )
        ), "flagd did not serve the group-grant overlays"


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
async def test_uc040_group_shield_event_blocked_then_applied(
    intervention_env,
    flag_service,
    docker_compose,
    headless_cleanup,
):
    """Three shadow players: blocked by the allow-list under ``full``, all shielded under ``shadow_group``."""
    _serve_group_gate(docker_compose, flag_service)
    headless_cleanup.add_tag(TAG)
    game_client, game_channel = await get_game_client(docker_compose)
    shadow = None
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=3, reserved=True, tag=TAG
        )
        game_id, shadow = await start_game_headless(
            game_client, build_start_config("JoustFFA", serials), timeout=25.0
        )
        headless_cleanup.add_game(game_id)
        running = await wait_until_running(game_client, {game_id}, timeout=15.0)
        assert game_id in running, f"shadow game {game_id} never reached RUNNING"
        flag_service.wait_game_registered(game_id)

        # The flag service checks the payload (serial_set): one serial is not a group.
        with pytest.raises(FlagServiceError) as refused:
            append_event(
                "group_shield", game_id, {"serials": serials[0], "seconds": "5"}
            )
        assert refused.value.status == 422, refused.value

        append_event(
            "group_shield", game_id, {"serials": ",".join(serials), "seconds": "5"}
        )
        assert await async_poll_until(
            lambda: _group_interventions(shadow, game_id), timeout=APPLY_TIMEOUT_SECONDS
        ), (
            "no grant_group_shield agent_intervention on the shadow game: "
            f"{[dict(e.data) for e in shadow.get_events('agent_intervention')]}"
        )
        await asyncio.sleep(
            RELOAD_SETTLE_SECONDS
        )  # absence window for a second dispatch

        events = _group_interventions(shadow, game_id)
        assert len(events) == 1, (
            f"group event dispatched {len(events)} times: {[dict(e.data) for e in events]}"
        )
        assert events[0].data.get("blocked") == "true"
        assert events[0].data.get("block_reason") == "not_allowed"
        assert events[0].data.get("target") == ""
        shielded = [
            e for e in shadow.get_events("player_shielded") if e.game_id == game_id
        ]
        assert not shielded, (
            f"a blocked group shielded players: {[dict(e.data) for e in shielded]}"
        )

        # A47 (#2467 PR 4): the opt-in group variant lets the next event apply.
        set_interventions_allowed("shadow_group")
        with flagd_probe_client(docker_compose, "agent") as client:
            assert poll_until(
                lambda: (
                    "grant_group_shield"
                    in client.get_string_value(
                        "interventions_allowed", "", EvaluationContext()
                    )
                )
            ), "flagd did not serve interventions_allowed=shadow_group"
        append_event(
            "group_shield", game_id, {"serials": ",".join(serials), "seconds": "5"}
        )
        assert await async_poll_until(
            lambda: len(_group_interventions(shadow, game_id)) >= 2,
            timeout=APPLY_TIMEOUT_SECONDS,
        ), "the second group_shield event never reached the coordinator"
        applied = _group_interventions(shadow, game_id)[1]
        assert applied.data.get("blocked") == "false", dict(applied.data)
        assert await async_poll_until(
            lambda: (
                {
                    e.data.get("serial")
                    for e in shadow.get_events("player_shielded")
                    if e.game_id == game_id
                }
                == set(serials)
            ),
            timeout=APPLY_TIMEOUT_SECONDS,
        ), (
            "not every player of the group was shielded: "
            f"{[dict(e.data) for e in shadow.get_events('player_shielded') if e.game_id == game_id]}"
        )
    finally:
        if shadow is not None:
            await shadow.stop()
        await game_channel.close()
