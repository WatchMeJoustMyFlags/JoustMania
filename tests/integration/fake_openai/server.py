"""Scriptable fake OpenAI-compatible decision backend for the decide e2e tests (#1230).

TEST-ONLY. Started per stack by the service tier (tests/service/harness/fakes/openai.py,
the AGENT composition, #1600); never by the production compose path. Stdlib only, so it also runs on a
stock python image with this file bind-mounted.

It serves ``POST /v1/chat/completions`` the way the agent's ``openai`` decision
backend (services/agent/decide/backends/openai) expects: a chat-completion
envelope whose ``choices[0].message.content`` is the structured answer JSON of
``llmio.AnswerSchema``. The answer is BUILT FROM THE REQUEST: the questions,
their options and ranges are parsed out of the rendered user prompt
(``llmio.Render``), so every choice is an option the agent actually offered and
every score lies inside the asked range. What the answer says is scripted per
test through the control API, so tests never restart the container:

    POST /_control/script   {"mode": "valid" | "garbage" | "hold",
                             "intervention": "adjust_global_difficulty",
                             "confidence": 0.95, "magnitude": 0.5,
                             "hold_max_seconds": 30}
        Replaces the script (unset keys take the defaults below) and clears
        the request log. ``valid`` answers immediately; ``garbage`` answers a
        well-formed chat envelope whose content is not JSON (a model emitting
        garbage); ``hold`` answers like ``valid`` but only after
        ``POST /_control/release`` (or ``hold_max_seconds``), so a test can
        flip a flag while the agent's call is provably in flight.
    POST /_control/release  releases every held request.
    GET  /_control/requests the request log: one entry per chat call with the
                            game id and question ids parsed from the prompt,
                            the mode it was answered with, the chosen
                            intervention, and whether it is still held.

A prompt this fake cannot parse (no question or no game id, i.e. llmio.Render
changed shape) is answered HTTP 500, logged as ``parse_failed`` and recorded
with ``"parse_failed": true``.

The request must carry ``Authorization: Bearer $FAKE_OPENAI_API_KEY``; anything
else is a 401, which proves the agent read DECISION_OPENAI_API_KEY from the
test-only compose overlay.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("FAKE_OPENAI_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("FAKE_OPENAI_PORT", "8080"))
EXPECTED_API_KEY = os.environ.get("FAKE_OPENAI_API_KEY", "")

DEFAULT_SCRIPT = {
    "mode": "valid",
    "intervention": "adjust_global_difficulty",
    "confidence": 0.95,
    "magnitude": 0.5,
    "hold_max_seconds": 30.0,
}
VALID_MODES = {"valid", "garbage", "hold"}

# Session-scoped interventions take target "none"; the e2e tests only script
# session-scoped ones (see decide_hook.go playerTargetedInterventions).
TARGET_NONE = "none"
OPTION_NOOP = "noop"

_lock = threading.Lock()
_script = dict(DEFAULT_SCRIPT)
_release = threading.Event()
_requests: list[dict] = []

# Rendered prompt shapes (services/agent/decide/llmio/llmio.go Render).
_GAME_ID_RE = re.compile(r"^game: id=(\S+)", re.MULTILINE)
_QUESTION_RE = re.compile(
    r"^- id: (\S+)\n  kind: (\w+)\n  (options|range): (\[.*\])$", re.MULTILINE
)


def parse_questions(user_prompt: str) -> list[dict]:
    """The questions of a rendered decide prompt, in order.

    Options are rendered with Go's strconv.Quote, which is valid JSON for the
    ASCII ids the agent uses; a range is ``[lo, hi]``.
    """
    questions = []
    for qid, kind, field, raw in _QUESTION_RE.findall(user_prompt):
        q = {"id": qid, "kind": kind}
        if field == "options":
            q["options"] = json.loads(raw)
        else:
            q["range"] = json.loads(raw)
        questions.append(q)
    return questions


def _choice_answer(
    qid: str, options: list[str], wanted: str, confidence: float
) -> dict:
    choice = wanted if wanted in options else options[0]
    probabilities = [{"option": choice, "p": 0.9}]
    others = [o for o in options if o != choice]
    if others:
        probabilities.append({"option": others[0], "p": 0.1})
    else:
        probabilities[0]["p"] = 1.0
    return {
        "id": qid,
        "choice": choice,
        "score": 0,
        "probabilities": probabilities,
        "confidence": confidence,
    }


def build_answer(questions: list[dict], script: dict) -> tuple[dict, str]:
    """The structured answer for ``questions`` under ``script``.

    Returns (answer, chosen intervention). If the scripted intervention is not
    among the offered options (not in interventions_allowed) the answer is noop,
    which the request log exposes so the test fails with a clear reason.
    """
    confidence = float(script["confidence"])
    answers = []
    chosen = ""
    for q in questions:
        if q["kind"] == "choice":
            if q["id"] == "ffa.intervention":
                wanted = script["intervention"]
            elif q["id"] == "ffa.target":
                wanted = TARGET_NONE
            else:
                wanted = q["options"][0]
            answer = _choice_answer(q["id"], q["options"], wanted, confidence)
            if q["id"] == "ffa.intervention":
                chosen = answer["choice"]
            answers.append(answer)
        else:
            lo, hi = q["range"]
            score = min(max(float(script["magnitude"]), lo), hi)
            answers.append(
                {
                    "id": q["id"],
                    "choice": "",
                    "score": score,
                    "probabilities": [],
                    "confidence": confidence,
                }
            )
    # Cross-question consistency: noop must target "none" (it already does).
    if chosen == "":
        chosen = OPTION_NOOP
    return {
        "answers": answers,
        "rationale": f"fake-openai scripted {chosen} at confidence {confidence}",
    }, chosen


def chat_completion(model: str, content: str) -> dict:
    return {
        "id": f"chatcmpl-fake-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - quieter, single-line logs
        print("fake-openai: " + fmt % args, flush=True)

    # -- helpers ---------------------------------------------------------
    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw or b"{}")

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - http.server naming
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
        elif self.path == "/_control/requests":
            with _lock:
                self._send_json(
                    200, {"script": dict(_script), "requests": list(_requests)}
                )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - http.server naming
        if self.path.rstrip("/").endswith("/chat/completions"):
            self._chat()
        elif self.path == "/_control/script":
            self._set_script()
        elif self.path == "/_control/release":
            _release.set()
            self._send_json(200, {"released": True})
        else:
            self._send_json(404, {"error": "not found"})

    def _set_script(self) -> None:
        global _release
        body = self._read_json()
        script = dict(DEFAULT_SCRIPT)
        script.update({k: v for k, v in body.items() if k in DEFAULT_SCRIPT})
        if script["mode"] not in VALID_MODES:
            self._send_json(
                400, {"error": f"mode must be one of {sorted(VALID_MODES)}"}
            )
            return
        with _lock:
            # Release anything still held by the previous script before swapping.
            _release.set()
            _release = threading.Event()
            _script.clear()
            _script.update(script)
            _requests.clear()
        self._send_json(200, {"script": script})

    def _chat(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {EXPECTED_API_KEY}":
            self._send_json(
                401,
                {
                    "error": {
                        "message": "invalid api key",
                        "type": "invalid_request_error",
                    }
                },
            )
            return
        body = self._read_json()
        messages = body.get("messages") or []
        user = next(
            (m.get("content", "") for m in messages if m.get("role") == "user"), ""
        )
        match = _GAME_ID_RE.search(user)
        game_id = match.group(1) if match else ""
        questions = parse_questions(user)

        if not questions or not game_id:
            # The rendered prompt no longer matches what this fake parses
            # (llmio.Render changed?). Fail loudly instead of answering junk.
            print(
                f"fake-openai: parse_failed game_id={game_id!r} questions={len(questions)}",
                flush=True,
            )
            with _lock:
                _requests.append(
                    {
                        "game_id": game_id,
                        "parse_failed": True,
                        "questions": [q["id"] for q in questions],
                        "at": time.time(),
                    }
                )
            self._send_json(
                500,
                {
                    "error": {
                        "message": "fake-openai: parse_failed",
                        "type": "server_error",
                    }
                },
            )
            return

        with _lock:
            script = dict(_script)
            release = _release
            entry = {
                "game_id": game_id,
                "mode": script["mode"],
                "model": body.get("model", ""),
                "response_format": (body.get("response_format") or {}).get("type", ""),
                "at": time.time(),
                "held": script["mode"] == "hold",
                "intervention": "",
                "questions": [q["id"] for q in questions],
            }
            _requests.append(entry)

        if script["mode"] == "garbage":
            content = "this is not the JSON you are looking for {"
        else:
            answer, chosen = build_answer(questions, script)
            with _lock:
                entry["intervention"] = chosen
            content = json.dumps(answer)

        if script["mode"] == "hold":
            release.wait(timeout=float(script["hold_max_seconds"]))
            with _lock:
                entry["held"] = False
                entry["released_at"] = time.time()

        try:
            self._send_json(200, chat_completion(body.get("model", ""), content))
        except BrokenPipeError, ConnectionResetError:
            # The agent gave up (its timeout); nothing to answer.
            with _lock:
                entry["client_gone"] = True


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    print(f"fake-openai listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
