# Agent Trace Visualization for Stage / Demo Visibility

The agent's trace **is** the audit log. During the talk it stays on screen so the
audience can watch the agent observe, decide, and act in real time. This guide
makes that trace legible from a projector: it explains the decision-span
hierarchy, ships pre-built Jaeger deep links for each demo act, and lists
stage-display tuning tips.

> Scope: this is a **docs + saved-query** guide. It does not change any span or
> attribute names — span/attribute emission lives in the Go agent and is owned by
> in-flight work. Where an attribute reads poorly on a projector it is noted as a
> follow-up, not changed here.

**Cross-references (read alongside, not duplicated here):**

- [agent-act-runbook.md](agent-act-runbook.md) — driving the agent live, the
  panic-stop flag, and the per-signal table of where to look.
- [observability-quickstart.md](observability-quickstart.md) — dashboards and the
  live performance tooling.
- [services/agent/README.md → Span schema (#724)](../services/agent/README.md#span-schema-724--the-trace-is-the-audit-log)
  — the authoritative span/attribute schema this guide visualizes.
- The #745 demo runbook (`docs/agent-demo-runbook.md`, authored separately) — the
  act-by-act stage script; this guide supplies the Jaeger views it points at.

**Jaeger base URL:** `http://localhost/jaeger/` (behind Envoy). The agent-act
runbook references the `:8080` form (`http://localhost:8080/jaeger/`); both reach
the same UI on this stack. Use whichever the stage laptop has bookmarked.

---

## 1. The decision-audit hierarchy (what the audience is looking at)

Every triggering OTLP Export from a watched game produces **one trace** with a
three-span spine. Verified live (trace `4f0a8448…`):

```
agent.signal_received          ROOT  — "a signal arrived, the agent woke up"
  └─ agent.decision            child — "the agent thought; here is the verdict"
       └─ agent.action         child — "the agent did (or was blocked from doing) it"
```

Read top-to-bottom on the projector it is a sentence: **signal → decision →
action**. A blocked decision still emits `signal_received → decision`; the
`action` span records the block reason rather than a real effect.

`agent.span_received` is the **trace-triggered twin** of `agent.signal_received`
(same shape, fired when the trigger was an OTLP *trace* Export instead of a
*metrics* Export). Treat the two as the same root for demo purposes.

### Where the load-bearing attributes live

All on the **`agent.decision`** span (this is the one to expand on stage — a
single span answers "which flags were in effect, which backend ran, what it
chose and why"). Verified keys/values from live span data:

| Question on stage | Attribute(s) on `agent.decision` | Example value |
|---|---|---|
| What did it decide? | `decision.action`, `decision.reason` | `grant_shield` / `shield the weakest player (skill 0.62) while the field shrinks` |
| Was it allowed to act? | `decision.blocked`, `decision.block_reason` | `true` / `not_allowed` |
| Which goal was it serving? | `decision.objective_served` | `balanced` |
| Which backend actually ran? | `inference.used`, `inference.configured`, `inference.fallback_reason` | `rules` / `phi4-mini` / `llm_not_eligible` |
| What were the flags in effect? | `agent.enabled`, `agent.mode`, `agent.model`, `agent.prompt_variant`, `agent.objectives`, `interventions.allowed` | `true` / `llm` / `phi4-mini` / `conservative` / `accelerate=0.1,balanced=0.7,…` / `none` |
| Did the move pass fitness? | `fitness.evaluated` (JSON array of `metric=value`) | `["balanced.skill_gap=0.318…","balanced.spike_survival_ratio=1",…]` |
| **Which game is this?** (#1095 join) | `game.id` (alias `session.id`), `game.kind` | `game_8c812a3d9063` / `shadow` |

**Inference attribution** (`inference.configured` / `inference.used` /
`inference.fallback_reason`) is the "is the LLM actually driving this?" trio. On
the current stack live decisions resolve to `inference.used=rules` with
`fallback_reason=llm_not_eligible` — the LLM path is exercised on the
**`agent.llm.retro`** span instead (post-game analyst), which carries the
`gen_ai.*` attributes: `gen_ai.request.model=phi4-mini`,
`gen_ai.operation.name=chat`, `gen_ai.output.type=json`, plus
`inference.used`/`inference.fallback_reason`. See act (c) below.

**`game.id` is the universal join key (#1095):** it appears on *every* agent
span — `signal_received`, `decision`, `action`, `llm.retro`, and
`agent.game.summary`. Filtering one trace view by `game.id` pulls the entire
agent storyline for a single game (act (d)).

---

## 2. Pre-built Jaeger deep links / saved queries per demo act

Each act below gives a click-ready URL **and** the manual field values (service /
operation / tags) in case the bookmark is stale or you need to retype on stage.
Jaeger's UI accepts `tags` as a URL-encoded JSON object; the same JSON works
verbatim in the API. Each query was run against the live stack on this branch —
the result counts below are real (`lookback` was `2d`; widen it if the stack has
been idle).

> Tip: bookmark these with the **stage laptop's** Jaeger base. Keep `lookback`
> generous (`1h`–`2d`) so a quiet stretch doesn't show an empty list mid-talk.

### (a) A game decision trace — the headline view

Land on the `agent.decision` search, newest first, then click the top trace to
show the three-span spine.

```
http://localhost/jaeger/search?service=agent&operation=agent.decision&lookback=1h&limit=20
```

Manual: **Service** `agent` · **Operation** `agent.decision` · Lookback `1h`.
**Validated live:** returns traces; each expands to
`signal_received → decision → action`.

### (b) Infrastructure remediation — `agent.infrastructure.decision`

```
http://localhost/jaeger/search?service=agent&operation=agent.infrastructure.decision&lookback=6h&limit=20
```

Manual: **Service** `agent` · **Operation** `agent.infrastructure.decision`.
**NOT yet populated on this stack** (live query returned 0 results, and the op
does not currently appear in `/api/operations?service=agent`). This is the
infrastructure-agent decision path; it needs a **live infra-agent run** during
the demo (or a dry-run that exercises remediation) before the operation shows up
in the dropdown. Until then, the operation field will be empty — keep this as the
"run it live" act rather than a pre-canned trace. *(See the noted follow-up in
§4.)*

### (c) Inference attribution — is the LLM actually driving?

The live LLM path is the post-game analyst span, which carries `gen_ai.*`:

```
http://localhost/jaeger/search?service=agent&operation=agent.llm.retro&lookback=2d&limit=20&tags=%7B%22gen_ai.request.model%22%3A%22phi4-mini%22%7D
```

Manual: **Service** `agent` · **Operation** `agent.llm.retro` · **Tags**
`{"gen_ai.request.model":"phi4-mini"}`. **Validated live: 10 results.**

To show the **decision-time** attribution trio instead (which backend a live
decision used), filter decisions by `inference.configured`:

```
http://localhost/jaeger/search?service=agent&operation=agent.decision&lookback=2d&limit=20&tags=%7B%22inference.configured%22%3A%22phi4-mini%22%7D
```

Manual tags: `{"inference.configured":"phi4-mini"}`. **Validated live: 10
results.** Expand a span and read `inference.used` / `inference.fallback_reason`
to narrate "configured for phi4-mini, fell back to rules because
`llm_not_eligible`." To find any decision that *did* run the model, filter
`{"inference.used":"llm"}` — **0 on the current stack** (all live decisions
resolve to `rules`), so this needs a live run with the LLM path eligible.

### (d) All activity for one game — the #1095 join

The single most useful "drill into this game" view. Substitute the real id
(every agent span shows `game.id`, e.g. on the trace you opened in act (a)):

```
http://localhost/jaeger/search?service=agent&lookback=2d&limit=50&tags=%7B%22game.id%22%3A%22game_8c812a3d9063%22%7D
```

Manual: **Service** `agent` · leave **Operation** as *all* · **Tags**
`{"game.id":"game_<id>"}`. **Validated live: 4 traces** for that id (the
signal/decision/action spine plus the game summary). This pulls the whole agent
storyline — signals, decisions, actions, the retro, and `agent.game.summary` —
for one game.

### (e) Blocked / discarded decisions

Show the audience the agent being *prevented* from acting (the safety story):

```
http://localhost/jaeger/search?service=agent&operation=agent.decision&lookback=2d&limit=20&tags=%7B%22decision.blocked%22%3A%22true%22%7D
```

Manual tags: `{"decision.blocked":"true"}`. **Validated live: 20 results.**
Expand to read `decision.block_reason` (e.g. `not_allowed`). Pair with the
panic-stop flag from the agent-act runbook to demonstrate "flip the flag → next
decision is blocked."

### Bonus — what action was taken (`agent.action`)

```
http://localhost/jaeger/search?service=agent&operation=agent.action&lookback=2d&limit=20&tags=%7B%22decision.action%22%3A%22grant_shield%22%7D
```

Manual tags: `{"decision.action":"grant_shield"}`. **Validated live: 10
results.** Swap the action value (`grant_shield`, etc.) to spotlight a specific
remediation.

### Bonus — interventions that actually *dispatched* (post-#1127)

Once `interventions_allowed` is set to a permitting variant (the demo uses
`shadow_experimental` — see
[demo runbook → Act 2b](agent-demo-runbook.md#act-2b--interventions-applying-not-just-blocked)),
decisions stop blocking `not_allowed` and **dispatch**. The dispatched ones are
the `agent.action` spans **without** `decision.blocked=true`. Filter for the
applied side directly:

```
http://localhost/jaeger/search?service=agent&operation=agent.action&lookback=2d&limit=20&tags=%7B%22decision.blocked%22%3A%22false%22%7D
```

Manual: **Operation** `agent.action` · **Tags** `{"decision.blocked":"false"}`.
Pair with the per-game join — add `"game.id":"game_<id>"` to the tag object — to
show every intervention the agent *applied* to one game (the storyline from
[act (d)](#d-all-activity-for-one-game--the-1095-join)). Until #1127 this filter
returned nothing, because the allow-list resolved to `none` and every decision
blocked `not_allowed`; it now returns the live dispatches. Contrast with
[act (e)](#e-blocked--discarded-decisions) (`decision.blocked":"true"`) to show
applied vs blocked side by side.

**The matching metrics** (Grafana **Agent Operations — Fleet**,
[`agent-operations.json`](../services/grafana/dashboards/agent-operations.json),
or `http://localhost/prometheus/`):

- `sum(rate(agent_decisions_total{blocked="false"}[5m]))` — applied decisions/sec
  (the `blocked="true"` complement is split by `block_reason`).
- `sum by (action) (rate(agent_interventions_applied_total[5m]))` — interventions
  that passed **every** gate and dispatched through the sink, split by action.
  *(`agent_interventions_applied_total` counts only true dispatches;
  `agent_decisions_total{blocked="false"}` counts permitted decisions — they track
  together once the ACT sink is enabled.)*

### Bonus — code-improvement loop

```
http://localhost/jaeger/search?service=agent&operation=code_improvement.proposed&lookback=14d&limit=20
```

Manual: **Operation** `code_improvement.proposed` (also
`code_improvement.propose_attempt`, `code_improvement.promote`). **Validated
live: 5 results** for `code_improvement.proposed`.

---

## 3. Stage-display tuning tips

Practical settings for a projector at the back of a room. None of these touch
code — they are browser/OS/Jaeger-UI knobs to set up before the talk.

- **Browser zoom 150–175 %** (`Ctrl/Cmd +`) on the Jaeger tab. The trace
  timeline and the span-detail key/value table both scale cleanly; the audience
  needs to read `decision.reason` from a distance.
- **Pin the trace-detail, not the search list.** The legible view is a single
  open trace with the `agent.decision` span **expanded** so its tags table shows.
  Bookmark act (a), click the top result, expand `agent.decision`, and present
  from there.
- **Which attributes to read aloud / point at**, in priority order:
  `decision.action` → `decision.reason` → `inference.used` →
  `decision.blocked`/`decision.block_reason` → `game.id`. Those five tell the
  whole story; everything else is supporting detail.
- **Two-window arrangement:** left = flagd / flag-flip surface (or the terminal
  driving the flag), right = Jaeger search from act (a) sorted newest-first.
  Flip a flag on the left, refresh the right, open the new top trace.
- **Fastest path from a flag flip to its trace (≈ seconds):** keep act (a)'s
  search open and sorted by **Most Recent**. After a flag flip, the next agent
  cycle emits a fresh `agent.decision`; hit the browser refresh (or Jaeger's
  "Find Traces" button) and the new trace is the top row. Open it → expand
  `agent.decision` → the changed flag is visible as `agent.*` / `interventions.*`
  attributes. Budget one agent decision-cycle of latency, not a page reload.
- **Disable browser autocomplete/history dropdowns** on the URL bar so typing a
  bookmarked Jaeger URL on stage doesn't drop a suggestion overlay over the view.
- **Lookback hygiene:** set every bookmarked query to `1h`–`2d`. A `15m` window
  shows an empty list if the stack idled during the previous speaker.

---

## 4. Attribute legibility review (projector readability)

Reviewed against the live values. Names use dot-namespacing
(`decision.*`, `inference.*`, `agent.*`) which reads well grouped in Jaeger's
tag table. Verdicts below — **no code is changed here**; cryptic items are noted
as follow-ups for the span-schema owners.

| Attribute | Reads well on a projector? | Note |
|---|---|---|
| `decision.action` | Yes | Verb-like values (`grant_shield`) are self-explaining. |
| `decision.reason` | Yes | Full sentence — the single best line to read aloud. |
| `decision.blocked` / `decision.block_reason` | Yes | Clear boolean + short reason (`not_allowed`). |
| `decision.objective_served` | Yes | `balanced` etc. is legible. |
| `inference.used` / `inference.configured` | Yes | `rules` vs `phi4-mini` contrast lands instantly. |
| `inference.fallback_reason` | Mostly | `llm_not_eligible` is jargon — fine for a technical audience, narrate it. |
| `game.id` / `session.id` | Adequate | `game_8c812a3d9063` is a long hex tail; readable but not memorable. **It is duplicated as both `game.id` and `session.id` with identical values** — visually redundant in the tag table. |
| `agent.objectives` | Borderline | `accelerate=0.1,balanced=0.7,chaos=0.1,endurance=0.1` is a dense single string; wraps awkwardly at large zoom. |
| `fitness.evaluated` | **Cryptic on a projector** | A JSON-array-as-string of `metric=value` pairs with full float precision (`0.6014032183343321`). Unreadable from a distance; expand only if asked. |

### Noted follow-ups (do **not** action in this docs PR)

1. **`fitness.evaluated`** — packing a JSON array of long-precision floats into
   one string attribute is not projector-legible. Possible follow-up for the
   span-schema owner: round to 2–3 sig figs and/or split into discrete keyed
   attributes (`fitness.balanced.skill_gap=0.32`). *Code change — out of scope.*
2. **`game.id` vs `session.id` duplication** — both carry the identical value on
   every span. Harmless but redundant in the tag table; a follow-up could drop
   one alias or document the intent. *Code change — out of scope.*
3. **`agent.infrastructure.decision` not emitted** — the operation does not
   appear in live Jaeger (`/api/operations?service=agent`) and act (b) returns
   no traces without a live infra-agent run. Confirm the infra path emits this
   span under the demo's infra scenario, or the act (b) bookmark will show an
   empty operation dropdown. *Operational/verification follow-up.*

---

## Verification status

- **Hierarchy** (`signal_received → decision → action`): verified against live
  trace `4f0a8448…`.
- **Demo-act queries**: validated live except where explicitly flagged — (a) ✓,
  (b) ✗ not yet emitted (needs live infra run), (c) ✓ (10), (d) ✓ (4), (e) ✓
  (20), action bonus ✓ (10), code-improvement bonus ✓ (5).
- **Attribute names/values**: read from real span data, not assumed.

**Acceptance-criterion caveat:** the issue's AC *"Verified on the actual stage
display setup"* **cannot be checked in this environment** — there is no projector
here. Everything verifiable from software (queries return data, the hierarchy is
accurate, the deep links are syntactically valid and resolve) is covered above.
The on-hardware check — fonts/zoom legible from the back of the actual room — is a
**manual step for the dry-run on the real stage display**.
