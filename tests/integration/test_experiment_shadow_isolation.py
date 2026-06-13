"""
Experiment shadow-isolation integration test (#932, closes the #954 review gap).

Proves the ONE agent-experiment safety link that no other automated gate covered
before this test: that a *written* game-flag experiment actually reaches a SHADOW
game and NEVER a REAL game, end-to-end through the REAL flagd server — not an
inline JSONLogic model (the Go `experiment.Gate`'s belt-and-suspenders eval in
`services/agent/experiment/gate.go` evaluates jsonlogic in-process; this test
proves the SAME targeting block resolves identically on the live flagd that the
game services actually read through).

The safety boundary, end-to-end:

    agent experiment writer (services/agent/experiment/writer.go) adds an
    `agent_experiment` variant + a shadow-scoped targeting rule to game.json
      ->  flagd file-watch reload (live server, flagSetId=game)
      ->  a SHADOW game's eval context (game_kind="shadow", set by the #954
          GameSession wiring) resolves the EXPERIMENTAL value
      ->  a REAL game's eval context (game_kind="real", set by the menu / the
          fail-safe API-level default in lib/feature_flags) resolves the
          BASELINE (defaultVariant) value — byte-identical to pre-experiment.

What makes this the #954 follow-up: PR #954 (`feat/game-kind-eval-context`) wired
`game_kind` into the OpenFeature eval context (`lib/feature_flags`,
`GameSession`, the menu). Its review noted that the one link still lacking an
automated gate was the runtime join — that the wiring + the writer's targeting
rule + real flagd together honour the "shadow only, never real" invariant. The
Go unit tests cover the writer/gate against an inline jsonlogic model; the #954
Python unit tests cover the context plumbing in isolation. NEITHER exercises the
actual flagd resolution of the written rule under the real context values. This
test does, and is the agent-experiment sibling of
`test_per_player_targeting_resolves_against_live_flagd` (intervention domain,
#730) and `test_rollout_plumbing.py` (rollout domain, #737).

Scope decision (mirrors the intervention/rollout live-flagd tests):

- The agent service (services/agent, Go) is NOT run in the integration compose —
  it lives behind the `agent` compose profile, exactly like the intervention
  ActionSink and the rollout writer. So we do not run the Go writer here; we
  REPRODUCE the exact mutation it makes (`buildShadowTargeting` in
  `services/agent/experiment/targeting.go`: add the reserved `agent_experiment`
  variant + `{"if":[{"!=":[{"var":"game_kind"},"real"]},"agent_experiment",
  <pre-existing-else-or-null>]}`). The point of THIS test is the flagd + #954
  game_kind wiring join, not the writer — the writer's own correctness is the Go
  unit tests' job (`services/agent/experiment/*_test.go`).

- We resolve via the flagd RPC resolver (port 8013, `flagSetId=game`) with
  explicit `game_kind` eval contexts. resolver="rpc" is deliberate: this test's
  PURPOSE is flagd's server-side evaluation of the written `!= real` targeting
  rule (in-process would evaluate the jsonlogic client-side and mask a
  server-side rule rejection — the same reason
  test_per_player_targeting_resolves_against_live_flagd uses rpc).

  The `game_kind` values used are the EXACT strings the #954 wiring puts on the
  context: `"shadow"` for a shadow session and `"real"` for a real game / the
  fail-safe API-level default. `test_game_kind_constants_match_source` pins them
  to the single source of truth in BOTH languages (Python `lib.feature_flags`
  GAME_KIND_* and the Go `experiment` GameKindVar/GameKindReal contract), so a
  drift fails loudly rather than silently testing a stale value.

Kept sequential (not batched per-game like #893/#778): it mutates the GLOBAL
`game` flagset's targeting and asserts file-level flagd resolution for distinct
context values, so it is order-dependent on global flag state — exactly the
constraint that keeps test_per_player_targeting_resolves_against_live_flagd
sequential. The autouse `coordinator_idle` quiesce + the `game_flags` snapshot
fixture keep it from leaking into neighbours.

The `game` flagset is a bind-mounted host file. WHICH host file flagd serves is
now resolved from one place (issue #959): helpers.active_flag_file("game") =
$FLAGD_FLAG_DIR/game.json, and the integration harness points FLAGD_FLAG_DIR at
the directory flagd is mounted on (the CI flag dir under docker-compose.ci.yml).
So the file this test mutates IS the file flagd serves — no glob over game*.json,
no env branch. The `game_flags` fixture snapshots/restores that single file
byte-for-byte and the test derives its baselines from the LIVE flagd.
"""

