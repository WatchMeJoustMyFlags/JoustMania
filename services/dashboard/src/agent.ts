/**
 * Agent data source (issue #792; spans since #2059).
 *
 * The "Agent" tab is the demo's observability focus screen: it shows what the
 * in-game agent is doing live: the decisions it evaluates (agent.decide), the
 * effective flag values it resolved, its activity (blocked decisions, rollouts)
 * and the interventions it applied vs blocked.
 *
 * SOURCES (UC-035 step 3, docs/specs/telemetry.md §6.1)
 * ------------------------------------------------------
 *   Jaeger (./jaeger.ts), every query without a browser-clock time:
 *     agent.decide          decision feed + strip (the decide poll, game traces)
 *     agent.decision        blocked activity rows: the decide poll's traces
 *                           already hold them (LinkedDecision), no extra query
 *     agent.flag_state      flag panel: also in the decide poll's game traces;
 *                           its own small search (FLAG_STATE_FALLBACK_LIMIT) only
 *                           when the decide poll brought none newer
 *     agent.infrastructure.decision  rollout rows, one tag search per
 *                           remediation.action (ROLLOUT_ACTIONS)
 *   VictoriaMetrics: the applied write counter and the blocked tallies (UC-040
 *   A21); `n/a` when it is down (no fallback: a fold over a few traces is not a
 *   10-minute window).
 *
 * An agent image older than #2059 emits no agent.flag_state: the flag panel then
 * shows a hint, never an error (degrade.ts).
 */

import {
  incrementalStartMs,
  searchParams,
  searchTraces,
  spanStartMs,
  spanStartUs,
  spanTags,
  spansNamed,
  tagBool,
  tagBoolOrNull,
  tagNumber,
  tagString,
  parentSpanId,
} from "./jaeger.js";
import { vmQuery } from "./vm.js";
import type { VmQueryResponse } from "./vm.js";
import type { JaegerSpan, JaegerTracesResponse } from "./jaeger.js";

export type { VmQueryResponse } from "./vm.js";
export type { JaegerSpan, JaegerTag, JaegerTrace, JaegerTracesResponse, JaegerReference } from "./jaeger.js";


/** Blocked-tally window (UC-040 A21). */
const LOOKBACK_SECONDS = 10 * 60; // 10 minutes

/** Max activity-feed rows (newest first). */
const MAX_FEED_ROWS = 80;

/**
 * VictoriaMetrics instant-query for the count of applied interventions, by result.
 *
 * `agent_intervention_writes_total{result="ok"|"error"}` is the ActionSink's
 * flag-file write counter (services/agent/actions/writer.go). VM receives the
 * metric under the namespaced job, so we sum across jobs rather than pinning one.
 * Best effort: when VM is unreachable the overlay shows `n/a`.
 */
const APPLIED_QUERY = 'sum(agent_intervention_writes_total) by (result)';

/**
 * VictoriaMetrics query for the blocked decisions of the window, by action and
 * reason (UC-040 A21, #1326): `agent_decisions_total{blocked="true"}` counts every
 * refusal, including the repeats that emit no agent.decision span.
 */
const BLOCKED_QUERY =
  `sum by (action, block_reason) (increase(agent_decisions_total{blocked="true"}[${LOOKBACK_SECONDS}s]))`;

/** Jaeger operation of the effective flag set per game partition (#2059). */
export const FLAG_STATE_OPERATION = "agent.flag_state";

/** Traces of the flag-state fallback search (one-span traces or small game traces). */
export const FLAG_STATE_FALLBACK_LIMIT = 2;

/** Jaeger operation of an active infra-loop cycle (own root). */
export const ROLLOUT_OPERATION = "agent.infrastructure.decision";

/** The remediation.action values that make an activity row (`none` does not). */
export const ROLLOUT_ACTIONS = ["expand", "rollback", "recommended_only"] as const;
export type RolloutAction = (typeof ROLLOUT_ACTIONS)[number];

