"""NFR-013 "never stall the game": the game coordinator has no client of the agent or a model.

NFR-013 holds by structure: the coordinator never calls the agent service or a
model, it only reacts to flag changes, so a remote decision adds 0 ms to the game
loop. This test pins that structure so a later change cannot quietly add such a
call. It parses every production module of the coordinator, plus every ``lib``
module they import (followed transitively through ``lib``), and checks:

- No LLM SDK and no HTTP client library is imported.
- The only gRPC stubs imported are the ones for the coordinator's known
  dependencies (controller manager, audio), its own service and its health
  server. The agent has no gRPC API at all (it only receives OTLP), so any new
  stub is a new outbound dependency that needs a spec change first.
- No dynamic import (``importlib.import_module`` / ``__import__``), which would
  hide an import from this scan.

Blind spots (what this static scan cannot see):
- third-party packages are not followed: an allowed dependency (grpc,
  openfeature, opentelemetry) is trusted not to call the agent or a model;
- a raw socket, a subprocess, or a gRPC channel used with a generic stub
  (``channel.unary_unary("/agent...")``) is not detected;
- the ``proto`` package itself is not scanned, only which stubs are imported.

The agent side of NFR-013 (asynchronous call, one in flight, timeout and error
fall back to rules) is covered by the Go tests tagged ``// NFR: NFR-013`` in
services/agent/decision/decide_hook_test.go.
"""

import ast
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVICE_DIR.parent.parent
LIB_DIR = PROJECT_ROOT / "lib"

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
# The one HTTP client the coordinator has: the flag service's game lifecycle
# client (FR-125, #1410). It talks to the flag service only (FLAG_SERVICE_URL),
# never to the agent or a model, and its only await on the game path is bounded
# at 200 ms (test_flag_service_client_targets_only_the_flag_service).
FLAG_SERVICE_CLIENT = "services/game_coordinator/flag_service_client.py"
ALLOWED_HTTP_IMPORTS = {FLAG_SERVICE_CLIENT: {"urllib.request"}}
ALLOWED_GRPC_STUBS = {
    "proto.audio_pb2_grpc",
    "proto.controller_manager_pb2_grpc",
    "proto.game_coordinator_pb2_grpc",
    "grpc_health.v1.health_pb2_grpc",  # the coordinator's own health server
}


def _production_modules() -> list[Path]:
    skipped = {"tests", ".venv", "__pycache__"}
    return [p for p in SERVICE_DIR.rglob("*.py") if not skipped & set(p.relative_to(SERVICE_DIR).parts)]


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


def _dynamic_imports(tree: ast.Module) -> list[int]:
    """Line numbers of importlib.import_module(...) / __import__(...) calls."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name in {"import_module", "__import__"}:
            lines.append(node.lineno)
    return lines


def _lib_module_path(dotted: str) -> Path | None:
    """Map ``lib.a.b`` (module or ``module.symbol``) to its file under lib/, if any."""
    parts = dotted.split(".")[1:]
    while parts:
        candidate = LIB_DIR.joinpath(*parts)
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        if (candidate / "__init__.py").is_file():
            return candidate / "__init__.py"
        parts = parts[:-1]
    return None


def _scanned_modules() -> dict[str, ast.Module]:
    """The coordinator's production modules plus the lib modules reachable from them."""
    trees = {str(p.relative_to(PROJECT_ROOT)): _parse(p) for p in _production_modules()}
    queue = list(trees.values())
    while queue:
        for name in _imports(queue.pop()):
            if not name.startswith("lib."):
                continue
            path = _lib_module_path(name)
            if path is None:
                continue
            key = str(path.relative_to(PROJECT_ROOT))
            if key not in trees:
                trees[key] = _parse(path)
                queue.append(trees[key])
    return trees


@pytest.fixture(scope="module")
def scanned() -> dict[str, ast.Module]:
    trees = _scanned_modules()
    assert sum(k.startswith("services/") for k in trees) > 10, f"expected the coordinator's modules under {SERVICE_DIR}"
    assert any(k.startswith("lib/") for k in trees), "expected lib modules reached from the coordinator"
    return trees


@pytest.fixture(scope="module")
def imports_by_module(scanned) -> dict[str, set[str]]:
    return {module: _imports(tree) for module, tree in scanned.items()}


@pytest.mark.nfr("NFR-013")
@pytest.mark.uc("UC-040", br=["BR-001"])
def test_coordinator_imports_no_model_sdk_or_http_client(imports_by_module):
    offenders = {
        module: sorted(
            n
            for n in names
            if n.startswith(FORBIDDEN_IMPORT_PREFIXES) and n not in ALLOWED_HTTP_IMPORTS.get(module, set())
        )
        for module, names in imports_by_module.items()
    }
    offenders = {m: n for m, n in offenders.items() if n}
    assert not offenders, f"game coordinator imports a model SDK or HTTP client (NFR-013): {offenders}"


@pytest.mark.nfr("NFR-013")
@pytest.mark.uc("UC-040", br=["BR-001"])
def test_coordinator_grpc_stubs_are_known_dependencies_only(imports_by_module):
    stubs = {n for names in imports_by_module.values() for n in names if n.endswith("_pb2_grpc")}
    assert "proto.controller_manager_pb2_grpc" in stubs, "scan found no gRPC stubs; is the import parsing broken?"
    unexpected = stubs - ALLOWED_GRPC_STUBS
    assert not unexpected, f"game coordinator imports a new gRPC client {sorted(unexpected)} (NFR-013)"


@pytest.mark.nfr("NFR-013")
@pytest.mark.uc("UC-040", br=["BR-001"])
def test_no_dynamic_imports_hide_dependencies(scanned):
    offenders = {module: lines for module, tree in scanned.items() if (lines := _dynamic_imports(tree))}
    assert not offenders, f"dynamic imports hide dependencies from the NFR-013 scan: {offenders}"


@pytest.mark.nfr("NFR-013")
def test_flag_service_client_targets_only_the_flag_service():
    """The allowed HTTP client reads its base URL from FLAG_SERVICE_URL only and
    names no agent or model endpoint; its registration await is 200 ms."""
    source = (PROJECT_ROOT / FLAG_SERVICE_CLIENT).read_text()
    assert 'os.getenv("FLAG_SERVICE_URL"' in source
    for marker in ("agent:", "13134", "anthropic", "openai", "api.typesafe"):
        assert marker not in source, marker
    from services.game_coordinator import flag_service_client

    assert flag_service_client.REGISTER_AWAIT_SECONDS <= 0.2
