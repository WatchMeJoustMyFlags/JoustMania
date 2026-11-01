/** agent.decide feed: parsing, follow anchor, stores, query windows and the decide poll. Split out of agent.ts (#2388). */

import { incrementalStartMs, searchTracesSized, spanStartMs, spanTags, tagBool, tagBoolOrNull, tagNumber, tagString, parentSpanId } from "./jaeger.js";
import type { JaegerSpan, JaegerTracesResponse, SearchSpec } from "./jaeger.js";
import { GoalStore } from "./agentGoal.js";
import type { GameGoal } from "./agentGoal.js";
import { pickAnchor } from "./agentAnchor.js";
import type { DecideFollow } from "./agentAnchor.js";
import { parseFlagStates } from "./agentFlags.js";
import type { FlagPanel } from "./agentFlags.js";

/* ======================================================================== */
/* agent.decide feed (#1233, epic #1225)                                    */
/* ======================================================================== */
/*
 * The decide contract (#1226) records every decision on an `agent.decide` span:
 * which backend answered (and which one the decision.backend flag requested),
 * the model, the answers + confidence, the confidence-threshold verdict, the
 * outcome and the model's one-line rationale. This feed reads the spans straight
 * from Jaeger's HTTP query API (./jaeger.ts), which envoy routes at /jaeger/*
 * (the same Jaeger the deep links open).
 *
 * TRACE SHAPE (services/agent/decision/decide_hook.go)
 *   rules path:  agent.signal_received ─┬─ agent.decide
 *                                        └─ agent.decision ─ agent.action   (0..2)
 *   remote path: agent.decide ─┬─ chat {model}            (backend CLIENT span)
 *                              └─ agent.decision ─ agent.action
 *                (agent.decide is a remote child of the game's gameplay_phase
 *                 span, so the trace is the whole per-game trace.)
 *
 * Jaeger's search returns WHOLE traces that contain a matching span, so one
 * query gives us the decide spans plus their chat / decision neighbours.
 *
 * Every string here (rationale above all) can be model output: it is untrusted
 * and the view escapes it before rendering.
 */

/** Jaeger operation the decide feed searches for. */
export const DECIDE_OPERATION = "agent.decide";

/*
 * CLOCK SKEW: the stage Pi has no RTC and venue WiFi may block NTP, so the
 * agent + Jaeger clock (same host) can be hours off the presenter's browser
 * clock. The decide query therefore NEVER uses Date.now(). The v3 search
 * (#2283) requires both window bounds, so the upper one is a fixed far-future
 * date and a cold start's lower one is the Unix epoch (jaeger.ts searchParams).
 * That is bounded by searchDepth=DECIDE_TRACE_LIMIT, and Jaeger returns the most
 * recent matching traces, so a cold start costs the same 5 traces as a short
 * window would. Once a decide
 * is held, `start` is that span's own (agent-clock) timestamp minus the overlap.
 */

/**
 * Longest a decide can run: the top of the configurable
 * `decision.<backend>.timeout_ms` range (100 to 60000, docs/specs/flags.md).
 */
export const DECIDE_TIMEOUT_CEILING_MS = 60 * 1000;

/** Span export batching on top of that (the OTLP batch delay, with margin). */
export const DECIDE_EXPORT_DELAY_MS = 2 * 1000;

/**
 * Overlap behind the newest decide already seen: spans are exported when they
 * END, so a decide that started earlier than the newest one we hold can still
 * arrive late, by up to the longest decide plus the export delay.
 */
export const DECIDE_OVERLAP_MS = DECIDE_TIMEOUT_CEILING_MS + DECIDE_EXPORT_DELAY_MS;

/**
 * Max traces per Jaeger search. Both rules and remote agent.decide spans live in
 * the whole per-GAME trace (since #1195), and Jaeger returns whole traces, so one
 * trace already carries every decide of its game. Keep this low: it is the main
 * bound on the payload of a poll (and on Jaeger's memory, see #1191).
 */
const DECIDE_TRACE_LIMIT = 5;

/** Max decide rows kept (newest first). */
const MAX_DECIDE_ROWS = 30;

/** Max prediction outcome spans kept for the per-game strip (a game scores a few dozen). */
const MAX_OUTCOMES = 400;