/** Traces per rollout tag search. */
export const ROLLOUT_LIMIT = 10;

/** Overlap behind the newest rollout span already held (spans export when they end). */
export const ROLLOUT_OVERLAP_MS = 30 * 1000;

/** Max rollout rows kept. */
const MAX_ROLLOUT_ROWS = 20;

/** One row in the activity feed. */
export interface DecisionFeedRow {
  /** Epoch ms of the span (agent clock). */
  tsMs: number;
  /** "blocked" | "rollout": drives the row style. */
  kind: "blocked" | "rollout";
  /** Span name the row came from. */
  msg: string;
  /** Short human label. */
  signal: string;
  /** The intervention (blocked) or rollout target (rollout), "" when unknown. */
  action: string;
  /** Whether this decision was blocked. */
  blocked: boolean;
  /** Block reason (blocked) or fitness violations (rollout). */
  reason: string;
  /** Game id the row belongs to ("" for infra rows). */
  sessionId: string;
  /** Targeted player (name, else serial), "" when session-scoped. */
  target: string;
  /** trace/span for the deep link. */
  traceId: string;
  spanId: string;
}

/** The effective flag set of one game, from its newest agent.flag_state span. */
export interface FlagPanel {
  /** Span start (agent clock). */
  tsMs: number;
  /** Span start in µs (agent clock): the ordering key. */
  startUs: number;
  traceId: string;
  spanId: string;
  gameId: string;
  gameKind: string;
  gameMode: string;
  /** agent.flag_state.trigger: game_start | changed. */
  trigger: string;
  /** decide.backend.requested: the decision.backend flag value. */
  decisionBackend: string;
  /** decide.act_confidence_threshold, null when absent. */
  actThreshold: number | null;
  batteryThreshold: string;
  maxPerMinute: string;
  /** agent.enabled (the kill switch), null when absent. */
  enabled: boolean | null;
  /** agent.objectives summary ("k=v,..."). */
  objectives: string;
  /** interventions.allowed summary. */
  interventionsAllowed: string;
  /** interventions.enabled, the write gate (#2093); null on an older agent image. */
  interventionsEnabled: boolean | null;
}

/** A single action's blocked tally for the intervention overlay. */
export interface InterventionTally {
  action: string;
  blocked: number;
}

/** Folded intervention overlay state. */
export interface InterventionOverlay {
  /** Per-action blocked counts from VictoriaMetrics (descending). */
  blockedByAction: InterventionTally[];
  /** Blocked counts grouped by reason (descending). */
  blockedByReason: { reason: string; count: number }[];
  /** Total blocked decisions in the window; null when VictoriaMetrics is unreachable. */
  totalBlocked: number | null;
  /** Applied writes (result="ok"), null when VM was unreachable / had no samples. */
  appliedOk: number | null;
  /** Failed flag-file writes (result="error"), or null when unavailable. */
  appliedError: number | null;
}

/** The full folded state the Agent tab renders. */
export interface AgentView {
  feed: DecisionFeedRow[];
  flags: FlagPanel | null;
  overlay: InterventionOverlay;
}

function toSortedTallies(m: Map<string, number>): InterventionTally[] {
  return Array.from(m.entries())
    .map(([action, blocked]) => ({ action, blocked }))
    .sort((a, b) => b.blocked - a.blocked || a.action.localeCompare(b.action));
}

function toSortedReasons(m: Map<string, number>): { reason: string; count: number }[] {
  return Array.from(m.entries())
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
}

/**
 * Parse a VictoriaMetrics instant-query response into the applied/error counts.
 * Sums across all `result` series. Returns nulls when the response is empty or
 * malformed so the overlay can show "n/a" without throwing.
 */
