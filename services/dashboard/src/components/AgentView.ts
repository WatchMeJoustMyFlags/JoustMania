/**
 * Agent View Component (issue #792).
 *
 * The demo's agent focus screen. Three panels, all fed from the agent's live
 * telemetry (see ../agent.ts):
 *
 *   1. Decision feed     — a rolling tail of the agent's recent decisions
 *                          (blocked decisions + discarded LLM results + rollout
 *                          activity), allowed-vs-blocked at a glance.
 *   2. Flag panel        — the resolved effective mode/model/policy values the
 *                          agent last evaluated against.
 *   3. Intervention      — applied vs blocked tallies (blocked by action and by
 *      overlay             reason from Loki, applied from VictoriaMetrics).
 *
 * Mirrors ExperimentsView's lifecycle exactly: setError / markStale /
 * updateFreshness / rendered, with the same STALE_AFTER_MS staleness model so the
 * operator is never fooled by frozen numbers when a poll is failing.
 */
import type {
  AgentView as AgentViewData,
  DecisionFeedRow,
  FlagPanel,
  InterventionOverlay,
} from "../agent.js";

/**
 * After this many ms without a successful poll, a kept-on-screen view is flagged
 * "stale". Set above the poll cadence so a single missed tick doesn't cry wolf.
 */
const STALE_AFTER_MS = 12_000;

export class AgentView {
  private readonly feedEl: HTMLElement;
  private readonly flagsEl: HTMLElement;
  private readonly overlayEl: HTMLElement;
  private readonly freshnessEl: HTMLElement | null;
  private hasRendered = false;
  private lastUpdatedMs: number | null = null;

  constructor(
    feedId: string,
    flagsId: string,
    overlayId: string,
    freshnessId?: string,
  ) {
    this.feedEl = requireEl(feedId);
    this.flagsEl = requireEl(flagsId);
    this.overlayEl = requireEl(overlayId);
    this.freshnessEl = freshnessId ? document.getElementById(freshnessId) : null;
  }

  setError(message: string) {
    const html = `<div class="loading">${escapeHtml(message)}</div>`;
    this.feedEl.innerHTML = html;
    this.flagsEl.innerHTML = html;
    this.overlayEl.innerHTML = html;
    this.hasRendered = true;
  }

  /** A poll just failed; keep the last good render and let the hint go stale. */
  markStale() {
    this.updateFreshness();
  }

  updateFreshness() {
    const el = this.freshnessEl;
    if (!el || this.lastUpdatedMs === null) return;
    const ageMs = Date.now() - this.lastUpdatedMs;
    const ageSec = Math.max(0, Math.round(ageMs / 1000));
    const stale = ageMs >= STALE_AFTER_MS;
    el.textContent = stale
      ? `stale — last updated ${ageSec}s ago`
      : `updated ${ageSec}s ago`;
    el.classList.toggle("stale", stale);
    el.hidden = false;
  }

  get rendered(): boolean {
    return this.hasRendered;
  }

  render(view: AgentViewData) {
    this.hasRendered = true;
    this.lastUpdatedMs = Date.now();
    this.updateFreshness();

    this.renderFeed(view.feed);
    this.renderFlags(view.flags);
    this.renderOverlay(view.overlay);
  }

  private renderFeed(feed: DecisionFeedRow[]) {
    if (feed.length === 0) {
      this.feedEl.innerHTML =
        '<div class="loading">No agent decisions in the last 10 minutes. ' +
        "Allowed decisions appear in the Jaeger trace (link above); this feed shows " +
        "blocked decisions, discarded LLM results, and rollout activity.</div>";
      return;
    }
    this.feedEl.innerHTML = feed.map((row) => this.renderFeedRow(row)).join("");
  }

  private renderFeedRow(row: DecisionFeedRow): string {
    const time = formatTime(row.tsMs);
    const session = row.sessionId
      ? `<span class="agent-feed-session" title="${escapeHtml(row.sessionId)}">${escapeHtml(shortId(row.sessionId))}</span>`
      : "";
    const action = row.action
      ? `<span class="agent-feed-action">${escapeHtml(row.action)}</span>`
      : "";
    const reason = row.reason
      ? `<span class="agent-feed-reason" title="${escapeHtml(row.reason)}">${escapeHtml(row.reason)}</span>`
      : "";
    const badge = row.blocked
      ? '<span class="agent-badge agent-badge-blocked">blocked</span>'
      : `<span class="agent-badge agent-badge-${row.kind}">${escapeHtml(row.kind)}</span>`;
    return `
      <div class="agent-feed-row agent-feed-${row.kind}">
        <span class="agent-feed-time">${time}</span>
        ${badge}
        <span class="agent-feed-signal">${escapeHtml(row.signal)}</span>
        ${action}
        ${session}
        ${reason}
      </div>
    `;
  }

