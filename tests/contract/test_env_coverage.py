"""Every env var a service needs is set in compose (#1526, T1 mitigation for the T2 tier).

The service tier (``tests/service/``) runs services from source, so the compose
wiring (``environment:`` lists, the CI overlay's ``!override`` blocks, the
image's ``ENV`` lines) is only exercised by the full stack. This static check
keeps that wiring honest on every PR: a var a service reads with no code
default (``env_inventory`` explains what counts as one) must be set for that
service in ``docker-compose.yml`` alone AND in ``docker-compose.yml`` +
``docker-compose.ci.yml``, unless ALLOWLIST says why the gap is intended.
"""

from __future__ import annotations

import env_inventory as inv
import pytest

STACKS: dict[str, tuple] = {
    "base": (),
    "ci": (inv.COMPOSE_CI,),
}

_OPT_IN_OFF = (
    "opt-in switch: only the literal 'true' turns it on, so unset = off (fail closed)"
)
_TUNING = "tuning override: unset or invalid falls back to the built-in default in code"

# (compose service | "*" for every service that reads it, var) -> why it may stay unset.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("*", "OTEL_RESOURCE_ATTRIBUTES"): (
        "extra resource attributes (host.name); empty adds none. The CI overlay's "
        "environment !override drops it for audio, controller-manager and game-coordinator"
    ),
    # ---- flag-service: tuning knobs (docs/specs/services.md §7) ----
    ("flag-service", "FLAG_SERVICE_EVENT_LIST_LEN"): _TUNING,
    ("flag-service", "FLAG_SERVICE_MAX_GAMES"): _TUNING,
    ("flag-service", "FLAG_SERVICE_EXPERIMENT_IDLE"): _TUNING,
    ("flag-service", "FLAG_SERVICE_MAX_GAME_AGE"): _TUNING,
    (
        "flag-service",
        "LOG_LEVEL",
    ): "parseLevel falls back to info for an empty or unknown value",
    # ---- agent: tuning knobs with a built-in fallback ----
    ("agent", "AGENT_EFFECT_WINDOW_SECONDS"): _TUNING
    + " (decision.DefaultEffectWindow)",
    ("agent", "AGENT_EXPERIMENT_COMPLETED_GRACE_SECONDS"): _TUNING,
    ("agent", "AGENT_EXPERIMENT_MAX_GAMES"): _TUNING,
    ("agent", "AGENT_MAX_SHADOW_GAMES"): _TUNING,
    ("agent", "AGENT_SHADOW_EFFECTIVE_CONCURRENCY"): _TUNING,
    ("agent", "AGENT_VERDICT_MIN_N"): _TUNING,
    ("agent", "AGENT_VERDICT_MIN_PAIRS"): _TUNING,
    (
        "agent",
        "AGENT_EXPERIMENT_DYNAMIC_OBJECTIVE",
    ): "a non-experimentable or empty value falls back to the balanced objective",
    (
        "agent",
        "AGENT_EXPERIMENT_DIR",
    ): "falls back to /var/lib/joustmania/agent/experiments, the agent-experiments volume mount",
    (
        "agent",
        "FLAGD_OFREP_URL",
    ): "falls back to http://<FLAGD_HOST>:8016, and FLAGD_HOST is set",
    # ---- agent: one-shot shadow game runner (UC-046), a dev/verification tool ----
    ("agent", "AGENT_SHADOW_GAME"): _OPT_IN_OFF
    + "; scripts/pattern-replay-check.sh sets it on its own stack",
    (
        "agent",
        "SHADOW_GAME_ARCHETYPE",
    ): "one-shot shadow runner only; empty = first archetype of experiment.mock_archetypes",
    (
        "agent",
        "SHADOW_GAME_SEED",
    ): "one-shot shadow runner only; empty = the archetype's own seed",
    (
        "agent",
        "SHADOW_GAME_SENSITIVITY",
    ): "one-shot shadow runner only; empty or out of 0-4 = sensitivity 2",
    # ---- agent: code-improvement promotion (#1011), never on in the compose stack ----
    ("agent", "AGENT_CODE_IMPROVEMENT_ENABLED"): _OPT_IN_OFF,
    ("agent", "AGENT_AUTONOMOUS_ENABLED"): _OPT_IN_OFF,
    (
        "agent",
        "AGENT_LOCAL_REPO_DIR",
    ): "read only when AGENT_CODE_IMPROVEMENT_ENABLED=true (local promotion target)",
    (
        "agent",
        "GITHUB_TOKEN",
    ): "read only when AGENT_CODE_IMPROVEMENT_ENABLED=true (GitHub promotion target)",
    (
        "agent",
        "GITHUB_REPOSITORY",
    ): "read only when AGENT_CODE_IMPROVEMENT_ENABLED=true (GitHub promotion target)",
    # ---- agent: known gap, reported on #1526 ----
    ("agent", "AGENT_EXPERIMENT_PROPOSER_ENABLED"): (
        _OPT_IN_OFF
        + ". KNOWN GAP (#1526): UC-042's trigger has no compose passthrough, "
        "so a host export or .env value never reaches the container (cf. #1078, #1041)"
    ),
}


