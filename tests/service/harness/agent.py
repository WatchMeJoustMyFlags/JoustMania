"""The Go agent on the service tier: ``go build`` of ``services/agent`` at HEAD, run as a subprocess.

Built from source, not the image: the binary is what the image runs
(``services/agent/Dockerfile`` is a plain ``go build``), a build is cached by a
hash of the sources like the flag service's (``flag_plane.build_flag_service``),
and it needs no image build or registry, so a Go change on the decide path is
always tested at HEAD. ``go run`` would recompile on every start.

The agent listens for OTLP/gRPC on an ephemeral port. The stack's ``OtlpSink``
tees every trace and metrics export of the services to it (the collector's
``otlp/agent`` exporter), and the agent exports its own spans and metrics back
to the sink, where the tests read them (Jaeger's role in compose).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .flag_plane import _cache_dir, _go_version, prune_binary_cache
from .processes import REPO_ROOT, ManagedProcess, base_env, free_port

AGENT_SRC = REPO_ROOT / "services" / "agent"
BINARY_PREFIX = "agent-"
OTEL_SERVICE_NAME = "agent"  # compose OTEL_SERVICE_NAME: the agent's span and metric resource
# The agent's own batch span processor: flush every 200 ms instead of the SDK's 5 s,
# so a test waits on spans, not on the exporter (standard OTel SDK env var).
BSP_SCHEDULE_DELAY_MS = "200"
# The shared per-minute remote-call budget (llm.max_requests_per_minute, 6/min by
# default) is lifted as in docker-compose.decide-e2e.yml: back-to-back decide
# cycles would otherwise turn into llm_budget_exhausted rules fallbacks.
LLM_MAX_REQUESTS_PER_MINUTE = "600"
# TCP port 1 (tcpmux) is never served here: a dial fails at once.
UNREACHABLE = "127.0.0.1:1"


def agent_source_hash() -> str:
    """Hash of every file ``go build`` reads (sources, go.mod/go.sum) and the Go toolchain."""
    digest = hashlib.sha256()
    digest.update(_go_version().encode())
    for path in sorted(AGENT_SRC.rglob("*")):
        if path.is_file() and path.suffix in (".go", ".mod", ".sum"):
            digest.update(str(path.relative_to(AGENT_SRC)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def build_agent() -> Path:
    """The agent binary for HEAD's sources; built once per source hash."""
    binary = _cache_dir() / f"{BINARY_PREFIX}{agent_source_hash()}"
    if binary.exists():
        binary.touch()  # mtime = last use, for the pruning below
        return binary
    tmp = binary.with_suffix(f".tmp-{uuid.uuid4().hex[:6]}")
    result = subprocess.run(
        ["go", "build", "-o", str(tmp), "."],
        cwd=AGENT_SRC,
        env={**os.environ, "CGO_ENABLED": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"go build services/agent failed:\n{result.stdout}{result.stderr}")
    tmp.replace(binary)  # atomic: a parallel run sees a whole binary or none
    prune_binary_cache(binary.parent, prefix=BINARY_PREFIX)
    return binary


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


class AgentProcess(ManagedProcess):
    """The agent binary with every address pointing into its stack."""

    def __init__(
        self,
        workdir: Path,
        *,
        listen_port: int,
        flagd_eval_port: int,
        flag_service_url: str,
        otlp_endpoint: str,
        coordinator_port: int,
        mock_control_port: int,
        openai_api_key: str,
    ):
        self.listen_port = listen_port
        self.health_port = free_port()
        env = {
            **base_env(),
            "AGENT_LISTEN_ADDR": f"127.0.0.1:{listen_port}",
            "AGENT_HEALTH_ADDR": f"127.0.0.1:{self.health_port}",
            # The agent resolves through flagd's RPC API (compose: flagd:8013).
            "FLAGD_HOST": "127.0.0.1",
            "FLAGD_PORT": str(flagd_eval_port),
            # flagd's OFREP port (8016) is not published by the flag plane: unreachable.
            "FLAGD_OFREP_URL": f"http://{UNREACHABLE}",
            "FLAG_SERVICE_URL": flag_service_url,
            # Only for an env-triggered shadow game (AGENT_SHADOW_GAME, off): still this stack.
            "GAME_COORDINATOR_ADDR": f"127.0.0.1:{coordinator_port}",
            "MOCK_CONTROLLER_ADDR": f"127.0.0.1:{mock_control_port}",
            "AGENT_INFERENCE_BACKEND": "stub",
            "AGENT_EXPERIMENTS_ENABLED": "false",
            "DECISION_OPENAI_API_KEY": openai_api_key,
            "AGENT_LLM_MAX_REQUESTS_PER_MINUTE": LLM_MAX_REQUESTS_PER_MINUTE,
            "LOG_LEVEL": "debug",
            "OTEL_SERVICE_NAME": OTEL_SERVICE_NAME,
            "OTEL_SERVICE_NAMESPACE": "infrastructure",
            "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
            "OTEL_BSP_SCHEDULE_DELAY": BSP_SCHEDULE_DELAY_MS,
            "SERVICE_VERSION": "service-tier",
        }
        super().__init__(
            "agent",
            [str(build_agent())],
            env=env,
            log_path=workdir / "logs" / "agent.log",
            ready=lambda: _http_ok(f"http://127.0.0.1:{self.health_port}/healthz"),
            cwd=AGENT_SRC,
        )
