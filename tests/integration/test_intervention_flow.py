"""
End-to-end intervention-flow integration tests (#730).

Proves the full agent intervention chain works against the real services:

    agent writes flag  ->  flagd file-watch reload  ->  game coordinator
    InterventionManager evaluates + enforces  ->  effect applied to the live
    game  ->  effect observable in the OTel/EventBus signals.

The agent ActionSink (services/agent/actions, Go) is NOT run in the integration
compose (it lives in the ``agent`` profile and writes the same file these tests
write). Instead these tests reproduce the EXACT mutation the Go writer produces
on the bind-mounted ``services/flagd/interventions.json`` file (state "active"
variant + defaultVariant flip, per-serial targeting if-ladder, fresh nonce per
edge dispatch). flagd reloads on the in-place write and the game coordinator —
which evaluates the ``interventions`` and ``agent`` domains via live flagd RPC —
converges on the new contents.

Observable signals used (no Prometheus pull needed — game-coordinator metrics
go OTLP->collector->Prometheus, a slow/flaky push path for assertions):

- ``StreamGameEvents`` ``agent_intervention`` events carry
  ``{type, target, blocked, block_reason}`` — the EventBus mirror of the
  ``game_interventions_total{type,objective,blocked}`` counter. Every applied
  or blocked intervention emits exactly one.
- ``GetGameState`` exposes per-player ``sensitivity_factor`` and ``alive`` —
  the applied state of difficulty/lifecycle interventions.
- A direct OpenFeature flagd RPC client (flagd port 8013, published by
  docker-compose.override.yml) evaluates the written per-serial targeting
  if-ladder, catching schema/targeting-rule rejections the Go unit tests
  (jsonlogic-direct) cannot.

interventions.json / agent.json are bind-mounted host files; the
``flag_files`` fixture snapshots and restores them so mutations never leak.
"""

import asyncio
import json
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2  # noqa: E402

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    active_flag_file,
    flagd_probe_client,
    get_game_client,
    get_mock_controller_serials,
    poll_until,
    setup_mock_controllers,
    start_game_via_menu,
    wait_flag_file_restored,
)

# Active host flag-file paths resolved through the single source of truth
# (helpers.active_flag_file -> lib.flagd_paths, issue #959): $FLAGD_FLAG_DIR/<domain>.json,
# the exact file flagd serves. Writing here on the host is what the Go ActionSink
# does inside the container.
INTERVENTIONS_PATH = active_flag_file("interventions")
AGENT_PATH = active_flag_file("agent")

# Absence window: how long the flagd reload + coordinator re-evaluate chain is
# given to (not) act before asserting that NOTHING happened. Positive waits are
# condition polls (#894, see helpers.poll_until); this constant remains ONLY for
# negative assertions — "the blocked/reverted/re-read write produced no effect"
# cannot be event-driven, the bounded wait IS the test.
RELOAD_SETTLE_SECONDS = 2.0

# Mutating a per-player state flag and observing the applied sensitivity_factor
# can take a couple of reload cycles under load; poll up to this long.
APPLY_TIMEOUT_SECONDS = 12.0

# Variant name the Go writer overwrites for state + edge flags.
ACTIVE_VARIANT = "active"


