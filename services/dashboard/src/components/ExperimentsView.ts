/**
 * Experiments View Component (issue #981).
 *
 * Renders one card per cohort experiment with its arms, per-arm sample counts,
 * the fitness delta (experimental − control), and the current verdict. Data
 * comes from the agent's experiment telemetry in Loki (see ../experiments.ts);
 * the view polls on a fixed cadence to update live during a shadow run.
 */
import type { ArmAggregate, ExperimentView } from "../experiments.js";

export class ExperimentsView {
  private readonly container: HTMLElement;
  private hasRendered = false;

  constructor(containerId: string) {
    const container = document.getElementById(containerId);
    if (!container) {
      throw new Error(`Container element not found: ${containerId}`);
    }
    this.container = container;
  }

  setError(message: string) {
    this.container.innerHTML = `<div class="loading">${escapeHtml(message)}</div>`;
    this.hasRendered = true;
  }

  render(experiments: ExperimentView[]) {
    this.hasRendered = true;

    if (experiments.length === 0) {
      this.container.innerHTML =
        '<div class="loading">No experiments running. ' +
        "Start a shadow run with the cohort framework enabled to see live arms here.</div>";
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
    const verdict = normalizeVerdict(exp.verdict, exp.live);
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
        <div class="experiment-arms">
          ${armsHtml}
        </div>
        <div class="experiment-footer">
          ${deltaHtml}
          ${exp.reason ? `<div class="experiment-reason" title="${escapeHtml(exp.reason)}">${escapeHtml(exp.reason)}</div>` : ""}
        </div>
      </div>
    `;
  }

  private renderArm(arm: ArmAggregate): string {
    const inFlight = Math.max(0, arm.assignedCount - arm.sampleCount);
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

function prettyArm(arm: string): string {
  if (arm === "experimental") return "Experimental";
  if (arm === "control") return "Control";
  return arm;
}

interface NormalizedVerdict {
  label: string;
  cssClass: string;
}

function normalizeVerdict(verdict: string, live: boolean): NormalizedVerdict {
  const v = verdict.toLowerCase();
  if (v === "promote" || v === "promoted" || v === "done") {
    return { label: "Promote", cssClass: "verdict-promote" };
  }
  if (v === "discard" || v === "discarded" || v === "rejected") {
    return { label: "Discard", cssClass: "verdict-discard" };
  }
  if (v === "inconclusive") {
    return { label: "Inconclusive", cssClass: "verdict-inconclusive" };
  }
  if (!live) {
    return { label: verdict || "Concluded", cssClass: "verdict-inconclusive" };
  }
  return { label: "Running", cssClass: "verdict-running" };
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
