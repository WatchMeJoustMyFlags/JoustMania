"""TC-010 on the service tier (T2): a real FFA game's end runs retro, summary and proposer (#1422 S10).

AGENT_CLAUDE composition, one ``fresh_stack`` (``enabled``, ``experiments_enabled`` and the
agent's LLM budget are global): real menu, game coordinator and controller manager (mock
backend) from source at HEAD, the Go agent built at HEAD with
the ``inference.backend=claude`` overlay (the stack writes it; the flag wins over the env, #1810),
``ANTHROPIC_API_KEY`` and ``AGENT_INFERENCE_BASE_URL`` pointed at the scriptable
fake Claude (Anthropic Messages API), a fresh flag plane, FakeAudio and the OtlpSink that
the agent exports its spans to (Jaeger's role).
"""

from __future__ import annotations

import json
import re

import pytest
from openfeature.evaluation_context import EvaluationContext

from tests.integration.flag_service import default as flag_service_default
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    flagd_probe_client,
    get_game_client,
    get_mock_client,
    kill_player_verified,
    poll_until,
)
from tests.service.agent_flow import AGENT_SERVICE, start_ffa
from tests.service.harness import AGENT_CLAUDE
from tests.service.harness.fakes.claude import MODEL as FAKE_CLAUDE_MODEL
from tests.service.harness.stack import TC010_NO_CANDIDATES

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT_CLAUDE

AGENT = "agent"
GAME_FLAGS = "game"
PROPOSED_FLAG = "sensitivity"
PROPOSED_VALUE = 1  # the "slow" variant of sensitivity (ints 0..4)
SHIPPED_VALUE = 2  # the "medium" variant: what a real game resolves
EXPERIMENT_ID_RE = re.compile(r"exp_[0-9a-f]+")
GAME_KIND_REAL = "real"
SPAN_TIMEOUT = 60.0  # game end -> agent chain -> batch export -> sink

RETRO_REPLY = json.dumps(
    {
        "session_assessment": "The first elimination came early and the game was decided quickly.",
        "suggestions": [{"intervention_type": "adjust_volume", "emphasis": "weight_up", "reason": "Quiet start."}],
        "session_focus": "endurance",
    }
)
PROPOSAL_REPLY = json.dumps(
    {
        "flag": PROPOSED_FLAG,
        "value": PROPOSED_VALUE,
        "rationale": "A less sensitive game should last longer.",
    }
)


def _configure_agent() -> None:
    fs = flag_service_default()
    fs.put(AGENT, "enabled", variant="on")
    fs.put(AGENT, "experiments_enabled", variant="on")
    # The text-backend model is the inference.model flag (the env var was removed, #1907),
    # so the fake's model is set here.
    fs.put(AGENT, "inference.model", value=FAKE_CLAUDE_MODEL)
    # The harness's candidate list naming no game flag (#1907: a flag, not an env var).
    fs.put(AGENT, "experiment_dynamic_candidates", value=TC010_NO_CANDIDATES)


def _wait_flags_served(stack) -> None:
    ctx = EvaluationContext()
    with flagd_probe_client(stack, AGENT) as client:
        served = poll_until(
            lambda: (
                client.get_boolean_value("experiments_enabled", False, ctx) is True
                and client.get_string_value("llm.eligible_game_kinds", "", ctx) == "real"
                and client.get_string_value("inference.model", "", ctx) == FAKE_CLAUDE_MODEL
                and client.get_string_value("inference.backend", "", ctx) == "claude"
                and client.get_string_value("experiment_dynamic_candidates", "", ctx) == TC010_NO_CANDIDATES
            ),
            rewrite=_configure_agent,
        )
    assert served, "flagd did not serve the TC-010 agent flags"


def _ctx(**attributes) -> EvaluationContext:
    return EvaluationContext(attributes=attributes)


async def _wait_span(stack, name: str, attrs: dict | None = None):
    sink = stack.otlp_sink
    assert await async_poll_until(lambda: bool(sink.spans(name, attrs, service=AGENT_SERVICE)), SPAN_TIMEOUT), (
        f"no {name} span {attrs or ''} from the agent within {SPAN_TIMEOUT}s"
    )
    spans = sink.spans(name, attrs, service=AGENT_SERVICE)
    assert len(spans) == 1, f"{name} must be recorded once, got {len(spans)}: {[s.attributes for s in spans]}"
    return spans[0]


