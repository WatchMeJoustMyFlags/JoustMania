"""Every flagd client is pointed at flagd's sync port (#1713, #2090).

All flagd clients use the in-process resolver (docs/specs/flags.md §1.1), which
syncs from flagd's sync port (8015). Its RPC evaluation port (8013) accepts the
TCP connection but never sends a sync, so a client pointed there keeps its code
defaults for good: rust-hid's image set ``FLAGD_PORT=8013`` and never synced on
the stage Pi (#2090). Compose leaves rust-hid's ``FLAGD_PORT`` unset, so the
image's ``ENV`` is what runs. Every layer that sets the port is checked: each
compose file's ``environment``, each Dockerfile's ``ENV`` and rust-hid's code
default.
"""

from __future__ import annotations

import re

import env_inventory as ei
import flagd_harness as fh
import infra_config as ic
import pytest

COMPOSE_FILES = sorted(ic.REPO_ROOT.glob("docker-compose*.yml"))
DOCKERFILES = sorted(
    p for p in (ic.REPO_ROOT / "services").glob("*/Dockerfile*") if p.is_file()
)
RUST_HID_FLAGS = ic.REPO_ROOT / "services" / "rust-hid" / "src" / "feature_flags.rs"


def _dockerfile_env(path) -> dict[str, str]:
    """``ENV`` values over every stage (``ENV K=V ...`` and ``ENV K V``)."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("ENV "):
            continue
        rest = stripped[4:].strip()
        if "=" in rest.split()[0]:
            env.update(tok.split("=", 1) for tok in rest.split() if "=" in tok)
        else:
            key, _, value = rest.partition(" ")
            env[key] = value.strip()
    return env


def test_the_layers_are_found():
    assert ic.COMPOSE_FILE in COMPOSE_FILES
    assert ic.REPO_ROOT / "services" / "rust-hid" / "Dockerfile" in DOCKERFILES


@pytest.mark.uc("UC-035", flow="main")
@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_sets_flagd_port_only_to_the_sync_port(compose):
    wrong = {
        name: env["FLAGD_PORT"]
        for name, service in (ei._load_compose(compose) or {}).items()
        if (env := ei._env_dict((service or {}).get("environment")))
        and env.get("FLAGD_PORT") not in (None, str(fh.SYNC_PORT))
    }
    assert not wrong, (
        f"{compose.name}: FLAGD_PORT must be flagd's sync port {fh.SYNC_PORT}: {wrong}"
    )


@pytest.mark.uc("UC-035", flow="main")
@pytest.mark.parametrize(
    "dockerfile", DOCKERFILES, ids=lambda p: str(p.relative_to(ic.REPO_ROOT))
)
def test_image_sets_flagd_port_only_to_the_sync_port(dockerfile):
    port = _dockerfile_env(dockerfile).get("FLAGD_PORT")
    assert port in (None, str(fh.SYNC_PORT)), (
        f"{dockerfile.relative_to(ic.REPO_ROOT)}: ENV FLAGD_PORT={port}; the in-process "
        f"resolver syncs only from flagd's sync port {fh.SYNC_PORT} (#2090)"
    )


@pytest.mark.uc("UC-035", flow="main")
def test_rust_hid_code_default_is_the_sync_port():
    match = re.search(
        r"const DEFAULT_SYNC_PORT: u16 = (\d+);", RUST_HID_FLAGS.read_text()
    )
    assert match, (
        "DEFAULT_SYNC_PORT not found in services/rust-hid/src/feature_flags.rs"
    )
    assert int(match.group(1)) == fh.SYNC_PORT
