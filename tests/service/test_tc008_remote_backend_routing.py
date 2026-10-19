"""TC-008 on the service tier (T2): switch to a remote backend and route by confidence (#1600, S7 of #1422).

AGENT composition, one ``fresh_stack`` per case: real menu, game coordinator and
controller manager (mock backend) from source at HEAD, the Go agent built at
HEAD, a fresh flag plane, FakeAudio, the OtlpSink (Jaeger's role: the agent
exports its spans there, and the sink tees the services' telemetry to the agent
as the collector does) and the scriptable fake OpenAI backend
(``tests/integration/fake_openai``).

Moved 1:1 from the full-stack ``tests/integration/test_decide_backend_e2e.py``
(e2e-agent shard): the same flags, scripts and assertions; the audio-service
``SetVolume:65%`` span is now the SetVolume(0.65) call FakeAudio records.

Each case runs one menu game on a stack of its own: the agent's interventions are
un-targeted, ``enabled`` and ``interventions_allowed`` are global and the LLM
budget is per process, so cases must not share an agent.
"""

import pytest

from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    get_game_client,
    get_mock_client,
    kill_player_verified,
)
from tests.service.agent_flow import (
    ACT_THRESHOLD,
    APPLY_TIMEOUT,
    CHAT_SPAN,
    EXPECTED_VALUE,
    FAKE_MODEL,
    HIGH_CONFIDENCE,
    LOW_CONFIDENCE,
    RULES_DECIDE_TIMEOUT,
    SCRIPTED_FLAG,
    SCRIPTED_INTERVENTION,
    SCRIPTED_MAGNITUDE,
    SPAN_TIMEOUT,
    assert_nothing_applied,
    children,
    configure_agent,
    decide_spans,
    evaluation_events,
    intervention_events,
    parent,
    scripted_flag_value,
    start_ffa,
    wait_agent_flags_served,
    wait_decide_span,
    wait_first_call,
)
from tests.service.harness import AGENT

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT


