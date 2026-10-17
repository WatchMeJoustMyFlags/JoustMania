"""End-to-end test of the decide confidence router on a real FFA game (#1230, epic #1225).

Proves, through the REAL stack (menu-started JoustFFA game on mock controllers,
game-coordinator -> OTel collector -> the agent container -> flag service ->
flagd -> game-coordinator), that the agent's in-game decision
backend contract behaves as specified:

1. ``decision.backend=openai`` pointed at a fake OpenAI-compatible server that
   answers HIGH confidence -> ``decide.outcome=act``: the agent writes the
   intervention for the game through the flag service and the coordinator applies it (an unblocked
   ``agent_intervention`` event on the game stream). The ``agent.decide`` span
   carries the routing judgement (``gen_ai.evaluation.result`` event) and parents
   the backend's ``chat {model}`` CLIENT span (``gen_ai.provider.name``).
2. LOW confidence -> ``decide.outcome=held``: nothing applied.
3. The kill switch (``enabled``) flipped while the fake holds its answer ->
   ``decide.outcome=discarded``, ``decide.discarded_reason=kill_switch``:
   nothing applied, although the late answer was a high-confidence act.
4. The fake answers garbage -> rules fallback with
   ``decide.fallback_reason=invalid_answer``.
5. ``decision.backend=rules`` (the default) -> unchanged rules path: an
   ``agent.decide`` span with ``decide.backend=rules`` beside the rules
   ``agent.decision``.

Harness
-------
``docker-compose.decide-e2e.yml`` (test-only overlay) adds ``fake-openai``
(tests/integration/fake_openai/server.py) and gives the agent a DUMMY
``DECISION_OPENAI_API_KEY``. The module fixture brings the agent + fake up on
top of the session stack under ``--profile agent`` (the
test_experiment_cohort_loop.py pattern) and removes both afterwards, so other
modules still see the agent absent. Each test scripts the fake's next answers
through its control API (no restarts) and points the agent at it with the
``decision.openai.*`` flags in ci/agent.json; the ``decide_flags`` fixture
snapshots and restores agent.json byte-for-byte. The agent's interventions are
game-scoped (they die with the game) and read back via ``GET /interventions/<flag>``.

Spans are read from Jaeger's query API through the fake's ``/_jaeger/`` relay:
the integration stack publishes no Jaeger host port.

Why sequential, one game per test (not batched like the parallel suites)
------------------------------------------------------------------------
The agent's ActionSink writes UN-TARGETED interventions (no gameId scoping), and
remote decisions only run for ``llm.eligible_game_kinds`` (``real`` by default:
headless shadow games are rules-only). Concurrent games would therefore share
every applied intervention, which would void the "nothing applied" assertions of
the held / kill-switch / garbage cases; and the kill switch is a GLOBAL flag
(like ``interventions_allowed``, read without a gameId). So each case runs one
real menu game. The
fake's request log is keyed by the game id parsed from the prompt, and every
span assertion is scoped to that game id, so a straggling call from a previous
game can never satisfy the current test.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    async_poll_until,
    flagd_probe_client,
    get_game_client,
    get_mock_client,
    get_mock_controller_serials,
    kill_player_verified,
    poll_until,
    setup_mock_controllers,
    start_game_via_menu,
)
from tests.integration.flag_service import default as flag_service_default  # noqa: E402


AGENT_SERVICE = "agent"
AGENT_CONTAINER = "joustmania-agent"
# Tag of the agent image this module builds from the checkout (see decide_stack).
AGENT_IMAGE_TAG = "decide-e2e"
FAKE_SERVICE = "fake-openai"
DECIDE_COMPOSE_FILE = "docker-compose.decide-e2e.yml"

# Host port of the fake's control API + Jaeger relay (docker-compose.decide-e2e.yml).
FAKE_URL = "http://localhost:18089"
# What the agent dials from inside the compose network.
FAKE_BASE_URL_IN_NETWORK = "http://fake-openai:8080/v1"
FAKE_MODEL = "fake-decider"
CHAT_SPAN = f"chat {FAKE_MODEL}"

# Variant name injected into ci/agent.json flags (flagd tolerates extra variants;
# precedent: test_experiment_cohort_loop's ``it`` variant).
E2E_VARIANT = "e2e"

# The intervention the fake scripts: session-scoped (target "none"), with a
# numeric magnitude, observable on the game stream as
# agent_intervention(type=adjust_volume), and in the DEFAULT ``ambient``
# interventions_allowed variant (services/flagd/ci/agent.json), so no wider
# permission is needed. Its coordinator handler awaits the game's grpc.aio audio
# client (SetVolume), which lives on the game's own thread loop: before #1250 /
# PR #1406 it failed here with handler_error ("attached to a different loop").
# The act case therefore also proves the side effect now reaches audio-service.
SCRIPTED_INTERVENTION = "adjust_volume"
SCRIPTED_FLAG = "volume_override"
INTERVENTIONS_ALLOWED_VARIANT = "ambient"
# magnitude 0.5 over adjust_volume's [0.3, 1.0] bounds (decide.ResolveMagnitude)
SCRIPTED_MAGNITUDE = 0.5
EXPECTED_VALUE = 0.65
# The audio servicer renames its SetVolume SERVER span to f"SetVolume:{volume:.0%}"
# (services/audio/servicer.py); 65% is distinct from the game's own 70% ramp.
AUDIO_SERVICE = "audio-service"
EXPECTED_SET_VOLUME_SPAN = f"SetVolume:{EXPECTED_VALUE:.0%}"
HIGH_CONFIDENCE = 0.95
LOW_CONFIDENCE = 0.3
# decision.act_confidence_threshold default variant (ci/agent.json).
ACT_THRESHOLD = 0.8

# llm.eligible_game_kinds (CSV string flag, #1221): the SHIPPED default, left
# untouched. Menu games reach the agent as game_kind "real" (the coordinator's
# "primary" label is normalised at ingest, #1249), so the remote cases also prove
# that a real menu game is eligible for a remote decision under the default.
ELIGIBLE_GAME_KINDS = "real"

# Remote call budget per decide call (decision.openai.timeout_ms). Long so the
# held call in the kill-switch case is still in flight after the switch flips and
# the agent has provably seen it; every other case answers at once. It must stay
# below the agent's shared HTTP client backstop (decideHTTPTimeout, 30s), and the
# fake's hold below it, so the held call ends by OUR release, never by a timeout.
DECIDE_TIMEOUT_MS = 25000
KILL_HOLD_MAX_SECONDS = 28
# Kill-switch sequence budget, measured from "the fake holds the call": flag
# write -> flagd serves enabled=false -> an agent.disabled span for this game
# proves the agent's own flag read sees it. Must leave headroom under
# DECIDE_TIMEOUT_MS so the release (not a timeout) ends the call.
KILL_SIGNAL_TIMEOUT = 15.0
KILL_RELEASE_HEADROOM = 3.0

# Polling budgets (worst-case bounds, not paid waits).
FIRST_CALL_TIMEOUT = 60.0  # game start -> OTLP -> agent cycle -> first remote call
SPAN_TIMEOUT = 45.0  # span end -> collector batch -> Jaeger query
APPLY_TIMEOUT = 30.0  # agent write -> flagd reload -> coordinator applies
RULES_DECIDE_TIMEOUT = 90.0  # a rules decision needs a rule to fire
# Absence window for the "nothing applied" assertions: several remote decision
# cadences (decision.min_interval_ms=fast, 1.5s) plus a flagd reload cycle. A
# bounded wait IS the test when the correct behaviour is "nothing happens".
ABSENCE_WINDOW = 6.0


# =============================================================================
# Fake OpenAI server control + Jaeger relay
# =============================================================================


def _fake_call(method: str, path: str, body: dict | None = None, timeout: float = 10.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FAKE_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def script_fake(mode: str = "valid", **kwargs) -> None:
    """Set the fake's answer script (and clear its request log)."""
    _fake_call("POST", "/_control/script", {"mode": mode, **kwargs})


