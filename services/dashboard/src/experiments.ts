/**
 * Experiments data source (issue #981; spans since #2059).
 *
 * The agent's experiment registry emits an `experiment.progress` span after every
 * state change (started, game assigned / concluded / released, concluded,
 * promoting, terminal, rehydrate) and as a 60 s heartbeat per live experiment
 * (docs/specs/telemetry.md §4.1). Each span is its OWN one-span trace (never a
 * child of `agent.experiment`, whose trace holds every shadow game), so a search
 * stays small. Each carries the registry snapshot: status, flag, verdict once
 * there is one, the end reason on `terminal`, and one `experiment.arm` event per
 * arm with `experiment.arm_stats.assigned` (games in flight), `.samples` and
 * `.mean_fitness` (the journal summary).
 *
 * The view keeps the NEWEST snapshot per `experiment.id` across polls
 * (`ExperimentStore`): a cold start searches the newest EXPERIMENT_COLD_LIMIT
 * traces with no `start`; later polls start at the newest span time already seen
 * minus EXPERIMENT_OVERLAP_MS (agent clock, never the browser's: see jaeger.ts).
 *
 * An agent image older than #2059 emits no experiment.progress: the tab then shows
 * a hint, never an error.
 */

import { incrementalStartMs, searchTraces, spanEvents, spanStartMs, spanTags, spansNamed, tagNumber, tagString } from "./jaeger.js";
import type { JaegerSpan, JaegerTracesResponse } from "./jaeger.js";

/** Jaeger operation of the registry snapshot span. */
export const EXPERIMENT_OPERATION = "experiment.progress";

/** Span event per arm on experiment.progress. */
export const ARM_EVENT = "experiment.arm";

/** Traces (= snapshots) of the cold-start search. */
export const EXPERIMENT_COLD_LIMIT = 200;

/** Traces per incremental search. */
export const EXPERIMENT_INCREMENTAL_LIMIT = 200;

/** Overlap behind the newest snapshot already held. */
export const EXPERIMENT_OVERLAP_MS = 30 * 1000;

/** Finished experiments kept on screen (the newest ones; maintainer decision D6). */
export const MAX_TERMINAL_EXPERIMENTS = 10;

/** Terminal registry statuses (services/agent/experiment/registry.go IsTerminal). */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(["done", "discarded", "aborted"]);

/** Statuses that still accrue games: the verdict is not final yet. */
const LIVE_STATUSES: ReadonlySet<string> = new Set(["proposed", "running", ""]);

/** Per-arm values of the newest snapshot. */
export interface ArmAggregate {
  arm: string;
  /** Games the journal folded for this arm. */
  sampleCount: number;
  /** Mean fitness across the folded games (0 when none). */
  meanFitness: number;
  /** Games bound to the arm and still in flight (point in time). */
  inFlight: number;
}

/** One experiment, from its newest experiment.progress snapshot. */
export interface ExperimentView {
  experimentId: string;
  /** experiment.status of the snapshot. */
  status: string;
  arms: ArmAggregate[];
  /** experimental mean minus control mean when both arms have samples, else null. */
  delta: number | null;
  /** The verdict label source: see resolveVerdict. */
  verdict: string;
  /** experiment.end_reason (terminal snapshot only). */
  reason: string;
  /** Span time of the snapshot (agent clock). */
  lastUpdatedMs: number;
  /** True while the experiment is proposed or running. */
  live: boolean;
  traceId: string;
  spanId: string;
}

/** The two canonical arm names (mirror journal.ArmExperimental / ArmControl). */
const ARM_EXPERIMENTAL = "experimental";
const ARM_CONTROL = "control";

function armRank(arm: string): number {
  if (arm === ARM_EXPERIMENTAL) return 0;
  if (arm === ARM_CONTROL) return 1;
  return 2;
}

/**
 * The rendered verdict, with the precedence the Loki fold had: the statistical
 * verdict (`verdict.outcome`) once the experiment stopped accruing games, else
 * its lifecycle status (`experiment.status`, e.g. aborted without a verdict),
 * else "running". While the experiment is proposed or running a `verdict.outcome`
 * is only the interim verdict, so it shows as "running".
 */
export function resolveVerdict(outcome: string, status: string): string {
  if (LIVE_STATUSES.has(status)) return "running";
  return outcome || status || "running";
}

