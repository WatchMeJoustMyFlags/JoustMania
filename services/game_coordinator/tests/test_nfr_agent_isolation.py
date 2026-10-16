"""NFR-013 "never stall the game": the game coordinator has no client of the agent or a model.

NFR-013 holds by structure: the coordinator never calls the agent service or a
model, it only reacts to flag changes, so a remote decision adds 0 ms to the game
loop. This test pins that structure so a later change cannot quietly add such a
call: it parses every production module of the coordinator and checks its imports.

- No LLM SDK and no HTTP client library is imported.
- The only gRPC stubs imported are the ones for the coordinator's known
  dependencies (controller manager, audio), its own service and its health
  server. The agent has no gRPC API at all (it only receives OTLP), so any new
  stub is a new outbound dependency that needs a spec change first.

The agent side of NFR-013 (asynchronous call, one in flight, timeout and error
fall back to rules) is covered by the Go tests tagged ``// NFR: NFR-013`` in
services/agent/decision/decide_hook_test.go.
"""

import ast
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent

FORBIDDEN_IMPORT_PREFIXES = (
    "anthropic",
    "openai",
    "litellm",
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "http.client",
    "websockets",
)
ALLOWED_GRPC_STUBS = {
    "proto.audio_pb2_grpc",
    "proto.controller_manager_pb2_grpc",
    "proto.game_coordinator_pb2_grpc",
    "grpc_health.v1.health_pb2_grpc",  # the coordinator's own health server
}


def _production_modules() -> list[Path]:
    skipped = {"tests", ".venv", "__pycache__"}
    return [p for p in SERVICE_DIR.rglob("*.py") if not skipped & set(p.relative_to(SERVICE_DIR).parts)]


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


@pytest.fixture(scope="module")
def imports_by_module() -> dict[str, set[str]]:
    modules = _production_modules()
    assert len(modules) > 10, f"expected the coordinator's production modules under {SERVICE_DIR}"
    return {str(p.relative_to(SERVICE_DIR)): _imports(p) for p in modules}


@pytest.mark.nfr("NFR-013")
def test_coordinator_imports_no_model_sdk_or_http_client(imports_by_module):
    offenders = {
        module: sorted(n for n in names if n.startswith(FORBIDDEN_IMPORT_PREFIXES))
        for module, names in imports_by_module.items()
    }
    offenders = {m: n for m, n in offenders.items() if n}
    assert not offenders, f"game coordinator imports a model SDK or HTTP client (NFR-013): {offenders}"


@pytest.mark.nfr("NFR-013")
def test_coordinator_grpc_stubs_are_known_dependencies_only(imports_by_module):
    stubs = {n for names in imports_by_module.values() for n in names if n.endswith("_pb2_grpc")}
    assert "proto.controller_manager_pb2_grpc" in stubs, "scan found no gRPC stubs; is the import parsing broken?"
    unexpected = stubs - ALLOWED_GRPC_STUBS
    assert not unexpected, f"game coordinator imports a new gRPC client {sorted(unexpected)} (NFR-013)"