def _allowed(service: str, var: str) -> bool:
    return (service, var) in ALLOWLIST or ("*", var) in ALLOWLIST


def unset_required(
    service: str, env: dict[str, str | None]
) -> dict[str, list[inv.EnvRead]]:
    """Required vars of ``service`` that neither ``env`` nor the image's ENV sets."""
    image_env = inv.dockerfile_env(service)
    return {
        var: reads
        for var, reads in inv.required_vars(service).items()
        if var not in env and var not in image_env
    }


def _cases():
    for service in inv.CODE_ROOTS:
        for stack in STACKS:
            yield pytest.param(service, stack, id=f"{service}-{stack}")


@pytest.mark.parametrize(("service", "stack"), _cases())
def test_required_env_vars_are_set_in_compose(service, stack):
    env = inv.compose_environment(service, STACKS[stack])
    assert env is not None, (
        f"{service} is no longer a docker-compose.yml service: update CODE_ROOTS"
    )
    gaps = {
        var: reads
        for var, reads in unset_required(service, env).items()
        if not _allowed(service, var)
    }
    assert not gaps, (
        f"{service} ({stack} stack) reads env vars with no code default that compose does not set:\n"
        + "\n".join(
            f"  {var}: read at {', '.join(r.where() for r in reads)}"
            for var, reads in sorted(gaps.items())
        )
        + "\nSet them in the service's environment (and in docker-compose.ci.yml when it uses "
        "!override), give the read a default, or add an ALLOWLIST entry with a reason."
    )


def test_allowlist_entries_are_live():
    """An entry whose var is now set, defaulted or no longer read must be removed."""
    stale = []
    for (service, var), reason in ALLOWLIST.items():
        assert reason.strip(), f"ALLOWLIST[{service!r}, {var!r}] has no reason"
        services = list(inv.CODE_ROOTS) if service == "*" else [service]
        live = any(
            var in unset_required(svc, inv.compose_environment(svc, overlays) or {})
            for svc in services
            for overlays in STACKS.values()
        )
        if not live:
            stale.append((service, var))
    assert not stale, (
        f"stale ALLOWLIST entries (var is set, defaulted or no longer read): {stale}"
    )


def test_every_python_service_is_inventoried():
    """A new Python service under services/ must be mapped to its compose service."""
    mapped = {root for _, roots in inv.CODE_ROOTS.values() for root in roots}
    python_services = {p.parent for p in inv.SERVICES.glob("*/server.py")}
    assert python_services <= mapped, (
        f"unmapped Python services: {sorted(p.name for p in python_services - mapped)}"
    )


def test_inventory_classifies_defaults():
    """Guards the scanner itself: known reads keep their default / no-default classification."""
    menu = {(r.var, r.has_default) for r in inv.service_reads("menu")}
    assert ("AUDIO_HOST", True) in menu  # os.getenv(K, "audio")
    assert (
        "FLAG_SERVICE_URL",
        True,
    ) in menu  # lib/flag_service_writer: `... or os.getenv(K) or DEFAULT_URL`
    coordinator = inv.required_vars("game-coordinator")
    assert (
        "FLAG_SERVICE_URL" in coordinator
    )  # os.getenv(K, "") = empty default = lifecycle reporting off
    agent = inv.required_vars("agent")
    assert {
        "ANTHROPIC_API_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "AGENT_EXPERIMENT_SEED_FLAG",
    } <= set(agent)  # raw + const key
    assert "FLAGD_HOST" not in agent  # getEnv("FLAGD_HOST", "flagd")
    assert (
        "AGENT_ROLLOUT_COOLDOWN_SECONDS" not in agent
    )  # secondsEnv helper with fallback


def test_ci_override_replaces_environment():
    """``environment: !override`` in the CI overlay replaces the base list instead of merging."""
    base = inv.compose_environment("controller-manager")
    ci = inv.compose_environment("controller-manager", (inv.COMPOSE_CI,))
    assert "RUST_HID_HOST" in base
    assert "RUST_HID_HOST" not in ci


def test_check_fails_when_a_required_var_is_dropped():
    """The coordinator's FLAG_SERVICE_URL (FR-135) is caught when compose stops setting it."""
    env = inv.compose_environment("game-coordinator", STACKS["ci"])
    assert "FLAG_SERVICE_URL" not in unset_required("game-coordinator", env)
    env.pop("FLAG_SERVICE_URL")
    assert "FLAG_SERVICE_URL" in unset_required("game-coordinator", env)
    assert not _allowed("game-coordinator", "FLAG_SERVICE_URL")