/** Fold one experiment.progress span. */
export function experimentFrom(span: JaegerSpan): ExperimentView {
  const tags = spanTags(span);
  const status = tagString(tags, "experiment.status");
  const arms: ArmAggregate[] = spanEvents(span, ARM_EVENT)
    .map((ev) => ({
      arm: tagString(ev, "experiment.arm"),
      sampleCount: tagNumber(ev, "experiment.arm_stats.samples") ?? 0,
      meanFitness: tagNumber(ev, "experiment.arm_stats.mean_fitness") ?? 0,
      inFlight: tagNumber(ev, "experiment.arm_stats.assigned") ?? 0,
    }))
    .filter((a) => a.arm !== "")
    .sort((x, y) => armRank(x.arm) - armRank(y.arm) || x.arm.localeCompare(y.arm));

  // Δ from the arm means (the Loki-era parity), not verdict.delta (the paired delta).
  const experimental = arms.find((a) => a.arm === ARM_EXPERIMENTAL);
  const control = arms.find((a) => a.arm === ARM_CONTROL);
  const delta =
    experimental && control && experimental.sampleCount > 0 && control.sampleCount > 0
      ? experimental.meanFitness - control.meanFitness
      : null;

  return {
    experimentId: tagString(tags, "experiment.id"),
    status,
    arms,
    delta,
    verdict: resolveVerdict(tagString(tags, "verdict.outcome"), status),
    reason: tagString(tags, "experiment.end_reason"),
    lastUpdatedMs: spanStartMs(span),
    live: LIVE_STATUSES.has(status),
    traceId: span.traceID,
    spanId: span.spanID,
  };
}

/** Every experiment.progress snapshot of a Jaeger response, folded. */
export function parseExperimentSnapshots(body: JaegerTracesResponse): ExperimentView[] {
  return spansNamed(body, EXPERIMENT_OPERATION)
    .map(experimentFrom)
    .filter((e) => e.experimentId !== "");
}

function newestFirst(a: ExperimentView, b: ExperimentView): number {
  return b.lastUpdatedMs - a.lastUpdatedMs || b.spanId.localeCompare(a.spanId);
}

/**
 * The newest snapshot per experiment across polls. Live (non-terminal)
 * experiments are all kept; finished ones only the newest `maxTerminal` by
 * snapshot time (agent clock).
 */
export class ExperimentStore {
  private readonly byId = new Map<string, ExperimentView>();
  private newestMs: number | null = null;

  constructor(private readonly maxTerminal = MAX_TERMINAL_EXPERIMENTS) {}

  merge(snapshots: ExperimentView[]): void {
    for (const snap of snapshots) {
      if (this.newestMs === null || snap.lastUpdatedMs > this.newestMs) this.newestMs = snap.lastUpdatedMs;
      const held = this.byId.get(snap.experimentId);
      if (!held || newestFirst(snap, held) < 0) this.byId.set(snap.experimentId, snap);
    }
    const terminal = Array.from(this.byId.values())
      .filter((e) => TERMINAL_STATUSES.has(e.status))
      .sort(newestFirst);
    for (const old of terminal.slice(this.maxTerminal)) this.byId.delete(old.experimentId);
  }

  /** Next search start (agent clock); null before the first snapshot (cold start). */
  queryStartMs(): number | null {
    return incrementalStartMs(this.newestMs, EXPERIMENT_OVERLAP_MS);
  }

  get size(): number {
    return this.byId.size;
  }

  list(): ExperimentView[] {
    return Array.from(this.byId.values()).sort(newestFirst);
  }
}

/**
 * One poll: search experiment.progress from the store's window and merge it.
 * Throws JaegerUnavailableError when Jaeger is down so the caller can back off.
 */
export async function pollExperiments(store: ExperimentStore, signal?: AbortSignal): Promise<ExperimentView[]> {
  const startMs = store.queryStartMs();
  const body = await searchTraces(
    {
      operation: EXPERIMENT_OPERATION,
      limit: startMs === null ? EXPERIMENT_COLD_LIMIT : EXPERIMENT_INCREMENTAL_LIMIT,
      startMs,
    },
    signal,
  );
  store.merge(parseExperimentSnapshots(body));
  return store.list();
}

/** A one-shot read with a fresh store (cold start). */
export async function fetchExperiments(signal?: AbortSignal): Promise<ExperimentView[]> {
  return pollExperiments(new ExperimentStore(), signal);
}