def release_fake() -> None:
    _fake_call("POST", "/_control/release", {})


def fake_requests(game_id: str) -> list[dict]:
    """The fake's chat calls for ``game_id`` (parsed from the rendered prompt)."""
    return [
        r
        for r in _fake_call("GET", "/_control/requests")["requests"]
        if r["game_id"] == game_id
    ]


def _span_tags(span: dict) -> dict:
    return {t["key"]: t["value"] for t in span.get("tags", [])}


def _span_events(span: dict) -> list[dict]:
    """Span events (Jaeger "logs") as {field: value} dicts; the name is "event"."""
    return [
        {f["key"]: f["value"] for f in log.get("fields", [])}
        for log in span.get("logs", [])
    ]


def jaeger_spans(
    operation: str, game_id: str, since_s: float
) -> list[tuple[dict, list[dict]]]:
    """Every agent span named ``operation`` for ``game_id`` started after
    ``since_s`` (epoch s), each with the full span list of its trace."""
    params = {
        "service": AGENT_SERVICE,
        "operation": operation,
        "tags": json.dumps({"game.id": game_id}),
        "start": str(int(since_s * 1_000_000)),
        "end": str(int((time.time() + 60) * 1_000_000)),
        "limit": "200",
    }
    try:
        doc = _fake_call("GET", "/_jaeger/api/traces?" + urllib.parse.urlencode(params))
    except urllib.error.URLError, OSError, ValueError:
        return []
    out = []
    for trace in doc.get("data") or []:
        spans = trace.get("spans", [])
        for span in spans:
            if span["operationName"] != operation:
                continue
            if _span_tags(span).get("game.id") != game_id:
                continue
            out.append((span, spans))
    return out