# =============================================================================
# Flag-file mutation helpers (mirror services/agent/actions/writer.go)
# =============================================================================


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write (matches the Go writer / admin-mode writer).

    No temp+rename: rename over a bind mount that flagd watches triggers EBUSY.
    """
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)


def write_state(flag_key: str, value) -> None:
    """State-shaped write: set the ``active`` variant + flip defaultVariant.

    Mirrors Writer.setState (services/agent/actions/writer.go).
    """
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][ACTIVE_VARIANT] = value
    flag["defaultVariant"] = ACTIVE_VARIANT
    _dump_in_place(INTERVENTIONS_PATH, doc)


def write_targeted(flag_key: str, neutral: str, serial: str, value) -> None:
    """Per-player state write with a flagd targeting if-ladder.

    Mirrors Writer.setTargeted: a per-serial ``agent_<serial>`` variant plus a
    nested ``if(=== targetingKey serial, agent_<serial>, ... neutral)`` ladder,
    defaultVariant flipped to the neutral variant. Merges with any existing
    agent-driven serials (does not clobber other players).
    """
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][f"agent_{serial}"] = value

    agent_serials = sorted(
        name[len("agent_"):] for name in flag["variants"] if name.startswith("agent_")
    )
    expr: object = neutral
    for s in reversed(agent_serials):
        expr = {
            "if": [
                {"===": [{"var": "targetingKey"}, s]},
                f"agent_{s}",
                expr,
            ]
        }
    flag["targeting"] = expr
    flag["defaultVariant"] = neutral
    _dump_in_place(INTERVENTIONS_PATH, doc)


def write_edge(flag_key: str, payload: str = "") -> str:
    """Edge-triggered write: ``active`` variant holds ``<nonce>:<payload>``.

    Mirrors Writer.setEdge. Returns the fresh nonce so the caller can later
    re-assert exactly-once (same nonce = no re-apply). ``payload=""`` writes a
    nonce-only value (the end_game shape).
    """
    nonce = uuid.uuid4().hex
    value = nonce if payload == "" else f"{nonce}:{payload}"
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][ACTIVE_VARIANT] = value
    flag["defaultVariant"] = ACTIVE_VARIANT
    _dump_in_place(INTERVENTIONS_PATH, doc)
    return nonce


def rewrite_edge_value(flag_key: str, raw_value: str) -> None:
    """Force flagd to re-serve a specific ``active`` value (re-read with the SAME
    nonce, to assert the exactly-once guard). Touches another field so the file
    changes byte-wise and flagd reloads, but the edge value itself is unchanged.
    """
    doc = _load(INTERVENTIONS_PATH)
    flag = doc["flags"][flag_key]
    flag["variants"][ACTIVE_VARIANT] = raw_value
    flag["defaultVariant"] = ACTIVE_VARIANT
    # Toggle a harmless metadata marker to guarantee a byte-level change.
    flag.setdefault("_reload_marker", 0)
    flag["_reload_marker"] = int(flag["_reload_marker"]) + 1
    _dump_in_place(INTERVENTIONS_PATH, doc)


def set_interventions_allowed(variant: str) -> None:
    """Flip the agent-domain ``interventions_allowed`` flag to a named variant
    (``none`` / ``ambient`` / ``standard`` / ``full``)."""
    doc = _load(AGENT_PATH)
    doc["flags"]["interventions_allowed"]["defaultVariant"] = variant
    _dump_in_place(AGENT_PATH, doc)


def set_policy_budget(per_minute: int) -> None:
    """Raise BOTH weighted rate-limit budgets for headroom (#919).

    Rate limiting is enforced in two layers that read DIFFERENT flags (the
    defense-in-depth contract): the agent's authoritative per-game budget
    (``policy.max_interventions_per_minute``) and the coordinator's generous
    process-global backstop (``policy.coordinator_backstop_per_minute``). Tests
    that drive several interventions in a tight window need headroom in BOTH, so
    we inject a custom ``active`` variant into each and flip its defaultVariant.
    The shipped variants cap below this (agent ``aggressive`` = 4, coordinator
    ``generous`` = 120), so the injected variant gives the suite the headroom it
    needs. Each manager reads its own flag via ``get_integer_value`` and feeds it
    to its weighted sliding-window limiter.
    """
    doc = _load(AGENT_PATH)
    for flag_key in ("policy.max_interventions_per_minute", "policy.coordinator_backstop_per_minute"):
        flag = doc["flags"][flag_key]
        flag["variants"]["active"] = int(per_minute)
        flag["defaultVariant"] = "active"
    _dump_in_place(AGENT_PATH, doc)


# =============================================================================
# Fixtures
# =============================================================================


# Generous rate-limit budget the intervention-flow suite runs under. The
# weighted sliding-window limiter is process-GLOBAL and lives on the
# game-coordinator's InterventionManager for the whole compose session — it is
# NOT reset between tests. A test that applies several interventions can leave
# residual weight in the shared 60s window, and budget-flag propagation to flagd
# lags the first write by a reload cycle; either can rate-limit a later test's
# final write regardless of suite size/order (the #824 flake: the last test's
# final write of [1.5,0.7,1.5,0.5,2.0] never reached 2.0). Pinning a high budget
# for the WHOLE suite, applied by the snapshot/restore fixture before any test
# body runs, removes that cross-test bleed systemically. Rate-limit SEMANTICS are
# unit-tested in services/game_coordinator/tests; the integration suite's job is
# propagation/effect, so a high budget here is the correct posture — a test that
# explicitly exercises rate limiting would pin its own (lower) budget after this.
SUITE_RATE_LIMIT_BUDGET = 50


@pytest.fixture
def flag_files(docker_compose):
    """Snapshot interventions.json + agent.json; restore byte-for-byte on exit.

    Also raises ``policy.max_interventions_per_minute`` to a generous suite-wide
    budget for the duration of the test (see ``SUITE_RATE_LIMIT_BUDGET``) and
    waits a reload cycle so flagd serves it before the test body issues any
    intervention write — this makes the process-global rate limiter
    headroom-independent of test ordering / suite size. Restored at teardown via
    the same in-place write so flagd reloads the committed baseline and no
    mutation (budget included) leaks into the repo or later tests.
    """
    with open(INTERVENTIONS_PATH) as fh:
        interventions_backup = fh.read()
    with open(AGENT_PATH) as fh:
        agent_backup = fh.read()

    # Pin the generous budget up front and wait until flagd actually SERVES it
    # before the test body runs, so the very first intervention write already
    # sees the headroom (#894: condition poll, not a fixed settle).
    set_policy_budget(SUITE_RATE_LIMIT_BUDGET)
    from openfeature.evaluation_context import EvaluationContext

    with flagd_probe_client(docker_compose, "agent") as client:
        assert poll_until(
            lambda: client.get_integer_value(
                "policy.max_interventions_per_minute", -1, EvaluationContext()
            )
            == SUITE_RATE_LIMIT_BUDGET,
            rewrite=lambda: set_policy_budget(SUITE_RATE_LIMIT_BUDGET),
        ), "flagd did not serve the pinned suite rate-limit budget"

    yield

    def _restore():
        with open(INTERVENTIONS_PATH, "w") as fh:
            fh.write(interventions_backup)
        with open(AGENT_PATH, "w") as fh:
            fh.write(agent_backup)

    with open(INTERVENTIONS_PATH) as fh:
        interventions_mutated = fh.read()
    with open(AGENT_PATH) as fh:
        agent_mutated = fh.read()
    _restore()
    # Wait until flagd serves the restored baselines (every flag this test
    # observably mutated) before the next test mutates them (#894).
    wait_flag_file_restored(
        docker_compose, "interventions", interventions_backup, interventions_mutated, _restore
    )
    wait_flag_file_restored(docker_compose, "agent", agent_backup, agent_mutated, _restore)


@pytest.fixture
async def game_client(docker_compose):
    client, channel = await get_game_client(docker_compose)
    yield client
    await channel.close()


# =============================================================================
# Observable-signal helpers
# =============================================================================


def _intervention_events(collector: GameEventCollector, type_id: str) -> list:
    """All agent_intervention events for a given intervention type."""
    return [
        e
        for e in collector.get_events("agent_intervention")
        if e.data.get("type") == type_id
    ]


async def _wait_for_intervention_event(
    collector: GameEventCollector,
    type_id: str,
    *,
    blocked: str | None = None,
    timeout: float = APPLY_TIMEOUT_SECONDS,
):
    """Wait until an agent_intervention event of ``type_id`` (optionally with a
    given ``blocked`` value) has been observed. Returns that event."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in _intervention_events(collector, type_id):
            if blocked is None or e.data.get("blocked") == blocked:
                return e
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"no agent_intervention(type={type_id}, blocked={blocked}) within {timeout}s; "
        f"saw: {[dict(e.data) for e in collector.get_events('agent_intervention')]}"
    )


