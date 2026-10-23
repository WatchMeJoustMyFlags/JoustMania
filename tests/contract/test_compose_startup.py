"""Compose start-up contract (UC-060 BR-004, FR-181, #2027).

The game must start while observability boots: no game service depends on an
observability service, directly or transitively, and the collector does not wait
for its backends. Every service restarts and rotates its logs.
"""

from __future__ import annotations

import infra_config as ic
import pytest

OBS = {"jaeger", "loki", "victoria-metrics", "grafana", "otel-collector"}
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.lite.yml"]


def _services(name: str) -> dict:
    return ic.load_yaml(ic.REPO_ROOT / name)["services"]


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
    services = _services(compose)
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