export function parseAppliedCounts(body: VmQueryResponse): {
  appliedOk: number | null;
  appliedError: number | null;
} {
  const results = body.data?.result;
  if (!results || results.length === 0) {
    return { appliedOk: null, appliedError: null };
  }
  let ok: number | null = null;
  let err: number | null = null;
  for (const series of results) {
    const result = series.metric["result"];
    const v = Number.parseFloat(series.value?.[1] ?? "");
    if (!Number.isFinite(v)) continue;
    if (result === "ok") ok = (ok ?? 0) + v;
    else if (result === "error") err = (err ?? 0) + v;
  }
  return { appliedOk: ok, appliedError: err };
}

/** The blocked tallies of the intervention overlay. */
export type BlockedTallies = Pick<InterventionOverlay, "blockedByAction" | "blockedByReason" | "totalBlocked">;

/** VictoriaMetrics down: every blocked tally is n/a (UC-035 A3, UC-040 A21). */
export const BLOCKED_UNAVAILABLE: BlockedTallies = { blockedByAction: [], blockedByReason: [], totalBlocked: null };

/**
 * Parse the BLOCKED_QUERY response into the overlay's blocked tallies. `increase`
 * extrapolates, so each series is rounded to a whole count. A successful empty
 * answer is zero blocked (no samples in the window), not unavailable.
 */
export function parseBlockedCounts(body: VmQueryResponse): BlockedTallies {
  const results = body.data?.result ?? [];
  const byAction = new Map<string, number>();
  const byReason = new Map<string, number>();
  let total = 0;
  for (const series of results) {
    const n = Math.round(Number.parseFloat(series.value?.[1] ?? ""));
    if (!Number.isFinite(n) || n <= 0) continue;
    const action = series.metric["action"] ?? "";
    const reason = series.metric["block_reason"] ?? "";
    total += n;
    if (action) byAction.set(action, (byAction.get(action) ?? 0) + n);
    if (reason) byReason.set(reason, (byReason.get(reason) ?? 0) + n);
  }
  return {
    blockedByAction: toSortedTallies(byAction),
    blockedByReason: toSortedReasons(byReason),
    totalBlocked: total,
  };
}

/** The VictoriaMetrics half of the overlay. Never throws: VM down = all n/a. */
export async function fetchInterventionCounts(signal?: AbortSignal): Promise<InterventionOverlay> {
  const [applied, blocked] = await Promise.all([vmQuery(APPLIED_QUERY, signal), vmQuery(BLOCKED_QUERY, signal)]);
  return {
    ...(blocked ? parseBlockedCounts(blocked) : BLOCKED_UNAVAILABLE),
    ...(applied ? parseAppliedCounts(applied) : { appliedOk: null, appliedError: null }),
  };
}

/* ======================================================================== */
/* agent.flag_state: the flag panel (#2059)                                  */
/* ======================================================================== */

/** Fold one agent.flag_state span. */
export function flagPanelFrom(span: JaegerSpan): FlagPanel {
  const tags = spanTags(span);
  return {
    tsMs: spanStartMs(span),
    startUs: spanStartUs(span),
    traceId: span.traceID,
    spanId: span.spanID,
    gameId: tagString(tags, "game.id"),
    gameKind: tagString(tags, "game.kind"),
    gameMode: tagString(tags, "game.mode"),
    trigger: tagString(tags, "agent.flag_state.trigger"),
    decisionBackend: tagString(tags, "decide.backend.requested"),
    actThreshold: tagNumber(tags, "decide.act_confidence_threshold"),
    batteryThreshold: tagString(tags, "policy.battery_threshold"),
    maxPerMinute: tagString(tags, "policy.max_interventions_per_minute"),
    enabled: tagBoolOrNull(tags, "agent.enabled"),
    objectives: tagString(tags, "agent.objectives"),
    interventionsAllowed: tagString(tags, "interventions.allowed"),
    interventionsEnabled: tagBoolOrNull(tags, "interventions.enabled"),
  };
}

/** Every agent.flag_state span of a Jaeger response, folded. */
export function parseFlagStates(body: JaegerTracesResponse): FlagPanel[] {
  return spansNamed(body, FLAG_STATE_OPERATION).map(flagPanelFrom);
}

