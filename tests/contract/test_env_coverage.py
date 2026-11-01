"""Every env var a service needs is set in compose (#1526, T1 mitigation for the T2 tier).

The service tier (``tests/service/``) runs services from source, so the compose
wiring (``environment:`` lists, the CI overlay's ``!override`` blocks, the
image's ``ENV`` lines) is only exercised by the full stack. This static check
keeps that wiring honest on every PR: a var a service reads with no code
default (``env_inventory`` explains what counts as one) must be set for that
service in every stack in STACKS, unless ALLOWLIST says why the gap is intended.

Stacks: ``docker-compose.yml`` alone, with the CI overlay, with each agent
overlay (dry-run; it overrides ``agent`` and
``flag-service`` environment), and the standalone Pi stack
``docker-compose.lite.yml`` (its own file, not an overlay; it has no agent).
Not covered: ``docker-compose.dev/override/ci-rust.yml`` (local-development and
Rust-CI tweaks, not deployed or env-contract stacks) and services that exist
only in an overlay. See env_inventory's docstring
for what compose features count as "set".
"""

from __future__ import annotations

from pathlib import Path

import env_inventory as inv
import pytest

# stack name -> (base compose file, overlays merged on top, in order)
STACKS: dict[str, tuple[Path, tuple[Path, ...]]] = {
    "base": (inv.COMPOSE_BASE, ()),
    "ci": (inv.COMPOSE_BASE, (inv.COMPOSE_CI,)),
    "dry-run": (inv.COMPOSE_BASE, (inv.COMPOSE_DRY_RUN,)),
    "lite": (inv.COMPOSE_LITE, ()),
}


def _stack_env(service: str, stack: str) -> dict[str, str | None] | None:
    base_file, overlays = STACKS[stack]
    return inv.compose_environment(service, overlays, base_file)


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
    # ---- lite stack has no OTel collector (docker-compose.lite.yml header) ----
    (
        "flag-service",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ): "unset = no exporter (otel.go returns a no-op); the Pi lite stack ships no collector",
    (
        "connect-proxy",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ): "unset = slog.Default and no exporter (otel.go); the Pi lite stack ships no collector",
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
    (
        "agent",
        "AGENT_EXPERIMENT_MAX_GAMES",
    ): "operator ceiling (C-034, #1810): unset = no ceiling, the experiment_max_games flag governs",
    (
        "agent",
        "AGENT_EXPERIMENT_DIR",
    ): "falls back to /var/lib/joustmania/agent/experiments, the agent-experiments volume mount",
    (
        "agent",
        "FLAGD_OFREP_URL",
    ): "falls back to http://<FLAGD_HOST>:8016, and FLAGD_HOST is set",
    (
        "agent",
        "AGENT_INFERENCE_BASE_URL",
    ): "operator-only override of the claude text backend root (#1422 S10); empty = https://api.anthropic.com, so compose must not set it",
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
        "AGENT_AUTONOMOUS_VALIDATED_OBJECTIVES",
    ): "operator attestation (UC-043 A23, C-034): unset = no objective validated, autonomous promotion refused (fail closed)",
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
        for stack, (base_file, _) in STACKS.items():
            # a stack is checked for the services its base file defines (lite has no agent)
            if service in inv._load_compose(base_file):
                yield pytest.param(service, stack, id=f"{service}-{stack}")


@pytest.mark.parametrize(("service", "stack"), _cases())
def test_required_env_vars_are_set_in_compose(service, stack):
    env = _stack_env(service, stack)
    assert env is not None, (
        f"{service} is no longer a service of the {stack} stack: update CODE_ROOTS"
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
            var in unset_required(svc, env)
            for svc in services
            for stack in STACKS
            if (env := _stack_env(svc, stack)) is not None
        )
        if not live:
            stale.append((service, var))
    assert not stale, (
        f"stale ALLOWLIST entries (var is set, defaulted or no longer read): {stale}"
    )


@pytest.mark.parametrize(
    "stack", [s for s in STACKS if _stack_env("agent", s) is not None]
)
def test_agent_compose_env_is_read(stack):
    """Every AGENT_* key the agent's compose environment sets is read by the agent (#1907).

    Guards against compose drift: a removed env fallback whose passthrough stays in
    compose would be silently ignored, hiding a host .env value that no longer acts.
    """
    env = _stack_env("agent", stack)
    read = {r.var for r in inv.service_reads("agent")}
    dead = sorted(k for k in env if k.startswith("AGENT_") and k not in read)
    assert not dead, (
        f"agent ({stack} stack) compose sets AGENT_* vars the agent never reads: {dead}. "
        "Remove the passthrough (or the env read lost its helper detection in env_inventory)."
    )


