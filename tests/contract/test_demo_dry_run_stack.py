"""``make dry-run`` starts the demo stack as FR-090 says (UC-070 Pre-1).

FR-090: one command starts the latest images with mock controllers, the agent and a
seeded experiment. "Seeded" means the seed env is set; the experiment loop itself stays
gated by the fail-closed ``enabled`` / ``experiments_enabled`` flags until
``scripts/agent-dryrun-enable.sh on``. Static checks over the Makefile recipe, the compose layers and the
flagd CI baseline; ``make -n`` prints the recipe without running docker.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from pathlib import Path

import infra_config as ic
import pytest
import yaml

REPO_ROOT = ic.REPO_ROOT
DRY_RUN_FILE = REPO_ROOT / "docker-compose.dry-run.yml"
CI_FLAGD_DIR = REPO_ROOT / "services" / "flagd" / "ci"


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads compose merge tags (``!reset``, ``!override``) as plain values."""


def _construct_tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_tagged)


def _load_layer(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_ComposeLoader)


def _dry_run_recipe(env_image_tag: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("IMAGE_TAG", "MAKEFLAGS")}
    if env_image_tag is not None:
        env["IMAGE_TAG"] = env_image_tag
    result = subprocess.run(
        ["make", "-n", "--no-print-directory", "-C", str(REPO_ROOT), "dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout


def _agent_env() -> dict[str, str]:
    entries = _load_layer(DRY_RUN_FILE)["services"]["agent"]["environment"]
    return dict(entry.split("=", 1) for entry in entries)


@pytest.mark.uc("UC-070", cond=["Pre-1"])
def test_dry_run_pins_latest_images_not_the_env_release_pin():
    command = _dry_run_recipe().splitlines()[0]
    assert command.startswith("IMAGE_TAG=latest docker compose")
    assert "0.7.0" not in command
    # An exported IMAGE_TAG still wins, so a rehearsal can pin a tag on purpose.
    assert _dry_run_recipe("main").lstrip().startswith("IMAGE_TAG=main docker compose")


@pytest.mark.uc("UC-070", cond=["Pre-1"])
def test_dry_run_layers_the_ci_and_dry_run_compose_files_with_agent_profile():
    recipe = _dry_run_recipe()
    # Only the `docker compose` command line; later recipe lines are echo banners.
    compose_cmd = " ".join(
        line.rstrip("\\").strip()
        for line in recipe.replace("\\\n", " ").splitlines()
        if "docker compose" in line and not line.lstrip().startswith("echo")
    )
    files = re.findall(r"-f (docker-compose[\w.-]*\.yml)", compose_cmd)
    assert files == [
        "docker-compose.yml",
        "docker-compose.override.yml",
        "docker-compose.ci.yml",
        "docker-compose.dry-run.yml",
    ]
    assert "--profile agent" in compose_cmd and " up -d" in compose_cmd


def test_dry_run_layer_puts_the_agent_in_the_profile_make_dry_run_activates():
    """FR-090: the agent only starts if its service sits in the activated profile."""
    activated = re.findall(r"--profile (\w+)", _dry_run_recipe())
    assert "agent" in activated
    profiles = _load_layer(DRY_RUN_FILE)["services"]["agent"].get("profiles", [])
    assert "agent" in profiles


@pytest.mark.uc("UC-070", cond=["Pre-1"])
def test_every_image_follows_image_tag_and_no_layer_hard_pins_a_tag():
    for name, service in ic.compose_services().items():
        image = service.get("image", "")
        if "joustmania/" in image:
            # #1780: the collector may be overridden by its content-hash tag in CI.
            image = image.replace(
                "${OTEL_COLLECTOR_TAG:-${IMAGE_TAG:-latest}}", "${IMAGE_TAG:-latest}"
            )
            assert image.endswith(":${IMAGE_TAG:-latest}"), f"{name}: {image}"
    for layer in ("docker-compose.override.yml", "docker-compose.ci.yml"):
        services = _load_layer(REPO_ROOT / layer).get("services", {})
        for name, service in services.items():
            assert "joustmania/" not in service.get("image", ""), f"{layer}:{name}"
    assert "image" not in str(_load_layer(DRY_RUN_FILE)["services"].get("agent", {}))


@pytest.mark.uc("UC-070", cond=["Pre-1"])
def test_dry_run_uses_mock_controllers_from_the_ci_flag_baseline():
    ci = _load_layer(REPO_ROOT / "docker-compose.ci.yml")["services"]
    assert "./services/flagd/ci:/etc/flagd:ro" in ci["flag-service"]["volumes"]
    backend = json.loads((CI_FLAGD_DIR / "controller.json").read_text())["flags"][
        "backend"
    ]
    assert backend["variants"][backend["defaultVariant"]] == "mock"


def test_dry_run_agent_starts_with_a_seeded_experiment():
    env = _agent_env()
    # #1907: the loop is gated only by the experiments_enabled flag; the dry-run sets
    # no env gate or tick (the enable step flips the flag through the flag service).
    for removed in (
        "AGENT_EXPERIMENTS_ENABLED",
        "AGENT_EXPERIMENT_TICK_SECONDS",
        "AGENT_EXPERIMENT_DYNAMIC_ENABLED",
    ):
        assert removed not in env, f"dry-run still sets the removed {removed}"
    seed_flag = env["AGENT_EXPERIMENT_SEED_FLAG"]
    default = re.fullmatch(r"\$\{AGENT_EXPERIMENT_SEED_FLAG:-(\w+)\}", seed_flag)
    assert default, f"seed flag has no default: {seed_flag}"
    assert (
        default.group(1)
        in json.loads((CI_FLAGD_DIR / "game.json").read_text())["flags"]
    )
    for name in ("VALUE", "OBJECTIVE", "TARGET_N", "HYPOTHESIS"):
        assert re.search(
            rf"\$\{{AGENT_EXPERIMENT_SEED_{name}:-.+\}}",
            env[f"AGENT_EXPERIMENT_SEED_{name}"],
        ), f"AGENT_EXPERIMENT_SEED_{name} has no default"
    # The base compose passes the same names through, empty by default (seed off).
    base_env = ic.compose_services()["agent"]["environment"]
    assert "AGENT_EXPERIMENT_SEED_FLAG=${AGENT_EXPERIMENT_SEED_FLAG:-}" in base_env


def test_agent_reads_the_seed_env_names_the_dry_run_sets():
    source = (REPO_ROOT / "services" / "agent" / "experiment_loop.go").read_text()
    for name in _agent_env():
        if name.startswith("AGENT_EXPERIMENT_SEED_"):
            # Resolve the Go const holding the name, then require a real Getenv call
            # on it (a comment or a bare string mention does not count).
            const = re.search(rf'^\s*(\w+)\s*=\s*"{name}"', source, re.M)
            assert const, f"agent declares no const for {name}"
            assert re.search(rf"os\.Getenv\(\s*{const.group(1)}\s*\)", source), (
                f"agent never reads {name}"
            )
