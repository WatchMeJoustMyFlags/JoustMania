"""FakeJev: a scriptable TypeSafe System One server (``POST /v1/systemone``), one per stack (#1404, TC-008).

The agent's ``jev`` decide backend (``services/agent/decide/backends/jev``, wire
format in ``wire.go`` and ``docs/specs/decide-contract.md`` section 2) posts a typed
request ``{model, state, questions: {id: {type, instructions, criteria}}}`` with
``Authorization: Bearer <JEV_API_KEY>`` and expects ``{model, answers: {id: {...}}}``.
The agent is pointed here through the ``fake`` variant of ``decision.jev.base_url``
(the stack rewrites that variant's URL in its copy of ``services/flagd/ci``).

The answers are BUILT FROM THE REQUEST, so every choice is an option the agent
offered and every score level exists in the ladder it sent::

    stack.fake_jev.script("valid", intervention="adjust_volume", confidence=0.95)
    calls = stack.fake_jev.requests()

``confidence`` is the gate noul's p_yes (the acting confidence of the round, decide-contract
section 3.1) and every other answer's native confidence. ``mode="garbage"`` answers a
well-formed envelope with no usable answers.

An in-process threaded server (stdlib only, instance state): nothing is shared between
stacks. The key check mirrors the real API (401 without the expected bearer token), so a
test proves the agent sends its key.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..processes import free_port

# Dummy key the agent sends (JEV_API_KEY) and the fake checks; never a real key.
API_KEY = "fake-jev-service-tier-key"
GATE_ID = "jev.gate"  # the internal gate noul (stages.go GateQuestionID)
INTERVENTION_ID = "ffa.intervention"
MAGNITUDE_ID = "ffa.magnitude"
TARGET_NONE = "none"


class FakeJev:
    def __init__(self):
        self.port = free_port()
        self._lock = threading.Lock()
        self._script: dict = {"mode": "valid", "intervention": "adjust_volume", "confidence": 0.95, "magnitude": 0.5}
        self._requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- addresses
    @property
    def base_url(self) -> str:
        """What the agent dials (the ``fake`` variant of ``decision.jev.base_url``)."""
        return f"http://127.0.0.1:{self.port}/v1"

    # -------------------------------------------------------------- lifecycle
    def start(self) -> int:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ARG002
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    body = {}
                bearer = self.headers.get("Authorization", "")
                status, payload = fake._answer(self.path, bearer, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Typesafe-Request-Id", "req_fake_jev")
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-jev", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def log_tail(self) -> str:
        with self._lock:
            return f"fake-jev: {len(self._requests)} request(s): " + ", ".join(
                f"{r['stage']}(key_ok={r['key_ok']})" for r in self._requests
            )

    # ---------------------------------------------------------------- control
    def script(
        self,
        mode: str = "valid",
        *,
        intervention: str = "adjust_volume",
        confidence: float = 0.95,
        magnitude: float = 0.5,
    ) -> None:
        """Set the answer script (``valid`` or ``garbage``) and clear the request log."""
        with self._lock:
            self._script = {
                "mode": mode,
                "intervention": intervention,
                "confidence": confidence,
                "magnitude": magnitude,
            }
            self._requests.clear()

    def requests(self, stage: str | None = None) -> list[dict]:
        """The calls so far, oldest first; ``stage`` = ``intervention``, ``details`` or ``single``."""
        with self._lock:
            return [dict(r) for r in self._requests if stage is None or r["stage"] == stage]

    # ----------------------------------------------------------------- server
    def _answer(self, path: str, bearer: str, body: dict) -> tuple[int, dict]:
        if path != "/v1/systemone":
            return 404, {"error": f"no route {path}"}
        questions = body.get("questions") or {}
        stage = "intervention" if GATE_ID in questions else "details" if MAGNITUDE_ID in questions else "single"
        key_ok = bearer == f"Bearer {API_KEY}"
        with self._lock:
            script = dict(self._script)
            entry = {
                "stage": stage,
                "key_ok": key_ok,
                "model": body.get("model"),
                "question_ids": list(questions),
                "options": {
                    qid: list(q["criteria"])
                    for qid, q in questions.items()
                    if isinstance(q.get("criteria"), dict) and q.get("type") == "choice"
                },
                "intervention": "",
            }
            self._requests.append(entry)
        if not key_ok:
            return 401, {"error": "invalid api key"}
        if script["mode"] == "garbage":
            return 200, {"model": "jev-fake", "answers": {}}
        answers = {qid: _answer_for(qid, q, script) for qid, q in questions.items()}
        chosen = (answers.get(INTERVENTION_ID) or {}).get("choice", "")
        with self._lock:
            entry["intervention"] = chosen
        return 200, {"model": "jev-fake", "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 20}}


def _answer_for(qid: str, question: dict, script: dict) -> dict:
    kind = question.get("type")
    confidence = float(script["confidence"])
    if kind == "noul":
        return {"type": "noul", "noul": confidence}
    if kind == "score":
        levels = len(question.get("criteria") or [])
        level = min(max(round(float(script["magnitude"]) * (levels - 1)), 0), levels - 1)
        probabilities = {str(i): (1.0 if i == level else 0.0) for i in range(levels)}
        return {"type": "score", "score": float(level), "probabilities": probabilities, "confidence": 0.9}
    options = list((question.get("criteria") or {}).keys())
    wanted = script["intervention"] if qid == INTERVENTION_ID else TARGET_NONE
    choice = wanted if wanted in options else options[0]
    rest = [o for o in options if o != choice]
    probabilities = {choice: 0.9 if rest else 1.0}
    probabilities.update({o: 0.1 / len(rest) for o in rest})
    return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": 0.9}