@pytest.mark.asyncio
@pytest.mark.uc("UC-020", flow="main", tc="TC-010")
@pytest.mark.uc("UC-041", flow="main", steps=[7], tc="TC-010")
@pytest.mark.uc("UC-042", flow="main", steps=[1, 2, 3, 4, 5], tc="TC-010")
@pytest.mark.uc("UC-043", flow="main", steps=[1], tc="TC-010")
@pytest.mark.usefixtures("flag_service")
async def test_tc010_game_end_runs_retro_summary_and_proposer(fresh_stack):
    """A real FFA game ends with a winner: the agent asks Claude for a retro and a proposal,
    records both with the game summary in order, and the proposal becomes an experiment-arm
    value that only a shadow game of that experiment resolves."""
    stack = fresh_stack
    fake = stack.fake_claude
    fake.script(retro=RETRO_REPLY, proposal=PROPOSAL_REPLY)
    _configure_agent()
    _wait_flags_served(stack)

    game_client, channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            # Step 1: play FFA to a winner (the fourth controller).
            game_id, serials = await start_ffa(stack, collector)
            for serial in serials[:3]:
                await kill_player_verified(mock_client, game_client, serial, lambda p: not p.alive, game_id=game_id)
            await collector.wait_for_event("game_ended", timeout=30.0)
            winners = [e.data["serial"] for e in collector.get_events("game_winner") if e.game_id == game_id]
            assert winners == [serials[3]], winners

        # Steps 2-4: the chain ran for the game id; the retro carries the model's answer.
        retro = await _wait_span(stack, "agent.llm.retro", {"game.id": game_id})
        summary = await _wait_span(stack, "agent.game.summary", {"game.id": game_id})
        assert retro.attributes.get("inference.used") == "claude", retro.attributes
        assert retro.attributes.get("llm.retro.parse_ok") is True, retro.attributes
        assert retro.attributes.get("llm.retro.suggestion_count") == 1, retro.attributes
        assert retro.attributes.get("llm.retro.session_focus") == "endurance", retro.attributes
        assert retro.attributes.get("llm.retro.session_assessment") == json.loads(RETRO_REPLY)["session_assessment"]
        assert summary.attributes.get("summary.player_count") == 4, summary.attributes
        assert summary.attributes.get("summary.elimination_count") == 3, summary.attributes

        # Step 5: the proposer submitted the proposal and the experiment gate accepted it.
        proposed = await _wait_span(stack, "code_improvement.proposed")
        assert proposed.attributes.get("code_improvement.flag_key") == PROPOSED_FLAG, proposed.attributes
        assert proposed.attributes.get("code_improvement.blocked") is False, proposed.attributes

        # Validation 1: the chain order is retro, summary, proposal (the first two start in the
        # same game-end hook, so their clock readings may tie but never invert).
        assert retro.start_time_unix_nano <= summary.start_time_unix_nano < proposed.start_time_unix_nano, (
            retro.start_time_unix_nano,
            summary.start_time_unix_nano,
            proposed.start_time_unix_nano,
        )

        # The model was called once per seam, with the key, and the proposer's prompt offered the flag.
        calls = fake.requests()
        assert [c["kind"] for c in calls].count("retro") == 1, calls
        assert [c["kind"] for c in calls].count("proposal") == 1, calls
        assert all(c["key_ok"] and c["model"] == FAKE_CLAUDE_MODEL for c in calls), calls
        # BR-008: the catalog line carries the registry description (flag-service/registry.yaml, sensitivity).
        sensitivity_description = "movement a player survives: 0 ultra_slow (strictest) to 4 ultra_fast (most lenient)"
        assert f"  - {PROPOSED_FLAG}: {sensitivity_description}" in fake.requests("proposal")[0]["system"]
        assert game_id in fake.requests("retro")[0]["user"]

        # Step 6: the flag service holds one experiment-arm value, experimental arm only.
        view = flag_service_default().get(GAME_FLAGS, PROPOSED_FLAG)
        experiments = view.get("experiments") or {}
        assert len(experiments) == 1, experiments
        experiment_id, arms = next(iter(experiments.items()))
        assert EXPERIMENT_ID_RE.fullmatch(experiment_id), experiment_id
        assert list(arms) == ["experimental"], arms
        assert arms["experimental"]["value"] == PROPOSED_VALUE, arms

        # Step 7 / Validation 2: only a shadow game of that experiment's experimental arm resolves it.
        shadow = _ctx(game_kind="shadow", experiment_id=experiment_id, arm="experimental")
        control = _ctx(game_kind="shadow", experiment_id=experiment_id, arm="control")
        real = _ctx(game_kind=GAME_KIND_REAL)
        with flagd_probe_client(stack, GAME_FLAGS, resolver="rpc") as client:
            assert poll_until(lambda: client.get_integer_value(PROPOSED_FLAG, -1, shadow) == PROPOSED_VALUE), (
                "flagd never served the experiment value to the experimental arm"
            )
            assert client.get_integer_value(PROPOSED_FLAG, -1, real) == SHIPPED_VALUE
            assert client.get_integer_value(PROPOSED_FLAG, -1, control) == SHIPPED_VALUE
    finally:
        await mock_channel.close()
        await channel.close()
