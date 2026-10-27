"""The pairing daemon runs on the host network in every stack that reads real controllers (#1402 I2).

Raw HCI (hciconfig / hcitool, the Bluetooth monitor's RSSI) needs the host network
namespace (UC-001 precondition), so the full and the lite stack wire it the same way:
flagd and rust-hid are reached through loopback-published ports, and the connect-proxy
reaches the daemon's control API through ``host.docker.internal`` (UC-005 BR-003).
Static checks over the compose files; hardware behaviour needs a Pi.
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


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return dict(item.split("=", 1) for item in env)


@pytest.mark.uc("UC-001", cond=["Pre-1"])
@pytest.mark.parametrize("stack", sorted(STACKS))
def test_pairing_daemon_uses_the_host_network(stack):
    daemon = _services(stack)["pairing-daemon"]
    assert daemon.get("network_mode") == "host"
    assert "networks" not in daemon, "network_mode: host excludes compose networks"
    assert daemon.get("privileged") is True


@pytest.mark.uc("UC-001", flow="main", steps=[4], br=["BR-003"])
@pytest.mark.parametrize("stack", sorted(STACKS))
@pytest.mark.parametrize(
    ("host_var", "port_var", "service"),
    [
        ("FLAGD_HOST", "FLAGD_PORT", "flagd"),
        ("RUST_HID_HOST", "RUST_HID_PORT", "rust-hid"),
    ],
)
def test_pairing_daemon_reaches_loopback_published_ports(
    stack, host_var, port_var, service
):
    """Docker DNS is unavailable on the host network: 127.0.0.1 plus a loopback-only publish (#1319)."""
    services = _services(stack)
    env = _env(services["pairing-daemon"])
    assert env[host_var] == "127.0.0.1"
    port = env[port_var]
    assert set(services[service].get("ports") or []) == {f"127.0.0.1:{port}:{port}"}


@pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
@pytest.mark.parametrize("stack", sorted(STACKS))
def test_pairing_daemon_game_state_goes_through_the_host_published_proxy(stack):
    """The host network cannot resolve ``envoy``: the game state URL must use the published port."""
    url = _env(_services(stack)["pairing-daemon"])["GAME_STATE_URL"]
    assert url.startswith("http://localhost/")


@pytest.mark.uc("UC-005", flow="main", br=["BR-003"])
@pytest.mark.parametrize("stack", sorted(STACKS))
def test_control_api_is_reachable_by_the_proxy_but_not_the_lan(stack):
    services = _services(stack)
    bind = _env(services["pairing-daemon"])["CONTROL_BIND"]
    assert "0.0.0.0" not in bind
    assert "127.0.0.1" in bind and "172.17.0.1" in bind
    proxy = services["connect-proxy"]
    assert _env(proxy)["PAIRING_DAEMON_URL"] == "http://host.docker.internal:8002"
    assert "host.docker.internal:host-gateway" in (proxy.get("extra_hosts") or [])
