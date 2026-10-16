# jev decision backend (stub)

`decision.backend=jev`: TypeSafe's hosted Jev API, called directly by the agent
(epic #1225, issue #1229).

**Status: stub.** There is no API key and no API documentation yet.
`Available()` is false until both `JEV_API_KEY` (env) and
`decision.jev.base_url` (flag) are set. The wire format in `wire.go` is an
**assumed OpenAI-compatible shape** that exists only so the backend, its
telemetry and the contract test work end to end. It is probably wrong in detail.

## Layout

| file | what | changes when the real API arrives |
|---|---|---|
| `wire.go` | ALL wire-format knowledge: path, auth, request/response structs, mapping to and from `decide.Request` / `decide.Response` (`TODO(jev-api)` markers) | **rewrite** |
| `testdata/*.json` | hand-written fixtures (each has a `_fixture` marker saying so) | **replace** with recorded Jev traffic |
| `contract_test.go` | replays the fixtures through `wire.go` and through `Decide` | update the expectations |
| `jev.go` | Config, `Available`, timeout, HTTP call, GenAI span and metrics | nothing |

## What we need from Jev

1. **Endpoint**: base URL(s) per region/environment and the path of the
   decision/inference call. Is there a health or warm-up endpoint?
2. **Auth scheme**: Bearer token, an API-key header, or signed requests? Key
   rotation and per-key rate limits.
3. **Request schema**: does Jev take a prompt (system + user text, like an
   OpenAI-compatible chat API) or typed input (state + typed questions with
   options/ranges)? Model selection (is `decision.jev.model` meaningful)?
   Structured-output support (JSON schema? strict?), max output length,
   temperature/determinism knobs.
4. **Response schema**: where the answer lives, finish/stop reasons, error
   envelope and status codes (429/5xx semantics, retry-after), response id,
   token usage (input/output/reasoning), any reasoning or explanation text.
5. **Probabilities and confidence**: can Jev return a probability per option
   (choice questions) and a calibrated confidence (0..1) per answer natively,
   or do we keep asking for them in the structured answer as with the LLM
   backends? Are probabilities calibrated?
6. **Latency SLA**: p50/p95/p99 for a ~1.5k-token request with a ~300-token
   answer, cold-start behaviour, and region. The in-game budget is
   `decision.jev.timeout_ms` (default 2500ms); anything slower falls back to
   rules every time.
7. **Telemetry**: does Jev accept W3C `traceparent` (we send it) so its spans
   join our trace?
8. **Data handling**: retention of request content (the game narrative carries
   controller serials, no personal data).

## Stage fallback

- With `decision.backend=jev` while Jev is unavailable (no key, no base URL) the
  decision wiring falls back to `rules` with `fallback_reason=unavailable`.
  If Jev is configured but unreachable, slow or returns an invalid answer, the
  result also comes from `rules` (fallback reasons timeout / error / invalid).
  The game never stalls; the `agent.decide` span shows why.
- To switch away live, flip the backend to `claude` with
  `scripts/agent-backend.sh` (#1232); flagd hot-reloads `agent.json`, no restart.

## Telemetry

One CLIENT span per call, `chat {decision.jev.model}` (just `chat` without a
model), using the OTel GenAI semantic conventions (v1.41.0) with the custom
`gen_ai.provider.name=jev`. Prompt and output content (with reasoning and the
rationale as `reasoning` parts) are recorded only with `decision.capture_content`.