import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import (  # noqa: E402
    FLAGD_RESOLVE_TIMEOUT_SECONDS,
    active_flag_file,
    flagd_probe_client,
    poll_until,
)

# game_kind context var + values — the SAME contract the #954 wiring sets on the
# eval context (lib/feature_flags GAME_KIND_*) and that the experiment writer
# scopes targeting on (services/agent/experiment/targeting.go GameKindVar/Real).
# We define them here rather than importing lib.feature_flags because the
# integration-tests venv intentionally does NOT install the full app dependency
# stack (lib.feature_flags pulls openfeature.contrib.hook, absent here). The
# values are pinned to source by test_game_kind_constants_match_source below, so
# any drift fails loudly — the import-free equivalent of the #954 cross-language
# constant-match unit test.
GAME_KIND_VAR = "game_kind"
GAME_KIND_REAL = "real"
GAME_KIND_SHADOW = "shadow"

# The single active host `game` flag file (#959): $FLAGD_FLAG_DIR/game.json,
# resolved via the same accessor flagd, the agent writers, and the rest of the
# integration suite use. Writing here on the host is exactly what the Go
# experiment Writer does inside the container, and it is the exact file flagd
# serves (the harness sets FLAGD_FLAG_DIR to the mounted dir). No glob over
# game*.json, no compose-stack branch. Kept as a one-element list so the
# apply/snapshot/restore loops below are unchanged.
GAME_PATHS = [active_flag_file("game")]

# The single reserved variant name the experiment Writer adds/overwrites. MUST
# match services/agent/experiment/targeting.go ExperimentVariant.
EXPERIMENT_VARIANT = "agent_experiment"

# How long flagd's file-watch reload of the experiment write is given to be
# served before asserting. We use the harness-wide FLAGD_RESOLVE_TIMEOUT_SECONDS
# (30s): flagd v0.16.0 can briefly serve a STATIC/defaultVariant state mid-reload
# before the new targeting rule takes effect (observed: a transient resolve to
# the first variant under the loaded warmup), and poll_until's rewrite self-heal
# re-issues the write every couple of seconds to produce a fresh inotify event
# if a reload was missed/coalesced — a generous deadline avoids a flaky timeout.
RESOLVE_TIMEOUT_SECONDS = FLAGD_RESOLVE_TIMEOUT_SECONDS


# =============================================================================
# Experiment-write helper (mirrors services/agent/experiment/writer.go +
# targeting.go buildShadowTargeting). Pure read-modify-write, in place.
# =============================================================================


