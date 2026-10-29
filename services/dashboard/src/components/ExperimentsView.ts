/**
 * Experiments View Component (issue #981).
 *
 * Renders one card per cohort experiment with its arms, per-arm sample counts,
 * the fitness delta (experimental − control), and the current verdict. Data
 * comes from the newest experiment.progress span per experiment in Jaeger (see
 * ../experiments.ts, #2059); the view polls to update live during a shadow run.
 */
import { renderFreshness } from "../freshness.js";
import { escapeHtml } from "../html.js";
import type { ArmAggregate, ExperimentView, Recommendation } from "../experiments.js";
import { EXPERIMENT_PROGRESS_NO_DATA } from "../degrade.js";

/**
 * After this many ms without a successful poll, a kept-on-screen view is flagged
 * "stale" so an operator isn't fooled by frozen numbers. Set comfortably above
 * the poll cadence (5s) so a single missed tick doesn't cry wolf.
 */
const STALE_AFTER_MS = 15_000;

export class ExperimentsView {
  private readonly container: HTMLElement;
  private readonly freshnessEl: HTMLElement | null;
  private hasRendered = false;
  /** Epoch ms of the last SUCCESSFUL render (null until the first one). */
  private lastUpdatedMs: number | null = null;

  constructor(containerId: string, freshnessId?: string) {
    const container = document.getElementById(containerId);
    if (!container) {
      throw new Error(`Container element not found: ${containerId}`);
    }
    this.container = container;
    this.freshnessEl = freshnessId ? document.getElementById(freshnessId) : null;
  }

  setError(message: string) {
    this.container.innerHTML = `<div class="loading">${escapeHtml(message)}</div>`;
    this.hasRendered = true;
  }

  /**
   * Mark the data as potentially stale (a poll just failed but we're keeping the
   * last good render on screen). The freshness hint updates to "stale" once the
   * last good update is older than STALE_AFTER_MS.
   */
  markStale() {
    this.updateFreshness();
  }

  /** Refresh the "last updated Ns ago / stale" hint from the current clock. */
  updateFreshness() {
    const el = this.freshnessEl;
    if (!el || this.lastUpdatedMs === null) return;
    renderFreshness(el, this.lastUpdatedMs, STALE_AFTER_MS);
  }

  render(experiments: ExperimentView[]) {
    this.hasRendered = true;
    this.lastUpdatedMs = performance.now();
    this.updateFreshness();

    if (experiments.length === 0) {
      this.container.innerHTML = `<div class="loading">${escapeHtml(EXPERIMENT_PROGRESS_NO_DATA)}</div>`;
      return;
    }

    this.container.innerHTML = experiments
      .map((exp) => this.renderCard(exp))
      .join("");
  }

  /** True once any render/error has painted (so we don't flash the loader). */
  get rendered(): boolean {
    return this.hasRendered;
  }

  private renderCard(exp: ExperimentView): string {
    const verdict = normalizeVerdict(exp.verdict, exp.live, exp.status);
    const armsHtml = exp.arms.map((arm) => this.renderArm(arm)).join("");
    const deltaHtml = this.renderDelta(exp.delta);

    return `
      <div class="experiment-card">
        <div class="experiment-header">
          <span class="experiment-id" title="${escapeHtml(exp.experimentId)}">
            ${escapeHtml(exp.experimentId)}
          </span>
          <span class="experiment-verdict ${verdict.cssClass}">${escapeHtml(verdict.label)}</span>
        </div>
        ${renderTested(exp)}
        <div class="experiment-arms">
          ${armsHtml}
        </div>
        <div class="experiment-footer">
          ${deltaHtml}
          ${exp.recommendation ? `<div class="experiment-recommendation">${escapeHtml(recommendationText(exp.recommendation))}</div>` : ""}
          ${exp.reason ? `<div class="experiment-reason" title="${escapeHtml(exp.reason)}">${escapeHtml(endedText(exp))}</div>` : ""}
          ${renderTraceLink(exp)}
        </div>
      </div>
    `;
  }