/*
 * PAYLOAD BUDGET (#2287): Jaeger's search returns WHOLE traces, so a live real
 * game's trace (every phase, player and decide span of the game) is downloaded
 * in full on every poll, and it keeps growing: 4.9 MB per poll on the stage Pi.
 * The legacy API has no "spans since" query, so the dashboard bounds the cost in
 * three ways:
 * - pace: the next decide poll waits at least `bytes / DECIDE_BYTES_PER_SEC`
 *   (capped at DECIDE_MAX_PACE_MS, or DECIDE_LIVE_MAX_PACE_MS while a real
 *   game is live), so one browser costs Jaeger at most about
 *   DECIDE_BYTES_PER_SEC however large the trace grows; small traces keep the
 *   1.5 s cadence;
 * - settle: once the newest decide held has not changed for DECIDE_OVERLAP_MS
 *   (browser-elapsed time, never a browser timestamp), the window starts just
 *   after it, so an ended or quiet game's trace is no longer re-fetched;
 * - target: while a real game is live the search asks for that game's
 *   `game.id` (shadow traces cannot crowd it out of the limit, #2255) and the
 *   shadow games' decides come from their own `game.kind=shadow` search every
 *   DECIDE_SHADOW_EVERY_TICKS polls.
 */

/** Jaeger answer size per second the decide poll may cause (characters of JSON). */
export const DECIDE_BYTES_PER_SEC = 512 * 1024;

/** Upper bound of the payload pace, so a huge trace still refreshes. */
export const DECIDE_MAX_PACE_MS = 15_000;

/**
 * The same cap while a real game is live: the presenter's decision strip then
 * refreshes at least every 5 s even though a live game's trace only grows
 * (about 1 MB/s for a 4.9 MB trace instead of 512 KiB/s).
 */
export const DECIDE_LIVE_MAX_PACE_MS = 5_000;

/** While a real game is live, the shadow-game decide search runs every Nth poll. */
export const DECIDE_SHADOW_EVERY_TICKS = 4;

/** Search lanes of the decide poll; each has its own query window. */
export const DECIDE_LANE_ALL = "all";
export const DECIDE_LANE_SHADOW = "shadow";
const DECIDE_LANE_GAME_PREFIX = "game:";

/** Query windows kept for per-game lanes (old games drop out). */
const MAX_GAME_LANES = 4;

/** The minimum wait before the next decide poll after a `bytes`-sized answer. */
export function decidePaceMs(bytes: number, maxMs: number = DECIDE_MAX_PACE_MS): number {
  if (!Number.isFinite(bytes) || bytes <= 0) return 0;
  return Math.min(maxMs, Math.ceil((bytes * 1000) / DECIDE_BYTES_PER_SEC));
}

/** The rules path's per-cycle parent span (decide + decisions are its children). */
const SIGNAL_OPERATION = "agent.signal_received";

/** decide.outcome values (DecideOutcome* in decide_hook.go). */
export type DecideOutcome =
  | "act"
  | "held"
  | "noop"
  | "invalid"
  | "fallback"
  | "discarded";

const KNOWN_OUTCOMES: ReadonlySet<string> = new Set([
  "act",
  "held",
  "noop",
  "invalid",
  "fallback",
  "discarded",
]);

/** An agent.decision span linked to a decide row via the trace shape. */
export interface LinkedDecision {
  spanId: string;
  /** Span start, epoch ms (agent clock). */
  tsMs: number;
  action: string;
  blocked: boolean;
  /** decision.reason: why the agent proposed it. */
  reason: string;
  /** decision.block_reason ("" unless blocked). */
  blockReason: string;
  /**
   * decision.applied (#2093): false when a permitted decision was not written,
   * true when it was, null when the span predates the attribute or the decision
   * was blocked / failed.
   */
  applied: boolean | null;
  /** decision.not_applied_reason ("" unless applied is false), e.g. write_gate_off. */
  notAppliedReason: string;
  /** player.serial of a player-targeted decision ("" when session-scoped), #1295. */
  targetSerial: string;
  /** player.name of the targeted player ("" when the agent knew no name), #1295. */
  targetName: string;
}

/** The targeted player as shown in the Decisions row: name, else serial, else "". */
export function decisionTargetLabel(d: Pick<LinkedDecision, "targetSerial" | "targetName">): string {
  return d.targetName || d.targetSerial;
}

/** One agent.decide span, folded for the feed. */
export interface DecideRow {
  traceId: string;
  spanId: string;
  /** Span start, epoch ms. */
  tsMs: number;
  /** decide.latency_ms, else the span duration. */
  latencyMs: number;
  /** decide.backend: the backend whose answers were used ("rules" after a fallback). */
  backend: string;
  /** decide.backend.requested: the decision.backend flag value. */
  requested: string;
  fallbackReason: string;
  /** gen_ai.request.model, else the child chat span's model ("" for rules). */
  model: string;
  /** ffa.intervention choice ("" when no answer was recorded). */
  intervention: string;
  /** ffa.target choice ("none" for session-scoped, "" when absent). */
  target: string;
  /** decide.confidence (the routing confidence), null when absent. */
  confidence: number | null;
  /** decide.act_confidence_threshold, null when the router did not run. */
  threshold: number | null;
  /** decide.outcome; unknown values are passed through as "unknown". */
  outcome: DecideOutcome | "unknown";
  /** decide.invalid_reason / decide.discarded_reason, whichever is set. */
  outcomeReason: string;
  /** decide.rationale: untrusted model text. */
  rationale: string;
  /** error.type, when the backend call failed. */
  errorType: string;
  gameId: string;
  gameKind: string;
  /** agent.decision spans this decide produced (remote) or ran beside (rules). */
  decisions: LinkedDecision[];
  /** The prediction this decision carried (#2479, UC-040 A44); null when it had none. */
  prediction: DecidePrediction | null;
}

