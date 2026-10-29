/** Intervention overlay: the VictoriaMetrics half of the Agent tab (applied and blocked tallies). Split out of agent.ts (#2388). */

import { vmQuery } from "./vm.js";
import type { VmQueryResponse } from "./vm.js";

/** Blocked-tally window (UC-040 A21). */
const LOOKBACK_SECONDS = 10 * 60; // 10 minutes


/**
 * VictoriaMetrics query for the interventions written in the window, by result.
 *
 * `agent_intervention_writes_total{result="ok"|"error"}` is the ActionSink's
 * flag-file write counter (services/agent/actions/writer.go). VM receives the
 * metric under the namespaced job, so we sum across jobs rather than pinning one.
 * Same window as BLOCKED_QUERY (#2288), not the counter's lifetime. Grouped by
 * game_kind so real and shadow writes are counted apart (#2294).
 * Best effort: when VM is unreachable the overlay shows `n/a`.
 */
const APPLIED_QUERY = `sum by (result, game_kind) (increase(agent_intervention_writes_total[${LOOKBACK_SECONDS}s]))`;

/**
 * The agent counters carry `game_kind` in the telemetry vocabulary (#2294,
 * telemetry.md): `primary` is a real, player-facing game, `shadow` a parallel
 * evaluation game. A series without the label comes from an agent image that
 * predates it and counts as real, as the panel did before the split.
 */
export function isShadowKind(gameKind: string | undefined): boolean {
  return gameKind === "shadow";
}

/**
 * VictoriaMetrics query for the blocked decisions of the window, by action and
 * reason (UC-040 A21, #1326): `agent_decisions_total{blocked="true"}` counts every
 * refusal, including the repeats that emit no agent.decision span. `already_applied`
 * is left out: the value was already in place, not a refusal (#2046, #2288).
 */
const BLOCKED_QUERY =
  `sum by (action, block_reason, game_kind) (increase(agent_decisions_total{blocked="true",block_reason!="already_applied"}[${LOOKBACK_SECONDS}s]))`;

/** A single action's blocked tally for the intervention overlay. */
export interface InterventionTally {
  action: string;
  blocked: number;
}

/** Folded intervention overlay state. */
export interface InterventionOverlay {
  /** Per-action blocked counts of real games from VictoriaMetrics (descending). */
  blockedByAction: InterventionTally[];
  /** Blocked counts grouped by reason (descending). */
  blockedByReason: { reason: string; count: number }[];
  /** Total blocked decisions of real games in the window; null when VictoriaMetrics is unreachable. */
  totalBlocked: number | null;
  /** Total blocked decisions of shadow games in the window; null when VictoriaMetrics is unreachable. */
  shadowBlocked: number | null;
  /** Applied writes (result="ok"), null when VM was unreachable (0 when it had no samples). */
  appliedOk: number | null;
  /** Failed flag-file writes (result="error"), or null when unavailable. */
  appliedError: number | null;
  /** Applied writes of shadow games (game_kind="shadow"), null when VM was unreachable. */
  shadowAppliedOk: number | null;
  /** Failed writes of shadow games, or null when unavailable. */
  shadowAppliedError: number | null;
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

/** The applied/error write counts, real and shadow games apart (#2294). */
export type AppliedCounts = Pick<
  InterventionOverlay,
  "appliedOk" | "appliedError" | "shadowAppliedOk" | "shadowAppliedError"
>;

/** VictoriaMetrics down or no samples: every applied count is n/a. */
export const APPLIED_UNAVAILABLE: AppliedCounts = {
  appliedOk: null,
  appliedError: null,
  shadowAppliedOk: null,
  shadowAppliedError: null,
};

/**
 * Parse a VictoriaMetrics instant-query response into the applied/error counts.
 * Sums across all `result` series. A successful empty answer is zero (no writes in
 * the window), not unavailable; only a malformed body returns nulls ("n/a").
 */
export function parseAppliedCounts(body: VmQueryResponse): AppliedCounts {
  const results = body.data?.result;
  if (body.status !== "success" || !results) {
    return APPLIED_UNAVAILABLE;
  }
  // The query answered, so a missing outcome (or an empty answer) is zero, not n/a.
  const out = { appliedOk: 0, appliedError: 0, shadowAppliedOk: 0, shadowAppliedError: 0 };
  for (const series of results) {
    const result = series.metric["result"];
    const v = Math.round(Number.parseFloat(series.value?.[1] ?? ""));
    if (!Number.isFinite(v)) continue;
    const shadow = isShadowKind(series.metric["game_kind"]);
    if (result === "ok") out[shadow ? "shadowAppliedOk" : "appliedOk"] += v;
    else if (result === "error") out[shadow ? "shadowAppliedError" : "appliedError"] += v;
  }
  return out;
}

/** The blocked tallies of the intervention overlay. */
export type BlockedTallies = Pick<InterventionOverlay, "blockedByAction" | "blockedByReason" | "totalBlocked" | "shadowBlocked">;

/** VictoriaMetrics down: every blocked tally is n/a (UC-035 A3, UC-040 A21). */
export const BLOCKED_UNAVAILABLE: BlockedTallies = { blockedByAction: [], blockedByReason: [], totalBlocked: null, shadowBlocked: null };

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
  let shadowTotal = 0;
  for (const series of results) {
    const n = Math.round(Number.parseFloat(series.value?.[1] ?? ""));
    if (!Number.isFinite(n) || n <= 0) continue;
    if (isShadowKind(series.metric["game_kind"])) {
      shadowTotal += n;
      continue;
    }
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
    shadowBlocked: shadowTotal,
  };
}

/** The VictoriaMetrics half of the overlay. Never throws: VM down = all n/a. */
export async function fetchInterventionCounts(signal?: AbortSignal): Promise<InterventionOverlay> {
  const [applied, blocked] = await Promise.all([vmQuery(APPLIED_QUERY, signal), vmQuery(BLOCKED_QUERY, signal)]);
  return {
    ...(blocked ? parseBlockedCounts(blocked) : BLOCKED_UNAVAILABLE),
    ...(applied ? parseAppliedCounts(applied) : APPLIED_UNAVAILABLE),
  };
}
