"""The operator pad deploys as an opt-in, unprivileged compose service (UC-015 Pre-1, FR-195, #2050 PR7).

Static checks over the compose files: ``docker-compose.yml`` starts ``operator-pad`` only
under its ``operator-pad`` profile, never privileged, with exactly one host mount
(``/dev/input``, read-only), the evdev cgroup rule (char major 13, so a pad plugged in later
works), the host's input group through ``INPUT_GID`` and no ``/run/udev``. No other service
waits for it, so a stack without the profile behaves as before (FR-195). The lite stack has
no optional services and no pad. Hardware behaviour (grab, LEDs) needs a Pi (#2050 PR8).
"""

from __future__ import annotations

import re

import infra_config as ic
import pytest

PAD = "operator-pad"
OVERLAYS = [
    "docker-compose.override.yml",
    "docker-compose.dev.yml",
    "docker-compose.ci.yml",
    "docker-compose.dry-run.yml",
]


def _services(name: str = "docker-compose.yml") -> dict:
    return ic.load_yaml(ic.REPO_ROOT / name)["services"]


def _pad() -> dict:
    return _services()[PAD]


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_pad_starts_only_with_its_profile():
    assert _pad().get("profiles") == [PAD]
    for overlay in OVERLAYS:
        path = ic.REPO_ROOT / overlay
        # text match: the overlays use compose merge tags (!override, !reset) safe_load rejects
        if path.exists():
            redefined = re.search(rf"^  {PAD}:", path.read_text(), re.MULTILINE)
            assert not redefined, (
                f"{overlay} redefines {PAD}; a merged profile could start it by default"
            )


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_pad_is_not_privileged():
    pad = _pad()
    assert pad.get("privileged", False) is False
    for key in (
        "user",
        "pid",
        "network_mode",
        "cap_add",
        "devices",
        "ipc",
        "security_opt",
    ):
        assert key not in pad, f"{PAD} must not set {key}"
    assert pad.get("restart") == "unless-stopped"


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_pad_mounts_only_dev_input_read_only():
    volumes = _pad().get("volumes")
    assert volumes == ["/dev/input:/dev/input:ro"], volumes
    assert not any("udev" in str(v) for v in volumes), "evdev needs no /run/udev"


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_pad_cgroup_rule_allows_evdev_nodes_only():
    assert _pad().get("device_cgroup_rules") == ["c 13:* rw"]


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_pad_joins_the_host_input_group():
    group_add = _pad().get("group_add")
    assert isinstance(group_add, list) and len(group_add) == 1, group_add
    match = re.fullmatch(r"\$\{INPUT_GID:-(\d+)\}", str(group_add[0]))
    assert match, (
        f"group_add must be ${{INPUT_GID:-<gid>}} (host identity, C-035), got {group_add[0]!r}"
    )
    assert match.group(1) == "996", "the default is the stage Pi's input GID"


@pytest.mark.uc("UC-015", cond=["Pre-1"])
def test_pad_waits_only_for_flagd():
    depends = _pad().get("depends_on") or {}
    assert set(depends) == {"flagd"}, depends


@pytest.mark.uc("UC-015", cond=["Pre-1"])
@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.lite.yml"])
def test_no_service_depends_on_the_pad(compose):
    """FR-195: removing the pad leaves every other service unchanged."""
    for name, svc in _services(compose).items():
        assert PAD not in (svc.get("depends_on") or {}), f"{name} depends on {PAD}"


def test_lite_stack_has_no_pad():
    assert PAD not in _services("docker-compose.lite.yml")
