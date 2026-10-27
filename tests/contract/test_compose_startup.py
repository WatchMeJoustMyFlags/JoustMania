"""Compose start-up contract (UC-060 BR-004, FR-181, #2027).

The game must start while observability boots: no game service depends on an
observability service, directly or transitively, and the collector does not wait
for its backends. Every service restarts and rotates its logs.
"""

from __future__ import annotations

import infra_config as ic
import pytest
import yaml

OBS = {"jaeger", "victoria-metrics", "grafana", "otel-collector"}
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.lite.yml"]
# Overlays merged on top of docker-compose.yml (make dev, make ci, make dry-run).
OVERLAYS = [
    "docker-compose.override.yml",
    "docker-compose.dev.yml",
    "docker-compose.ci.yml",
    "docker-compose.dry-run.yml",
]


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads compose merge tags (!override, !reset) as plain values."""


def _construct_tagged(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_tagged)


def _services(name: str) -> dict:
    return ic.load_yaml(ic.REPO_ROOT / name)["services"]


def _merged_with_overlays(base: str) -> dict:
    """Base services with every overlay's depends_on edges added (union, the
    conservative superset of any merge or !override)."""
    services = {n: dict(v) for n, v in _services(base).items()}
    for name in OVERLAYS:
        path = ic.REPO_ROOT / name
        if not path.exists():
            continue
        overlay = (
            yaml.load(path.read_text(), Loader=_ComposeLoader).get("services") or {}
        )
        for svc_name, svc in overlay.items():
            extra = _deps(svc or {})
            if extra:
                merged = _deps(services.get(svc_name, {})) | extra
                services.setdefault(svc_name, {})["depends_on"] = sorted(merged)
    return services


def _deps(svc: dict) -> set[str]:
    deps = svc.get("depends_on") or {}
    return set(deps) if isinstance(deps, (list, dict)) else set()


def _reachable(services: dict, start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        for dep in _deps(services.get(todo.pop(), {})):
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return seen


@pytest.mark.uc("UC-060", flow="main", br=["BR-004"])
@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_no_game_service_depends_on_observability(compose):
    _assert_no_game_to_obs(_services(compose))


@pytest.mark.uc("UC-060", flow="main", br=["BR-004"])
def test_overlays_add_no_game_to_observability_dependency():
    _assert_no_game_to_obs(_merged_with_overlays("docker-compose.yml"))


@pytest.mark.uc("UC-060", flow="main", br=["BR-004"])
def test_graph_walk_finds_transitive_game_to_observability_edge():
    """Pins _reachable: game -> relay -> jaeger must be reported for game."""
    synthetic = {
        "game": {"depends_on": {"relay": {"condition": "service_started"}}},
        "relay": {"depends_on": ["jaeger"]},
        "jaeger": {},
        "audio": {},
    }
    assert _reachable(synthetic, "game") == {"relay", "jaeger"}
    with pytest.raises(AssertionError, match="game"):
        _assert_no_game_to_obs(synthetic)


def _assert_no_game_to_obs(services: dict) -> None:
    offenders = {
        name: sorted(_reachable(services, name) & OBS)
        for name in services
        if name not in OBS and _reachable(services, name) & OBS
    }
    assert not offenders, f"game services reach observability: {offenders}"


@pytest.mark.uc("UC-060", flow="main", br=["BR-004"])
def test_collector_does_not_wait_for_its_backends():
    collector = _services("docker-compose.yml")["otel-collector"]
    assert not (_deps(collector) & OBS), _deps(collector)


@pytest.mark.uc("UC-060", flow="main", br=["BR-004"])
@pytest.mark.uc("UC-002", flow="A7")
@pytest.mark.nfr("NFR-054")
@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_every_service_restarts_and_rotates_logs(compose):
    bad = {}
    for name, svc in _services(compose).items():
        problems = []
        if svc.get("restart") != "unless-stopped":
            problems.append(f"restart={svc.get('restart')!r}")
        options = (svc.get("logging") or {}).get("options") or {}
        if not options.get("max-size") or not options.get("max-file"):
            problems.append("no max-size/max-file")
        if problems:
            bad[name] = problems
    assert not bad, bad
