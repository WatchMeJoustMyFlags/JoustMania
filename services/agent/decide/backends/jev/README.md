# jev decision backend

`decision.backend=jev`: TypeSafe's hosted Jev model through its System One API
(<https://docs.typesafe.ai/api>), called directly by the agent (epic #1225,
issue #1229). There is no Go SDK (TypeSafe ships Python and JS SDKs), so the
client is a plain HTTP POST through `internal/chathttp`.

`Available()` needs only `JEV_API_KEY`. The base URL and model default to
`https://api.typesafe.ai/v1` and `jev-latest`, so `scripts/agent-backend.sh jev`
works with just the key set.

## Wire format

```
POST {decision.jev.base_url}/systemone
Authorization: Bearer $JEV_API_KEY
{"model": "jev-latest", "state": {...}, "questions": {"<id>": {"type", "instructions", "criteria"}}}
-> {"model": "jev-1.13.0", "answers": {"<id>": {...}}, "usage": {"input_tokens", "output_tokens"}}
```

The request id comes back only as the `x-typesafe-request-id` header; it becomes
`gen_ai.response.id`.

| our question (`decide`) | Jev primitive | criteria we send | answer mapping |
|---|---|---|---|
| `choice` | `choice` | ordered `{option: description}` (`criteria.go`): lever effects for `ffa.intervention`, one sentence per player for `ffa.target` (status, signals and their rank among the alive players, computed in code), `null` otherwise | `choice`, `probabilities` (rescaled if the sum is within 0.05 of 1) |
| `score` | `score` | a 5-level ladder, lowest to highest setting; for `ffa.magnitude` each level lists the value it resolves to per allowed numeric lever, and the end levels say what that value does | Jev's `score` s in [0, 4] maps to `lo + s/4 * (hi - lo)` of `Question.Range` |
| `noul` (new kind, #1229) | `noul` | none | `Answer.Noul` = P(yes) |

`state` is `decide.State` as a JSON object: a static game description, game
kind, elapsed seconds, the alive serials, the players and the narrative. Never
`Request.Context`, and not the digest or game id. Non-finite readings are
dropped (Jev rejects them with a 400).

Choice options keep `Question.Options` order on the wire. The docs say Jev 1.13
leans toward the first option, and our first options are `noop` and `none`, the
conservative ones.

### Confidence

The `decide` contract says confidence is how sure the backend is that acting on
the answer is right. Jev's `confidence` measures how peaked the distribution
is. For choices it is `(p_max - 1/n) / (1 - 1/n)`.

- `ffa.intervention`: the same acting confidence as the rules adapter, computed
  from Jev's calibrated probabilities. It is `1 - p(noop)` when the choice is an
  intervention and `p(noop)` when it is noop. Jev's peakedness would penalize
  two strong interventions that together outweigh noop, which the contract
  calls a confident case for acting. With about 15 options it also roughly
  collapses to `p_max`.
- `ffa.target`, `ffa.magnitude`, other choice and score questions: Jev's native
  confidence.
- noul: `|2p - 1|`. Jev returns no confidence for a noul, and this is the formula
  its docs give (the choice formula for two outcomes).

Jev's native value for every choice and score is on the chat span as
`jev.native_confidence`, for example
`{"ffa.intervention":0.75,"ffa.magnitude":0.13,"ffa.target":0.17}`.

### Errors

| outcome | error.type | decision fallback |
|---|---|---|
| 400 / 401 / 413 / 415 / 422 / 429 / 529 / 5xx | the status code (`max_tokens_exceeded` for a 400 over the context length) | `error` |
| transport timeout / cancel | `timeout` / `canceled` | `timeout` / `error` |
| unparseable body, no answers, a missing or wrong-typed answer, out-of-range numbers | `invalid_response` / `no_answers` / `invalid_output` (wraps `decide.ErrInvalid`) | `invalid_answer` |

Error bodies follow the FastAPI shape: `{"detail": {"error_type", "message"}}`
for 401, and `{"detail": [{"type", "loc", "msg", "input"}]}` for 422 (see
`testdata/response_401.json` and `response_422.json`). Jev has no refusal or
finish reason. We do not retry 429/529: the in-game budget is one call.

## Layout

| file | what |
|---|---|
| `wire.go` | endpoint, auth, request/response structs, the mapping to and from `decide.Request` / `decide.Response`, confidence |
| `criteria.go` | the natural-language option and level descriptions and the state's game description |
| `jev.go` | Config and defaults, `Available`, timeout, the HTTP call, GenAI span and metrics |
| `testdata/*.json` | **recorded** Jev traffic (jev-1.13.0, 2026-10-16) for `testRequest()` / `noulRequest()` |
| `contract_test.go` | replays the fixtures (and mutations of them) through `wire.go` and `Decide` |
| `live_test.go` | `-tags live`: real calls, latency/tokens log, `JEV_RECORD_FIXTURES=1` re-records `testdata/` |

```sh
JEV_API_KEY=... go test -tags live ./decide/backends/jev -run TestLive -v
JEV_API_KEY=... JEV_RECORD_FIXTURES=1 go test -tags live ./decide/backends/jev -run TestLive -v  # re-record
```

`TestContract_Request` fails when the encoding changes (criteria wording, state
shape) until you re-record. The recorder refuses to write a body that contains
the key, and the key only ever travels in the `Authorization` header.

## Measured (2026-10-16, from WSL in Vienna, jev-latest = jev-1.13.0)

| call | latency | input / output tokens |
|---|---|---|
| FFA, 3 questions, 13 interventions, cold (DNS + TLS) | 315 ms | 2184 / 225 |
| same, warm | 240 ms | 2184 / 225 |
| 2 nouls, warm | 237 ms | 882 / 40 |

That is about 10x faster than Claude (Haiku about 3.4 s, Sonnet about 3.0 s
warm), and well inside the 2.5 s `decision.jev.timeout_ms` budget. Jev bills
input tokens only.

## Still unknown

- **Latency SLA** (p50/p95/p99, regions): not documented. The numbers above
  are three samples.
- **W3C `traceparent`**: we send it (`chathttp`), but the docs do not say
  whether Jev joins our trace. Treat it as ignored.
- **Retention**: no zero data retention unless on an enterprise plan (see
  docs/legal). The state carries controller serials only, no personal data.
- **Rate limits**: per organization, in the TypeSafe console. 429 responses can
  carry `retry-after`.

## Caveats from the docs (jev-1.13 jaggedness)

- Questions are evaluated **independently**. `ffa.target` and
  `ffa.magnitude` cannot be conditioned on the `ffa.intervention` answer in
  the same call. The magnitude ladder therefore lists every allowed lever.
- Jev is weak at **numeric comparison and interpolation**. That is why player
  ranks are computed in code. The docs also warn against reading a Score's
  fractional value as an exact magnitude between levels, and that is exactly
  what `ffa.magnitude` does. Its confidence came back low (about 0.13) in the
  recorded call.
- **Option order bias** toward the first option: see above.

## Stage fallback

- With `decision.backend=jev` and no `JEV_API_KEY`, the decision wiring falls
  back to `rules` with `fallback_reason=backend_unavailable`. If Jev is configured but
  unreachable, slow, or returns an invalid answer, the result also comes from
  `rules`, with fallback reason `timeout`, `error` or `invalid_answer`. The game
  never stalls, and the `agent.decide` span shows why.
- To switch away live, flip the backend with `scripts/agent-backend.sh`.
  `scripts/agent-backend.sh jev pinned` pins `jev-1.13.0`.

## Telemetry

One CLIENT span per call, `chat {decision.jev.model}`, using the OTel GenAI
semantic conventions (v1.41.0) with the custom `gen_ai.provider.name=jev`.
The span also carries `gen_ai.response.model` (the versioned id), the usage
tokens and `jev.native_confidence`. With `decision.capture_content` it also
records the request body as `gen_ai.input.messages` and the answers JSON as
`gen_ai.output.messages`. Jev has no system prompt.
