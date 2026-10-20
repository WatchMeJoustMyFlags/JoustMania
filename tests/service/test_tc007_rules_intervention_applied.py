"""TC-007 on the service tier (T2): a rules decision applied end to end in one real FFA game (#1404).

AGENT composition on a ``fresh_stack``: the real menu, game coordinator and
controller manager (mock backend) from source at HEAD, the Go agent built at
HEAD, a fresh flag plane, FakeAudio and the OtlpSink (the agent exports its
spans there; the sink tees the services' telemetry to the agent as the
collector does).

Interventions are ON (``interventions_allowed=standard``) and
``decision.backend=rules``. After an early elimination the rules engine decides
an intervention; the test follows it through the whole chain:

- the agent records an unblocked ``agent.decision`` for the game (TC-007 step 4);
- the agent writes it to the flag service, scoped to the game (UC-040 step 12);
- the coordinator applies it and publishes an unblocked ``agent_intervention``
  of the SAME type on the game's stream (UC-040 step 13, TC-007 step 6,
  validation 2).

``test_tc008_remote_backend_routing.py::test_rules_backend_default_unchanged``
runs the same game with interventions OFF and only observes the decision.
"""

import pytest

from tests.integration.flag_service import default as flag_service_default
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    get_game_client,
    get_mock_client,
    kill_player_verified,
)
from tests.service.agent_flow import (
    APPLY_TIMEOUT,
    HIGH_CONFIDENCE,
    RULES_DECIDE_TIMEOUT,
    agent_spans,
    configure_agent,
    start_ffa,
    wait_agent_flags_served,
)
from tests.service.harness import AGENT

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = AGENT

# The variant that lets the rules engine's in-game levers through on both sides
# (agent Permits and the coordinator's allow-list re-check): tempo, audio cue,
# controller effect, sensitivity, shield (services/flagd/ci/agent.json).
ALLOWED_VARIANT = "standard"

# intervention type -> its flag in the ``interventions`` set
# (services/game_coordinator/interventions.py INTERVENTION_SPECS); the types of
# the ``standard`` allow-list.
FLAG_OF_TYPE = {
    "adjust_music_tempo": "music_tempo_override",
    "play_audio_cue": "audio_cue",
    "send_controller_effect": "controller_effect",
    "adjust_volume": "volume_override",
    "set_pacing_profile": "pacing_profile",
    "adjust_player_sensitivity": "player_sensitivity_factor",
    "grant_shield": "shield_seconds",
}


def _applied_decisions(stack, game_id: str) -> list[str]:
    """Intervention types of the agent's UNBLOCKED agent.decision spans for ``game_id``."""
    return [
        s.attributes.get("decision.action", "")
        for s in agent_spans(stack, "agent.decision", game_id)
        if s.attributes.get("decision.blocked") is False and s.attributes.get("decision.action")
    ]


def _applied_interventions(collector: GameEventCollector, game_id: str) -> list[str]:
    """Types of the unblocked agent_intervention events the coordinator published for ``game_id``."""
    return [
        e.data.get("type", "")
        for e in collector.get_events("agent_intervention")
        if e.game_id == game_id and e.data.get("blocked") == "false"
    ]


def _written_for_game(type_id: str, game_id: str) -> bool:
    """The agent's write of ``type_id`` is in the flag service, scoped to ``game_id``."""
    flag = FLAG_OF_TYPE.get(type_id)
    if flag is None:
        return False
    view = flag_service_default().get("interventions", flag)
    # A state value lives under "games", a per-player one under "serials" and a
    # one-shot event under "events", each keyed by game id
    # (services/flag-service/state/views.go FlagView).
    return any(game_id in (view.get(scope) or {}) for scope in ("games", "serials", "events"))


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main", steps=[12, 13], tc="TC-007")
@pytest.mark.uc("UC-040", flow="A1", tc="TC-007")
@pytest.mark.uc("UC-020", flow="A3", tc="TC-007")
@pytest.mark.usefixtures("flag_service")
async def test_tc007_rules_decision_applied_in_ffa_game(fresh_stack):
    """TC-007 steps 1 to 6: the rules engine's unblocked decision is written for the game,
    applied by the coordinator, and published as an unblocked agent_intervention of the
    same type on the game's own stream."""
    stack = fresh_stack
    # Scripted only so an accidental remote call would be visible; rules never calls it.
    stack.fake_openai.script("valid", confidence=HIGH_CONFIDENCE)

    def configure() -> None:
        configure_agent(backend="rules", interventions=True, allowed=ALLOWED_VARIANT)

    configure()
    wait_agent_flags_served(stack, backend="rules", enabled=True, rewrite=configure)

    game_client, channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            # Step 1: start FFA from the menu (G1).
            game_id, serials = await start_ffa(stack, collector)

            # Step 3: early elimination; the endurance rule now wants to slow the
            # game down (tempo or calming cue).
            await kill_player_verified(mock_client, game_client, serials[0], lambda p: not p.alive)

            # Steps 4 and 6 (validation 2): an unblocked agent.decision and an
            # unblocked agent_intervention of the same type, for this game.
            matched: list[str] = []

            def decision_took_effect() -> bool:
                decided = set(_applied_decisions(stack, game_id))
                matched[:] = [t for t in _applied_interventions(collector, game_id) if t in decided]
                return bool(matched)

            ok = await async_poll_until(decision_took_effect, RULES_DECIDE_TIMEOUT + APPLY_TIMEOUT, interval=0.5)
            assert ok, (
                f"no unblocked agent_intervention matching an unblocked rules agent.decision for {game_id}: "
                f"decisions {_applied_decisions(stack, game_id)}, "
                f"interventions {[dict(e.data) for e in collector.get_events('agent_intervention')]}"
            )
            applied_type = matched[0]

            # UC-040 step 12: the agent wrote it to the flag service, scoped to G1
            # (the game is still running, so its scope still exists, UC-053 BR-003).
            assert _written_for_game(applied_type, game_id), (
                f"{applied_type} applied in {game_id} but no game-scoped "
                f"{FLAG_OF_TYPE.get(applied_type)} write in the flag service"
            )

            # Validation 1: one game, one context; every applied event is G1's.
            seen_ids = [e.game_id for e in collector.get_events("agent_intervention")]
            assert all(gid == game_id for gid in seen_ids), f"agent_intervention for another game: {seen_ids}"
            kinds = {s.attributes.get("game.kind") for s in agent_spans(stack, "agent.decision", game_id)}
            assert kinds == {"real"}, f"agent.decision game.kind for a menu game: {kinds}"
            assert stack.fake_openai.requests(game_id) == [], "the rules backend must never call the remote backend"
    finally:
        await mock_channel.close()
        await channel.close()
