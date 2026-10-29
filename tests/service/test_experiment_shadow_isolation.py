"""
Experiment shadow-isolation integration test (#932, #954; flag service #1415).

Proves the agent-experiment safety link end-to-end through the REAL flag
service and the REAL flagd: an experiment value written for one experiment's
experimental arm reaches a SHADOW game of that experiment and arm, and NEVER a
REAL game, a control-arm game or a game of another experiment (FR-062, FR-138).

The safety boundary, end-to-end:

    the agent's experiment Writer (services/agent/experiment/writer.go)
    PUT /game/<flag> {"value": V, "scope": {"experiment_id": E, "arm": "experimental"}}
      ->  the flag service renders the rung
          game_kind != "real" AND experiment_id == E AND arm == "experimental"
          in front of the baseline targeting (flags.md §13, render.go)
      ->  flagd receives it over the gRPC sync stream (flagSetId=game)
      ->  a shadow game of E's experimental arm resolves V
      ->  a REAL game (game_kind="real"), E's control arm and any other
          experiment resolve the pre-experiment value, byte-identical.

The agent service is not run in the integration compose (it lives behind the
`agent` profile), so the test sends the exact request the Writer sends. The
Writer's own behaviour is covered by the Go unit tests
(services/agent/experiment/experiment_test.go).

We resolve via the flagd RPC resolver (port 8013, `flagSetId=game`): this
test's purpose is flagd's server-side evaluation of the rendered rung. The
context keys and values are the exact strings lib.feature_flags puts on the
context; `test_game_kind_constants_match_source`
(lib/tests/test_shipped_safety_defaults.py) pins them to the Python, Go agent
and flag-service sources, so a drift fails loudly.

Kept sequential: it changes the GLOBAL `game` flag set and asserts resolution
for distinct contexts. The `experiment_values` fixture deletes the experiment
values afterwards and waits until flagd no longer serves them.
"""

import contextlib
import math

import pytest

from tests.integration.helpers import (
    FLAGD_RESOLVE_TIMEOUT_SECONDS,
    flagd_probe_client,
    poll_until,
)
from tests.service.compositions import FLAG_PLANE

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = FLAG_PLANE

# Evaluation-context keys and values, pinned to lib.feature_flags and the Go
# sources by test_game_kind_constants_match_source (the integration venv does
# not install lib.feature_flags' dependencies, so they are copied here).
GAME_KIND_VAR = "game_kind"
GAME_KIND_REAL = "real"
GAME_KIND_SHADOW = "shadow"
EXPERIMENT_ID_VAR = "experiment_id"
ARM_VAR = "arm"
ARM_EXPERIMENTAL = "experimental"

GAME = "game"
EXPERIMENT_ID = "exp_itest_isolation"
OTHER_EXPERIMENT_ID = "exp_itest_other"
EXPERIMENT_FLAGS = ("nonstop.respawn_seconds", "sensitivity")

RESOLVE_TIMEOUT_SECONDS = FLAGD_RESOLVE_TIMEOUT_SECONDS


def write_experiment(flag_service, flag_key: str, value) -> dict:
    """The exact request of the agent's experiment Writer (W3, FR-138)."""
    return flag_service.put(
        GAME,
        flag_key,
        value=value,
        scope={"experiment_id": EXPERIMENT_ID, "arm": ARM_EXPERIMENTAL},
        writer="agent-experiment",
        reason="experiment",
    )


def _ctx(**attributes):
    from openfeature.evaluation_context import EvaluationContext

    return EvaluationContext(attributes=attributes)


@pytest.fixture
def experiment_values(stack, flag_service):
    """Delete this test's experiment values and wait until flagd stops serving them."""
    yield

    def _strip():
        for flag in EXPERIMENT_FLAGS:
            with contextlib.suppress(AssertionError):
                flag_service.delete(GAME, flag, experiment_id=EXPERIMENT_ID, arm=ARM_EXPERIMENTAL)

    _strip()
    shadow = _ctx(
        **{
            GAME_KIND_VAR: GAME_KIND_SHADOW,
            EXPERIMENT_ID_VAR: EXPERIMENT_ID,
            ARM_VAR: ARM_EXPERIMENTAL,
        }
    )
    real = _ctx(**{GAME_KIND_VAR: GAME_KIND_REAL})
    with flagd_probe_client(stack, GAME, resolver="rpc") as client:

        def _gone() -> bool:
            a = client.get_float_value("nonstop.respawn_seconds", float("nan"), shadow)
            b = client.get_float_value("nonstop.respawn_seconds", float("nan"), real)
            return abs(a - b) <= 0.01

        assert poll_until(_gone, FLAGD_RESOLVE_TIMEOUT_SECONDS), (
            "flagd still served the experiment after its values were deleted"
        )