def test_every_service_is_inventoried():
    """A new Python (server.py) or Go (main.go) service under services/ must be mapped."""
    mapped = {root for _, roots in inv.CODE_ROOTS.values() for root in roots}
    services = {p.parent for p in inv.SERVICES.glob("*/server.py")} | {
        p.parent for p in inv.SERVICES.glob("*/main.go")
    }
    assert services <= mapped, (
        f"unmapped services: {sorted(p.name for p in services - mapped)}"
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
        "AGENT_EXPERIMENT_PROPOSER_ENABLED",
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
    env = _stack_env("game-coordinator", "ci")
    assert "FLAG_SERVICE_URL" not in unset_required("game-coordinator", env)
    env.pop("FLAG_SERVICE_URL")
    assert "FLAG_SERVICE_URL" in unset_required("game-coordinator", env)
    assert not _allowed("game-coordinator", "FLAG_SERVICE_URL")


# ---- scanner guards: planted reads of every spelling must be found ---------- #
@pytest.mark.parametrize(
    ("source", "var", "has_default"),
    [
        ("import os\nos.getenv('X')", "X", False),
        ("import os\nos.getenv('X', 'd')", "X", True),
        ("import os\nos.environ.get('X')", "X", False),
        ("import os\nos.environ['X']", "X", False),
        ("import os\nos.environ.setdefault('X', 'd')", "X", True),
        ("import os\nos.environ.pop('X', None)", "X", False),
        ("import os\nos.environ.pop('X', 'd')", "X", True),
        ("import os\n'X' in os.environ", "X", False),
        ("import os\n'X' not in os.environ", "X", False),
        ("from os import environ\nenviron.get('X', 'd')", "X", True),
        ("from os import environ\nenviron['X']", "X", False),
        ("from os import environ as e\n'X' in e", "X", False),
        ("from os import getenv\ngetenv('X')", "X", False),
        ("from os import getenv as g\ng('X', 'd')", "X", True),
        ("import os as _o\n_o.getenv('X')", "X", False),
        ("import os as _o\n_o.environ['X']", "X", False),
        ("import os as _o\n_o.environ.get('X', 'd')", "X", True),
        ("import os\nos.getenv('X') or 'd'", "X", True),
    ],
)
def test_python_scanner_finds_planted_read(tmp_path, source, var, has_default):
    path = tmp_path / "planted.py"
    path.write_text(source + "\n")
    reads = inv.python_reads(path)
    assert [(r.var, r.has_default) for r in reads] == [(var, has_default)]


def test_python_scanner_skips_non_literal_keys(tmp_path):
    path = tmp_path / "planted.py"
    path.write_text(
        "import os\n"
        "def env_int(key, default):\n"
        "    return int(os.getenv(key, default))\n"
        "os.environ[some_name]\n"
        "os.getenv(f'A_{1}')\n"
        "os.getenv('LITERAL')\n"
    )
    assert [r.var for r in inv.python_reads(path)] == ["LITERAL"]


def test_python_scanner_ignores_writes_and_other_objects(tmp_path):
    path = tmp_path / "planted.py"
    path.write_text(
        "import os\n"
        "os.environ['W'] = 'v'\n"
        "del os.environ['D']\n"
        "cfg = {}\n"
        "cfg.get('NOT_ENV')\n"
        "'NOT_ENV' in cfg\n"
    )
    assert inv.python_reads(path) == []


def test_go_scanner_ignores_comments(tmp_path):
    (tmp_path / "main.go").write_text(
        "package main\n"
        '// os.Getenv("LINE_COMMENT")\n'
        '/* os.Getenv("BLOCK_COMMENT")\n   os.Getenv("BLOCK_TWO") */\n'
        'func f() { _ = os.Getenv("REAL") } // os.Getenv("TRAILING")\n'
    )
    reads = inv.go_reads(tmp_path)
    assert [(r.var, r.line) for r in reads] == [("REAL", 5)]


def test_go_scanner_rejects_unresolved_key_matching_a_non_key_param(tmp_path):
    """Only a helper's key parameter may stay unresolved, not any param name (#1907)."""
    (tmp_path / "main.go").write_text(
        "package main\n"
        "func ceiling(key string, name string) string {\n"
        "\treturn os.Getenv(key)\n}\n"
        'func g() string { name := "X"; return os.Getenv(name) }\n'
    )
    with pytest.raises(ValueError, match="cannot resolve env key 'name'"):
        inv.go_reads(tmp_path)


def test_go_scanner_follows_a_helper_key_in_any_position(tmp_path):
    """A helper whose key is not its first parameter still counts as a read (#1907)."""
    (tmp_path / "main.go").write_text(
        "package main\n"
        'const envCeiling = "PLANTED_CEILING"\n'
        "func ceiling(logger *slog.Logger, key string, warn bool) int {\n"
        "\tv := os.Getenv(key)\n\treturn len(v)\n}\n"
        "func g() { _ = ceiling(logger, envCeiling, true) }\n"
    )
    reads = inv.go_reads(tmp_path)
    assert [(r.var, r.line, r.has_default) for r in reads] == [
        ("PLANTED_CEILING", 7, True)
    ]


def test_go_scanner_tolerates_unnamed_func_parameters(tmp_path):
    """An unnamed parameter like ``[]*http.Request`` is not an identifier (#2330)."""
    (tmp_path / "main.go").write_text(
        "package main\n"
        "func NoRedirect(*http.Request, []*http.Request) error { return nil }\n"
        'func f() { _ = os.Getenv("REAL") }\n'
    )
    assert [r.var for r in inv.go_reads(tmp_path)] == ["REAL"]
