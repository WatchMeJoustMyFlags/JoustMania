/**
 * Agent data source (issue #792).
 *
 * The "Agent" tab is the demo's observability focus screen: it shows what the
 * in-game agent is doing live — the decisions it evaluates, the effective flag
 * values it resolved, and the interventions it applied vs blocked.
 *
 * TRANSPORT — reuse the experiments-tab path, don't invent a new one
 * -----------------------------------------------------------------
 * The experiments tab (#981) established the dashboard's read-only telemetry
 * path: query the Loki HTTP API at `/loki/...` through envoy and fold the agent's
 * raw slog lines client-side. This module reuses that exact path for the decision
 * feed and the intervention "blocked" counts, and adds ONE more already-routed
 * datasource — VictoriaMetrics at `/victoria-metrics/...` (envoy.yaml) — for the
 * applied-intervention count, which is only emitted as a metric (see below), and
 * for the exact blocked tallies (BLOCKED_QUERY; the Loki fold is their fallback).
 *
 * LOKI LABEL CONTRACT — `service_name="agent"`
 * --------------------------------------------
 * Identical to ../experiments.ts: this view is empty unless the agent's container
 * logs arrive in Loki under the stream label `service_name="agent"`, produced by
 * the compose-label -> OTEL `service.name` -> Loki `service_name` chain documented
 * in services/otel-collector/config.yaml (`transform/docker_logs`). If that chain
 * changes the selector below silently returns zero streams and the view goes blank
 * with no error.
 *
 * WHAT THE AGENT EMITS (and where)
 * --------------------------------
 * The agent's full per-decision audit trail lives on OTEL *spans*
 * (agent.signal_received -> agent.decision -> agent.action, see
 * services/agent/decision/telemetry.go) — that is where decision.action /
 * decision.objective_served on the ALLOWED path are recorded. Those are NOT in
 * Loki. The agent DOES emit a useful subset as slog lines we can fold here:
 *
 *   agent.evaluate         one per evaluated cycle — carries the RESOLVED effective
 *                          flag values (decision_backend and the three policy
 *                          numbers). This is our flag-panel source.
 *   agent.decision_blocked blocked decision — intervention, target_serial, reason
 *                          (the BlockReason), decision_reason, allowed.
 *   agent.rollout_expand / agent.rollout_rollback(_recommended) — rollout activity.
 *
 * GAP (documented, not faked): the allowed-path decision detail (action +
 * objective_served) and the boolean agent.json flags (enabled,
 * experiments_enabled, experiment_dynamic_enabled, interventions_allowed list,
 * verdict_*) are span-only / flagd-only and not present in the agent's stdout
 * logs. The decision feed therefore renders the blocked decisions with full
 * detail and the allowed cycles as liveness, and deep-links to Jaeger
 * for the full per-decision audit. The flag panel surfaces the resolved values the
 * agent.evaluate line DOES carry, and links to Jaeger/Grafana for the rest. When a
 * flag-state read endpoint or a flagd evaluation route lands (#505), the flag panel
 * can be upgraded to the full effective set.
 */

import { parseLogfmt } from "./experiments.js";

const BASE_URL = import.meta.env.DEV ? "" : globalThis.location.origin;

/**
 * Loki LogQL selector for the agent's decision/flag activity log lines.
 *
 * `service_name="agent"` is a load-bearing contract — see the LOKI LABEL CONTRACT
 * note in the module header before changing it. The `|~` line filter narrows to
 * the agent.* messages this view folds, so a busy run's experiment.* lines (folded
 * by the experiments tab) don't crowd out the limit.
 */
const AGENT_LOG_QUERY =
  '{service_name="agent"} |~ "agent\\\\.(evaluate|decision_blocked|rollout_)"';

/** How far back to look for agent activity (decision feed window). */
const LOOKBACK_SECONDS = 10 * 60; // 10 minutes

/** Max log lines Loki returns per query. */
const QUERY_LIMIT = 2000;

/** Max decision-feed rows kept after folding (newest first). */
const MAX_FEED_ROWS = 80;

