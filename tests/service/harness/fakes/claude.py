"""FakeClaude: a scriptable Anthropic Messages API server, one per stack (#1422 S10, TC-010).

The agent's ``claude`` decide backend (``decision.claude.base_url``, #1900, TC-008) and
its ``claude`` text backend (retro and proposer, ``services/agent/inference``)
post to ``<base>/v1/messages`` (the SDK appends the path; the decide base URL has no
``/v1``). ``AGENT_INFERENCE_BASE_URL`` points the text backend here; the ``fake``
variant of ``decision.claude.base_url`` points the decide backend. The fake classifies
each call by its system prompt, answers with a canned text (a real Messages API
``message`` object) and records what it was sent::

    stack.fake_claude.script(retro='{"session_assessment": "..."}', proposal='{"flag": ...}')
    calls = stack.fake_claude.requests("proposal")
    stack.fake_claude.script(decide_mode="valid", intervention="adjust_volume", confidence=0.95)
    calls = stack.fake_claude.requests("decide")

A ``decide`` answer is BUILT FROM THE REQUEST (the shared builder of the fake OpenAI
server), so every choice is an option the agent offered; ``decide_mode="garbage"``
answers 200 with non-JSON text.

An in-process threaded server (stdlib only, instance state): nothing is shared
between stacks. The key check mirrors the real API (401 without the expected
``x-api-key``), so a test proves the agent sends its key.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.integration.fake_openai.server import build_answer, parse_questions

from ..processes import free_port

# Dummy key the agent sends (ANTHROPIC_API_KEY) and the fake checks; never a real key.
API_KEY = "fake-claude-service-tier-key"
MODEL = "fake-claude"
# The proposer's system prompt (services/agent/experiment/proposer.go buildPrompt).
PROPOSAL_MARKER = "propose EXACTLY ONE experiment"
# The decide system prompt (services/agent/decide/llmio/llmio.go systemPrompt).
DECIDE_MARKER = "in-game decision engine of JoustMania"


class FakeClaude:
    def __init__(self):
        self.port = free_port()
        self._lock = threading.Lock()
        self._replies: dict[str, str] = {"retro": "{}", "proposal": "{}"}
        self._decide: dict = {"mode": "valid", "intervention": "adjust_volume", "confidence": 0.95, "magnitude": 0.5}
        self._requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- addresses
    @property
    def base_url(self) -> str:
        """What the agent dials (``AGENT_INFERENCE_BASE_URL``)."""
        return f"http://127.0.0.1:{self.port}"

    # -------------------------------------------------------------- lifecycle
    def start(self) -> int:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ARG002
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                status, payload = fake._answer(self.path, self.headers.get("x-api-key", ""), body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-claude", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def log_tail(self) -> str:
        with self._lock:
            return f"fake-claude: {len(self._requests)} request(s): " + ", ".join(
                f"{r['kind']}(key_ok={r['key_ok']})" for r in self._requests
            )

    # ---------------------------------------------------------------- control
    def script(
        self,
        *,
        retro: str | None = None,
        proposal: str | None = None,
        decide_mode: str | None = None,
        intervention: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Set the reply text per call kind (and the decide answer) and clear the request log."""
        with self._lock:
            if decide_mode is not None:
                if decide_mode not in ("valid", "garbage"):
                    raise ValueError(f"decide_mode must be valid or garbage, not {decide_mode!r}")
                self._decide["mode"] = decide_mode
            if intervention is not None:
                self._decide["intervention"] = intervention
            if confidence is not None:
                self._decide["confidence"] = confidence
            if retro is not None:
                self._replies["retro"] = retro
            if proposal is not None:
                self._replies["proposal"] = proposal
            self._requests.clear()

    def requests(self, kind: str | None = None) -> list[dict]:
        """The calls so far (``kind`` = ``retro``, ``proposal`` or ``decide``), oldest first."""
        with self._lock:
            return [dict(r) for r in self._requests if kind is None or r["kind"] == kind]

    # ----------------------------------------------------------------- server
    def _answer(self, path: str, key: str, body: dict) -> tuple[int, dict]:
        if not path.startswith("/v1/messages"):
            return 404, _error("not_found_error", f"no route {path}")
        system = "".join(b.get("text", "") for b in body.get("system") or [])
        user = "".join(
            b.get("text", "")
            for m in body.get("messages") or []
            for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        )
        kind = "decide" if DECIDE_MARKER in system else ("proposal" if PROPOSAL_MARKER in system else "retro")
        key_ok = key == API_KEY
        with self._lock:
            self._requests.append(
                {"kind": kind, "key_ok": key_ok, "model": body.get("model"), "system": system, "user": user}
            )
            text = self._decide_text(user) if kind == "decide" else self._replies[kind]
        if not key_ok:
            return 401, _error("authentication_error", "invalid x-api-key")
        return 200, {
            "id": f"msg_fake_{kind}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model") or MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 40},
        }

    def _decide_text(self, user: str) -> str:
        """The decide answer text; the caller holds the lock."""
        if self._decide["mode"] == "garbage":
            return "this is not the JSON you are looking for {"
        answer, chosen = build_answer(parse_questions(user), self._decide)
        self._requests[-1]["intervention"] = chosen
        return json.dumps(answer)


def _error(kind: str, message: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": message}}