@pytest.mark.asyncio
@pytest.mark.uc("UC-053", flow="A3")
@pytest.mark.uc("UC-042", flow="main")
@pytest.mark.uc("UC-043", br=["BR-001"], cond=["Post-F-1"])
@pytest.mark.usefixtures("fresh_stack", "experiment_values")
async def test_experiment_reaches_shadow_never_real(flag_service, stack):
    """An experiment-arm value resolves for that experiment's experimental arm only.

    Baselines are read from the live flagd before the write (the CI baseline
    differs from the local one). Two flags cover both rendering cases:
    ``nonstop.respawn_seconds`` has no baseline targeting (the rung's else is the
    default) and ``sensitivity`` has one (game_mode == Werewolf -> fast), which a
    real Werewolf game must keep.
    """
    experimental = _ctx(
        **{
            GAME_KIND_VAR: GAME_KIND_SHADOW,
            EXPERIMENT_ID_VAR: EXPERIMENT_ID,
            ARM_VAR: ARM_EXPERIMENTAL,
        }
    )
    control = _ctx(
        **{
            GAME_KIND_VAR: GAME_KIND_SHADOW,
            EXPERIMENT_ID_VAR: EXPERIMENT_ID,
            ARM_VAR: "control",
        }
    )
    other_experiment = _ctx(
        **{
            GAME_KIND_VAR: GAME_KIND_SHADOW,
            EXPERIMENT_ID_VAR: OTHER_EXPERIMENT_ID,
            ARM_VAR: ARM_EXPERIMENTAL,
        }
    )
    real = _ctx(**{GAME_KIND_VAR: GAME_KIND_REAL})
    # A real game that carries the experiment's keys anyway: still protected.
    real_with_keys = _ctx(
        **{
            GAME_KIND_VAR: GAME_KIND_REAL,
            EXPERIMENT_ID_VAR: EXPERIMENT_ID,
            ARM_VAR: ARM_EXPERIMENTAL,
        }
    )
    real_werewolf = _ctx(**{GAME_KIND_VAR: GAME_KIND_REAL, "game_mode": "Werewolf"})
    # No context at all: no experiment_id, so no experiment rung can match.
    empty = _ctx()

    with flagd_probe_client(stack, GAME, resolver="rpc") as client:

        def _respawn(ctx) -> float:
            return client.get_float_value("nonstop.respawn_seconds", float("nan"), ctx)

        def _sens(ctx) -> int:
            return int(client.get_integer_value("sensitivity", -1, ctx))

        # Step 1: the RPC provider serves a baseline.
        assert poll_until(
            lambda: not math.isnan(_respawn(real)) and _sens(real) != -1,
            RESOLVE_TIMEOUT_SECONDS,
        ), "flagd RPC provider did not serve a baseline in time"

        respawn_baseline = _respawn(real)
        sens_baseline = _sens(real)
        sens_werewolf_baseline = _sens(real_werewolf)
        respawn_value = 8.0 if respawn_baseline < 8.0 else 2.0
        sens_value = 0 if sens_baseline != 0 else 4

        # Step 2: the agent's experiment write, through the flag service.
        result = write_experiment(flag_service, "nonstop.respawn_seconds", respawn_value)
        write_experiment(flag_service, "sensitivity", sens_value)
        assert result.get("variant"), f"no variant in the write result: {result}"

        # Step 3: flagd serves the rung (sync stream, no file watch).
        assert poll_until(
            lambda: abs(_respawn(experimental) - respawn_value) <= 0.01,
            RESOLVE_TIMEOUT_SECONDS,
        ), f"experimental arm resolves {_respawn(experimental)}, want {respawn_value}"

        # Step 4: only the experimental arm of this experiment sees the values.
        assert _sens(experimental) == sens_value
        for name, ctx in (
            ("real", real),
            ("real with experiment keys", real_with_keys),
            ("control arm", control),
            ("other experiment", other_experiment),
            ("empty context", empty),
        ):
            assert abs(_respawn(ctx) - respawn_baseline) <= 0.01, (
                f"{name} resolved the experiment: respawn={_respawn(ctx)} (must stay {respawn_baseline})"
            )
        assert _sens(real) == sens_baseline, f"REAL sensitivity leaked: {_sens(real)}"
        assert _sens(control) == sens_baseline
        # The baseline targeting stays the rung's else: a real Werewolf game keeps it.
        assert _sens(real_werewolf) == sens_werewolf_baseline, (
            f"REAL Werewolf targeting was clobbered: {_sens(real_werewolf)}"
        )

    # Step 5: the flag service view shows the overlay, the baseline default is unchanged.
    view = flag_service.get(GAME, "nonstop.respawn_seconds")
    arms = (view.get("experiments") or {}).get(EXPERIMENT_ID) or {}
    assert ARM_EXPERIMENTAL in arms, view
    assert view["effective_default"] == view["baseline_default"], view
