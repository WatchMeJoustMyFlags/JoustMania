"""
Intervention-smoke driver: deterministically prove EACH ``shadow_experimental``
agent action APPLIES end-to-end, independent of LLM model choice (#1144).

WHY THIS EXISTS (#1137 finding 2): verification run #5 could not demo the
advanced interventions (``set_player_handicap``, ``ramp_tempo``,
``partial_shield``, ``soft_penalty``) because the LLM backend was CUDA-down and
the rule fallback only ever picked ``grant_shield``. So whether an advanced
action ever reaches the coordinator's apply path was left to model selection.
This driver removes the model from the loop: it injects EACH currently-merged
``shadow_experimental`` action ONCE by sending the same game-scoped flag-service
write the agent sends (NOT via the agent's decision loop / LLM), and asserts the
game-coordinator APPLIED it.

WHAT "APPLIED" MEANS HERE. None of these five actions surface their applied
state on the ``GetGameState`` proto — ``PlayerInfo`` exposes only
``sensitivity_factor``; there is no handicap / shield / grace / partial-shield /
soft-penalty / tempo field (verified against proto/game_coordinator.proto). The
load-bearing, model-independent apply-proof is therefore the coordinator's own
``agent_intervention`` EventBus event with ``blocked="false"``: the
InterventionManager emits that event ONLY AFTER the action passed the permission
+ mode + battery chain AND its handler ran to completion without error (a
handler error records ``blocked="true", block_reason="handler_error"`` instead —
interventions.py ``_dispatch_one`` / ``_record``).

SCOPE: this driver asserts APPLY only (one ``blocked="false"`` event per action).
Revert/neutral-transition assertions are intentionally NOT made here: the
neutral-revert change-detection for STRING-valued targeted flags
(``partial_shield_seconds``, ``soft_penalty_action``) is currently unreliable
because ``interventions.py`` change-detects them via a float read (TYPE_MISMATCH
→ default; tracked separately in #1150). The time-boxed actions self-expire via
their per-frame ``*_until`` check independent of flag change-detection, so
apply-only is the deterministic, model-independent contract this regression
guards. Add revert assertions once #1150 is fixed.

DETERMINISM / GLOBAL GOTCHAS (carried from #893 / #919):
- ``interventions_allowed`` is a GLOBAL agent-domain flag (read with a bare
  EvaluationContext, NOT gameId-scopeable). We flip it ONCE to
  ``shadow_experimental`` — the only variant that allow-lists all five advanced
  actions — and poll flagd until it actually serves that string before any
  scenario writes a flag.
- The coordinator's weighted rate limiter is process-GLOBAL and not reset
  between tests. The re-exported ``intervention_env`` fixture pins a generous
  suite-wide budget (``SUITE_RATE_LIMIT_BUDGET``) in BOTH rate-limit flags and
  waits a reload cycle before the body runs, so the handful of injections here
  sit far under budget.
- Every intervention write is game-scoped through the flag service (FR-126): a
  write for game A is a guaranteed no-op for every other live session, so
  scenarios batch concurrently without cross-talk.
- All waits POLL for the apply event (no fixed-sleep one-shots), so the driver
  is robust to flag-reload latency (#894).

This is the standing regression each new #1103 ``shadow_experimental`` action
plugs into: add a scenario that writes the action's flag and asserts its
``blocked="false"`` apply event.
"""

import asyncio
import json
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import game_coordinator_pb2  # noqa: E402

from tests.integration.helpers import (  # noqa: E402
    GameEventCollector,
    async_poll_until,
    build_start_config,
    flagd_probe_client,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    list_games,
    remove_reserved_controllers,
    setup_mock_controllers,
    start_game_headless,
    verify_controllers_have_color,
)
from tests.integration.test_intervention_flow import (  # noqa: E402
    AGENT_PATH,
    intervention_env,  # re-exported: snapshot/restore + suite-wide rate-limit budget pin
    set_interventions_allowed,
)
from tests.integration.test_parallel_intervention_flow import (  # noqa: E402
    _await_running,
    _start_headless_with_retry,
    _wait_for_game_colors,
    _wait_for_intervention,
)
from tests.integration.flag_service import set_game_value, set_player_value  # noqa: E402

# The single permission variant that allow-lists every advanced action under
# test. Resolved from the live agent.json so the test tracks the committed
# allow-list rather than hard-coding the (long) comma-separated string.
PERMISSION_VARIANT = "shadow_experimental"

# Keep re-exported fixture referenced so linters don't flag it unused.
__all__ = ["intervention_env"]