/** Jaeger operation of a scored prediction (decide-contract §14, UC-040 A44). */
export const PREDICTION_OUTCOME_OPERATION = "agent.prediction.outcome";

/**
 * hit / miss / unscorable come from the outcome span; pending = no outcome span yet;
 * unknown = still no outcome span although the decide query window has moved
 * DECIDE_OVERLAP_MS past the row, so it will not be fetched again (a long-horizon or
 * game-end outcome exported after the window settled).
 */
export type PredictionState = "hit" | "miss" | "unscorable" | "pending" | "unknown";

/** The prediction a decision carried (decide.prediction.* tags) and its scored state. */
export interface DecidePrediction {
  id: string;
  template: string;
  characteristic: string;
  scope: string;
  horizonS: number | null;
  /** The stated probability that it comes true. */
  p: number | null;
  /** rules | lever_default | model. */
  source: string;
  /** The rules-table row ("R4") or "". */
  rule: string;
  /** True when an identical prediction was already pending (decide.prediction.merged). */
  merged: boolean;
  /** Joined from the agent.prediction.outcome span of the same trace by id. */
  outcome: PredictionState;
  /** prediction.unscorable_reason of the joined span ("" otherwise). */
  unscorableReason: string;
}

/** One agent.prediction.outcome span: a scored decision prediction or a control. */
export interface PredictionOutcomeSpan {
  id: string;
  traceId: string;
  tsMs: number;
  gameId: string;
  gameKind: string;
  backend: string;
  /** rules | lever_default | model | control. */
  source: string;
  template: string;
  p: number | null;
  /** The decision was applied (false = counterfactual, never in the headline). */
  acted: boolean;
  outcome: "hit" | "miss" | "unscorable";
  unscorableReason: string;
}

/** The "current backend" strip: derived from the newest agent.decide span. */
export interface DecideCurrent {
  tsMs: number;
  backend: string;
  requested: string;
  model: string;
  /** Threshold of the newest span, else the newest one that carried it. */
  threshold: number | null;
  /** Game the strip describes (see currentFrom, UC-070 BR-005). */
  gameId: string;
  gameKind: string;
  /** True when `threshold` came from an older span than the newest one. */
  thresholdFromOlder: boolean;
}

export interface DecideFeed {
  rows: DecideRow[];
  current: DecideCurrent | null;
  /** agent.flag_state spans found in the same game traces (#2059); parse results only. */
  flagStates?: FlagPanel[];
  /** Every agent.prediction.outcome span found (decisions and controls); parse results only. */
  outcomes?: PredictionOutcomeSpan[];
  /** The followed game's hit rate vs control base rate per source (#2479); null without outcomes. */
  predictionStrip?: PredictionStrip | null;
  /** The followed game's goal profile and attainment (#2498); null until its summary exists. */
  goal?: GameGoal | null;
}

/** One source's acted predictions of a game against the controls' base rate. */
export interface PredictionStripSource {
  source: string;
  hits: number;
  misses: number;
  unscorable: number;
  /** hits / (hits + misses); null when nothing was scored. */
  hitRate: number | null;
  /**
   * Hit rate the controls reach on the same templates, weighted by this source's
   * template mix; null while no control of those templates was scored.
   */
  baseRate: number | null;
}

/** The per-game prediction strip: acted predictions only (counterfactuals never count). */
export interface PredictionStrip {
  gameId: string;
  gameKind: string;
  sources: PredictionStripSource[];
  /** Counterfactual (held, blocked, discarded) predictions left out of the rates. */
  counterfactual: number;
}

/**
 * Pull the ffa.intervention / ffa.target choices out of decide.answers. The
 * attribute is size-capped JSON (it may end in "...(truncated)"), so a parse
 * failure is expected and yields blanks rather than an error.
 */
