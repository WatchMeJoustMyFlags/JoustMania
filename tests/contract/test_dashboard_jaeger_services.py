"""Every Jaeger service name the dashboard SPA searches is a real OTel service identity (#2061).

The SPA links and embeds `/jaeger/search?service=<name>` and queries `/jaeger/api/v3/traces`
with a `query.serviceName` parameter (#2283). A name that is not an `OTEL_SERVICE_NAME` shows an empty
Jaeger (the coordinator once linked `game_coordinator` instead of `game-coordinator-service`).
Declared identities: docs/specs/telemetry.md section 1 and docker-compose.yml.
"""

from __future__ import annotations

import re

import infra_config as ic
import pytest

SRC = ic.REPO_ROOT / "services" / "dashboard" / "src"
TELEMETRY_MD = ic.REPO_ROOT / "docs" / "specs" / "telemetry.md"

# `service=<name>` in a /jaeger/ URL, or a `service: "<name>"` query parameter.
URL_RE = re.compile(r"/jaeger/[^\"'`\s]*?[?&]service=([A-Za-z0-9_.-]+)")
PARAM_RE = re.compile(r"""\bservice:\s*["']([A-Za-z0-9_.-]+)["']""")


def _spa_files():
    for path in sorted(SRC.rglob("*")):
        if (
            path.suffix in {".html", ".ts"}
            and "__tests__" not in path.parts
            and "gen" not in path.parts
        ):
            yield path


def _used_services() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in _spa_files():
        text = path.read_text()
        for name in URL_RE.findall(text) + (
            PARAM_RE.findall(text) if "jaeger" in text.lower() else []
        ):
            used.setdefault(name, set()).add(str(path.relative_to(ic.REPO_ROOT)))
    return used


def _compose_identities() -> set[str]:
    names = set()
    for svc in ic.compose_services().values():
        env = svc.get("environment") or []
        items = [f"{k}={v}" for k, v in env.items()] if isinstance(env, dict) else env
        for item in items:
            if str(item).startswith("OTEL_SERVICE_NAME="):
                names.add(str(item).split("=", 1)[1])
    return names


def _spec_identities() -> set[str]:
    text = TELEMETRY_MD.read_text()
    block = text.split("Service identities", 1)[1].split("**Traceability:**", 1)[0]
    return set(re.findall(r"`([a-z][a-z0-9-]+)`", block)) - {
        "joustmania",
        "infrastructure",
    }


def test_spa_uses_jaeger_services():
    used = _used_services()
    assert "game-coordinator-service" in used and "agent" in used, used


@pytest.mark.parametrize("name", sorted(_used_services()))
def test_jaeger_service_is_a_declared_identity(name):
    assert name in _compose_identities(), (
        f"{name!r} is no OTEL_SERVICE_NAME in docker-compose.yml"
    )
    assert name in _spec_identities(), (
        f"{name!r} is not a service identity in telemetry.md section 1"
    )