# =============================================================================
# gameId-scoped flag-file writers (moved from test_parallel_intervention_flow.py,
# #1422). Every write is scoped to one game_id by a flagd targeting rule on the
# ``gameId`` context var, with the default variant left neutral, so a write for
# game A resolves to a no-op for every other live session. flagd's side of this
# is checked in tests/contract/test_flagd_resolution.py
# (test_gameid_scoped_ladders_resolve_only_for_their_game).
#
# The writers are synchronous (no ``await`` between the in-place read and
# write), so two gathered coroutines cannot interleave mid-write; the lock is
# defense in depth.
# =============================================================================

_FILE_LOCK = threading.Lock()


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write (rename over a watched bind mount triggers EBUSY)."""
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)


def _branch_var_name(game_id: str, serial: str | None) -> str:
    """A flagd variant name unique per (game_id[, serial]).

    game_id contains an underscore (``game_<hex>``), so the name is a hash and
    is never parsed; the routing lives in the ``_branches`` side registry.
    """
    key = f"{game_id}\x00{serial or ''}"
    return "g_" + uuid.uuid5(uuid.NAMESPACE_OID, key).hex


def _rebuild_ladder(flag: dict, neutral: str) -> None:
    """Rebuild the flag's targeting if-ladder from its ``_branches`` registry.

    Each ``variant_name -> {gameId, serial?}`` entry becomes one branch matching
    ``gameId`` (and ``targetingKey`` for player-targeted flags), falling through
    to the neutral default, so concurrent writes for distinct games compose.
    """
    branches = flag.get("_branches", {})
    expr: object = neutral
    for name in sorted(branches, reverse=True):
        meta = branches[name]
        serial = meta.get("serial")
        if serial:
            cond = {
                "and": [
                    {"===": [{"var": "gameId"}, meta["gameId"]]},
                    {"===": [{"var": "targetingKey"}, serial]},
                ]
            }
        else:
            cond = {"===": [{"var": "gameId"}, meta["gameId"]]}
        expr = {"if": [cond, name, expr]}
    flag["targeting"] = expr
    flag["defaultVariant"] = neutral


def write_targeted_for_game(
    flag_key: str, game_id: str, serial: str, value, *, neutral: str = "default"
) -> None:
    """Per-player write scoped to one gameId AND one serial."""
    with _FILE_LOCK:
        doc = _load(INTERVENTIONS_PATH)
        flag = doc["flags"][flag_key]
        var_name = _branch_var_name(game_id, serial)
        flag["variants"][var_name] = value
        flag.setdefault("_branches", {})[var_name] = {"gameId": game_id, "serial": serial}
        _rebuild_ladder(flag, neutral)
        _dump_in_place(INTERVENTIONS_PATH, doc)


def write_state_for_game(flag_key: str, game_id: str, value, *, neutral: str) -> None:
    """Session-state write scoped to one gameId."""
    with _FILE_LOCK:
        doc = _load(INTERVENTIONS_PATH)
        flag = doc["flags"][flag_key]
        var_name = _branch_var_name(game_id, None)
        flag["variants"][var_name] = value
        flag.setdefault("_branches", {})[var_name] = {"gameId": game_id, "serial": None}
        _rebuild_ladder(flag, neutral)
        _dump_in_place(INTERVENTIONS_PATH, doc)


# =============================================================================
# Game helpers (moved from test_parallel_intervention_flow.py, #1422)
# =============================================================================


async def _wait_for_intervention(
    collector: GameEventCollector, type_id: str, *, blocked=None, timeout: float = APPLY_TIMEOUT_SECONDS
):
    """Poll the game's stream for an ``agent_intervention`` of ``type_id``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hits = [
            e
            for e in collector.get_events("agent_intervention")
            if e.data.get("type") == type_id and (blocked is None or e.data.get("blocked") == blocked)
        ]
        if hits:
            return hits[-1]
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"no agent_intervention(type={type_id}, blocked={blocked}) within {timeout}s; "
        f"saw: {[dict(e.data) for e in collector.get_events('agent_intervention')]}"
    )