export function parseDecideAnswers(raw: string): { intervention: string; target: string } {
  const out = { intervention: "", target: "" };
  if (!raw) return out;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return out;
  }
  if (!Array.isArray(parsed)) return out;
  for (const a of parsed) {
    if (!a || typeof a !== "object") continue;
    const ans = a as { question_id?: unknown; choice?: unknown };
    if (typeof ans.choice !== "string") continue;
    if (ans.question_id === "ffa.intervention") out.intervention = ans.choice;
    else if (ans.question_id === "ffa.target") out.target = ans.choice;
  }
  return out;
}

function isChatSpan(span: JaegerSpan, tags: Map<string, unknown>): boolean {
  return span.operationName.startsWith("chat ") || tags.get("gen_ai.operation.name") === "chat";
}

/**
 * The decide.prediction.* tags of an agent.decide span, with the outcome joined from
 * the agent.prediction.outcome span of the same trace by prediction id. No outcome
 * span (yet) reads as pending. null when the decision carried no prediction.
 */
function predictionFrom(
  tags: Map<string, unknown>,
  outcomes: Map<string, PredictionOutcomeSpan>,
): DecidePrediction | null {
  const id = tagString(tags, "decide.prediction.id");
  if (!id) return null;
  const scored = outcomes.get(id);
  return {
    id,
    template: tagString(tags, "decide.prediction.template"),
    characteristic: tagString(tags, "decide.prediction.characteristic"),
    scope: tagString(tags, "decide.prediction.scope"),
    horizonS: tagNumber(tags, "decide.prediction.horizon_s"),
    p: tagNumber(tags, "decide.prediction.p"),
    source: tagString(tags, "decide.prediction.source"),
    rule: tagString(tags, "decide.prediction.rule"),
    merged: tagBool(tags, "decide.prediction.merged"),
    outcome: scored ? scored.outcome : "pending",
    unscorableReason: scored ? scored.unscorableReason : "",
  };
}

const KNOWN_PREDICTION_OUTCOMES: ReadonlySet<string> = new Set(["hit", "miss", "unscorable"]);

/** Fold one agent.prediction.outcome span; null for an unknown outcome or a missing id. */
function outcomeSpanFrom(span: JaegerSpan): PredictionOutcomeSpan | null {
  const tags = spanTags(span);
  const id = tagString(tags, "prediction.id");
  const outcome = tagString(tags, "prediction.outcome");
  if (!id || !KNOWN_PREDICTION_OUTCOMES.has(outcome)) return null;
  return {
    id,
    traceId: span.traceID,
    tsMs: spanStartMs(span),
    gameId: tagString(tags, "game.id"),
    gameKind: tagString(tags, "game.kind"),
    backend: tagString(tags, "prediction.backend"),
    source: tagString(tags, "prediction.source"),
    template: tagString(tags, "prediction.template"),
    p: tagNumber(tags, "prediction.p"),
    acted: tagBool(tags, "prediction.acted"),
    outcome: outcome as PredictionOutcomeSpan["outcome"],
    unscorableReason: tagString(tags, "prediction.unscorable_reason"),
  };
}

/**
 * Hit rate per source against the controls' base rate (#2479). Only the game's acted
 * predictions count (UC-040 BR-034: a counterfactual is never mixed in); unscorable
 * is neither a hit nor a miss. The base rate of a source is the controls' hit rate on
 * the templates that source predicted, weighted by its own template mix, so a source
 * that only picks easy templates is compared with how easy they are.
 */
export function predictionStripFrom(
  outcomes: Iterable<PredictionOutcomeSpan>,
  gameId: string,
): PredictionStrip | null {
  const game = [...outcomes].filter((o) => o.gameId === gameId);
  if (game.length === 0) return null;
  const control = new Map<string, { hits: number; scored: number }>();
  for (const o of game) {
    if (o.source !== "control" || o.outcome === "unscorable") continue;
    const c = control.get(o.template) ?? { hits: 0, scored: 0 };
    c.scored++;
    if (o.outcome === "hit") c.hits++;
    control.set(o.template, c);
  }
  const bySource = new Map<string, PredictionStripSource & { mix: Map<string, number> }>();
  let counterfactual = 0;
  for (const o of game) {
    if (o.source === "control") continue;
    if (!o.acted) {
      counterfactual++;
      continue;
    }
    let src = bySource.get(o.source);
    if (!src) {
      src = { source: o.source, hits: 0, misses: 0, unscorable: 0, hitRate: null, baseRate: null, mix: new Map() };
      bySource.set(o.source, src);
    }
    if (o.outcome === "unscorable") {
      src.unscorable++;
      continue;
    }
    if (o.outcome === "hit") src.hits++;
    else src.misses++;
    src.mix.set(o.template, (src.mix.get(o.template) ?? 0) + 1);
  }
  const sources: PredictionStripSource[] = [];
  for (const src of [...bySource.values()].sort((a, b) => a.source.localeCompare(b.source))) {
    const scored = src.hits + src.misses;
    let weight = 0;
    let expected = 0;
    for (const [template, n] of src.mix) {
      const c = control.get(template);
      if (!c || c.scored === 0) continue;
      weight += n;
      expected += n * (c.hits / c.scored);
    }
    sources.push({
      source: src.source,
      hits: src.hits,
      misses: src.misses,
      unscorable: src.unscorable,
      hitRate: scored > 0 ? src.hits / scored : null,
      baseRate: weight > 0 ? expected / weight : null,
    });
  }
  const gameKind = game[0].gameKind;
  return { gameId, gameKind, sources, counterfactual };
}