/**
 * VictoriaMetrics instant-query for the count of applied interventions, by result.
 *
 * `agent_intervention_writes_total{result="ok"|"error"}` is the ActionSink's
 * flag-file write counter (services/agent/actions/writer.go). It is the only
 * signal for the APPLIED count (the dispatched path is not logged to stdout). VM
 * receives the metric under the namespaced job (see MEMORY: prometheusremotewrite
 * maps service.name -> job, prefixed by service.namespace), so we sum across jobs
 * rather than pinning one. This is a best-effort secondary source: if VM is
 * unreachable the overlay still renders the Loki-derived blocked counts.
 */
const APPLIED_QUERY = 'sum(agent_intervention_writes_total) by (result)';

/**
 * VictoriaMetrics query for the blocked decisions of the feed window, by action
 * and reason (UC-040 A21, #1326). The agent logs a `rate_limit` refusal that
 * repeats while its budget is unchanged only at debug level (no
 * agent.decision_blocked line), so the Loki fold undercounts; the counter
 * `agent_decisions_total{blocked="true"}` counts every refusal. The overlay's
 * blocked tallies come from here and fall back to the Loki fold only when VM is
 * unreachable or has no samples.
 */
const BLOCKED_QUERY =
  `sum by (action, block_reason) (increase(agent_decisions_total{blocked="true"}[${LOOKBACK_SECONDS}s]))`;

/** One row in the rolling decision feed. */
export interface DecisionFeedRow {
  /** Epoch ms of the log line. */
  tsMs: number;
  /** "evaluate" | "blocked" | "rollout" — drives the row style. */
  kind: "evaluate" | "blocked" | "rollout";
  /** The raw agent.* message (e.g. "agent.decision_blocked"). */
  msg: string;
  /** Short human label for the signal/cycle. */
  signal: string;
  /** The action/intervention this row is about, when known ("" otherwise). */
  action: string;
  /** Whether this decision was blocked. */
  blocked: boolean;
  /** Block / rollout reason, when known. */
  reason: string;
  /** Game/session id this row belongs to, when known. */
  sessionId: string;
}

/** The resolved effective flag/policy values from the latest agent.evaluate line. */
export interface FlagPanel {
  /** Epoch ms of the agent.evaluate line these values came from. */
  tsMs: number;
  /** Requested decision.backend: "rules" | "claude" | "openai" | "jev". */
  decisionBackend: string;
  batteryThreshold: string;
  varianceWindow: string;
  maxPerMinute: string;
  gameMode: string;
}

/** A single action's applied/blocked tallies for the intervention overlay. */
export interface InterventionTally {
  action: string;
  blocked: number;
}

/** Folded intervention overlay state. */
export interface InterventionOverlay {
  /** Per-action blocked counts, folded from the Loki window (descending). */
  blockedByAction: InterventionTally[];
  /** Blocked counts grouped by reason (descending). */
  blockedByReason: { reason: string; count: number }[];
  /** Total blocked decisions in the window. */
  totalBlocked: number;
  /**
   * Applied-intervention count from VictoriaMetrics (writes result="ok"), or null
   * when VM was unreachable / had no samples. A counter total, not windowed.
   */
  appliedOk: number | null;
  /** Failed flag-file writes (writes result="error"), or null when unavailable. */
  appliedError: number | null;
}

/** The full folded state the Agent tab renders. */
export interface AgentView {
  feed: DecisionFeedRow[];
  flags: FlagPanel | null;
  overlay: InterventionOverlay;
}

interface LokiStreamResult {
  stream: Record<string, string>;
  values: [string, string][];
}

interface LokiQueryResponse {
  status: string;
  data: { resultType: string; result: LokiStreamResult[] };
}

export interface VmQueryResponse {
  status: string;
  data?: {
    resultType: string;
    result: { metric: Record<string, string>; value: [number, string] }[];
  };
}

/** Classify an agent.* message into a feed row kind (null = ignore). */
function classify(msg: string): DecisionFeedRow["kind"] | null {
  if (msg === "agent.evaluate") return "evaluate";
  if (msg === "agent.decision_blocked") return "blocked";
  if (msg.startsWith("agent.rollout_")) return "rollout";
  return null;
}

