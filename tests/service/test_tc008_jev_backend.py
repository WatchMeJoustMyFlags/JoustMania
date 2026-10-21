"""TC-008 with the Jev backend on the service tier (T2): switch to jev and route by confidence (#1404).

AGENT_JEV composition (the AGENT composition plus a fake Jev, ``tests/service/harness/fakes/jev.py``,
behind the ``fake`` variant of ``decision.jev.base_url`` and the agent's dummy ``JEV_API_KEY``), one
``fresh_stack`` per case. Same journey as ``test_tc008_remote_backend_routing.py`` (act, held,
broken answer -> rules fallback), through Jev's two-stage round (the gate noul + intervention
choice, then the details stage; decide-contract section 2 / 3.1). The fake's ``confidence`` is
the gate's p_yes, which is the acting confidence of the round; jev's act threshold is its own
(``decision.jev.act_confidence_threshold``, default 0.6), so 0.55 is a valid intervention
answer below it.

The Claude decide backend cannot be pointed at a fake from here: the agent does not pass a base
URL to it (``decide_backends.go``), so only its missing key (UC-044 A1) is covered, in
``test_tc008_remote_backend_routing.py``.
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
from tests.service.harness import AGENT_JEV

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT_JEV

JEV_HELD_CONFIDENCE = 0.55  # a valid intervention answer below jev's act threshold (0.6)
JEV_ACT_CONFIDENCE = 0.7  # acts only under jev's own threshold (0.6), would be held under the global one (0.8)
JEV_THRESHOLD = 0.6
JEV_MODEL = "jev-latest"  # the shipped decision.jev.model default
JEV_CHAT_SPAN_PREFIX = "chat "


def _configure(stack) -> None:
    configure_agent(backend="jev", jev_fake=True)
    wait_agent_flags_served(
        stack, backend="jev", enabled=True, rewrite=lambda: configure_agent(backend="jev", jev_fake=True)
    )


async def _wait_first_call(stack) -> list[dict]:
    fake = stack.fake_jev
    ok = await async_poll_until(lambda: bool(fake.requests()), FIRST_CALL_TIMEOUT, interval=0.5)
    assert ok, f"the agent never called fake-jev within {FIRST_CALL_TIMEOUT}s: {fake.log_tail()}"
    return fake.requests()


def _jev_chats(stack, span):
    return [s for s in children(stack, span) if s.name.startswith(JEV_CHAT_SPAN_PREFIX)]


async def _act_round(stack, confidence: float) -> None:
    """Script the fake at ``confidence`` and assert the whole act journey: the key and model on the
    wire, the two-stage round, the applied intervention and the agent.decide span (backend jev,
    jev's own act threshold)."""
    stack.fake_jev.script("valid", intervention=SCRIPTED_INTERVENTION, confidence=confidence)
    _configure(stack)

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            calls = await _wait_first_call(stack)
            assert all(c["key_ok"] for c in calls), f"the agent did not send JEV_API_KEY as a bearer token: {calls}"
            assert calls[0]["stage"] == "intervention", calls[0]
            assert calls[0]["model"] == JEV_MODEL, calls[0]
            assert SCRIPTED_INTERVENTION in calls[0]["options"]["ffa.intervention"], calls[0]

            assert await async_poll_until(
                lambda: any(e.get("blocked") == "false" for e in intervention_events(collector)), APPLY_TIMEOUT
            ), f"no applied {SCRIPTED_INTERVENTION} on the game stream; saw {intervention_events(collector)}"
            assert any(c["stage"] == "details" for c in stack.fake_jev.requests()), "no details stage call"

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "act", "decide.outcome=act", within=SPAN_TIMEOUT
            )
            tags = span.attributes
            assert tags.get("decide.backend") == "jev", tags
            assert tags.get("decide.backend.requested") == "jev", tags
            assert tags.get("decide.fallback_reason", "") == "", tags
            assert abs(float(tags.get("decide.confidence")) - confidence) < 1e-9, tags
            # jev's own threshold decided, not the global 0.8 (decision.jev.act_confidence_threshold).
            assert abs(float(tags.get("decide.act_confidence_threshold")) - JEV_THRESHOLD) < 1e-9, tags
            assert tags.get("decide.act_confidence_threshold.source") == "jev", tags
            assert _jev_chats(stack, span), (
                f"no jev chat span under agent.decide: {[s.name for s in children(stack, span)]}"
            )
            assert children(stack, span, "agent.decision"), "the act must route through agent.decision"
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", cond=["Post-S-1"], tc="TC-008")
@pytest.mark.uc("UC-040", flow="A13", tc="TC-008")
@pytest.mark.uc("UC-044", flow="main", steps=[3, 4, 5], tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_jev_high_confidence_acts(fresh_stack):
    """Jev, gate p_yes 0.95 -> decide.outcome=act: the two-stage round reaches the fake with the
    agent's key and the jev model, the intervention is written and applied by the coordinator,
    and the agent.decide span names jev as the backend that decided."""
    await _act_round(fresh_stack, HIGH_CONFIDENCE)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A13", tc="TC-008")
@pytest.mark.uc("UC-044", flow="main", steps=[4], tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_jev_acts_under_its_own_threshold(fresh_stack):
    """Jev, gate p_yes 0.7: above jev's act threshold (0.6) but below the global one (0.8) ->
    decide.outcome=act with decide.act_confidence_threshold=0.6, source jev. Fails if the agent
    ignores decision.jev.act_confidence_threshold and uses the global threshold."""
    await _act_round(fresh_stack, JEV_ACT_CONFIDENCE)


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A5", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_jev_below_threshold_is_held(fresh_stack):
    """Jev, gate p_yes 0.55 (below jev's 0.6 act threshold) -> decide.outcome=held: no
    agent.decision, nothing written, nothing applied."""
    stack = fresh_stack
    stack.fake_jev.script("valid", intervention=SCRIPTED_INTERVENTION, confidence=JEV_HELD_CONFIDENCE)
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
            assert tags.get("decide.backend") == "jev", tags
            assert abs(float(tags.get("decide.confidence")) - JEV_HELD_CONFIDENCE) < 1e-9, tags
            assert not children(stack, span, "agent.decision"), "a held answer must not reach runDecision"

            await assert_nothing_applied(collector, game_id)
            outcomes = {
                s.attributes.get("decide.outcome")
                for s in decide_spans(stack, game_id)
                if s.attributes.get("decide.backend") == "jev"
            }
            assert outcomes == {"held"}, outcomes
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A2", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_jev_without_answers_falls_back_to_rules(fresh_stack):
    """Jev answers a 200 with no answers -> the rules engine decides instead:
    decide.outcome=fallback, decide.backend=rules, decide.fallback_reason=invalid_answer."""
    stack = fresh_stack
    stack.fake_jev.script("garbage")
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
            assert tags.get("decide.backend.requested") == "jev", tags
            assert _jev_chats(stack, span), "the failed call still records its chat span"
    finally:
        await channel.close()