def _configure(stack, backend: str, **kwargs) -> None:
    configure_agent(backend=backend, **kwargs)
    wait_agent_flags_served(
        stack, backend=backend, enabled=True, rewrite=lambda: configure_agent(backend=backend, **kwargs)
    )


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A1", tc="TC-008")
@pytest.mark.uc("UC-040", flow="A1", tc="TC-007")
@pytest.mark.uc("UC-044", flow="main", steps=[2, 3, 4, 5], tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_rules_backend_default_unchanged(fresh_stack):
    """decision.backend=rules (TC-008 step 8): the rules engine decides, recorded on an
    agent.decide span (backend=rules, requested=rules, no fallback) that is a SIBLING of
    the rules agent.decision under agent.signal_received; the fake is never called."""
    stack = fresh_stack
    stack.fake_openai.script("valid", confidence=HIGH_CONFIDENCE)
    # interventions stay off: this case observes the rules decision, not its effect.
    _configure(stack, "rules", interventions=False)

    game_client, channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, serials = await start_ffa(stack, collector)
            # R1 (endurance): an early elimination in a young session makes the rules
            # engine slow the tempo / cue audio, i.e. decide something.
            await kill_player_verified(mock_client, game_client, serials[0], lambda p: not p.alive)

            span = await wait_decide_span(
                stack,
                game_id,
                lambda t: t.get("decide.backend") == "rules",
                "decide.backend=rules",
                within=RULES_DECIDE_TIMEOUT,
            )
            tags = span.attributes
            assert tags.get("decide.backend.requested") == "rules", tags
            assert tags.get("decide.fallback_reason", "") == "", tags
            assert tags.get("decide.outcome") == "act", tags
            # The coordinator's "primary" label is normalised to "real" (#1249).
            assert tags.get("game.kind") == "real", tags
            # Sync rules path shape: agent.decide and agent.decision share the
            # agent.signal_received parent; no backend chat span.
            assert span.parent_span_id, "agent.decide has no parent on the rules path"
            signal = parent(stack, span)
            assert signal is not None and signal.name == "agent.signal_received", signal
            assert children(stack, signal, "agent.decision"), "no sibling agent.decision"
            assert not children(stack, span), "the rules agent.decide span must have no children"
            assert stack.fake_openai.requests(game_id) == [], "the rules backend must never call the remote backend"
    finally:
        await mock_channel.close()
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", tc="TC-008")
@pytest.mark.uc("UC-040", flow="A26", br=["BR-018"], tc="TC-008")
@pytest.mark.uc("UC-044", flow="main", steps=[2, 3, 4, 5], tc="TC-008")
@pytest.mark.uc("UC-020", flow="A3", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_openai_high_confidence_acts(fresh_stack):
    """High confidence -> decide.outcome=act (TC-008 steps 1 to 3): the agent writes the
    scripted intervention for the game through the flag service and the coordinator
    applies it; the agent.decide span carries the passing gen_ai.evaluation.result and
    parents the chat span and the dispatched agent.decision."""
    stack = fresh_stack
    stack.fake_openai.script(
        "valid", intervention=SCRIPTED_INTERVENTION, confidence=HIGH_CONFIDENCE, magnitude=SCRIPTED_MAGNITUDE
    )
    _configure(stack, "openai")

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            calls = await wait_first_call(stack, game_id)
            assert calls[0]["intervention"] == SCRIPTED_INTERVENTION, (
                f"{SCRIPTED_INTERVENTION} was not among the offered options: {calls[0]}"
            )
            assert calls[0]["model"] == FAKE_MODEL, calls[0]
            assert calls[0]["response_format"] == "json_schema", calls[0]

            # Effect: the agent wrote the flag and the coordinator applied it.
            assert await async_poll_until(
                lambda: any(e.get("blocked") == "false" for e in intervention_events(collector)), APPLY_TIMEOUT
            ), f"no applied {SCRIPTED_INTERVENTION} on the game stream; saw {intervention_events(collector)}"
            value = scripted_flag_value(game_id)
            assert value is not None, f"agent did not write {SCRIPTED_FLAG} for {game_id}"
            assert abs(float(value) - EXPECTED_VALUE) < 1e-6, value
            applied = [e for e in intervention_events(collector) if e.get("blocked") == "false"]
            assert not applied[0].get("block_reason"), applied
            # The handler's grpc.aio SetVolume ran on the game's loop and reached the
            # audio service (#1250): blocked=false alone would also hold for the
            # no-audio-client no-op, so look for the SetVolume call itself.
            assert await async_poll_until(
                lambda: any(
                    abs(float(c.request.get("volume", -1)) - EXPECTED_VALUE) < 1e-6
                    for c in stack.fake_audio.calls_of("SetVolume")
                ),
                SPAN_TIMEOUT,
            ), (
                f"no SetVolume({EXPECTED_VALUE}) at the audio service (cross-loop dispatch regression, #1250?); "
                f"SetVolume calls: {[c.request for c in stack.fake_audio.calls_of('SetVolume')]}"
            )

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "act", "decide.outcome=act"
            )
            tags = span.attributes
            assert tags.get("decide.backend") == "openai", tags
            assert tags.get("decide.backend.requested") == "openai", tags
            assert tags.get("decide.fallback_reason", "") == "", tags
            assert tags.get("gen_ai.request.model") == FAKE_MODEL, tags
            # Eligible under the shipped llm.eligible_game_kinds=real (#1249).
            assert tags.get("game.kind") == "real", tags
            assert abs(float(tags.get("decide.confidence")) - HIGH_CONFIDENCE) < 1e-9, tags
            assert abs(float(tags.get("decide.act_confidence_threshold")) - ACT_THRESHOLD) < 1e-9, tags
            assert SCRIPTED_INTERVENTION in tags.get("decide.answers", ""), tags

            evaluations = evaluation_events(span)
            assert evaluations, f"no gen_ai.evaluation.result event: {span.events}"
            assert evaluations[0].get("gen_ai.evaluation.name") == "act_confidence_threshold", evaluations
            assert evaluations[0].get("gen_ai.evaluation.score.label") == "pass", evaluations

            chats = children(stack, span, CHAT_SPAN)
            assert chats, f"no '{CHAT_SPAN}' child under agent.decide: {[s.name for s in children(stack, span)]}"
            chat_tags = chats[0].attributes
            assert chat_tags.get("gen_ai.provider.name") == "openai", chat_tags
            assert chat_tags.get("gen_ai.operation.name") == "chat", chat_tags
            assert chat_tags.get("gen_ai.request.model") == FAKE_MODEL, chat_tags
            assert children(stack, span, "agent.decision"), "the act must route through agent.decision"
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A5", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_openai_low_confidence_held(fresh_stack):
    """Low confidence -> decide.outcome=held (TC-008 steps 4 and 5): no agent.decision,
    nothing written, nothing applied; the gen_ai.evaluation.result fails the threshold."""
    stack = fresh_stack
    stack.fake_openai.script("valid", intervention=SCRIPTED_INTERVENTION, confidence=LOW_CONFIDENCE)
    _configure(stack, "openai")

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            calls = await wait_first_call(stack, game_id)
            assert calls[0]["intervention"] == SCRIPTED_INTERVENTION, calls[0]

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "held", "decide.outcome=held"
            )
            tags = span.attributes
            assert tags.get("decide.backend") == "openai", tags
            assert abs(float(tags.get("decide.confidence")) - LOW_CONFIDENCE) < 1e-9, tags
            evaluations = evaluation_events(span)
            assert evaluations and evaluations[0].get("gen_ai.evaluation.score.label") == "fail", evaluations
            assert children(stack, span, CHAT_SPAN), "the held call still has its chat span"
            assert not children(stack, span, "agent.decision"), "a held answer must not reach runDecision"

            await assert_nothing_applied(collector, game_id)
            # Every openai answer of this game is held: no act slipped through. Rules
            # fallback spans (decide.backend=rules) are not answers from the backend under
            # test; assert_nothing_applied already covers anything a fallback could apply.
            outcomes = {
                s.attributes.get("decide.outcome")
                for s in decide_spans(stack, game_id)
                if s.attributes.get("decide.backend") == "openai"
            }
            assert outcomes == {"held"}, outcomes
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A2", tc="TC-008")
@pytest.mark.usefixtures("flag_service")
async def test_openai_garbage_falls_back_to_rules(fresh_stack):
    """Garbage model output -> the rules engine decides instead (TC-008 steps 6 and 7):
    decide.outcome=fallback, decide.backend=rules, decide.fallback_reason=invalid_answer."""
    stack = fresh_stack
    stack.fake_openai.script("garbage")
    _configure(stack, "openai")

    game_client, channel = await get_game_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id, _ = await start_ffa(stack, collector)
            await wait_first_call(stack, game_id)

            span = await wait_decide_span(
                stack, game_id, lambda t: t.get("decide.outcome") == "fallback", "decide.outcome=fallback"
            )
            tags = span.attributes
            assert tags.get("decide.fallback_reason") == "invalid_answer", tags
            assert tags.get("decide.backend") == "rules", tags
            assert tags.get("decide.backend.requested") == "openai", tags
            assert tags.get("error.type") == "invalid_answer", tags
            chats = children(stack, span, CHAT_SPAN)
            assert chats, "the failed call still records its chat span"
            assert chats[0].attributes.get("error.type") == "invalid_output", chats[0].attributes
    finally:
        await channel.close()