/**
 * Fold a batch of agent log lines into the decision feed, flag panel, and the
 * Loki-derived half of the intervention overlay.
 *
 * Pure and order-independent for the aggregates (blocked tallies are additive).
 * The feed itself is sorted newest-first and capped; the flag panel takes the
 * single newest agent.evaluate line.
 *
 * Each entry is { line, tsMs }.
 */
export function aggregateAgentLogs(
  lines: { line: string; tsMs: number }[],
): Omit<AgentView, "overlay"> & {
  overlay: Omit<InterventionOverlay, "appliedOk" | "appliedError">;
} {
  const feed: DecisionFeedRow[] = [];
  let flags: FlagPanel | null = null;
  const blockedByAction = new Map<string, number>();
  const blockedByReason = new Map<string, number>();
  let totalBlocked = 0;

  for (const { line, tsMs } of lines) {
    const fields = parseLogfmt(line);
    const msg = fields["msg"];
    if (!msg) continue;
    const kind = classify(msg);
    if (!kind) continue;

    const sessionId = fields["session_id"] ?? "";

    if (kind === "evaluate") {
      // Keep the newest agent.evaluate as the flag-panel snapshot.
      if (!flags || tsMs >= flags.tsMs) {
        flags = {
          tsMs,
          decisionBackend: fields["decision_backend"] ?? "",
          batteryThreshold: fields["battery_threshold"] ?? "",
          varianceWindow: fields["variance_window"] ?? "",
          maxPerMinute: fields["max_per_minute"] ?? "",
          gameMode: fields["game_mode"] ?? "",
        };
      }
      // agent.evaluate is a high-volume liveness line; don't flood the feed with
      // it. It feeds the flag panel only.
      continue;
    }

    let action = "";
    let reason = "";
    let blocked = false;

    if (kind === "blocked") {
      action = fields["intervention"] ?? "";
      reason = fields["reason"] ?? "";
      blocked = true;
      totalBlocked += 1;
      if (action) blockedByAction.set(action, (blockedByAction.get(action) ?? 0) + 1);
      if (reason) blockedByReason.set(reason, (blockedByReason.get(reason) ?? 0) + 1);
    } else if (kind === "rollout") {
      reason = fields["reason"] ?? "";
    }

    feed.push({
      tsMs,
      kind,
      msg,
      signal: feedSignal(msg),
      action,
      blocked,
      reason,
      sessionId,
    });
  }

  feed.sort((a, b) => b.tsMs - a.tsMs);

  return {
    feed: feed.slice(0, MAX_FEED_ROWS),
    flags,
    overlay: {
      blockedByAction: toSortedTallies(blockedByAction),
      blockedByReason: toSortedReasons(blockedByReason),
      totalBlocked,
    },
  };
}

