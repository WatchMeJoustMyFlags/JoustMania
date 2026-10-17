"""Shipped safety defaults: fail-closed flag defaults and cross-language constants.

These are pure file/source checks: they read the flag files shipped under
``services/flagd/`` (base and ``ci/``, which flagd serves in CI, #801/#959) and
the Go agent source. They need no running stack, so they live here instead of
in the docker-compose integration suite (moved from tests/integration, #1422).
"""

import json
import re
from pathlib import Path

import pytest

from lib.feature_flags import (
    ARM_EXPERIMENTAL,
    ARM_VAR,
    EXPERIMENT_ID_VAR,
    GAME_KIND_REAL,
    GAME_KIND_SHADOW,
    GAME_KIND_VAR,
)

# lib/tests/ -> lib/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLAGD_DIR = REPO_ROOT / "services" / "flagd"
GO_TARGETING = REPO_ROOT / "services" / "agent" / "experiment" / "targeting.go"
# The flag service renders the experiment rung (FR-128) from its own constants.
FLAG_SERVICE_RENDER = REPO_ROOT / "services" / "flag-service" / "state" / "render.go"
# The integration test keeps its own copies of the constants (its venv lacks
# lib.feature_flags' dependencies), so they are pinned here too.
SHADOW_ISOLATION_CONTRACT = REPO_ROOT / "tests" / "contract" / "test_flagd_resolution.py"

# Base and CI variants of each flag file: flagd serves ci/ in CI.
FLAG_FILE_VARIANTS = ("", "ci/")


def _flags(rel: str) -> dict:
    return json.loads((FLAGD_DIR / rel).read_text())["flags"]


@pytest.mark.uc("UC-040", flow="A11")
@pytest.mark.parametrize("prefix", FLAG_FILE_VARIANTS, ids=["base", "ci"])
def test_agent_interventions_disabled_by_default(prefix):
    """The ``interventions_enabled`` actuation gate (#1213) ships OFF, so the agent
    never mutates interventions.json by default. The no-write behaviour itself is
    covered by the Go unit tests (services/agent/actions/gated_test.go,
    action_sink_test.go); this pins the default those tests rely on.
    """
    rel = f"{prefix}agent.json"
    flags = _flags(rel)
    assert "interventions_enabled" in flags, f"{rel} should define the interventions_enabled safety gate (#1213)"
    flag = flags["interventions_enabled"]
    default_variant = flag["defaultVariant"]
    assert flag["variants"][default_variant] is False, (
        f"interventions_enabled in {rel} must default OFF (fail-closed); "
        f"defaultVariant {default_variant!r} resolves to "
        f"{flag['variants'][default_variant]!r}"
    )


@pytest.mark.uc("UC-051", flow="A1", br=["BR-002"])
@pytest.mark.parametrize("prefix", FLAG_FILE_VARIANTS, ids=["base", "ci"])
def test_remediation_allowed_flag_is_resolvable_shape(prefix):
    """The ``remediation_allowed`` gate the agent's RemediationReader reads from
    rollout.json exists with both boolean variants and ships OFF (fail-closed):
    the agent only auto-rolls-back when an operator explicitly flips it on.
    """
    rel = f"{prefix}rollout.json"
    flag = _flags(rel)["remediation_allowed"]
    assert set(flag["variants"]) == {"on", "off"}, (
        f"remediation_allowed variants drifted in {rel}: {sorted(flag['variants'])}"
    )
    assert flag["variants"]["on"] is True and flag["variants"]["off"] is False
    assert flag["defaultVariant"] == "off", f"remediation_allowed in {rel} must ship default OFF (fail-closed)"


def _const(src: str, name: str) -> str | None:
    """Value of a top-level ``name = "value"`` string constant (Go or Python)."""
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"', src, re.MULTILINE)
    return match.group(1) if match else None


@pytest.mark.uc("UC-043", br=["BR-001"])
def test_game_kind_constants_match_source():
    """The game_kind / experiment split is a cross-language contract: the flag
    service renders an experiment value as ``game_kind != "real" AND
    experiment_id AND arm == "experimental"`` (render.go, FR-128), the Go agent
    names the same keys (targeting.go), and the Python eval context
    (lib/feature_flags) sets them. If a name or value drifts, real games stop
    being protected from shadow experiments.
    """
    go_src = GO_TARGETING.read_text()
    assert _const(go_src, "GameKindVar") == GAME_KIND_VAR, "Go GameKindVar drifted from lib.feature_flags.GAME_KIND_VAR"
    assert _const(go_src, "GameKindReal") == GAME_KIND_REAL, (
        "Go GameKindReal drifted from lib.feature_flags.GAME_KIND_REAL"
    )
    assert _const(go_src, "ExperimentIDVar") == EXPERIMENT_ID_VAR
    assert _const(go_src, "ArmVar") == ARM_VAR
    assert _const(go_src, "ArmExperimental") == ARM_EXPERIMENTAL

    render_src = FLAG_SERVICE_RENDER.read_text()
    assert _const(render_src, "ctxGameKind") == GAME_KIND_VAR, "flag-service ctxGameKind drifted"
    assert _const(render_src, "gameKindReal") == GAME_KIND_REAL, "flag-service gameKindReal drifted"
    assert _const(render_src, "ctxExperimentID") == EXPERIMENT_ID_VAR, "flag-service ctxExperimentID drifted"
    assert _const(render_src, "ctxArm") == ARM_VAR, "flag-service ctxArm drifted"

    assert GAME_KIND_VAR == "game_kind"
    assert GAME_KIND_REAL == "real"
    assert GAME_KIND_SHADOW == "shadow"

    contract_src = SHADOW_ISOLATION_CONTRACT.read_text()
    for name, want in (
        ("GAME_KIND_VAR", GAME_KIND_VAR),
        ("GAME_KIND_REAL", GAME_KIND_REAL),
        ("GAME_KIND_SHADOW", GAME_KIND_SHADOW),
        ("EXPERIMENT_ID_VAR", EXPERIMENT_ID_VAR),
        ("ARM_VAR", ARM_VAR),
        ("ARM_EXPERIMENTAL", ARM_EXPERIMENTAL),
    ):
        assert _const(contract_src, name) == want, f"{SHADOW_ISOLATION_CONTRACT.name} {name} drifted from source"