  private renderFlags(flags: FlagPanel | null) {
    if (!flags) {
      this.flagsEl.innerHTML =
        '<div class="loading">No agent.evaluate cycle observed yet. ' +
        "Effective mode/model/policy values appear here once the agent evaluates a game.</div>";
      return;
    }
    const rows: [string, string][] = [
      ["mode", flags.mode || "—"],
      ["model", flags.model || "—"],
      ["prompt variant", flags.promptVariant || "—"],
      ["battery threshold", flags.batteryThreshold || "—"],
      ["variance window", flags.varianceWindow || "—"],
      ["max interventions/min", flags.maxPerMinute || "—"],
      ["game mode", flags.gameMode || "—"],
    ];
    const body = rows
      .map(
        ([k, v]) => `
        <div class="agent-flag">
          <span class="agent-flag-key">${escapeHtml(k)}</span>
          <span class="agent-flag-val">${escapeHtml(v)}</span>
        </div>`,
      )
      .join("");
    this.flagsEl.innerHTML = `
      <div class="agent-flags-grid">${body}</div>
      <div class="agent-flags-note">
        Resolved effective values from the latest <code>agent.evaluate</code> cycle.
        Boolean kill-switch flags (enabled, experiments_enabled, interventions_allowed)
        live in flagd and are not yet exposed to the dashboard &mdash; see
        <a href="/jaeger/search?service=agent" target="_blank" rel="noreferrer">Jaeger</a>
        for the per-decision flag attribution.
      </div>
    `;
  }

  private renderOverlay(overlay: InterventionOverlay) {
    const applied =
      overlay.appliedOk === null
        ? '<span class="agent-stat-val agent-stat-na" title="VictoriaMetrics unreachable">n/a</span>'
        : `<span class="agent-stat-val agent-stat-applied">${overlay.appliedOk}</span>`;
    const appliedErr =
      overlay.appliedError && overlay.appliedError > 0
        ? `<span class="agent-stat-sub" title="failed flag-file writes">(${overlay.appliedError} failed)</span>`
        : "";

    const byAction =
      overlay.blockedByAction.length > 0
        ? overlay.blockedByAction
            .map(
              (t) => `
              <div class="agent-tally">
                <span class="agent-tally-label">${escapeHtml(t.action || "(unknown)")}</span>
                <span class="agent-tally-count agent-tally-blocked">${t.blocked}</span>
              </div>`,
            )
            .join("")
        : '<div class="agent-tally-empty">none blocked</div>';

    const byReason =
      overlay.blockedByReason.length > 0
        ? overlay.blockedByReason
            .map(
              (r) => `
              <div class="agent-tally">
                <span class="agent-tally-label">${escapeHtml(r.reason)}</span>
                <span class="agent-tally-count">${r.count}</span>
              </div>`,
            )
            .join("")
        : '<div class="agent-tally-empty">&mdash;</div>';

    this.overlayEl.innerHTML = `
      <div class="agent-overlay-stats">
        <div class="agent-stat">
          <span class="agent-stat-label">applied</span>
          ${applied} ${appliedErr}
        </div>
        <div class="agent-stat">
          <span class="agent-stat-label">blocked (10m)</span>
          <span class="agent-stat-val agent-stat-blocked">${overlay.totalBlocked}</span>
        </div>
      </div>
      <div class="agent-overlay-cols">
        <div class="agent-overlay-col">
          <h4>Blocked by action</h4>
          ${byAction}
        </div>
        <div class="agent-overlay-col">
          <h4>Blocked by reason</h4>
          ${byReason}
        </div>
      </div>
    `;
  }
}

function requireEl(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`Container element not found: ${id}`);
  return el;
}

/** epoch ms -> HH:MM:SS local time. */
function formatTime(tsMs: number): string {
  const d = new Date(tsMs);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Trim a long game/session id to a readable tail. */
function shortId(id: string): string {
  return id.length > 12 ? `…${id.slice(-10)}` : id;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
