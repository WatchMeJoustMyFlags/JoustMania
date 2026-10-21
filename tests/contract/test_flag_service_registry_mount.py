"""The flag service gets its registry from the same checkout as its baseline (#1838).

``registry.yaml`` is validated against the baseline flag files at start
(``registry: interventions/<key>: no such flag in the baseline``). Before #1838 the
registry came from the image and the baseline from the checkout, so a flag rename
with an old image beside a new checkout (or the reverse) stopped the flag service
and with it every flag in the stack. Compose mounts the registry next to the
baseline and points ``FLAG_SERVICE_REGISTRY`` at it.
"""

from __future__ import annotations

import infra_config as ic
import pytest
import yaml

REGISTRY_SOURCE = "./services/flag-service/registry.yaml"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.lite.yml"]
# Overlays that replace (``!override``) the flag-service volume list, so the base mount is
# gone unless the overlay repeats it (the CI stack: make test, integration, dry-run).
VOLUME_OVERRIDE_FILES = ["docker-compose.ci.yml"]


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose files that use the ``!override`` / ``!reset`` merge tags."""


def _plain(loader, tag_suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _plain)


def _flag_service(compose_name: str) -> dict:
    services = ic.load_yaml(ic.REPO_ROOT / compose_name)["services"]
    assert "flag-service" in services, f"{compose_name} has no flag-service"
    return services["flag-service"]


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    return dict(item.split("=", 1) for item in env if "=" in item)


@pytest.mark.uc("UC-053", flow="main")
@pytest.mark.parametrize("compose_name", COMPOSE_FILES)
def test_compose_mounts_the_registry_from_the_checkout(compose_name):
    service = _flag_service(compose_name)
    target = _environment(service).get("FLAG_SERVICE_REGISTRY")
    assert target, f"{compose_name}: FLAG_SERVICE_REGISTRY is not set"
    mounts = [str(v) for v in service.get("volumes", [])]
    expected = f"{REGISTRY_SOURCE}:{target}:ro"
    assert expected in mounts, (
        f"{compose_name}: no read-only mount {expected!r} in {mounts}"
    )


@pytest.mark.uc("UC-053", flow="main")
def test_the_mounted_registry_file_exists():
    assert (ic.REPO_ROOT / REGISTRY_SOURCE).is_file()


@pytest.mark.uc("UC-053", flow="main")
@pytest.mark.parametrize("compose_name", VOLUME_OVERRIDE_FILES)
def test_overlays_that_override_the_volumes_repeat_the_registry_mount(compose_name):
    text = (ic.REPO_ROOT / compose_name).read_text(encoding="utf-8")
    service = yaml.load(text, Loader=_ComposeLoader)["services"]["flag-service"]
    mounts = [str(v) for v in service.get("volumes", [])]
    target = _environment(_flag_service("docker-compose.yml"))["FLAG_SERVICE_REGISTRY"]
    expected = f"{REGISTRY_SOURCE}:{target}:ro"
    assert expected in mounts, (
        f"{compose_name}: the volumes override drops {expected!r}: {mounts}"
    )
