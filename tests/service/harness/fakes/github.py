"""FakeGitHub: a recording GitHub REST API stub, one per stack (#1404, TC-011 promotion).

The agent's promoter (``services/agent/promote/github.go``) builds its real GitHub
client only when ``AGENT_CODE_IMPROVEMENT_ENABLED=true``, ``GITHUB_TOKEN`` and
``GITHUB_REPOSITORY`` are set and the env target is ``github``; ``GITHUB_API_URL``
points it here, so a promotion in ``issue`` or ``pr`` mode reaches this stub and never
github.com. The stub answers ``POST /repos/<owner>/<repo>/issues`` and ``/pulls`` with
a ``201`` and an ``html_url``, and records every request::

    issues = stack.fake_github.requests("issues")
    assert issues[0]["body"]["title"] == "Flag experiment: windows"

An in-process threaded server (stdlib only, instance state). The token check
mirrors the real API (401 without ``Bearer <TOKEN>``), so a test proves the agent
sends it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..processes import free_port

# Dummy credential and repository the agent is given; never real ones.
TOKEN = "fake-github-service-tier-token"
REPOSITORY = "joustmania-service-tier/fake-repo"


class FakeGitHub:
    def __init__(self):
        self.port = free_port()
        self._lock = threading.Lock()
        self._requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- addresses
    @property
    def base_url(self) -> str:
        """What the agent dials (``GITHUB_API_URL``)."""
        return f"http://127.0.0.1:{self.port}"

    def html_url(self, kind: str, number: int) -> str:
        return f"{self.base_url}/{REPOSITORY}/{kind}/{number}"

    def agent_env(self) -> dict[str, str]:
        """The promoter env gate of services/agent/promote/github.go, pointed at this stub."""
        return {
            "AGENT_CODE_IMPROVEMENT_ENABLED": "true",
            "AGENT_CODE_IMPROVEMENT_TARGET": "github",
            "GITHUB_TOKEN": TOKEN,
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_API_URL": self.base_url,
        }

    # -------------------------------------------------------------- lifecycle
    def start(self) -> int:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ARG002
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
                status, payload = fake._answer(self.path, self.headers.get("Authorization", ""), json.loads(raw))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-github", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def log_tail(self) -> str:
        with self._lock:
            return f"fake-github: {len(self._requests)} request(s): " + ", ".join(
                f"{r['kind']}(token_ok={r['token_ok']})" for r in self._requests
            )

    # ---------------------------------------------------------------- control
    def requests(self, kind: str | None = None) -> list[dict]:
        """The calls so far (``kind`` = ``issues`` or ``pulls``), oldest first."""
        with self._lock:
            return [dict(r) for r in self._requests if kind is None or r["kind"] == kind]

    # ----------------------------------------------------------------- server
    def _answer(self, path: str, authorization: str, body: dict) -> tuple[int, dict]:
        prefix = f"/repos/{REPOSITORY}/"
        kind = path[len(prefix) :] if path.startswith(prefix) else ""
        if kind not in ("issues", "pulls"):
            return 404, {"message": "Not Found", "path": path}
        token_ok = authorization == f"Bearer {TOKEN}"
        with self._lock:
            self._requests.append({"kind": kind, "token_ok": token_ok, "body": body})
            number = len(self._requests)
        if not token_ok:
            return 401, {"message": "Bad credentials"}
        return 201, {"number": number, "html_url": self.html_url(kind, number)}