async def _get_state(game_client) -> game_coordinator_pb2.GameInfo:
    resp = await game_client.GetGameState(game_coordinator_pb2.GetGameStateRequest())
    assert resp.success, f"GetGameState failed: {resp.error}"
    return resp.game_info


async def _player(game_client, serial: str):
    info = await _get_state(game_client)
    for p in info.players:
        if p.serial == serial:
            return p
    return None


async def _wait_for_sensitivity_factor(
    game_client, serial: str, expected: float, *, tol: float = 0.01, timeout: float = APPLY_TIMEOUT_SECONDS
) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        p = await _player(game_client, serial)
        if p is not None:
            last = p.sensitivity_factor
            if abs(p.sensitivity_factor - expected) <= tol:
                return
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"{serial} sensitivity_factor did not reach {expected} (last={last}) within {timeout}s"
    )


# =============================================================================
# Per-player targeting resolution against LIVE flagd (sequential)
#
# Kept sequential: it writes the UN-TARGETED (no gameId) player-targeting
# if-ladder and asserts file-level flagd resolution for distinct serials, so it
# is order-dependent on global flag state. The per-GAME, batched variant of the
# player-sensitivity *game effect* lives in test_parallel_intervention_flow.py.
# =============================================================================


@pytest.mark.asyncio
async def test_per_player_targeting_resolves_against_live_flagd(flag_files, docker_compose):
    """Per-player targeting against LIVE flagd RPC: the written if-ladder must
    resolve correctly via OpenFeature for distinct serials + unknown->default.
    Catches schema/targeting-rule rejections the Go unit tests cannot.
    """
    from openfeature.evaluation_context import EvaluationContext

    serial_a, serial_b, unknown = "SER_A", "SER_B", "SER_UNKNOWN"

    def _write_branches():
        # Write two distinct per-serial branches (merged into one if-ladder).
        # Idempotent, so it doubles as poll_until's rewrite self-heal.
        write_targeted("player_sensitivity_factor", "default", serial_a, 1.5)
        write_targeted("player_sensitivity_factor", "default", serial_b, 0.7)

    _write_branches()

    # resolver="rpc" deliberately: this test's PURPOSE is flagd's server-side
    # evaluation of the written targeting ladder (in-process would evaluate the
    # jsonlogic client-side and mask server-side rule rejections).
    with flagd_probe_client(docker_compose, "interventions", resolver="rpc") as client:

        def _eval(serial: str) -> float:
            return client.get_float_value(
                "player_sensitivity_factor", 1.0, EvaluationContext(targeting_key=serial)
            )

        # No fixed settle: poll until flagd serves the written ladder (provider
        # connect + file-watch reload may lag the write), then assert.
        assert poll_until(
            lambda: abs(_eval(serial_a) - 1.5) <= 0.01 and abs(_eval(serial_b) - 0.7) <= 0.01,
            APPLY_TIMEOUT_SECONDS,
            rewrite=_write_branches,
        ), (
            f"ladder did not resolve: serial_a={_eval(serial_a)} (want 1.5), "
            f"serial_b={_eval(serial_b)} (want 0.7)"
        )
        # Unknown serial falls through the if-ladder to the neutral default (1.0).
        assert abs(_eval(unknown) - 1.0) <= 0.01, f"unknown resolved to {_eval(unknown)}"