/** Short, readable signal label for a feed row. */
function feedSignal(msg: string): string {
  switch (msg) {
    case "agent.decision_blocked":
      return "decision blocked";
    case "agent.rollout_expand":
      return "rollout expand";
    case "agent.rollout_rollback":
      return "rollout rollback";
    case "agent.rollout_rollback_recommended":
      return "rollback recommended";
    default:
      return msg.replace(/^agent\./, "").replace(/_/g, " ");
  }
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
 * Sums across all `result` series (the metric may arrive under several namespaced
 * jobs). Returns nulls when the response is empty/malformed so the overlay can show
 * "unavailable" without throwing.
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
type BlockedTallies = Pick<InterventionOverlay, "blockedByAction" | "blockedByReason" | "totalBlocked">;

/**
 * Parse the BLOCKED_QUERY response into the overlay's blocked tallies. `increase`
 * extrapolates, so each series is rounded to a whole count. Returns null when the
 * response is empty or malformed, so the caller keeps the Loki fold.
 */
export function parseBlockedCounts(body: VmQueryResponse): BlockedTallies | null {
  const results = body.data?.result;
  if (!results || results.length === 0) return null;
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

/** Best-effort VM read of the blocked tallies; null (never throws) on any failure. */
async function fetchBlockedCounts(): Promise<BlockedTallies | null> {
  try {
    const params = new URLSearchParams({ query: BLOCKED_QUERY });
    const url = `${BASE_URL}/victoria-metrics/api/v1/query?${params.toString()}`;
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) return null;
    return parseBlockedCounts((await response.json()) as VmQueryResponse);
  } catch {
    return null;
  }
}

/** Query Loki for the agent's recent decision/flag log lines. */
async function fetchAgentLogs(): Promise<{ line: string; tsMs: number }[]> {
  const endNs = BigInt(Date.now()) * 1_000_000n;
  const startNs = endNs - BigInt(LOOKBACK_SECONDS) * 1_000_000_000n;

  const params = new URLSearchParams({
    query: AGENT_LOG_QUERY,
    start: startNs.toString(),
    end: endNs.toString(),
    limit: String(QUERY_LIMIT),
    // backward = newest-first: the decision feed is a rolling tail, so when the
    // window is truncated we want the most RECENT lines, not the oldest. (The
    // experiments tab uses forward because its aggregates need the complete
    // lifecycle prefix; this view's feed is the opposite need.)
    direction: "backward",
  });

  const url = `${BASE_URL}/loki/loki/api/v1/query_range?${params.toString()}`;
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`Loki query failed: ${response.status} ${response.statusText}`);
  }

  const body = (await response.json()) as LokiQueryResponse;
  const lines: { line: string; tsMs: number }[] = [];
  for (const stream of body.data?.result ?? []) {
    for (const [tsNs, line] of stream.values) {
      const tsMs = Number(BigInt(tsNs) / 1_000_000n);
      lines.push({ line, tsMs });
    }
  }
  return lines;
}

/**
 * Best-effort query of VictoriaMetrics for the applied-intervention counter.
 * Returns nulls (never throws) on any failure so it can't break the Loki-backed
 * panels — the overlay degrades to showing only blocked counts.
 */
async function fetchAppliedCounts(): Promise<{
  appliedOk: number | null;
  appliedError: number | null;
}> {
  try {
    const params = new URLSearchParams({ query: APPLIED_QUERY });
    const url = `${BASE_URL}/victoria-metrics/api/v1/query?${params.toString()}`;
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) return { appliedOk: null, appliedError: null };
    const body = (await response.json()) as VmQueryResponse;
    return parseAppliedCounts(body);
  } catch {
    return { appliedOk: null, appliedError: null };
  }
}

/**
 * Fetch and fold the full Agent view. Throws only if the PRIMARY (Loki) source is
 * unreachable; the VictoriaMetrics applied-count is best-effort and never throws.
 */
export async function fetchAgentView(): Promise<AgentView> {
  const [lines, applied, blocked] = await Promise.all([
    fetchAgentLogs(),
    fetchAppliedCounts(),
    fetchBlockedCounts(),
  ]);
  const folded = aggregateAgentLogs(lines);
  return {
    feed: folded.feed,
    flags: folded.flags,
    overlay: {
      ...folded.overlay,
      // Exact counter tallies when VM answers (UC-040 A21); else the Loki fold.
      ...(blocked ?? {}),
      appliedOk: applied.appliedOk,
      appliedError: applied.appliedError,
    },
  };
}

/* ======================================================================== */
/* agent.decide feed (#1233, epic #1225)                                    */
/* ======================================================================== */
/*
 * The decide contract (#1226) records every decision on an `agent.decide` span:
 * which backend answered (and which one the decision.backend flag requested),
 * the model, the answers + confidence, the confidence-threshold verdict, the
 * outcome and the model's one-line rationale. None of that is in Loki, so this
 * feed reads the spans straight from Jaeger's HTTP query API, which envoy already
 * routes at /jaeger/* (the same Jaeger the deep links open).
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
  action: string;
  blocked: boolean;
  reason: string;
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
}

/* --- Jaeger HTTP API (/api/traces) response shape (the subset we read) --- */