/** Newest first by span time in µs (two flag states can share a ms), then span id. */
function flagNewestFirst(a: FlagPanel, b: FlagPanel): number {
  return b.startUs - a.startUs || b.spanId.localeCompare(a.spanId);
}

/**
 * The newest flag state per game across polls. The panel shows the followed game
 * (UC-070 BR-005) chosen by the same anchor rule as the current-backend strip
 * (`pickAnchor`), so a shadow game's flag state never replaces the stage game's.
 */
export class FlagStateStore {
  private readonly byGame = new Map<string, FlagPanel>();

  constructor(private readonly maxGames = 20) {}

  /**
   * Fold new flag states in. Over `maxGames` the oldest go, but never the live
   * primary game's (`follow`): a burst of newer shadow games must not evict the
   * flag state the panel shows (UC-070 BR-005).
   */
  merge(states: FlagPanel[], follow?: DecideFollow): void {
    for (const s of states) {
      const held = this.byGame.get(s.gameId);
      if (!held || flagNewestFirst(s, held) < 0) this.byGame.set(s.gameId, s);
    }
    if (this.byGame.size > this.maxGames) {
      const pinned =
        follow?.realGameLive === true && follow.primaryGameId ? this.byGame.get(follow.primaryGameId) : undefined;
      const rest = this.sorted().filter((s) => s !== pinned);
      const keep = pinned ? [pinned, ...rest.slice(0, this.maxGames - 1)] : rest.slice(0, this.maxGames);
      this.byGame.clear();
      for (const s of keep) this.byGame.set(s.gameId, s);
    }
  }

  get size(): number {
    return this.byGame.size;
  }

  has(gameId: string): boolean {
    return this.byGame.has(gameId);
  }

  /**
   * The followed game's flag state (null before any agent.flag_state span).
   * While a real game is live it is that game's (`primaryGameId`) or null (the
   * hint until its flag state arrives), never an older game's or a shadow game's.
   */
  current(follow?: DecideFollow): FlagPanel | null {
    if (follow?.realGameLive === true) {
      if (follow.primaryGameId) return this.byGame.get(follow.primaryGameId) ?? null;
      return this.sorted().find((s) => !isShadowGame(s.gameKind)) ?? null;
    }
    return pickAnchor(this.sorted(), follow) ?? null;
  }

  private sorted(): FlagPanel[] {
    return Array.from(this.byGame.values()).sort(flagNewestFirst);
  }
}

/**
 * Whether the flag panel needs its own agent.flag_state search: no flag state is
 * held, or none for the followed game. While a real game is live that is the
 * primary game (`follow.primaryGameId`, follow.ts): a new game that has not
 * decided yet still shows the previous game on the decide strip, so the strip's
 * game is not enough. Otherwise it is the game the decide strip describes
 * (`decideGameId`, the UC-070 BR-005 anchor), e.g. a quiet game with no
 * agent.decide spans, whose game trace the decide poll never fetched. While
 * decides flow for a game whose flag state is held, the decide poll's traces are
 * enough.
 */
export function needsFlagStateFallback(
  store: FlagStateStore,
  decideGameId: string | null,
  follow?: DecideFollow,
): boolean {
  if (store.size === 0) return true;
  if (follow?.realGameLive === true && follow.primaryGameId) return !store.has(follow.primaryGameId);
  return decideGameId === null || !store.has(decideGameId);
}

/**
 * The flag-state fallback search: the newest FLAG_STATE_FALLBACK_LIMIT traces,
 * with no `start` (Jaeger's own window; a flag state is emitted once per game and
 * on change, so an incremental window would miss a long quiet game's). While a
 * real game is live the search asks for that game's `game.id`: shadow games
 * started after it would otherwise fill the limit.
 */
