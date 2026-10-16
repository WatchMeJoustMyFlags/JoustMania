"""Static helpers over the infrastructure config (envoy, compose, collector) for #1365."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENVOY_FILE = REPO_ROOT / "services" / "envoy" / "envoy.yaml"
COLLECTOR_FILE = REPO_ROOT / "services" / "otel-collector" / "config.yaml"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def compose_services() -> dict:
    return load_yaml(COMPOSE_FILE)["services"]


def compose_image(service: str) -> str:
    """The image tag docker-compose.yml runs for ``service`` (keeps CI and the stack in step)."""
    return compose_services()[service]["image"]


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        == 0
    )


# --------------------------------------------------------------------------- #
# Envoy
# --------------------------------------------------------------------------- #
def envoy_routes() -> list[dict]:
    listener = load_yaml(ENVOY_FILE)["static_resources"]["listeners"][0]
    hcm = listener["filter_chains"][0]["filters"][0]["typed_config"]
    return hcm["route_config"]["virtual_hosts"][0]["routes"]


def envoy_listener_port() -> int:
    listener = load_yaml(ENVOY_FILE)["static_resources"]["listeners"][0]
    return listener["address"]["socket_address"]["port_value"]


def envoy_clusters() -> dict[str, tuple[str, int]]:
    """cluster name -> (upstream host, port)."""
    out = {}
    for cluster in load_yaml(ENVOY_FILE)["static_resources"]["clusters"]:
        endpoint = cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0][
            "endpoint"
        ]
        sock = endpoint["address"]["socket_address"]
        out[cluster["name"]] = (sock["address"], sock["port_value"])
    return out


def resolve_route(path: str) -> dict:
    """Emulate envoy's first-match routing for ``path`` (``prefix`` and exact ``path`` matchers).

    Returns ``{"cluster": ..., "upstream_path": ...}``, ``{"redirect": ...}`` or
    ``{"direct_response": status}``.
    """
    for route in envoy_routes():
        match = route["match"]
        if "path" in match:
            hit = path == match["path"]
        elif "prefix" in match:
            hit = path.startswith(match["prefix"])
        else:
            raise AssertionError(f"unsupported matcher in envoy.yaml: {match}")
        if not hit:
            continue
        if "redirect" in route:
            return {"redirect": route["redirect"].get("path_redirect")}
        if "direct_response" in route:
            return {"direct_response": route["direct_response"]["status"]}
        action = route["route"]
        upstream = path
        if "prefix_rewrite" in action:
            upstream = action["prefix_rewrite"] + path[len(match["prefix"]) :]
        return {"cluster": action["cluster"], "upstream_path": upstream}
    raise AssertionError(f"no envoy route matches {path}")


# Host-less ``:PORT`` (``AGENT_HEALTH_ADDR=:13134``) or a local listen/probe address.
# ``http://otel-collector:4317`` in an env value names ANOTHER service's port and
# must not count as this service's own.
_PORT_IN_TEXT_RE = re.compile(
    r"(?:(?<![\w.-])|\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)):(\d{2,5})\b"
)


def compose_container_ports(service: str) -> set[int]:
    """Container ports compose shows for ``service``: ``expose``, the container side of
    ``ports``, ``:PORT`` in environment values (e.g. ``AGENT_HEALTH_ADDR=:13134``) and
    the port the healthcheck probes."""
    svc = compose_services()[service]
    ports = {int(p) for p in svc.get("expose") or []}
    ports |= {int(str(p).rsplit(":", 1)[-1]) for p in svc.get("ports") or []}
    env = svc.get("environment") or []
    env_values = (
        env.values() if isinstance(env, dict) else [e.split("=", 1)[-1] for e in env]
    )
    texts = [str(v) for v in env_values]
    texts.append(" ".join(map(str, (svc.get("healthcheck") or {}).get("test") or [])))
    for text in texts:
        ports |= {int(p) for p in _PORT_IN_TEXT_RE.findall(text)}
    return ports
