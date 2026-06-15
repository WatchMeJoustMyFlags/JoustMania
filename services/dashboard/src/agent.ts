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
 * applied-intervention count, which is only emitted as a metric (see below).
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
 *                          flag values (mode, model, prompt_variant, and the three
 *                          policy numbers). This is our flag-panel source.
 *   agent.decision_blocked blocked decision — intervention, target_serial, reason
 *                          (the BlockReason), decision_reason, allowed.
 *   agent.llm.discarded    async LLM result dropped at apply time — reason, tier.
 *   agent.rollout_expand / agent.rollout_rollback(_recommended) — rollout activity.
 *
 * GAP (documented, not faked): the allowed-path decision detail (action +
 * objective_served) and the boolean agent.json flags (enabled,
 * experiments_enabled, experiment_dynamic_enabled, interventions_allowed list,
 * verdict_*) are span-only / flagd-only and not present in the agent's stdout
 * logs. The decision feed therefore renders the blocked decisions and discards
 * with full detail and the allowed cycles as liveness, and deep-links to Jaeger
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
  '{service_name="agent"} |~ "agent\\\\.(evaluate|decision_blocked|llm\\\\.discarded|rollout_)"';

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

/** One row in the rolling decision feed. */
export interface DecisionFeedRow {
  /** Epoch ms of the log line. */
  tsMs: number;
  /** "evaluate" | "blocked" | "discarded" | "rollout" — drives the row style. */
  kind: "evaluate" | "blocked" | "discarded" | "rollout";
  /** The raw agent.* message (e.g. "agent.decision_blocked"). */
  msg: string;
  /** Short human label for the signal/cycle. */
  signal: string;
  /** The action/intervention this row is about, when known ("" otherwise). */
  action: string;
  /** Whether this decision was blocked. */
  blocked: boolean;
  /** Block/discard reason, when known. */
  reason: string;
  /** Game/session id this row belongs to, when known. */
  sessionId: string;
}

/** The resolved effective flag/policy values from the latest agent.evaluate line. */
export interface FlagPanel {
  /** Epoch ms of the agent.evaluate line these values came from. */
  tsMs: number;
  /** Inference mode: "rules" | "llm". */
  mode: string;
  model: string;
  promptVariant: string;
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
  if (msg === "agent.llm.discarded") return "discarded";
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
          mode: fields["mode"] ?? "",
          model: fields["model"] ?? "",
          promptVariant: fields["prompt_variant"] ?? "",
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
    } else if (kind === "discarded") {
      // Async LLM result dropped at apply time — a different kind of "not applied".
      reason = fields["reason"] ?? "";
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
    case "agent.llm.discarded":
      return "llm result discarded";
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
  const [lines, applied] = await Promise.all([
    fetchAgentLogs(),
    fetchAppliedCounts(),
  ]);
  const folded = aggregateAgentLogs(lines);
  return {
    feed: folded.feed,
    flags: folded.flags,
    overlay: {
      ...folded.overlay,
      appliedOk: applied.appliedOk,
      appliedError: applied.appliedError,
    },
  };
}