async def _wait_for_game_colors(mock_client, serials: list[str], timeout: float = 10.0) -> None:
    """Poll until every controller shows a non-zero LED (parallel load lags it)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await verify_controllers_have_color(mock_client, serials)
            return
        except AssertionError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.5)


async def _start_headless_with_retry(game_client, start_config, attempts: int = 4, backoff: float = 2.0):
    """Start headless, retrying brief "Game already in progress" cap windows.

    A sibling batch coroutine's force-ended game may still be in its ENDING
    window when this start hits the concurrency cap; the slot frees moments
    later, so a short retry is correct (mirrors test_parallel_lifecycle).
    """
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return await start_game_headless(game_client, start_config, timeout=25.0)
        except Exception as exc:  # noqa: BLE001 - only the cap window is retryable
            if "Game already in progress" not in str(exc):
                raise
            last_exc = exc
            await asyncio.sleep(backoff)
    raise last_exc


async def _await_running(game_client, game_id: str, timeout: float = 12.0) -> None:
    """Poll ListGames until this game is RUNNING (start returns on STARTING)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        running = {
            g.game_id
            for g in await list_games(game_client)
            if g.state == game_coordinator_pb2.RUNNING
        }
        if game_id in running:
            return
        await asyncio.sleep(0.3)
    raise AssertionError(f"game {game_id} never reached RUNNING within {timeout}s")


def _shadow_experimental_string() -> str:
    with open(AGENT_PATH) as fh:
        return json.load(fh)["flags"]["interventions_allowed"]["variants"][
            PERMISSION_VARIANT
        ]


def _set_permission() -> None:
    set_interventions_allowed(PERMISSION_VARIANT)


# =============================================================================
# game-domain (game.json) gameId-scoped write — ramp_tempo needs the
# ``game.tempo_schedule_mode`` SOURCE flipped to ``agent`` so the armed curve
# drives the schedule. That flag lives in the GAME domain, not interventions,
# and is read with a gameId context (tempo_schedule.py resolve_tempo_schedule_mode),
# so we scope it to this game exactly like the intervention writers.
# =============================================================================

from tests.integration.helpers import active_flag_file  # noqa: E402

GAME_PATH = active_flag_file("game")


def _set_tempo_schedule_mode_for_game(game_id: str, mode: str) -> None:
    """gameId-scope ``game.tempo_schedule_mode`` to ``mode`` for one game only.

    Serves ``mode`` when ``gameId == game_id`` and falls back to the committed
    ``rule`` default for every other session, so concurrent games keep their own
    schedule source. Idempotent (rebuilds the same single-branch rule each call)
    so it doubles as a poll self-heal rewrite.
    """
    with open(GAME_PATH) as fh:
        doc = json.load(fh)
    flag = doc["flags"]["tempo_schedule_mode"]
    flag.setdefault("variants", {})[mode] = mode
    flag["targeting"] = {"if": [{"===": [{"var": "gameId"}, game_id]}, mode, "rule"]}
    flag["defaultVariant"] = "rule"
    with open(GAME_PATH, "w") as fh:
        fh.write(json.dumps(doc, indent=2) + "\n")


# =============================================================================
# Per-action scenarios. Each: own tagged mock controllers + own headless shadow
# game + gameId-scoped flag write + ONE focused apply assertion. Cleans up in a
# finally. Raising tags the failure with the scenario name in the batch runner.
# =============================================================================


async def _scenario_set_player_handicap(docker_compose, tag: str) -> None:
    """#1107 ``set_player_handicap``: a per-player handicap factor != 1.0 for the
    targeted player of THIS game reaches the coordinator and APPLIES (unblocked).

    Handicap is not surfaced on GetGameState, so the apply-proof is the
    ``set_player_handicap`` ``blocked="false"`` event — permission+mode+battery
    passed and ``handle_player_handicap_factor`` ran without error. (Player-
    targeted state flags dispatch as a fan-out with an EMPTY event ``target``;
    the targeting is enforced in flagd's per-serial ladder, so the per-player
    scoping is asserted by the TC-013 journey and the flagd contract tier,
    not re-asserted on this event.)
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=tag
        )
        target = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # "help" = 1.25 (harder to die), != the neutral 1.0 default -> an actual
        # intervention. Scoped to one gameId + serial.
        set_player_value("player_handicap_factor", game_id, target, 1.25)

        evt = await _wait_for_intervention(
            collector, "set_player_handicap", blocked="false"
        )
        assert evt.data.get("blocked") == "false"
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            tag,
            mock_channel,
            game_channel,
        )


async def _scenario_partial_shield(docker_compose, tag: str) -> None:
    """#1132 ``partial_shield``: a time-boxed threshold BOOST for the targeted
    player reaches the coordinator and APPLIES (unblocked).

    Apply-proof: a ``partial_shield`` ``blocked="false"`` event — permission+mode
    +battery passed and ``handle_partial_shield`` re-resolved the "5:2.0" value as
    a string and called ``grant_partial_shield`` without error. The boost is a
    WINDOW that expires on its own (no flag revert handler; the deadline on
    ``player.partial_shield_until`` lapses), and neither the boost nor its expiry
    is surfaced on GetGameState, so the apply event is the load-bearing signal.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=tag
        )
        target = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # "5:2.0" — 5s of a 2.0x harder-to-die boost, scoped to this player.
        set_player_value("partial_shield_seconds", game_id, target, "5:2.0")
        evt = await _wait_for_intervention(collector, "partial_shield", blocked="false")
        assert evt.data.get("blocked") == "false"
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            tag,
            mock_channel,
            game_channel,
        )


