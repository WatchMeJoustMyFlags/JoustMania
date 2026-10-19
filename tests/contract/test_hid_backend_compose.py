"""rust-hid is the default Bluetooth backend in every stack that reads real controllers (UC-002 BR-003, #1371).

Static checks over the compose files: ``docker-compose.yml`` and ``docker-compose.lite.yml``
both start rust-hid without a profile and make the controller manager wait for it to be
healthy; python-hid is opt-in only (the ``python-hid`` profile, an optional dependency) or
absent. Hardware behaviour needs a Pi.
"""

from __future__ import annotations

import infra_config as ic
import pytest

STACKS = {
    "full": ic.COMPOSE_FILE,
    "lite": ic.REPO_ROOT / "docker-compose.lite.yml",
}


def _services(stack: str) -> dict:
    return ic.load_yaml(STACKS[stack])["services"]


@pytest.mark.uc("UC-002", br=["BR-003"])
@pytest.mark.parametrize("stack", sorted(STACKS))
def test_stack_starts_rust_hid_by_default(stack):
    services = _services(stack)
    rust_hid = services["rust-hid"]
    assert not rust_hid.get("profiles"), (
        "a profile would keep rust-hid from starting by default"
    )
    depends = services["controller-manager"]["depends_on"]
    assert depends["rust-hid"]["condition"] == "service_healthy"
    assert depends["rust-hid"].get("required", True) is True


@pytest.mark.uc("UC-002", br=["BR-003"])
@pytest.mark.parametrize("stack", sorted(STACKS))
def test_python_hid_is_opt_in(stack):
    services = _services(stack)
    python_hid = services.get("python-hid")
    if python_hid is None:
        return
    assert python_hid.get("profiles") == ["python-hid"]
    dependency = services["controller-manager"]["depends_on"].get("python-hid")
    if dependency is not None:
        assert dependency.get("required") is False, (
            "the controller manager must start without python-hid"
        )
