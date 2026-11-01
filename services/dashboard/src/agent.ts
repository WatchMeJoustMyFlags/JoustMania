
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

import type { DecisionFeedRow } from "./agentActivity.js";
import type { FlagPanel } from "./agentFlags.js";
import type { InterventionOverlay } from "./agentOverlay.js";

// The data source is split by panel (#2388); this barrel keeps `./agent.js` the one import path.
export * from "./agentActivity.js";
export * from "./agentAnchor.js";
export * from "./agentDecide.js";
export * from "./agentFlags.js";
export * from "./agentGoal.js";
export * from "./agentOverlay.js";
export type { VmQueryResponse } from "./vm.js";
export type { JaegerSpan, JaegerTag, JaegerTrace, JaegerTracesResponse, JaegerReference } from "./jaeger.js";

/** The full folded state the Agent tab renders. */
export interface AgentView {
  feed: DecisionFeedRow[];
  flags: FlagPanel | null;
  overlay: InterventionOverlay;
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