export interface JaegerTag {
  key: string;
  type?: string;
  value: unknown;
}

export interface JaegerReference {
  refType: string;
  traceID: string;
  spanID: string;
}

export interface JaegerSpan {
  traceID: string;
  spanID: string;
  operationName: string;
  references?: JaegerReference[];
  /** Microseconds since epoch. */
  startTime: number;
  /** Microseconds. */
  duration: number;
  tags?: JaegerTag[];
}

export interface JaegerTrace {
  traceID: string;
  spans: JaegerSpan[];
}

export interface JaegerTracesResponse {
  data?: JaegerTrace[] | null;
}

function tagMap(span: JaegerSpan): Map<string, unknown> {
  const m = new Map<string, unknown>();
  for (const t of span.tags ?? []) m.set(t.key, t.value);
  return m;
}

function tagString(tags: Map<string, unknown>, key: string): string {
  const v = tags.get(key);
  if (v === undefined || v === null) return "";
  return String(v);
}

function tagNumber(tags: Map<string, unknown>, key: string): number | null {
  const v = tags.get(key);
  if (v === undefined || v === null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function tagBool(tags: Map<string, unknown>, key: string): boolean {
  const v = tags.get(key);
  return v === true || v === "true";
}

/** The CHILD_OF parent span id ("" for a root). */
function parentSpanId(span: JaegerSpan): string {
  const ref = (span.references ?? []).find((r) => r.refType === "CHILD_OF");
  return ref?.spanID ?? "";
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
  const tags = tagMap(span);
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
      const ct = tagMap(c);
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
    tsMs: Math.floor(span.startTime / 1000),
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
        const dt = tagMap(d);
        return {
          spanId: d.spanID,
          action: tagString(dt, "decision.action"),
          blocked: tagBool(dt, "decision.blocked"),
          reason: tagString(dt, "decision.reason"),
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
  return { rows: capped, current: currentFrom(rows) };
}

/** Shadow games are the experiment loop's headless games (game.kind="shadow"). */
export function isShadowGame(kind: string): boolean {
  return kind === "shadow";
}

/**
 * What the dashboard follows (UC-070 BR-005, follow.ts): whether a real game is
 * live (null = unknown) and the followed shadow game ("" = none).
 */
export interface DecideFollow {
  realGameLive: boolean | null;
  followGameId: string;
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
  if (rows.length === 0) return null;
  const followed = follow?.followGameId ? rows.find((r) => r.gameId === follow.followGameId) : undefined;
  // No real game live and nothing followed with rows: the newest row of any kind.
  const newestOverall = follow?.realGameLive === false ? rows[0] : undefined;
  const anchor = followed ?? newestOverall ?? rows.find((r) => !isShadowGame(r.gameKind)) ?? rows[0];
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
  return newestSeenMs === null ? null : newestSeenMs - DECIDE_OVERLAP_MS;
}

/** Query string for the decide search. Never contains browser-clock times. */
export function decideQueryParams(startMs: number | null): URLSearchParams {
  const params = new URLSearchParams({
    service: "agent",
    operation: DECIDE_OPERATION,
    limit: String(DECIDE_TRACE_LIMIT),
  });
  if (startMs !== null) params.set("start", String(Math.floor(startMs) * 1000));
  return params;
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
 * (agent clock; null = Jaeger's default window). Throws on transport errors and
 * on abort (the poller's per-request timeout) so the caller can back off. Polled
 * separately from the Loki view so a Loki outage never hides the decide feed.
 */
export async function fetchDecideFeed(
  startMs: number | null,
  signal?: AbortSignal,
): Promise<DecideFeed> {
  const url = `${BASE_URL}/jaeger/api/traces?${decideQueryParams(startMs).toString()}`;
  const response = await fetch(url, { headers: { Accept: "application/json" }, signal });
  if (!response.ok) {
    throw new Error(`Jaeger query failed: ${response.status} ${response.statusText}`);
  }
  return parseDecideTraces((await response.json()) as JaegerTracesResponse);
}