async def _scenario_soft_penalty_tighten(docker_compose, tag: str) -> None:
    """#1134 ``soft_penalty`` "tighten": a short EXPIRING threshold WEAKENING for
    the targeted player reaches the coordinator and APPLIES (unblocked).

    Apply-proof: a ``soft_penalty`` ``blocked="false"`` event for this player when
    the value is ``tighten:5:0.5`` — ``handle_soft_penalty`` parsed the tighten
    form and called ``apply_soft_penalty`` (the MIRROR of partial_shield:
    temporarily easier to die, clamped, window-expiring). The weakening window
    expires on its own and is not on GetGameState, so the apply event is the
    load-bearing signal.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=tag
        )
        target = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # "tighten:5:0.5" — 5s at a 0.5x (weaker) threshold, scoped to this player.
        set_player_value("soft_penalty_action", game_id, target, "tighten:5:0.5")
        evt = await _wait_for_intervention(collector, "soft_penalty", blocked="false")
        assert evt.data.get("blocked") == "false"
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            tag,
            mock_channel,
            game_channel,
        )


async def _scenario_soft_penalty_warn(docker_compose, tag: str) -> None:
    """#1134 ``soft_penalty`` "warn": the feedback-only form applies (fires the
    warning cue) with NO threshold change.

    "warn" and "tighten" share the ``soft_penalty`` type_id, so the apply-proof is
    the same ``blocked="false"`` event; the distinction (warn changes no threshold)
    is a code-level property covered by the unit tests — here we prove the "warn"
    value reaches the coordinator and is applied (not blocked/no-op'd) for this
    player.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=tag
        )
        target = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        set_player_value("soft_penalty_action", game_id, target, "warn")
        evt = await _wait_for_intervention(collector, "soft_penalty", blocked="false")
        assert evt.data.get("blocked") == "false"
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            tag,
            mock_channel,
            game_channel,
        )


