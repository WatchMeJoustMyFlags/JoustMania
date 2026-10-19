"""The mock control API port is published by the dev/test override only (UC-003 Post-F-1).

Static checks over the compose files: ``docker-compose.override.yml`` publishes the
controller manager's mock control API (50062) to the host, and the base
``docker-compose.yml`` does not, so a stack started without the override has no
host route to the mock API.
"""

from __future__ import annotations

import infra_config as ic
import pytest

MOCK_API_PORT = "50062"
OVERRIDE_FILE = ic.REPO_ROOT / "docker-compose.override.yml"


def _published_ports(compose_file) -> list[str]:
    service = ic.load_yaml(compose_file)["services"].get("controller-manager") or {}
    return [str(entry) for entry in service.get("ports", [])]


def _publishes_mock_api(ports: list[str]) -> bool:
    # Short syntax: "[host_ip:]host_port:container_port[/proto]"; the container port is last.
    return any(
        entry.split("/")[0].rsplit(":", 1)[-1] == MOCK_API_PORT for entry in ports
    )


@pytest.mark.uc("UC-003", cond=["Post-F-1"])
def test_override_publishes_the_mock_api_port():
    assert _publishes_mock_api(_published_ports(OVERRIDE_FILE))


@pytest.mark.uc("UC-003", cond=["Post-F-1"])
def test_base_compose_does_not_publish_the_mock_api_port():
    assert not _publishes_mock_api(_published_ports(ic.COMPOSE_FILE))