# =============================================================================
# Scenario 4: permission-block negative
# =============================================================================


@pytest.mark.asyncio
async def test_permission_layer_blocks_disallowed_intervention(flag_files, docker_compose, game_client):
    """Permission-block negative: with interventions_allowed=ambient (the
    committed default), eliminate_player is NOT in the allowed set, so the
    intervention is blocked (block_reason=not_allowed) and the player stays
    alive — the effect never reaches the kill path.
    """
    set_interventions_allowed("ambient")  # eliminate_player NOT allowed
    await setup_mock_controllers(docker_compose, count=4)
    serials = await get_mock_controller_serials(docker_compose)
    victim = serials[0]

    game_client_obj, channel = await get_game_client(docker_compose)
    try:
        collector = GameEventCollector(game_client_obj)
        async with collector:
            await start_game_via_menu(
                docker_compose, game_mode="JoustFFA", event_collector=collector
            )
            assert (await _player(game_client, victim)).alive

            write_edge("eliminate_player", victim)

            evt = await _wait_for_intervention_event(
                collector, "eliminate_player", blocked="true"
            )
            assert evt.data.get("blocked") == "true"
            assert evt.data.get("block_reason") == "not_allowed", (
                f"unexpected block_reason: {evt.data.get('block_reason')}"
            )

            # Game state unchanged: the blocked intervention never killed anyone.
            # Absence assertion — kept as a bounded wait by design (#894): there
            # is no event to wait for when the correct behavior is "nothing".
            await asyncio.sleep(RELOAD_SETTLE_SECONDS)
            assert (await _player(game_client, victim)).alive, (
                "blocked intervention still killed the player"
            )
    finally:
        await channel.close()


# =============================================================================
# Scenario 6: writer gate (agent never writes when disabled by default)
# =============================================================================


def test_agent_interventions_disabled_by_default():
    """Writer gate: the agent ActionSink is gated by the LIVE ``interventions_enabled``
    flagd flag (#1213, migrated from the AGENT_INTERVENTIONS_ENABLED env var) and the
    flag defaults OFF, so the agent never mutates interventions.json by default.

    The agent's decision loop cannot be driven deterministically from this harness
    (it lives in the ``agent`` compose profile and is not started for integration
    tests), so per the PR plan the no-write behavior is covered by the Go unit tests
    (services/agent/actions/gated_test.go + action_sink_test.go). Here we assert the
    flagd flag default keeps the gate OFF (fail-closed), which is the property that
    makes those unit tests load-bearing for the e2e contract. We check BOTH the base
    and CI flag configs since flagd serves ci/agent.json in CI (#801).
    """
    flagd_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../services/flagd")
    )
    for rel in ("agent.json", "ci/agent.json"):
        with open(os.path.join(flagd_dir, rel)) as fh:
            flags = json.load(fh)["flags"]
        assert "interventions_enabled" in flags, (
            f"{rel} should define the interventions_enabled safety gate (#1213)"
        )
        # The gate must FAIL CLOSED: the default variant resolves to false.
        flag = flags["interventions_enabled"]
        default_variant = flag["defaultVariant"]
        assert flag["variants"][default_variant] is False, (
            f"interventions_enabled in {rel} must default OFF (fail-closed); "
            f"defaultVariant {default_variant!r} resolves to "
            f"{flag['variants'][default_variant]!r}"
        )