def audio_set_volume_spans(since_s: float) -> list[dict]:
    """audio-service SetVolume SERVER spans named EXPECTED_SET_VOLUME_SPAN
    started after ``since_s`` (epoch s)."""
    params = {
        "service": AUDIO_SERVICE,
        "operation": EXPECTED_SET_VOLUME_SPAN,
        "start": str(int(since_s * 1_000_000)),
        "end": str(int((time.time() + 60) * 1_000_000)),
        "limit": "50",
    }
    try:
        doc = _fake_call("GET", "/_jaeger/api/traces?" + urllib.parse.urlencode(params))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [
        span
        for trace in doc.get("data") or []
        for span in trace.get("spans", [])
        if span["operationName"] == EXPECTED_SET_VOLUME_SPAN
    ]


def jaeger_decide_spans(game_id: str, since_s: float) -> list[tuple[dict, list[dict]]]:
    """Every agent.decide span for ``game_id`` started after ``since_s``."""
    return jaeger_spans("agent.decide", game_id, since_s)


def children(
    span: dict, trace_spans: list[dict], name: str | None = None
) -> list[dict]:
    """Direct children of ``span`` in its trace, optionally filtered by name."""
    out = []
    for s in trace_spans:
        if any(r.get("spanID") == span["spanID"] for r in s.get("references", [])):
            if name is None or s["operationName"] == name:
                out.append(s)
    return out


async def wait_decide_span(
    game_id: str, since_s: float, predicate, what: str, timeout: float = SPAN_TIMEOUT
):
    """Poll Jaeger until an agent.decide span for ``game_id`` satisfies
    ``predicate(tags, span, trace_spans)``; return (span, trace_spans)."""
    found: list = []

    def _check() -> bool:
        for span, trace_spans in jaeger_decide_spans(game_id, since_s):
            if predicate(_span_tags(span), span, trace_spans):
                found.append((span, trace_spans))
                return True
        return False

    ok = await async_poll_until(_check, timeout, interval=1.0)
    if not ok:
        seen = [
            {
                k: v
                for k, v in _span_tags(s).items()
                if k.startswith("decide.")
                and k not in ("decide.questions", "decide.answers")
            }
            for s, _ in jaeger_decide_spans(game_id, since_s)
        ]
        raise AssertionError(
            f"no agent.decide span for {game_id} with {what} within {timeout}s; saw: {seen}"
        )
    return found[0]


# =============================================================================
# Agent-set helpers: in-memory overlays on the flag service, cleared by the
# ``flag_service`` fixture at teardown (epic #1407, N4)
# =============================================================================


def _onoff(value: bool) -> str:
    return "on" if value else "off"


def configure_agent(*, backend: str, enabled: bool = True, interventions: bool = True) -> None:
    """Every agent-set flag a case needs, as flag-service overlays. The fake's
    URL and model are free strings (registry, re-review N5)."""
    fs = flag_service_default()
    fs.put("agent", "enabled", variant=_onoff(enabled))
    fs.put("agent", "interventions_enabled", variant=_onoff(interventions))
    fs.put("agent", "interventions_allowed", variant=INTERVENTIONS_ALLOWED_VARIANT)
    fs.put("agent", "decision.backend", variant=backend)
    fs.put("agent", "decision.openai.base_url", value=FAKE_BASE_URL_IN_NETWORK)
    fs.put("agent", "decision.openai.model", value=FAKE_MODEL)
    fs.put("agent", "decision.openai.timeout_ms", value=DECIDE_TIMEOUT_MS)
    fs.put("agent", "decision.min_interval_ms", value=1500)