def _load(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _dump_in_place(path: str, doc: dict) -> None:
    """In-place truncate+write — matches Writer.writeInPlace (no temp+rename,
    which EBUSYs on the flagd bind mount)."""
    text = json.dumps(doc, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(text)


def _build_shadow_targeting(existing_targeting):
    """Reproduce targeting.go buildShadowTargeting.

    Returns {"if":[{"!=":[{"var":"game_kind"},"real"]}, "agent_experiment",
    <else>]} where <else> is the flag's PRE-EXISTING targeting verbatim (so a
    flag that already targets game_mode keeps doing so for real games) or null
    when there was none. The "!= real" condition is shadow-scoped BY
    CONSTRUCTION: a real context makes it false, so flagd evaluates <else> —
    byte-identical to the pre-write resolution.
    """
    else_branch = existing_targeting if existing_targeting is not None else None
    return {
        "if": [
            {"!=": [{"var": GAME_KIND_VAR}, GAME_KIND_REAL]},
            EXPERIMENT_VARIANT,
            else_branch,
        ]
    }


def write_experiment(flag_key: str, experimental_value) -> None:
    """Apply a shadow-scoped experiment to a `game` flag, exactly as the Go
    Writer.Apply does: ADD the reserved `agent_experiment` variant (never
    mutating an existing variant or the defaultVariant) and wrap any
    pre-existing targeting in the shadow branch. Idempotent (overwrite, not
    accumulate), so it doubles as poll_until's rewrite self-heal.

    Applied to the single active game flag file (GAME_PATHS), which is exactly
    the file flagd serves ($FLAGD_FLAG_DIR/game.json, #959).
    """
    for path in GAME_PATHS:
        doc = _load(path)
        flag = doc["flags"][flag_key]
        # Merge the reserved variant in — every game-authored variant is kept.
        flag.setdefault("variants", {})[EXPERIMENT_VARIANT] = experimental_value
        flag["targeting"] = _build_shadow_targeting(flag.get("targeting"))
        _dump_in_place(path, doc)


# =============================================================================
# Fixture: snapshot/restore game.json byte-for-byte (mirrors flag_files /
# rollout_file). Waits until flagd serves the restored baseline so the
# experiment mutation never leaks into a neighbouring test.
# =============================================================================


@pytest.fixture
def game_flags(docker_compose):
    # Snapshot the active game flag file (GAME_PATHS) so it is restored
    # byte-for-byte after the test — it is the file flagd serves (#959).
    backups = {}
    for path in GAME_PATHS:
        with open(path) as fh:
            backups[path] = fh.read()

    yield

    def _restore():
        for path, text in backups.items():
            with open(path, "w") as fh:
                fh.write(text)

    _restore()
    # Wait until the LIVE flagd no longer serves the experiment (the added
    # `agent_experiment` variant + shadow targeting are gone) before the next
    # test runs — file-agnostic, so it is correct whichever game*.json flagd
    # mounts. Signal: with the experiment present, a shadow context resolves
    # invincibility_seconds to the experiment value while a real context
    # resolves the baseline (shadow != real); once restored, the experiment
    # variant is gone so BOTH fall through to the same defaultVariant
    # (invincibility has no other targeting) — shadow == real again. We can't
    # know the served file's exact value here, so we assert that equality rather
    # than a hardcoded baseline. ``_restore`` doubles as poll_until's rewrite
    # self-heal (fresh inotify event for a missed/coalesced reload).
    from openfeature.evaluation_context import EvaluationContext

    shadow_ctx = EvaluationContext(attributes={GAME_KIND_VAR: GAME_KIND_SHADOW})
    real_ctx = EvaluationContext(attributes={GAME_KIND_VAR: GAME_KIND_REAL})
    with flagd_probe_client(docker_compose, "game", resolver="rpc") as client:

        def _restored() -> bool:
            shadow = client.get_float_value(
                "invincibility_seconds", float("nan"), shadow_ctx
            )
            real = client.get_float_value(
                "invincibility_seconds", float("nan"), real_ctx
            )
            return abs(shadow - real) <= 0.01

        assert poll_until(_restored, FLAGD_RESOLVE_TIMEOUT_SECONDS, rewrite=_restore), (
            "flagd still served the experiment after restore (game*.json leak)"
        )


# =============================================================================
# Contract pin: the game_kind constants used here MUST match the single source
# of truth in both languages (Go writer/gate + Python #954 wiring). This is the
# import-free equivalent of the #954 cross-language constant-match unit test —
# it fails loudly if the var name / "real" / "shadow" values ever drift, so the
# resolution assertions below can never silently test a stale contract.
# =============================================================================


def test_game_kind_constants_match_source():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

    go_targeting = os.path.join(repo_root, "services/agent/experiment/targeting.go")
    with open(go_targeting) as fh:
        go_src = fh.read()
    assert f'GameKindVar = "{GAME_KIND_VAR}"' in go_src, (
        "Go GameKindVar drifted from the test's GAME_KIND_VAR"
    )
    assert f'GameKindReal = "{GAME_KIND_REAL}"' in go_src, (
        "Go GameKindReal drifted from the test's GAME_KIND_REAL"
    )
    assert f'ExperimentVariant = "{EXPERIMENT_VARIANT}"' in go_src, (
        "Go ExperimentVariant drifted from the test's EXPERIMENT_VARIANT"
    )

    py_flags = os.path.join(repo_root, "lib/feature_flags.py")
    with open(py_flags) as fh:
        py_src = fh.read()
    assert f'GAME_KIND_VAR = "{GAME_KIND_VAR}"' in py_src, (
        "Python GAME_KIND_VAR drifted from the test's GAME_KIND_VAR"
    )
    assert f'GAME_KIND_REAL = "{GAME_KIND_REAL}"' in py_src, (
        "Python GAME_KIND_REAL drifted from the test's GAME_KIND_REAL"
    )
    assert f'GAME_KIND_SHADOW = "{GAME_KIND_SHADOW}"' in py_src, (
        "Python GAME_KIND_SHADOW drifted from the test's GAME_KIND_SHADOW"
    )


# =============================================================================
# The test
# =============================================================================


@pytest.mark.asyncio
async def test_experiment_reaches_shadow_never_real(game_flags, docker_compose):
    """A written shadow-scoped experiment resolves the EXPERIMENTAL value for a
    shadow game and leaves every REAL context resolving its pre-experiment
    BASELINE — via the REAL flagd, whichever game flagset file is mounted.

    Baselines are derived DYNAMICALLY from the live flagd BEFORE the mutation
    (not hardcoded from game.json), because CI serves the ci/ flag dir
    (ci/game.json) — which carries different defaultVariant values (e.g.
    invincibility_seconds defaults to "2s" there, "4s" locally). Hardcoding the
    game.json numbers is exactly what made this test time out under CI. By
    snapshotting each context's live resolution first and asserting it is
    UNCHANGED afterwards, the "real never moves" safety invariant holds against
    either file with no env branch.

    Two flags, chosen to cover both branches of buildShadowTargeting:

    - ``invincibility_seconds``: NO pre-existing targeting → the experiment's
      else-branch is ``null`` → real falls through to its defaultVariant.
    - ``sensitivity``: HAS pre-existing targeting (game_mode==Werewolf → "fast")
      → the experiment wraps it as the else-branch, so a real game keeps its
      prior targeted behaviour verbatim while a shadow game overrides — the
      structural half of the experiment Gate's invariant, on live flagd.
    """
    from openfeature.evaluation_context import EvaluationContext

    shadow_ctx = EvaluationContext(attributes={GAME_KIND_VAR: GAME_KIND_SHADOW})
    real_ctx = EvaluationContext(attributes={GAME_KIND_VAR: GAME_KIND_REAL})
    # A real game whose menu config also set game_mode=Werewolf: proves the
    # experiment's else-branch preserves the pre-existing real targeting.
    real_werewolf_ctx = EvaluationContext(
        attributes={GAME_KIND_VAR: GAME_KIND_REAL, "game_mode": "Werewolf"}
    )
    # No game_kind at all: at the RAW flagd layer this resolves EXPERIMENTAL
    # (the rule is "!= real", and a missing var is not "real"). The fail-safe
    # "real-by-default" property lives in the APPLICATION context, NOT in flagd —
    # lib.feature_flags defaults the always-present API-level context to
    # game_kind="real" so an unlabeled evaluation is protected by construction
    # (verified in #954). Asserting the raw-flagd behaviour here documents exactly
    # where the safety boundary is enforced (and would catch a writer that
    # mistakenly used "== shadow", which would silently flip this).
    empty_ctx = EvaluationContext()

    with flagd_probe_client(docker_compose, "game", resolver="rpc") as client:

        def _inv(ctx) -> float:
            return client.get_float_value("invincibility_seconds", float("nan"), ctx)

        def _sens(ctx) -> int:
            return int(client.get_integer_value("sensitivity", -1, ctx))

        # Wait for the RPC provider to actually connect before reading baselines:
        # a cold probe resolves to the typed default (nan / -1) until the gRPC
        # stream is up, which would otherwise poison the dynamically-derived
        # baseline. Ready == both flags resolve a real (non-default) value.
        def _provider_ready() -> bool:
            return not math.isnan(_inv(real_ctx)) and _sens(real_ctx) != -1

        assert poll_until(_provider_ready, RESOLVE_TIMEOUT_SECONDS), (
            "flagd RPC provider did not connect / serve a baseline in time"
        )

        # --- Derive baselines from the live flagd BEFORE mutating. ---
        # Each real context's pre-experiment value is whatever the mounted file
        # serves; the safety invariant is that these do not move after the write.
        inv_real_baseline = _inv(real_ctx)
        sens_real_baseline = _sens(real_ctx)
        sens_real_werewolf_baseline = _sens(real_werewolf_ctx)

        # Choose experiment values DISTINCT from the live real baselines so
        # shadow != real is observable. Both are existing variant values in
        # every game flagset file; we pick the one furthest from the baseline.
        inv_experiment = 8.0 if inv_real_baseline < 8.0 else 2.0
        sens_experiment = 0 if sens_real_baseline != 0 else 4
        assert abs(inv_experiment - inv_real_baseline) > 0.01, (
            "invincibility experiment value must differ from the real baseline "
            f"({inv_real_baseline}) for the shadow!=real assertion to be meaningful"
        )
        assert sens_experiment != sens_real_baseline, (
            "sensitivity experiment value must differ from the real baseline "
            f"({sens_real_baseline})"
        )

        def _write():
            write_experiment("invincibility_seconds", inv_experiment)
            write_experiment("sensitivity", sens_experiment)

        _write()

        # Poll until flagd serves the written experiment (provider connect +
        # file-watch reload may lag the write), then assert the full matrix.
        def _served() -> bool:
            return (
                abs(_inv(shadow_ctx) - inv_experiment) <= 0.01
                and abs(_inv(real_ctx) - inv_real_baseline) <= 0.01
            )

        assert poll_until(_served, RESOLVE_TIMEOUT_SECONDS, rewrite=_write), (
            "experiment did not resolve as expected on live flagd: "
            f"shadow invincibility={_inv(shadow_ctx)} (want {inv_experiment}), "
            f"real invincibility={_inv(real_ctx)} (want baseline {inv_real_baseline})"
        )

        # --- invincibility_seconds (no pre-existing targeting, else=null) ---
        # SHADOW resolves the experiment...
        assert abs(_inv(shadow_ctx) - inv_experiment) <= 0.01, (
            f"shadow did NOT get the experiment: {_inv(shadow_ctx)}"
        )
        # ...REAL resolves the SAME value it did before the mutation (safety).
        assert abs(_inv(real_ctx) - inv_real_baseline) <= 0.01, (
            f"REAL leaked the experiment: invincibility={_inv(real_ctx)} "
            f"(MUST stay pre-experiment baseline {inv_real_baseline})"
        )

        # --- sensitivity (pre-existing Werewolf targeting wrapped as else) ---
        # SHADOW overrides to the experiment regardless of game_mode...
        assert _sens(shadow_ctx) == sens_experiment, (
            f"shadow sensitivity did NOT get the experiment: {_sens(shadow_ctx)}"
        )
        # ...a REAL game resolves the SAME value it did before the mutation...
        assert _sens(real_ctx) == sens_real_baseline, (
            f"REAL sensitivity leaked: {_sens(real_ctx)} "
            f"(MUST stay pre-experiment baseline {sens_real_baseline})"
        )
        # ...and a REAL Werewolf game resolves the SAME value before & after,
        # proving the experiment's else-branch preserved whatever real targeting
        # the mounted file had verbatim — robust to game.json vs ci/game.json
        # targeting differences (this is the structural half of the Gate's
        # invariant: pre-existing real targeting is untouched).
        assert _sens(real_werewolf_ctx) == sens_real_werewolf_baseline, (
            f"REAL Werewolf targeting was clobbered: {_sens(real_werewolf_ctx)} "
            f"(MUST stay pre-experiment baseline {sens_real_werewolf_baseline})"
        )

        # --- where the fail-safe lives: raw flagd vs application context ---
        # No game_kind on the raw context => flagd's "!= real" rule selects the
        # experiment. This is correct and intentional: the real-by-default
        # protection is the APPLICATION's job (lib.feature_flags defaults the
        # API-level context to game_kind="real"), not flagd's. Asserting it makes
        # the boundary explicit and would catch a "== shadow" writer mistake.
        assert abs(_inv(empty_ctx) - inv_experiment) <= 0.01, (
            "raw flagd should resolve EXPERIMENTAL for a context with no "
            f"game_kind (rule is != real): got {_inv(empty_ctx)}. The real-by-"
            "default fail-safe is enforced by the application context, not flagd."
        )
