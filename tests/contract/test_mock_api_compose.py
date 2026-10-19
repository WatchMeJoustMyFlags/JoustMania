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


def _published_ports(compose_file) -> list:
    services = ic.load_yaml(compose_file)["services"]
    assert "controller-manager" in services, (
        f"{compose_file.name} has no controller-manager service"
    )
    return services["controller-manager"].get("ports", [])


def _container_port(entry) -> str:
    """Container port of a short ("[ip:]host:container[/proto]") or long ({target, published}) entry."""
    if isinstance(entry, dict):
        return str(entry.get("target", ""))
    return str(entry).split("/")[0].rsplit(":", 1)[-1]


def _publishes_mock_api(ports: list) -> bool:
    return any(_container_port(entry) == MOCK_API_PORT for entry in ports)


@pytest.mark.uc("UC-003", cond=["Post-F-1"])
def test_override_publishes_the_mock_api_port():
    assert _publishes_mock_api(_published_ports(OVERRIDE_FILE))


@pytest.mark.uc("UC-003", cond=["Post-F-1"])
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("50062:50062", True),
        ("127.0.0.1:50062:50062/tcp", True),
        ({"target": 50062, "published": "50062"}, True),
        ("50052:50052", False),
        ({"target": 50052}, False),
    ],
)
def test_port_parser_reads_both_syntaxes(entry, expected):
    assert _publishes_mock_api([entry]) is expected


@pytest.mark.uc("UC-003", cond=["Post-F-1"])
def test_base_compose_does_not_publish_the_mock_api_port():
    assert not _publishes_mock_api(_published_ports(ic.COMPOSE_FILE))