/** Fold one agent.decide span (with its trace neighbours) into a feed row. */
function decideRowFrom(
  span: JaegerSpan,
  byParent: Map<string, JaegerSpan[]>,
  byId: Map<string, JaegerSpan>,
  outcomes: Map<string, PredictionOutcomeSpan>,
): DecideRow {
  const tags = spanTags(span);
  const parent = parentSpanId(span);

  // Remote path: decision + chat spans are children of the decide span.
  // Rules path: decisions are siblings under the same agent.signal_received.
  const children = byParent.get(span.spanID) ?? [];
  let decisionSpans = children.filter((s) => s.operationName === "agent.decision");
  // Only the rules path shares a parent with its decisions, and that parent is
  // always the cycle's agent.signal_received. A remote decide's parent is the
  // game's gameplay_phase, whose other children are unrelated: never link those.
  if (decisionSpans.length === 0 && byId.get(parent)?.operationName === SIGNAL_OPERATION) {
    decisionSpans = (byParent.get(parent) ?? []).filter(
      (s) => s.operationName === "agent.decision",
    );
  }

  let model = tagString(tags, "gen_ai.request.model");
  if (!model) {
    for (const c of children) {
      const ct = spanTags(c);
      if (!isChatSpan(c, ct)) continue;
      model =
        tagString(ct, "gen_ai.response.model") ||
        tagString(ct, "gen_ai.request.model") ||
        c.operationName.replace(/^chat /, "");
      if (model) break;
    }
  }

  const answers = parseDecideAnswers(tagString(tags, "decide.answers"));
  const rawOutcome = tagString(tags, "decide.outcome");
  const latency = tagNumber(tags, "decide.latency_ms");

  return {
    traceId: span.traceID,
    spanId: span.spanID,
    tsMs: spanStartMs(span),
    latencyMs: latency ?? Math.round(span.duration / 1000),
    backend: tagString(tags, "decide.backend"),
    requested: tagString(tags, "decide.backend.requested"),
    fallbackReason: tagString(tags, "decide.fallback_reason"),
    model,
    intervention: answers.intervention,
    target: answers.target,
    confidence: tagNumber(tags, "decide.confidence"),
    threshold: tagNumber(tags, "decide.act_confidence_threshold"),
    outcome: KNOWN_OUTCOMES.has(rawOutcome) ? (rawOutcome as DecideOutcome) : "unknown",
    outcomeReason:
      tagString(tags, "decide.invalid_reason") || tagString(tags, "decide.discarded_reason"),
    rationale: tagString(tags, "decide.rationale"),
    errorType: tagString(tags, "error.type"),
    gameId: tagString(tags, "game.id"),
    gameKind: tagString(tags, "game.kind"),
    prediction: predictionFrom(tags, outcomes),
    decisions: decisionSpans
      .slice()
      .sort((a, b) => a.startTime - b.startTime)
      .map((d) => {
        const dt = spanTags(d);
        return {
          spanId: d.spanID,
          tsMs: spanStartMs(d),
          action: tagString(dt, "decision.action"),
          blocked: tagBool(dt, "decision.blocked"),
          reason: tagString(dt, "decision.reason"),
          blockReason: tagString(dt, "decision.block_reason"),
          applied: tagBoolOrNull(dt, "decision.applied"),
          notAppliedReason: tagString(dt, "decision.not_applied_reason"),
          targetSerial: tagString(dt, "player.serial"),
          targetName: tagString(dt, "player.name"),
        };
      }),
  };
}

/**
 * Fold a Jaeger search response (jaeger.ts fromOtlp) into the decide feed.
 *
 * Pure. Rows are deduplicated by (traceID, spanID): Jaeger can return the same
 * trace more than once when it is still being written to, and the remote path
 * puts many decide spans in one per-game trace. Newest first, capped.
 */