def set_enabled(enabled: bool) -> None:
    flag_service_default().put("agent", "enabled", variant=_onoff(enabled))


def wait_agent_flags_served(docker_compose, *, backend: str, enabled: bool, rewrite=None) -> None:
    from openfeature.evaluation_context import EvaluationContext

    ctx = EvaluationContext()
    with flagd_probe_client(docker_compose, "agent") as client:
        served = poll_until(
            lambda: (
                client.get_string_value("decision.backend", "", ctx) == backend
                and client.get_boolean_value("enabled", not enabled, ctx) is enabled
                and client.get_string_value("decision.openai.model", "", ctx)
                == FAKE_MODEL
                # numeric flag: TYPED getter (the #903 ObjectValue TYPE_MISMATCH trap)
                and client.get_integer_value("decision.openai.timeout_ms", -1, ctx)
                == DECIDE_TIMEOUT_MS
                and client.get_string_value("llm.eligible_game_kinds", "", ctx)
                == ELIGIBLE_GAME_KINDS
            ),
            rewrite=rewrite,
        )
    assert served, f"flagd did not serve decision.backend={backend} enabled={enabled}"


def scripted_flag_value(game_id: str):
    """The value the agent wrote for the scripted intervention in ``game_id``
    through the flag service, or None when it wrote nothing for that game."""
    view = flag_service_default().get("interventions", SCRIPTED_FLAG)
    entry = (view.get("games") or {}).get(game_id)
    return None if entry is None else entry.get("value")


# =============================================================================
# Fixtures
# =============================================================================


def _compose_prefix(docker_compose) -> list[str]:
    """The session fixture's compose command plus the decide-e2e overlay (same
    project, so the agent joins the same network, flagd and collector)."""
    return [*docker_compose.docker_compose_command(), "-f", DECIDE_COMPOSE_FILE]


