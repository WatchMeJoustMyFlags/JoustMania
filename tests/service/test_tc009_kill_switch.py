"""TC-009 on the service tier (T2): kill switch during an FFA game (#1600, S7 of #1422).

AGENT composition on ``fresh_stack`` (``enabled`` is a global flag): real menu,
game coordinator and controller manager from source at HEAD, the Go agent built
at HEAD, a fresh flag plane, FakeAudio, the OtlpSink (the agent's spans) and the
fake OpenAI backend holding its answer until released.

Steps 1 to 6 moved 1:1 from the full-stack
``tests/integration/test_decide_backend_e2e.py::test_kill_switch_discards_inflight_answer``;
step 7 (turn the agent on again, the next cycle records ``agent.decide`` again) is new.
"""

import time

import pytest

from tests.integration.helpers import GameEventCollector, async_poll_until, get_game_client
from tests.service.agent_flow import (
    ABSENCE_WINDOW,
    APPLY_TIMEOUT,
    DECIDE_TIMEOUT_MS,
    FIRST_CALL_TIMEOUT,
    HIGH_CONFIDENCE,
    SCRIPTED_INTERVENTION,
    SPAN_TIMEOUT,
    agent_spans,
    assert_nothing_applied,
    configure_agent,
    decide_spans,
    intervention_events,
    scripted_flag_value,
    set_enabled,
    start_ffa,
    wait_agent_flags_served,
    wait_decide_span,
)
from tests.service.harness import AGENT

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT

# The fake holds the answer until released, at most this long (below DECIDE_TIMEOUT_MS
# and the agent's 30 s HTTP backstop: our release, never a timeout, ends the call).
KILL_HOLD_MAX_SECONDS = 28
# From "the fake holds the call": flag write -> flagd serves enabled=false -> an
# agent.disabled span for this game proves the agent's own flag read sees it.
KILL_SIGNAL_TIMEOUT = 15.0
KILL_RELEASE_HEADROOM = 3.0


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A6", tc="TC-009")
@pytest.mark.uc("UC-045", flow="main", steps=[2, 3, 4, 7], tc="TC-009")
@pytest.mark.usefixtures("flag_service")
async def test_kill_switch_discards_inflight_answer(fresh_stack):
    """The kill switch flipped while the remote call is in flight: the late
    high-confidence answer is discarded (decide.discarded_reason=kill_switch) and
    nothing is applied; turned on again, the agent decides again."""
    stack = fresh_stack
    fake = stack.fake_openai
    # Step 1: the fake holds a high-confidence act.
    fake.script(
        "hold", intervention=SCRIPTED_INTERVENTION, confidence=HIGH_CONFIDENCE, hold_max_seconds=KILL_HOLD_MAX_SECONDS
    )
    configure_agent(backend="openai")
    wait_agent_flags_served(stack, backend="openai", enabled=True, rewrite=lambda: configure_agent(backend="openai"))

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            # Step 2.
            game_id, _ = await start_ffa(stack, collector)
            # Step 3: the agent's call is in flight, the fake is holding it.
            assert await async_poll_until(
                lambda: any(r.get("held") for r in fake.requests(game_id)), FIRST_CALL_TIMEOUT, interval=0.5
            ), f"no held call for {game_id}: {fake.requests(game_id)}"
            held_at = time.monotonic()

            # Step 4: turn the agent off.
            disabled_before = {s.span_id for s in agent_spans(stack, "agent.disabled", game_id)}
            set_enabled(False)
            wait_agent_flags_served(stack, backend="openai", enabled=False, rewrite=lambda: set_enabled(False))
            # Step 6 / positive signal that the AGENT (not just flagd) reads enabled=false:
            # its kill-switch path emits agent.disabled for this game on the next cycle.
            # Only then release the held answer, which re-reads the live flags on landing.
            assert await async_poll_until(
                lambda: any(s.span_id not in disabled_before for s in agent_spans(stack, "agent.disabled", game_id)),
                KILL_SIGNAL_TIMEOUT,
                interval=0.5,
            ), f"no agent.disabled span for {game_id} within {KILL_SIGNAL_TIMEOUT}s of the kill-switch flip"
            elapsed = time.monotonic() - held_at
            assert elapsed < DECIDE_TIMEOUT_MS / 1000 - KILL_RELEASE_HEADROOM, (
                f"kill-switch sequence took {elapsed:.1f}s: the held call would time out before the "
                f"release (DECIDE_TIMEOUT_MS={DECIDE_TIMEOUT_MS}); budgets too tight"
            )
            fake.release()

            # Step 5: the late answer is discarded.
            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "discarded", "decide.outcome=discarded"
            )
            tags = span.attributes
            assert tags.get("decide.discarded_reason") == "kill_switch", tags
            assert tags.get("decide.backend") == "openai", tags
            # The answer itself was a valid high-confidence act: the switch stopped it.
            assert SCRIPTED_INTERVENTION in tags.get("decide.answers", ""), tags
            assert abs(float(tags.get("decide.confidence")) - HIGH_CONFIDENCE) < 1e-9, tags

            await assert_nothing_applied(collector, game_id)
            outcomes = {s.attributes.get("decide.outcome") for s in decide_spans(stack, game_id)}
            assert "act" not in outcomes, outcomes

            # Step 7: turn the agent on; the next cycle records agent.decide spans again.
            decided_before = {s.span_id for s in decide_spans(stack, game_id)}
            set_enabled(True)
            wait_agent_flags_served(stack, backend="openai", enabled=True, rewrite=lambda: set_enabled(True))
            assert await async_poll_until(
                lambda: any(s.span_id not in decided_before for s in decide_spans(stack, game_id)),
                SPAN_TIMEOUT,
                interval=0.5,
            ), f"no agent.decide span for {game_id} after the agent was turned on again"
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-045", flow="main", steps=[2, 3], cond=["Pre-1", "Post-S-1"])
@pytest.mark.usefixtures("flag_service")
async def test_kill_switch_keeps_written_state_intervention(fresh_stack):
    """UC-045 Post-S-1: the agent acts once (a state intervention written for the live
    game), then the kill switch turns it off. Nothing new is applied, and the value the
    agent already wrote for the game stays in place while the game runs (it is only
    dropped with the game's scope, UC-053 BR-003)."""
    stack = fresh_stack
    stack.fake_openai.script("valid", intervention=SCRIPTED_INTERVENTION, confidence=HIGH_CONFIDENCE)
    configure_agent(backend="openai")
    wait_agent_flags_served(stack, backend="openai", enabled=True, rewrite=lambda: configure_agent(backend="openai"))

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            # Pre-1: the agent runs and acts on the live game.
            game_id, _ = await start_ffa(stack, collector)
            assert await async_poll_until(lambda: scripted_flag_value(game_id) is not None, APPLY_TIMEOUT), (
                f"the agent wrote no {SCRIPTED_INTERVENTION} for {game_id}; saw {intervention_events(collector)}"
            )
            written = scripted_flag_value(game_id)

            # Steps 1 to 3: turn the agent off; its next cycle records agent.disabled.
            disabled_before = {s.span_id for s in agent_spans(stack, "agent.disabled", game_id)}
            set_enabled(False)
            wait_agent_flags_served(stack, backend="openai", enabled=False, rewrite=lambda: set_enabled(False))
            assert await async_poll_until(
                lambda: any(s.span_id not in disabled_before for s in agent_spans(stack, "agent.disabled", game_id)),
                KILL_SIGNAL_TIMEOUT,
                interval=0.5,
            ), f"no agent.disabled span for {game_id} within {KILL_SIGNAL_TIMEOUT}s of the kill-switch flip"
            applied_before = len(intervention_events(collector))

            # Post-S-1: no new effect reaches the game, and the state intervention the
            # agent already wrote stays for the running game.
            def violation() -> bool:
                return scripted_flag_value(game_id) != written or len(intervention_events(collector)) > applied_before

            assert not await async_poll_until(violation, ABSENCE_WINDOW), (
                f"after the kill switch {SCRIPTED_INTERVENTION} for {game_id} changed: "
                f"value {scripted_flag_value(game_id)!r} (was {written!r}), "
                f"events {intervention_events(collector)[applied_before:]}"
            )
    finally:
        await channel.close()