async def _scenario_ramp_tempo(docker_compose, tag: str) -> None:
    """#1122 ``ramp_tempo``: a scheduled tempo CURVE arms on the live game and the
    apply path runs unblocked, with ``game.tempo_schedule_mode=agent`` so the
    curve drives the schedule.

    The tempo curve / music_speed is not on GetGameState, so the apply-proof is
    the ``ramp_tempo`` ``blocked="false"`` event (session-scoped, empty target):
    the handler parsed the descriptor and armed ``game.tempo_ramp`` without error.
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=tag
        )
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # Flip THIS game's tempo SOURCE to agent so the curve drives the schedule,
        # and wait for the coordinator to actually serve+swap it before arming.
        _set_tempo_schedule_mode_for_game(game_id, "agent")
        with flagd_probe_client(docker_compose, "game") as flag_client:
            from openfeature.evaluation_context import EvaluationContext

            assert await async_poll_until(
                lambda: (
                    flag_client.get_string_value(
                        "tempo_schedule_mode",
                        "rule",
                        EvaluationContext(attributes={"gameId": game_id}),
                    )
                    == "agent"
                ),
                rewrite=lambda: _set_tempo_schedule_mode_for_game(game_id, "agent"),
            ), "flagd did not serve tempo_schedule_mode=agent for this game"

        # Arm a ramp toward 1.3x over 8s (linear), session-scoped to this game.
        set_game_value("tempo_ramp", game_id, "1.3:8:linear")
        evt = await _wait_for_intervention(collector, "ramp_tempo", blocked="false")
        assert evt.data.get("blocked") == "false"
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            tag,
            mock_channel,
            game_channel,
        )


async def _scenario_grant_shield(docker_compose, tag: str) -> None:
    """``grant_shield``: total grace immunity for the targeted player applies.

    grace_until is not surfaced on GetGameState, so the apply-proof is the
    ``grant_shield`` ``blocked="false"`` event for this player — the apply path
    that #1137 already confirmed live, asserted here deterministically (the
    regression anchor every new advanced action joins).
    """
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    collector = None
    game_id = ""
    try:
        serials = await setup_mock_controllers(
            docker_compose, count=4, reserved=True, tag=tag
        )
        target = serials[0]
        game_id, collector = await _start_headless_with_retry(
            game_client, build_start_config("JoustFFA", serials)
        )
        await _await_running(game_client, game_id)
        await _wait_for_game_colors(mock_client, serials)

        # 5s of grace, scoped to this player (neutral default is 0 = no shield).
        set_player_value("shield_seconds", game_id, target, 5)
        evt = await _wait_for_intervention(collector, "grant_shield", blocked="false")
        assert evt.data.get("blocked") == "false"
    finally:
        await _teardown(
            collector,
            game_client,
            game_id,
            docker_compose,
            tag,
            mock_channel,
            game_channel,
        )


async def _teardown(
    collector, game_client, game_id, docker_compose, tag, mock_channel, game_channel
):
    if collector is not None:
        await collector.stop()
    if game_id:
        await force_end_game_by_id(
            game_client, game_id, reason="intervention_smoke_cleanup"
        )
    await remove_reserved_controllers(docker_compose, tag)
    await mock_channel.close()
    await game_channel.close()


# =============================================================================
# Batches + gathered runner. All scenarios share the single GLOBAL
# ``interventions_allowed=shadow_experimental`` permission and write DISTINCT
# flag keys per action (so no per-flag slot is contended). Batch size <=
# GAME_MAX_CONCURRENT_GAMES (4 in CI). The two soft_penalty forms write the SAME
# ``soft_penalty_action`` key, so they are split across batches to keep each
# batch's gameId-scoped ladders independent.
# =============================================================================

_BATCHES = [
    pytest.param(
        {
            "set_player_handicap": _scenario_set_player_handicap,
            "partial_shield": _scenario_partial_shield,
            "soft_penalty_tighten": _scenario_soft_penalty_tighten,
            "ramp_tempo": _scenario_ramp_tempo,
        },
        id="shadow-experimental-batch-a",
    ),
    pytest.param(
        {
            "soft_penalty_warn": _scenario_soft_penalty_warn,
            "grant_shield": _scenario_grant_shield,
        },
        id="shadow-experimental-batch-b",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenarios", _BATCHES)
async def test_shadow_experimental_interventions_apply(
    intervention_env, docker_compose, headless_cleanup, scenarios
):
    """Inject each ``shadow_experimental`` action once via flagd targeting (NOT
    the LLM) and assert the coordinator APPLIED it (``blocked="false"`` event).

    Model-independent: nothing here drives the agent's decision loop; every
    action is written directly to its intervention flag scoped to its own
    headless shadow game. Proves the apply path for set_player_handicap (#1107),
    partial_shield (#1132), soft_penalty tighten+warn (#1134), ramp_tempo
    (#1122), and grant_shield — the gap #1137 finding 2 flagged.
    """
    # GLOBAL permission: allow-list every advanced action ONCE (not gameId-
    # scopeable). Poll flagd until it actually serves the variant before writes.
    _set_permission()
    expected = _shadow_experimental_string()
    from openfeature.evaluation_context import EvaluationContext

    with flagd_probe_client(docker_compose, "agent") as flag_client:
        assert await async_poll_until(
            lambda: (
                flag_client.get_string_value(
                    "interventions_allowed", "", EvaluationContext()
                )
                == expected
            ),
            rewrite=_set_permission,
        ), "flagd did not serve interventions_allowed=shadow_experimental"

    # Register tags up front so the fixture sweeps reserved controllers even if a
    # coroutine dies before its own finally runs.
    tags = {}
    for name in scenarios:
        tag = f"intv-smoke-{name}"
        headless_cleanup.add_tag(tag)
        tags[name] = tag

    async def _run(name, fn):
        try:
            await fn(docker_compose, tags[name])
        except Exception as exc:  # noqa: BLE001 - re-raise tagged with scenario
            raise AssertionError(
                f"[{name}] shadow_experimental action failed to apply: {exc}"
            ) from exc

    results = await asyncio.gather(
        *(_run(name, fn) for name, fn in scenarios.items()),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        messages = "; ".join(str(f) for f in failures)
        raise AssertionError(
            f"{len(failures)}/{len(scenarios)} actions failed to apply: {messages}"
        )
