"""TC-008 with the Claude decide backend on the service tier (T2): route by confidence (#1900).

AGENT_CLAUDE_DECIDE composition (the AGENT composition plus a fake Claude,
``tests/service/harness/fakes/claude.py``, behind the ``fake`` variant of ``decision.claude.base_url``
and the agent's dummy ``ANTHROPIC_API_KEY``), one ``fresh_stack`` per case. Same journey as
``test_tc008_remote_backend_routing.py`` (act, held, broken answer -> rules fallback). The fake only
answers ``POST /v1/messages``, so a base URL with a stray ``/v1`` (the SDK appends the path) is a 404
and the case fails. The text backend (retro, proposer) stays on the stub in this composition.
"""

import pytest

from tests.integration.helpers import GameEventCollector, async_poll_until, get_game_client
from tests.service.agent_flow import (
    APPLY_TIMEOUT,
    FIRST_CALL_TIMEOUT,
    HIGH_CONFIDENCE,
    SCRIPTED_INTERVENTION,
    SPAN_TIMEOUT,
    assert_nothing_applied,
    children,
    configure_agent,
    decide_spans,
    intervention_events,
    start_ffa,
    wait_agent_flags_served,
    wait_decide_span,
)
from tests.service.harness import AGENT_CLAUDE_DECIDE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT_CLAUDE_DECIDE

CLAUDE_HELD_CONFIDENCE = 0.5  # a valid intervention answer below the global act threshold (0.8)
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # the shipped decision.claude.model default
CLAUDE_CHAT_SPAN_PREFIX = "chat "


def _configure(stack) -> None:
    configure_agent(backend="claude", claude_fake=True)
    wait_agent_flags_served(
        stack, backend="claude", enabled=True, rewrite=lambda: configure_agent(backend="claude", claude_fake=True)
    )


async def _wait_first_call(stack) -> list[dict]:
    fake = stack.fake_claude
    ok = await async_poll_until(lambda: bool(fake.requests("decide")), FIRST_CALL_TIMEOUT, interval=0.5)
    assert ok, f"the agent never called fake-claude within {FIRST_CALL_TIMEOUT}s: {fake.log_tail()}"
    return fake.requests("decide")


def _claude_chats(stack, span):
    return [s for s in children(stack, span) if s.name.startswith(CLAUDE_CHAT_SPAN_PREFIX)]


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", cond=["Post-S-1"], tc="TC-008")
@pytest.mark.uc("UC-044", flow="main", steps=[3, 4, 5], tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_claude_high_confidence_acts(fresh_stack):
    """Claude, confidence 0.95 -> decide.outcome=act: the call reaches the fake on /v1/messages with
    the agent's key and the claude model, the intervention is written and applied by the coordinator,
    and the agent.decide span names claude as the backend that decided."""
    stack = fresh_stack
    stack.fake_claude.script(decide_mode="valid", intervention=SCRIPTED_INTERVENTION, confidence=HIGH_CONFIDENCE)
    _configure(stack)

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            calls = await _wait_first_call(stack)
            assert all(c["key_ok"] for c in calls), f"the agent did not send ANTHROPIC_API_KEY as x-api-key: {calls}"
            assert calls[0]["model"] == CLAUDE_MODEL, calls[0]
            assert not stack.fake_claude.requests("retro"), "the text backend must stay on the stub"

            assert await async_poll_until(
                lambda: any(e.get("blocked") == "false" for e in intervention_events(collector)), APPLY_TIMEOUT
            ), f"no applied {SCRIPTED_INTERVENTION} on the game stream; saw {intervention_events(collector)}"

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "act", "decide.outcome=act", within=SPAN_TIMEOUT
            )
            tags = span.attributes
            assert tags.get("decide.backend") == "claude", tags
            assert tags.get("decide.backend.requested") == "claude", tags
            assert tags.get("decide.fallback_reason", "") == "", tags
            assert abs(float(tags.get("decide.confidence")) - HIGH_CONFIDENCE) < 1e-9, tags
            assert _claude_chats(stack, span), (
                f"no claude chat span under agent.decide: {[s.name for s in children(stack, span)]}"
            )
            assert children(stack, span, "agent.decision"), "the act must route through agent.decision"
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A5", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_claude_below_threshold_is_held(fresh_stack):
    """Claude, confidence 0.5 (below the global 0.8) -> decide.outcome=held: no agent.decision,
    nothing written, nothing applied."""
    stack = fresh_stack
    stack.fake_claude.script(decide_mode="valid", intervention=SCRIPTED_INTERVENTION, confidence=CLAUDE_HELD_CONFIDENCE)
    _configure(stack)

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            await _wait_first_call(stack)

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "held", "decide.outcome=held"
            )
            tags = span.attributes
            assert tags.get("decide.backend") == "claude", tags
            assert abs(float(tags.get("decide.confidence")) - CLAUDE_HELD_CONFIDENCE) < 1e-9, tags
            assert not children(stack, span, "agent.decision"), "a held answer must not reach runDecision"

            await assert_nothing_applied(collector, game_id)
            outcomes = {
                s.attributes.get("decide.outcome")
                for s in decide_spans(stack, game_id)
                if s.attributes.get("decide.backend") == "claude"
            }
            assert outcomes == {"held"}, outcomes
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A2", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_claude_garbage_falls_back_to_rules(fresh_stack):
    """Claude answers a 200 with text that is not JSON -> the rules engine decides instead:
    decide.outcome=fallback, decide.backend=rules, decide.fallback_reason=invalid_answer."""
    stack = fresh_stack
    stack.fake_claude.script(decide_mode="garbage")
    _configure(stack)

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            await _wait_first_call(stack)

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "fallback", "decide.outcome=fallback"
            )
            tags = span.attributes
            assert tags.get("decide.fallback_reason") == "invalid_answer", tags
            assert tags.get("decide.backend") == "rules", tags
            assert tags.get("decide.backend.requested") == "claude", tags
            assert _claude_chats(stack, span), "the failed call still records its chat span"
    finally:
        await channel.close()