export function parseDecideTraces(body: JaegerTracesResponse): DecideFeed {
  const rows: DecideRow[] = [];
  const seen = new Set<string>();
  const allOutcomes: PredictionOutcomeSpan[] = [];

  for (const trace of body.data ?? []) {
    const spans = trace.spans ?? [];
    // The outcome span sits in the game trace beside the decide span: join by id.
    const traceOutcomes = new Map<string, PredictionOutcomeSpan>();
    for (const s of spans) {
      if (s.operationName !== PREDICTION_OUTCOME_OPERATION) continue;
      const o = outcomeSpanFrom(s);
      if (!o) continue;
      traceOutcomes.set(o.id, o);
      allOutcomes.push(o);
    }
    const byParent = new Map<string, JaegerSpan[]>();
    const byId = new Map<string, JaegerSpan>();
    for (const s of spans) {
      byId.set(s.spanID, s);
      const p = parentSpanId(s);
      if (!p) continue;
      const list = byParent.get(p);
      if (list) list.push(s);
      else byParent.set(p, [s]);
    }
    for (const s of spans) {
      if (s.operationName !== DECIDE_OPERATION) continue;
      const key = `${s.traceID}/${s.spanID}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push(decideRowFrom(s, byParent, byId, traceOutcomes));
    }
  }

  rows.sort(newestFirst);
  const capped = rows.slice(0, MAX_DECIDE_ROWS);
  const current = currentFrom(rows);
  // The outcome fields are only present when the traces carried outcome spans.
  const strip = current ? predictionStripFrom(allOutcomes, current.gameId) : null;
  return {
    rows: capped,
    current,
    flagStates: parseFlagStates(body),
    ...(allOutcomes.length > 0 ? { outcomes: allOutcomes } : {}),
    ...(strip ? { predictionStrip: strip } : {}),
  };
}

/**
 * The current-backend strip. By default it describes the newest NON-shadow game
 * (the one on stage): a shadow decide must never flip the strip while the real
 * game is on. When no real game is live (the screen-only fallback, UC-070 A9) it
 * describes the followed shadow game, else the game of the newest row of any
 * kind, so an older real game's rows do not hold the strip and an older shadow
 * row does not beat a newer real one. Without any non-shadow row it falls back to
 * the game of the newest row. `rows` must be newest first.
 */
export function currentFrom(rows: DecideRow[], follow?: DecideFollow): DecideCurrent | null {
  const anchor = pickAnchor(rows, follow);
  if (!anchor) return null;
  const gameRows = rows.filter((r) => r.gameId === anchor.gameId);
  const newest = gameRows[0] ?? anchor;
  let threshold = newest.threshold;
  let thresholdFromOlder = false;
  if (threshold === null) {
    const older = gameRows.find((r) => r.threshold !== null) ?? rows.find((r) => r.threshold !== null);
    if (older) {
      threshold = older.threshold;
      thresholdFromOlder = true;
    }
  }
  return {
    tsMs: newest.tsMs,
    backend: newest.backend,
    requested: newest.requested,
    model: newest.model,
    threshold,
    thresholdFromOlder,
    gameId: newest.gameId,
    gameKind: newest.gameKind,
  };
}

function rowKey(r: DecideRow): string {
  return `${r.traceId}/${r.spanId}`;
}

function newestFirst(a: DecideRow, b: DecideRow): number {
  return b.tsMs - a.tsMs || b.spanId.localeCompare(a.spanId);
}

/**
 * Start of the next decide query window (epoch ms, AGENT clock): DECIDE_OVERLAP_MS
 * behind the newest decide already held, derived only from span timestamps.
 * null on a cold start: the query then sends no `start` and Jaeger applies its
 * own server-side default (see CLOCK SKEW above).
 */
export function decideQueryStartMs(newestSeenMs: number | null): number | null {
  return incrementalStartMs(newestSeenMs, DECIDE_OVERLAP_MS);
}



/**
 * The incremental window of one decide search (#2287), agent clock. Before any
 * decide: null (no `start`). While the newest decide held is fresh: that span
 * minus DECIDE_OVERLAP_MS (a decide that started earlier can still end and be
 * exported later). Once the newest decide has stayed the newest for
 * DECIDE_OVERLAP_MS of browser-elapsed time (`now` is only ever subtracted from
 * itself, so clock skew does not matter), every older decide has ended: the
 * window starts just after it and Jaeger stops returning that game's trace.
 */
export class QueryWindow {
  private newestMs: number | null = null;
  private heldSinceMs = 0;

  constructor(private readonly now: () => number = () => performance.now()) {}

  observe(rows: Pick<DecideRow, "tsMs">[]): void {
    for (const r of rows) {
      if (this.newestMs === null || r.tsMs > this.newestMs) {
        this.newestMs = r.tsMs;
        this.heldSinceMs = this.now();
      }
    }
  }

  startMs(): number | null {
    if (this.newestMs === null) return null;
    if (this.now() - this.heldSinceMs >= DECIDE_OVERLAP_MS) return this.newestMs + 1;
    return decideQueryStartMs(this.newestMs);
  }
}

/**
 * Client-side decide rows across polls: each poll only fetches a short window,
 * so the feed is the union of what the polls returned, keyed by trace/span (a
 * re-fetched span replaces its row: its linked decisions may have arrived
 * since), bounded to the newest `max` rows. Each search lane (all decides, one
 * real game, the shadow games) keeps its own QueryWindow.
 */
export class DecideStore {
  private readonly rows = new Map<string, DecideRow>();
  private newestMs: number | null = null;
  private follow: DecideFollow = { realGameLive: null, followGameId: "" };
  private readonly windows = new Map<string, QueryWindow>();
  private readonly outcomes = new Map<string, PredictionOutcomeSpan>();
  private readonly goals = new GoalStore();

  constructor(
    private readonly max = MAX_DECIDE_ROWS,
    private readonly now: () => number = () => performance.now(),
  ) {}

  /** Merge rows a search of `lane` returned (its window moves with them). */
  merge(rows: DecideRow[], lane: string = DECIDE_LANE_ALL): void {
    this.window(lane).observe(rows);
    for (const r of rows) {
      this.rows.set(rowKey(r), r);
      if (this.newestMs === null || r.tsMs > this.newestMs) this.newestMs = r.tsMs;
    }
    if (this.rows.size > this.max) {
      const keep = this.sorted().slice(0, this.max);
      this.rows.clear();
      for (const r of keep) this.rows.set(rowKey(r), r);
    }
  }

  /** Merge the outcome spans a search returned (keyed by prediction id, newest kept, bounded). */
  mergeOutcomes(outcomes: PredictionOutcomeSpan[]): void {
    for (const o of outcomes) this.outcomes.set(o.id, o);
    if (this.outcomes.size > MAX_OUTCOMES) {
      const keep = [...this.outcomes.values()].sort((a, b) => b.tsMs - a.tsMs).slice(0, MAX_OUTCOMES);
      this.outcomes.clear();
      for (const o of keep) this.outcomes.set(o.id, o);
    }
  }

  /** Merge the goals of the game summaries a search returned (#2498). */
  mergeGoals(goals: GameGoal[]): void {
    this.goals.merge(goals);
  }

  /** Next query start of `lane` (agent clock), null before its first decide. */
  queryStartMs(lane: string = DECIDE_LANE_ALL): number | null {
    return this.windows.get(lane)?.startMs() ?? null;
  }

  /** Newest decide span time held (agent clock), null before the first one. */
  newestSeenMs(): number | null {
    return this.newestMs;
  }

  /** The follow state the next feed() uses. */
  get followState(): DecideFollow {
    return { ...this.follow };
  }

  get size(): number {
    return this.rows.size;
  }

  /** The game the dashboard follows (UC-070 BR-005); used by the next feed(). */
  setFollow(follow: DecideFollow): void {
    this.follow = { ...follow };
  }

  feed(): DecideFeed {
    const rows = this.sorted().map((r) => this.settlePrediction(r));
    const current = currentFrom(rows, this.follow);
    return {
      rows,
      current,
      predictionStrip: current ? predictionStripFrom(this.outcomes.values(), current.gameId) : null,
      goal: current ? this.goals.forGame(current.gameId) : null,
    };
  }

  /** A pending prediction the query window has left behind reads unknown, never pending forever. */
  private settlePrediction(row: DecideRow): DecideRow {
    if (row.prediction?.outcome !== "pending" || this.newestMs === null) return row;
    if (row.tsMs >= this.newestMs - DECIDE_OVERLAP_MS) return row;
    return { ...row, prediction: { ...row.prediction, outcome: "unknown" } };
  }

  private window(lane: string): QueryWindow {
    let w = this.windows.get(lane);
    if (!w) {
      w = new QueryWindow(this.now);
      this.windows.set(lane, w);
      const gameLanes = [...this.windows.keys()].filter((k) => k.startsWith(DECIDE_LANE_GAME_PREFIX));
      for (const old of gameLanes.slice(0, Math.max(0, gameLanes.length - MAX_GAME_LANES))) {
        this.windows.delete(old);
      }
    }
    return w;
  }

  private sorted(): DecideRow[] {
    return Array.from(this.rows.values()).sort(newestFirst);
  }
}

/** One search of a decide poll and the lane whose window it uses. */
export interface DecideSearch {
  lane: string;
  spec: SearchSpec;
}

/**
 * The searches of decide poll number `tick` (#2287, UC-035 BR-011). While a real
 * game is live (and its id known): that game's decides by `game.id`, plus every
 * DECIDE_SHADOW_EVERY_TICKS polls the shadow games' by `game.kind=shadow`, so a
 * burst of shadow traces never pushes the stage game out of the limit (#2255).
 * Otherwise one search over all decides (the shadow follow of UC-070 A9 and the
 * idle tab).
 */
export function decideSearches(store: DecideStore, tick: number): DecideSearch[] {
  const follow = store.followState;
  const primary = follow.realGameLive === true ? (follow.primaryGameId ?? "") : "";
  const search = (lane: string, tags?: Record<string, string>): DecideSearch => ({
    lane,
    spec: {
      operation: DECIDE_OPERATION,
      limit: DECIDE_TRACE_LIMIT,
      startMs: store.queryStartMs(lane),
      ...(tags ? { tags } : {}),
    },
  });
  if (!primary) return [search(DECIDE_LANE_ALL)];
  const out = [search(`${DECIDE_LANE_GAME_PREFIX}${primary}`, { "game.id": primary })];
  if (tick % DECIDE_SHADOW_EVERY_TICKS === 0) out.push(search(DECIDE_LANE_SHADOW, { "game.kind": "shadow" }));
  return out;
}

/** What one decide poll fetched: answer size (for the pace) and its flag states. */
export interface DecidePollResult {
  bytes: number;
  /** Minimum wait before the next poll (decidePaceMs, live-game cap while a game search ran). */
  paceMs: number;
  flagStates: FlagPanel[];
  /** Failures of lanes other than the primary feed (the shadow lane); their rows are simply missing. */
  laneErrors: unknown[];
}

/** Each decide search has its own timeout, so a big game trace cannot starve the lane after it (#2386). */
export const DECIDE_SEARCH_TIMEOUT_MS = 4000;

/** `signal` plus a timeout of its own: aborts when either does. */
function withTimeout(signal: AbortSignal | undefined, ms: number): { signal: AbortSignal; done: () => void } {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener("abort", abort);
  const timer = setTimeout(abort, ms);
  return {
    signal: controller.signal,
    done: () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    },
  };
}

/**
 * One decide poll (#2287): run decideSearches(store, tick) one after the other,
 * merge each lane's rows into the store and return the total answer size and the
 * agent.flag_state spans of the fetched game traces. Throws
 * JaegerUnavailableError as the single-lane poll did when the primary (game or all) lane
 * fails; rows of a lane that answered before the failure stay merged. A failing
 * shadow lane is reported in `laneErrors` instead (#2386, UC-035 BR-011).
 */
export async function pollDecides(
  store: DecideStore,
  tick: number,
  signal?: AbortSignal,
): Promise<DecidePollResult> {
  let bytes = 0;
  let liveGame = false;
  const flagStates: FlagPanel[] = [];
  const laneErrors: unknown[] = [];
  for (const { lane, spec } of decideSearches(store, tick)) {
    const isGame = lane.startsWith(DECIDE_LANE_GAME_PREFIX);
    if (isGame) liveGame = true;
    const guarded = withTimeout(signal, DECIDE_SEARCH_TIMEOUT_MS);
    try {
      const answer = await searchTracesSized(spec, guarded.signal);
      bytes += answer.bytes;
      const feed = parseDecideTraces(answer.body);
      store.merge(feed.rows, lane);
      store.mergeOutcomes(feed.outcomes ?? []);
      flagStates.push(...(feed.flagStates ?? []));
    } catch (error) {
      // Only the shadow lane is optional: it must not block the primary feed (#2386).
      if (lane !== DECIDE_LANE_SHADOW || signal?.aborted) throw error;
      laneErrors.push(error);
    } finally {
      guarded.done();
    }
  }
  const paceMs = decidePaceMs(bytes, liveGame ? DECIDE_LIVE_MAX_PACE_MS : DECIDE_MAX_PACE_MS);
  return { bytes, paceMs, flagStates, laneErrors };
}

/**
 * The decide poller's `run` (main.ts and its test share it). The tick advances
 * BEFORE the await: a failing poll must not repeat the same tick's searches
 * forever, the shadow lane (every 4th tick) would never be reached otherwise
 * (UC-035 BR-011, #2386). `onPolled` renders the result; its return is the pace.
 */
export function createDecideRun(
  store: DecideStore,
  onPolled: (polled: DecidePollResult) => void,
): (signal?: AbortSignal) => Promise<number> {
  let tick = 0;
  return async (signal) => {
    const polled = await pollDecides(store, tick++, signal);
    onPolled(polled);
    return polled.paceMs;
  };
}