export async function fetchFlagStates(signal?: AbortSignal, follow?: DecideFollow): Promise<FlagPanel[]> {
  const primary = follow?.realGameLive === true ? follow.primaryGameId : "";
  const body = await searchTraces(
    {
      operation: FLAG_STATE_OPERATION,
      limit: FLAG_STATE_FALLBACK_LIMIT,
      startMs: null,
      ...(primary ? { tags: { "game.id": primary } } : {}),
    },
    signal,
  );
  return parseFlagStates(body);
}

/* ======================================================================== */
/* Activity feed: blocked decisions + rollout rows (#2059, D5)               */
/* ======================================================================== */

/** The blocked agent.decision spans the decide poll already holds, as feed rows. */
export function blockedRowsFrom(rows: DecideRow[]): DecisionFeedRow[] {
  const out: DecisionFeedRow[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    for (const d of r.decisions) {
      if (!d.blocked) continue;
      const key = `${r.traceId}/${d.spanId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        tsMs: d.tsMs || r.tsMs,
        kind: "blocked",
        msg: "agent.decision",
        signal: "decision blocked",
        action: d.action,
        blocked: true,
        reason: d.blockReason || d.reason,
        sessionId: r.gameId,
        target: decisionTargetLabel(d),
        traceId: r.traceId,
        spanId: d.spanId,
      });
    }
  }
  return out;
}

const ROLLOUT_SIGNAL: Record<string, string> = {
  expand: "rollout expand",
  rollback: "rollout rollback",
  recommended_only: "rollback recommended",
};

/** Fold one agent.infrastructure.decision span into a rollout row (null for `none`). */
export function rolloutRowFrom(span: JaegerSpan): DecisionFeedRow | null {
  const tags = spanTags(span);
  const action = tagString(tags, "remediation.action");
  if (!(ROLLOUT_ACTIONS as readonly string[]).includes(action)) return null;
  const dryRun = tags.get("rollout.dry_run") === true || tags.get("rollout.dry_run") === "true";
  const violations = tagString(tags, "fitness.violations");
  return {
    tsMs: spanStartMs(span),
    kind: "rollout",
    msg: ROLLOUT_OPERATION,
    signal: ROLLOUT_SIGNAL[action] + (dryRun ? " (dry run)" : ""),
    action: tagString(tags, "rollout.target"),
    blocked: false,
    reason: violations,
    sessionId: "",
    target: "",
    traceId: span.traceID,
    spanId: span.spanID,
  };
}

/** Rollout rows across polls, with an incremental window per remediation.action. */
export class RolloutStore {
  private readonly rows = new Map<string, DecisionFeedRow>();
  private readonly newestByAction = new Map<string, number>();

  constructor(private readonly max = MAX_ROLLOUT_ROWS) {}

  merge(action: RolloutAction, body: JaegerTracesResponse): void {
    for (const span of spansNamed(body, ROLLOUT_OPERATION)) {
      const ts = spanStartMs(span);
      const newest = this.newestByAction.get(action);
      if (newest === undefined || ts > newest) this.newestByAction.set(action, ts);
      const row = rolloutRowFrom(span);
      if (row) this.rows.set(`${row.traceId}/${row.spanId}`, row);
    }
    if (this.rows.size > this.max) {
      const keep = this.list().slice(0, this.max);
      this.rows.clear();
      for (const r of keep) this.rows.set(`${r.traceId}/${r.spanId}`, r);
    }
  }

  /** Next search start for one action (agent clock), null before its first span. */
  queryStartMs(action: RolloutAction): number | null {
    return incrementalStartMs(this.newestByAction.get(action) ?? null, ROLLOUT_OVERLAP_MS);
  }

  list(): DecisionFeedRow[] {
    return Array.from(this.rows.values()).sort(feedNewestFirst);
  }
}

/** One search per rollout action; throws JaegerUnavailableError when Jaeger is down. */
export async function fetchRollouts(store: RolloutStore, signal?: AbortSignal): Promise<void> {
  const bodies = await Promise.all(
    ROLLOUT_ACTIONS.map((action) =>
      searchTraces(
        {
          operation: ROLLOUT_OPERATION,
          limit: ROLLOUT_LIMIT,
          startMs: store.queryStartMs(action),
          tags: { "remediation.action": action },
        },
        signal,
      ),
    ),
  );
  ROLLOUT_ACTIONS.forEach((action, i) => store.merge(action, bodies[i]));
}

function feedNewestFirst(a: DecisionFeedRow, b: DecisionFeedRow): number {
  return b.tsMs - a.tsMs || b.spanId.localeCompare(a.spanId);
}

/** The activity feed: blocked decisions plus rollout rows, newest first, capped. */
export function activityFeed(blocked: DecisionFeedRow[], rollouts: DecisionFeedRow[]): DecisionFeedRow[] {
  return [...blocked, ...rollouts].sort(feedNewestFirst).slice(0, MAX_FEED_ROWS);
}

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
 * clock. The decide query therefore NEVER uses Date.now(): it sends no `end`
 * (Jaeger defaults it to its own now) and, on a cold start, no `start` either.
 * Jaeger 2.19's /api/traces ignores a `lookback` param (it is only read by the
 * dependencies/metrics endpoints): a missing `start` means Jaeger-now minus its
 * built-in 48h trace-query lookback (default_params.go). That is bounded by
 * limit=DECIDE_TRACE_LIMIT, and Jaeger returns the most recent matching traces,
 * so a cold start costs the same 5 traces as a short window would. Once a decide
 * is held, `start` is that span's own (agent-clock) timestamp minus the overlap.
 */

/**
 * Overlap behind the newest decide already seen: spans are exported when they
 * END, so a decide that started a little earlier than the newest one we hold can
 * still arrive late.
 */
export const DECIDE_OVERLAP_MS = 30 * 1000;

/**
 * Max traces per Jaeger search. Both rules and remote agent.decide spans live in
 * the whole per-GAME trace (since #1195), and Jaeger returns whole traces, so one
 * trace already carries every decide of its game. Keep this low: it is the main
 * bound on the payload of a poll (and on Jaeger's memory, see #1191).
 */
const DECIDE_TRACE_LIMIT = 5;

/** Max decide rows kept (newest first). */
const MAX_DECIDE_ROWS = 30;

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

/** Fold one agent.decide span (with its trace neighbours) into a feed row. */
function decideRowFrom(
  span: JaegerSpan,
  byParent: Map<string, JaegerSpan[]>,
  byId: Map<string, JaegerSpan>,
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
 * Fold a Jaeger /api/traces response into the decide feed.
 *
 * Pure. Rows are deduplicated by (traceID, spanID): Jaeger can return the same
 * trace more than once when it is still being written to, and the remote path
 * puts many decide spans in one per-game trace. Newest first, capped.
 */
export function parseDecideTraces(body: JaegerTracesResponse): DecideFeed {
  const rows: DecideRow[] = [];
  const seen = new Set<string>();

  for (const trace of body.data ?? []) {
    const spans = trace.spans ?? [];
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
      rows.push(decideRowFrom(s, byParent, byId));
    }
  }

  rows.sort(newestFirst);
  const capped = rows.slice(0, MAX_DECIDE_ROWS);
  return { rows: capped, current: currentFrom(rows), flagStates: parseFlagStates(body) };
}

/** Shadow games are the experiment loop's headless games (game.kind="shadow"). */
export function isShadowGame(kind: string): boolean {
  return kind === "shadow";
}

/**
 * What the dashboard follows (UC-070 BR-005, follow.ts): whether a real game is
 * live (null = unknown), the followed shadow game ("" = none) and the primary
 * (real) game's id ("" = unknown; only the flag panel uses it).
 */
export interface DecideFollow {
  realGameLive: boolean | null;
  followGameId: string;
  primaryGameId?: string;
}

/**
 * The followed game's newest item (UC-070 BR-005), shared by the current-backend
 * strip and the flag panel. By default the newest NON-shadow game (the one on
 * stage); when no real game is live, the followed shadow game, else the newest
 * item of any kind. Without any non-shadow item, the newest item. `items` must be
 * newest first; undefined when empty.
 */
export function pickAnchor<T extends { gameId: string; gameKind: string }>(
  items: T[],
  follow?: DecideFollow,
): T | undefined {
  if (items.length === 0) return undefined;
  const followed = follow?.followGameId ? items.find((r) => r.gameId === follow.followGameId) : undefined;
  // No real game live and nothing followed with items: the newest item of any kind.
  const newestOverall = follow?.realGameLive === false ? items[0] : undefined;
  return followed ?? newestOverall ?? items.find((r) => !isShadowGame(r.gameKind)) ?? items[0];
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

/** Query string for the decide search. Never contains browser-clock times. */
export function decideQueryParams(startMs: number | null): URLSearchParams {
  return searchParams({ operation: DECIDE_OPERATION, limit: DECIDE_TRACE_LIMIT, startMs });
}

/**
 * Client-side decide rows across polls: each poll only fetches a short window,
 * so the feed is the union of what the polls returned, keyed by trace/span (a
 * re-fetched span replaces its row: its linked decisions may have arrived
 * since), bounded to the newest `max` rows.
 */
export class DecideStore {
  private readonly rows = new Map<string, DecideRow>();
  private newestMs: number | null = null;
  private follow: DecideFollow = { realGameLive: null, followGameId: "" };

  constructor(private readonly max = MAX_DECIDE_ROWS) {}

  merge(rows: DecideRow[]): void {
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

  /** Next query start (agent clock), null before the first decide is held. */
  queryStartMs(): number | null {
    return decideQueryStartMs(this.newestMs);
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
    const rows = this.sorted();
    return { rows, current: currentFrom(rows, this.follow) };
  }

  private sorted(): DecideRow[] {
    return Array.from(this.rows.values()).sort(newestFirst);
  }
}

/** Geometry of a confidence bar against the act threshold (all percentages 0..100). */
export interface ConfidenceBar {
  /** Filled width. 0 when confidence is unknown. */
  fillPct: number;
  /** Threshold marker position, null when no threshold was recorded. */
  thresholdPct: number | null;
  /** confidence >= threshold; null when either side is unknown. */
  pass: boolean | null;
}

function clampUnit(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return Math.min(1, Math.max(0, v));
}

/** Confidence vs threshold maths for the row bar. Mirrors the agent's `>=` routing. */
export function confidenceBar(confidence: number | null, threshold: number | null): ConfidenceBar {
  const fillPct = confidence === null ? 0 : Math.round(clampUnit(confidence) * 1000) / 10;
  const thresholdPct = threshold === null ? null : Math.round(clampUnit(threshold) * 1000) / 10;
  const pass = confidence === null || threshold === null ? null : confidence >= threshold;
  return { fillPct, thresholdPct, pass };
}

/** Jaeger UI deep link to one span of a trace. */
export function jaegerTraceUrl(traceId: string, spanId: string): string {
  return `/jaeger/trace/${encodeURIComponent(traceId)}?uiFind=${encodeURIComponent(spanId)}`;
}

/**
 * Query Jaeger for the agent.decide spans that started at or after `startMs`
 * (agent clock; null = Jaeger's default window). Throws JaegerUnavailableError on
 * transport errors, non-2xx and on abort (the poller's per-request timeout) so the
 * caller can back off. The result also carries the agent.flag_state spans of the
 * same game traces (`flagStates`).
 */
export async function fetchDecideFeed(
  startMs: number | null,
  signal?: AbortSignal,
): Promise<DecideFeed> {
  const body = await searchTraces({ operation: DECIDE_OPERATION, limit: DECIDE_TRACE_LIMIT, startMs }, signal);
  return parseDecideTraces(body);
}
