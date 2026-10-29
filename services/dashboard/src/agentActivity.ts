/** Activity feed: blocked decisions plus rollout rows. Split out of agent.ts (#2388). */

import { incrementalStartMs, searchTraces, spanStartMs, spanTags, spansNamed, tagString } from "./jaeger.js";
import type { JaegerSpan, JaegerTracesResponse } from "./jaeger.js";
import { decisionTargetLabel } from "./agentDecide.js";
import type { DecideRow } from "./agentDecide.js";

/** Max activity-feed rows (newest first). */
const MAX_FEED_ROWS = 80;

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
