"""FakeOpenAI: the scriptable OpenAI-compatible decide backend, one per stack.

It runs ``tests/integration/fake_openai/server.py`` (stdlib only, the same fake
the decide e2e module ran as a container) as a subprocess on an ephemeral
loopback port. A subprocess, not an in-process server: the fake keeps its script
and request log in module globals, so a module stack and a ``fresh_stack`` that
are alive at the same time would otherwise share one script.

The agent reaches it through the ``fake`` variant of
``decision.openai.base_url``: the stack rewrites that variant's URL in its
temporary copy of ``services/flagd/ci`` (the registry allows declared variants
only, no free string). A test scripts each answer through the control API::

    stack.fake_openai.script("valid", intervention="adjust_volume", confidence=0.95)
    calls = stack.fake_openai.requests(game_id)
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ..processes import REPO_ROOT, ManagedProcess, base_env, free_port

SERVER = REPO_ROOT / "tests" / "integration" / "fake_openai" / "server.py"
# Dummy key the agent sends (DECISION_OPENAI_API_KEY) and the fake checks; never a real key.
API_KEY = "fake-service-tier-key"
CONTROL_TIMEOUT_SECONDS = 10.0


class FakeOpenAI:
    def __init__(self, workdir: Path):
        self.port = free_port()
        self.process = ManagedProcess(
            "fake-openai",
            [sys.executable, "-u", str(SERVER)],
            env={
                **base_env(),
                "FAKE_OPENAI_HOST": "127.0.0.1",
                "FAKE_OPENAI_PORT": str(self.port),
                "FAKE_OPENAI_API_KEY": API_KEY,
            },
            log_path=workdir / "logs" / "fake-openai.log",
            ready=self._healthy,
        )

    # -------------------------------------------------------------- addresses
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        """What the agent dials (the ``fake`` variant of ``decision.openai.base_url``)."""
        return f"{self.url}/v1"

    # -------------------------------------------------------------- lifecycle
    def start(self) -> int:
        self.process.start()
        return self.port

    def stop(self) -> None:
        self.process.stop()

    def log_tail(self) -> str:
        return self.process.log_tail()

    def _healthy(self) -> bool:
        try:
            self._call("GET", "/healthz", timeout=1.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ---------------------------------------------------------------- control
    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = CONTROL_TIMEOUT_SECONDS):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def script(self, mode: str = "valid", **kwargs) -> None:
        """Set the answer script (and clear the request log); see server.py for the keys."""
        self._call("POST", "/_control/script", {"mode": mode, **kwargs})

    def release(self) -> None:
        """Release every held request."""
        self._call("POST", "/_control/release", {})

    def all_requests(self) -> list[dict]:
        return self._call("GET", "/_control/requests")["requests"]

    def requests(self, game_id: str) -> list[dict]:
        """The chat calls for ``game_id`` (parsed from the rendered prompt)."""
        return [r for r in self.all_requests() if r["game_id"] == game_id]

    def parse_failures(self) -> list[dict]:
        return [r for r in self.all_requests() if r.get("parse_failed")]