  private renderArm(arm: ArmAggregate): string {
    const inFlight = Math.max(0, arm.inFlight);
    return `
      <div class="experiment-arm">
        <div class="arm-name">${escapeHtml(prettyArm(arm.arm))}</div>
        <div class="arm-stats">
          <span class="arm-count" title="games concluded with a fitness sample">
            n=${arm.sampleCount}
          </span>
          <span class="arm-mean" title="mean fitness across this arm's samples">
            x&#772;=${arm.sampleCount > 0 ? arm.meanFitness.toFixed(3) : "&mdash;"}
          </span>
          ${inFlight > 0 ? `<span class="arm-inflight" title="games assigned but not yet concluded">+${inFlight} in&nbsp;flight</span>` : ""}
        </div>
      </div>
    `;
  }

  private renderDelta(delta: number | null): string {
    if (delta === null) {
      return `<div class="experiment-delta delta-neutral">&Delta; &mdash; <span class="delta-label">(awaiting both arms)</span></div>`;
    }
    const sign = delta > 0 ? "+" : "";
    const cssClass =
      delta > 0 ? "delta-positive" : delta < 0 ? "delta-negative" : "delta-neutral";
    return `<div class="experiment-delta ${cssClass}">&Delta; ${sign}${delta.toFixed(3)} <span class="delta-label">(experimental &minus; control fitness)</span></div>`;
  }
}

/** What is tested (#2291): the flag, the experimental value and the objective, then the thesis. */
export function testedText(exp: ExperimentView): string {
  const parts: string[] = [];
  if (exp.flagKey) parts.push(exp.experimentalValue ? `${exp.flagKey} = ${exp.experimentalValue}` : exp.flagKey);
  if (exp.objective) parts.push(`objective: ${exp.objective}`);
  return parts.join(" · ");
}

/** How it ended (#2291): the status and the end reason, e.g. "aborted: operator stop". */
export function endedText(exp: ExperimentView): string {
  return exp.reason ? `${exp.status || "ended"}: ${exp.reason}` : "";
}

function renderTested(exp: ExperimentView): string {
  const tested = testedText(exp);
  if (!tested && !exp.thesis) return "";
  return `<div class="experiment-tested">
          ${tested ? `<div class="experiment-flag" title="${escapeHtml(tested)}">${escapeHtml(tested)}</div>` : ""}
          ${exp.thesis ? `<div class="experiment-thesis" title="${escapeHtml(exp.thesis)}">${escapeHtml(exp.thesis)}</div>` : ""}
        </div>`;
}

/** Link to the snapshot's trace in the embedded Jaeger (same host, UC-035 BR-001). */
function renderTraceLink(exp: ExperimentView): string {
  if (!exp.traceId) return "";
  return `<a class="experiment-trace" href="/jaeger/trace/${encodeURIComponent(exp.traceId)}" target="_blank" rel="noopener">trace</a>`;
}

/** UC-035 step 4 / UC-043 A19 (#2276): the refused promotion as a recommendation. */
export function recommendationText(rec: Recommendation): string {
  return `would promote ${rec.flag}=${rec.value}; real value owned by ${rec.owner}`;
}

function prettyArm(arm: string): string {
  if (arm === "experimental") return "Experimental";
  if (arm === "control") return "Control";
  return arm;
}

export interface NormalizedVerdict {
  label: string;
  cssClass: string;
}

/**
 * The card's verdict label. A status of `done` is NOT a promotion: the span does
 * not say whether the dossier decision was promoted, reverted or deferred (#2291),
 * so a `done` without a promote outcome reads "Done".
 */
export function normalizeVerdict(verdict: string, live: boolean, status = ""): NormalizedVerdict {
  const v = verdict.toLowerCase();
  if (v === "done") {
    return { label: "Done", cssClass: "verdict-inconclusive" };
  }
  if (v === "promote" || v === "promoted") {
    return { label: "Promote", cssClass: "verdict-promote" };
  }
  if (v === "discard" || v === "discarded" || v === "rejected") {
    return { label: "Discard", cssClass: "verdict-discard" };
  }
  if (v === "inconclusive") {
    return { label: "Inconclusive", cssClass: "verdict-inconclusive" };
  }
  if (!live) {
    return { label: verdict || status || "Concluded", cssClass: "verdict-inconclusive" };
  }
  return { label: "Running", cssClass: "verdict-running" };
}