@pytest.fixture(scope="module")
def decide_stack(docker_compose):
    """Bring up fake-openai + the agent (with the dummy key) on the session stack,
    and remove both afterwards so later modules see the agent absent.

    The agent is ALWAYS built from this checkout, whatever the suite's
    USE_PREBUILT_IMAGES / USE_DEV_MOUNTS mode: CI's fast path runs the published
    :latest images (it only rebuilds on Dockerfile/deps changes), so without this
    a Go change to the decide path would be tested against the OLD agent. The build
    uses its own tag (AGENT_IMAGE_TAG) so it never overwrites the :latest image
    another module may run, and ``--no-deps`` so changing IMAGE_TAG for this call
    cannot recreate any shared service (flagd, collector, ...).
    """
    cwd = str(docker_compose.context)
    prefix = _compose_prefix(docker_compose)
    base_env = dict(os.environ)
    agent_env = {**base_env, "IMAGE_TAG": AGENT_IMAGE_TAG}

    def _compose(args: list[str], env: dict, timeout: int):
        return subprocess.run(
            [*prefix, "--profile", AGENT_SERVICE, *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    try:
        up = _compose(["up", "-d", "--wait", FAKE_SERVICE], base_env, 300)
        assert up.returncode == 0, (
            f"failed to start fake-openai:\nSTDOUT:{up.stdout}\nSTDERR:{up.stderr}"
        )
        up = _compose(
            ["up", "-d", "--wait", "--no-deps", "--build", AGENT_SERVICE],
            agent_env,
            900,
        )
        assert up.returncode == 0, (
            f"failed to build/start the agent at HEAD:\nSTDOUT:{up.stdout}\nSTDERR:{up.stderr}"
        )
        image = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", AGENT_CONTAINER],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert image.endswith(f"/agent:{AGENT_IMAGE_TAG}"), (
            f"{AGENT_CONTAINER} runs {image!r}, not the HEAD build (agent:{AGENT_IMAGE_TAG})"
        )
        assert poll_until(lambda: _fake_reachable(), timeout=30), (
            "fake-openai control API not reachable on the host"
        )
        yield docker_compose
    finally:
        _compose(["rm", "-sf", AGENT_SERVICE, FAKE_SERVICE], base_env, 120)


def _fake_reachable() -> bool:
    try:
        _fake_call("GET", "/healthz", timeout=2)
        return True
    except urllib.error.URLError, OSError:
        return False


@pytest.fixture
def decide_flags(decide_stack, flag_service):
    """The agent's flags are flag-service overlays the ``flag_service`` fixture
    clears at teardown (kill switch back to the fail-closed baseline); the
    agent's interventions are game-scoped and die with their game (UC-053
    BR-003). This fixture releases the fake so no agent goroutine lingers."""
    yield
    try:
        release_fake()
        script_fake("valid", confidence=0.0)
    except urllib.error.URLError, OSError:
        pass


async def _start_ffa(docker_compose, collector: GameEventCollector) -> str:
    """Start a 4-player JoustFFA game via the menu and return its game id."""
    await start_game_via_menu(
        docker_compose, game_mode="JoustFFA", timeout=30.0, event_collector=collector
    )
    started = collector.get_events("game_started")
    game_id = started[-1].game_id if started else ""
    assert game_id, "game_started carried no game_id"
    return game_id


def _intervention_events(collector: GameEventCollector) -> list[dict]:
    return [
        dict(e.data)
        for e in collector.get_events("agent_intervention")
        if e.data.get("type") == SCRIPTED_INTERVENTION
    ]


def _parse_failures() -> list[dict]:
    return [
        r
        for r in _fake_call("GET", "/_control/requests")["requests"]
        if r.get("parse_failed")
    ]


async def _wait_first_call(game_id: str) -> list[dict]:
    ok = await async_poll_until(
        lambda: bool(fake_requests(game_id)), FIRST_CALL_TIMEOUT, interval=0.5
    )
    assert ok, (
        f"the agent never called fake-openai for game {game_id} within {FIRST_CALL_TIMEOUT}s; "
        f"unparseable prompts: {_parse_failures()}"
    )
    calls = fake_requests(game_id)
    assert calls[0].get("questions"), (
        "fake-openai parsed no decide questions from the prompt: the rendered prompt "
        "(services/agent/decide/llmio/llmio.go Render) no longer matches "
        f"tests/integration/fake_openai/server.py _QUESTION_RE: {calls[0]}"
    )
    return calls


async def _assert_nothing_applied(collector: GameEventCollector, game_id: str) -> None:
    await asyncio.sleep(ABSENCE_WINDOW)
    events = _intervention_events(collector)
    assert not events, (
        f"{SCRIPTED_INTERVENTION} reached the game although nothing should act: {events}"
    )
    assert scripted_flag_value(game_id) is None, (
        f"the agent wrote {SCRIPTED_FLAG}={scripted_flag_value(game_id)} for {game_id}"
    )


# =============================================================================
# Tests (sequential, see the module docstring)
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A1")
async def test_rules_backend_default_unchanged(
    decide_flags, docker_compose, ensure_game_stopped
):
    """decision.backend=rules (the default): the rules engine decides exactly as
    before, recorded on an agent.decide span (backend=rules, requested=rules, no
    fallback) that is a SIBLING of the rules agent.decision under
    agent.signal_received; the fake is never called."""
    script_fake("valid", confidence=HIGH_CONFIDENCE)
    # interventions stay off: this case observes the rules decision, not its effect.
    configure_agent(backend="rules", interventions=False)
    wait_agent_flags_served(
        docker_compose,
        backend="rules",
        enabled=True,
        rewrite=lambda: configure_agent(backend="rules", interventions=False),
    )

    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    game_client, channel = await get_game_client(docker_compose)
    mock_client, _ = await get_mock_client(docker_compose)
    since = time.time()
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id = await _start_ffa(docker_compose, collector)
            # R1 (endurance): an early elimination in a young session makes the
            # rules engine slow the tempo / cue audio, i.e. decide something.
            await kill_player_verified(
                mock_client, game_client, serials[0], lambda p: not p.alive
            )

            span, trace_spans = await wait_decide_span(
                game_id,
                since,
                lambda t, s, ts: t.get("decide.backend") == "rules",
                "decide.backend=rules",
                timeout=RULES_DECIDE_TIMEOUT,
            )
            tags = _span_tags(span)
            assert tags.get("decide.backend.requested") == "rules", tags
            assert tags.get("decide.fallback_reason", "") == "", tags
            assert tags.get("decide.outcome") == "act", tags
            # The coordinator's "primary" label is normalised to "real" (#1249).
            assert tags.get("game.kind") == "real", tags
            # Sync rules path shape: agent.decide and agent.decision share the
            # agent.signal_received parent; no backend chat span.
            parent_ids = {r["spanID"] for r in span.get("references", [])}
            assert parent_ids, "agent.decide has no parent on the rules path"
            parents = [s for s in trace_spans if s["spanID"] in parent_ids]
            assert parents and parents[0]["operationName"] == "agent.signal_received", (
                parents
            )
            assert children(parents[0], trace_spans, "agent.decision"), (
                "no sibling agent.decision"
            )
            assert not children(span, trace_spans), (
                "the rules agent.decide span must have no children"
            )
            assert fake_requests(game_id) == [], (
                "the rules backend must never call the remote backend"
            )
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="main")
@pytest.mark.uc("UC-040", flow="A26", br=["BR-018"])
async def test_openai_high_confidence_acts(
    decide_flags, docker_compose, ensure_game_stopped
):
    """High confidence -> decide.outcome=act: the agent writes the scripted
    intervention for the game through the flag service and the coordinator applies it; the agent.decide span
    carries the passing gen_ai.evaluation.result and parents the chat span and the
    dispatched agent.decision."""
    script_fake(
        "valid",
        intervention=SCRIPTED_INTERVENTION,
        confidence=HIGH_CONFIDENCE,
        magnitude=SCRIPTED_MAGNITUDE,
    )
    configure_agent(backend="openai")
    wait_agent_flags_served(
        docker_compose,
        backend="openai",
        enabled=True,
        rewrite=lambda: configure_agent(backend="openai"),
    )

    await setup_mock_controllers(docker_compose, count=4)
    game_client, channel = await get_game_client(docker_compose)
    since = time.time()
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id = await _start_ffa(docker_compose, collector)
            calls = await _wait_first_call(game_id)
            assert calls[0]["intervention"] == SCRIPTED_INTERVENTION, (
                f"{SCRIPTED_INTERVENTION} was not among the offered options: {calls[0]}"
            )
            assert calls[0]["model"] == FAKE_MODEL, calls[0]
            assert calls[0]["response_format"] == "json_schema", calls[0]

            # Effect: the agent wrote the flag and the coordinator applied it.
            assert await async_poll_until(
                lambda: any(
                    e.get("blocked") == "false" for e in _intervention_events(collector)
                ),
                APPLY_TIMEOUT,
            ), (
                f"no applied {SCRIPTED_INTERVENTION} on the game stream; saw {_intervention_events(collector)}"
            )
            value = scripted_flag_value(game_id)
            assert value is not None, (
                f"agent did not write {SCRIPTED_FLAG} for {game_id}"
            )
            assert abs(float(value) - EXPECTED_VALUE) < 1e-6, value
            # The handler's grpc.aio SetVolume ran on the game's loop and reached
            # audio-service (#1250): blocked=false alone would also hold for the
            # no-audio-client no-op, so look for the audio SERVER span itself.
            applied = [
                e
                for e in _intervention_events(collector)
                if e.get("blocked") == "false"
            ]
            assert not applied[0].get("block_reason"), applied
            assert await async_poll_until(
                lambda: bool(audio_set_volume_spans(since)),
                SPAN_TIMEOUT,
                interval=1.0,
            ), (
                f"no {AUDIO_SERVICE} '{EXPECTED_SET_VOLUME_SPAN}' span: SetVolume never "
                "reached audio (cross-loop dispatch regression, #1250?)"
            )

            span, trace_spans = await wait_decide_span(
                game_id,
                since,
                lambda t, s, ts: t.get("decide.outcome") == "act",
                "decide.outcome=act",
            )
            tags = _span_tags(span)
            assert tags.get("decide.backend") == "openai", tags
            assert tags.get("decide.backend.requested") == "openai", tags
            assert tags.get("decide.fallback_reason", "") == "", tags
            assert tags.get("gen_ai.request.model") == FAKE_MODEL, tags
            # Eligible under the shipped llm.eligible_game_kinds=real (#1249).
            assert tags.get("game.kind") == "real", tags
            assert abs(float(tags.get("decide.confidence")) - HIGH_CONFIDENCE) < 1e-9, (
                tags
            )
            assert (
                abs(float(tags.get("decide.act_confidence_threshold")) - ACT_THRESHOLD)
                < 1e-9
            ), tags
            assert SCRIPTED_INTERVENTION in tags.get("decide.answers", ""), tags

            evaluations = [
                e
                for e in _span_events(span)
                if e.get("event") == "gen_ai.evaluation.result"
            ]
            assert evaluations, (
                f"no gen_ai.evaluation.result event: {_span_events(span)}"
            )
            assert (
                evaluations[0].get("gen_ai.evaluation.name")
                == "act_confidence_threshold"
            ), evaluations
            assert evaluations[0].get("gen_ai.evaluation.score.label") == "pass", (
                evaluations
            )

            chats = children(span, trace_spans, CHAT_SPAN)
            assert chats, (
                f"no '{CHAT_SPAN}' child under agent.decide: {[s['operationName'] for s in trace_spans]}"
            )
            chat_tags = _span_tags(chats[0])
            assert chat_tags.get("gen_ai.provider.name") == "openai", chat_tags
            assert chat_tags.get("gen_ai.operation.name") == "chat", chat_tags
            assert chat_tags.get("gen_ai.request.model") == FAKE_MODEL, chat_tags
            assert children(span, trace_spans, "agent.decision"), (
                "the act must route through agent.decision"
            )
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A5")
async def test_openai_low_confidence_held(
    decide_flags, docker_compose, ensure_game_stopped
):
    """Low confidence -> decide.outcome=held: no agent.decision, nothing written,
    nothing applied; the gen_ai.evaluation.result fails the threshold."""
    script_fake("valid", intervention=SCRIPTED_INTERVENTION, confidence=LOW_CONFIDENCE)
    configure_agent(backend="openai")
    wait_agent_flags_served(
        docker_compose,
        backend="openai",
        enabled=True,
        rewrite=lambda: configure_agent(backend="openai"),
    )

    await setup_mock_controllers(docker_compose, count=4)
    game_client, channel = await get_game_client(docker_compose)
    since = time.time()
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id = await _start_ffa(docker_compose, collector)
            calls = await _wait_first_call(game_id)
            assert calls[0]["intervention"] == SCRIPTED_INTERVENTION, calls[0]

            span, trace_spans = await wait_decide_span(
                game_id,
                since,
                lambda t, s, ts: t.get("decide.outcome") == "held",
                "decide.outcome=held",
            )
            tags = _span_tags(span)
            assert tags.get("decide.backend") == "openai", tags
            assert abs(float(tags.get("decide.confidence")) - LOW_CONFIDENCE) < 1e-9, (
                tags
            )
            evaluations = [
                e
                for e in _span_events(span)
                if e.get("event") == "gen_ai.evaluation.result"
            ]
            assert (
                evaluations
                and evaluations[0].get("gen_ai.evaluation.score.label") == "fail"
            ), evaluations
            assert children(span, trace_spans, CHAT_SPAN), (
                "the held call still has its chat span"
            )
            assert not children(span, trace_spans, "agent.decision"), (
                "a held answer must not reach runDecision"
            )

            await _assert_nothing_applied(collector, game_id)
            # Every openai answer of this game is held: no act slipped through.
            # Rules-fallback spans (decide.backend=rules, e.g. llm_budget_exhausted
            # carried over from an earlier case) are not answers from the backend
            # under test, so they are left out; _assert_nothing_applied above
            # already covers anything a fallback could have applied.
            outcomes = {
                _span_tags(s).get("decide.outcome")
                for s, _ in jaeger_decide_spans(game_id, since)
                if _span_tags(s).get("decide.backend") == "openai"
            }
            assert outcomes == {"held"}, outcomes
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A2")
async def test_openai_garbage_falls_back_to_rules(
    decide_flags, docker_compose, ensure_game_stopped
):
    """Garbage model output -> the rules engine decides instead:
    decide.outcome=fallback, decide.backend=rules, decide.fallback_reason=invalid_answer."""
    script_fake("garbage")
    configure_agent(backend="openai")
    wait_agent_flags_served(
        docker_compose,
        backend="openai",
        enabled=True,
        rewrite=lambda: configure_agent(backend="openai"),
    )

    await setup_mock_controllers(docker_compose, count=4)
    game_client, channel = await get_game_client(docker_compose)
    since = time.time()
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id = await _start_ffa(docker_compose, collector)
            await _wait_first_call(game_id)

            span, trace_spans = await wait_decide_span(
                game_id,
                since,
                lambda t, s, ts: t.get("decide.outcome") == "fallback",
                "decide.outcome=fallback",
            )
            tags = _span_tags(span)
            assert tags.get("decide.fallback_reason") == "invalid_answer", tags
            assert tags.get("decide.backend") == "rules", tags
            assert tags.get("decide.backend.requested") == "openai", tags
            assert tags.get("error.type") == "invalid_answer", tags
            chats = children(span, trace_spans, CHAT_SPAN)
            assert chats, "the failed call still records its chat span"
            assert _span_tags(chats[0]).get("error.type") == "invalid_output", (
                _span_tags(chats[0])
            )
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.uc("UC-040", flow="A6")
async def test_kill_switch_discards_inflight_answer(
    decide_flags, docker_compose, ensure_game_stopped
):
    """The kill switch flipped while the remote call is in flight: the late
    high-confidence answer is discarded (decide.discarded_reason=kill_switch) and
    nothing is applied. Sequential by nature: ``enabled`` is a global flag."""
    script_fake(
        "hold",
        intervention=SCRIPTED_INTERVENTION,
        confidence=HIGH_CONFIDENCE,
        hold_max_seconds=KILL_HOLD_MAX_SECONDS,
    )
    configure_agent(backend="openai")
    wait_agent_flags_served(
        docker_compose,
        backend="openai",
        enabled=True,
        rewrite=lambda: configure_agent(backend="openai"),
    )

    await setup_mock_controllers(docker_compose, count=4)
    game_client, channel = await get_game_client(docker_compose)
    since = time.time()
    try:
        collector = GameEventCollector(game_client)
        async with collector:
            game_id = await _start_ffa(docker_compose, collector)
            # The agent's call is in flight: the fake is holding it.
            assert await async_poll_until(
                lambda: any(r.get("held") for r in fake_requests(game_id)),
                FIRST_CALL_TIMEOUT,
                interval=0.5,
            ), f"no held call for {game_id}: {fake_requests(game_id)}"
            held_at = time.time()

            flipped_at = time.time()
            set_enabled(False)
            wait_agent_flags_served(
                docker_compose,
                backend="openai",
                enabled=False,
                rewrite=lambda: set_enabled(False),
            )
            # Positive signal that the AGENT (not just flagd) reads enabled=false:
            # its kill-switch path emits agent.disabled for this game on the next
            # evaluation cycle. Only then release the held answer, which re-reads
            # the live flags when it lands.
            assert await async_poll_until(
                lambda: bool(jaeger_spans("agent.disabled", game_id, flipped_at)),
                KILL_SIGNAL_TIMEOUT,
                interval=0.5,
            ), (
                f"no agent.disabled span for {game_id} within {KILL_SIGNAL_TIMEOUT}s of the kill-switch flip"
            )
            elapsed = time.time() - held_at
            assert elapsed < DECIDE_TIMEOUT_MS / 1000 - KILL_RELEASE_HEADROOM, (
                f"kill-switch sequence took {elapsed:.1f}s: the held call would time out before the "
                f"release (DECIDE_TIMEOUT_MS={DECIDE_TIMEOUT_MS}); budgets too tight"
            )
            release_fake()

            span, _ = await wait_decide_span(
                game_id,
                since,
                lambda t, s, ts: t.get("decide.outcome") == "discarded",
                "decide.outcome=discarded",
            )
            tags = _span_tags(span)
            assert tags.get("decide.discarded_reason") == "kill_switch", tags
            assert tags.get("decide.backend") == "openai", tags
            # The answer itself was a valid high-confidence act: the switch stopped it.
            assert SCRIPTED_INTERVENTION in tags.get("decide.answers", ""), tags
            assert abs(float(tags.get("decide.confidence")) - HIGH_CONFIDENCE) < 1e-9, (
                tags
            )

            await _assert_nothing_applied(collector, game_id)
            outcomes = {
                _span_tags(s).get("decide.outcome")
                for s, _ in jaeger_decide_spans(game_id, since)
            }
            assert "act" not in outcomes, outcomes
    finally:
        await channel.close()
