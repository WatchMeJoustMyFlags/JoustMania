/**
 * Agent View Component (issue #792).
 *
 * The demo's agent focus screen. Panels fed from the agent's live telemetry
 * (see ../agent.ts; spans from Jaeger since #2059):
 *
 *   1. Activity feed     — blocked agent.decision spans and rollout rows
 *                          (agent.infrastructure.decision).
 *   2. Flag panel        — the followed game's effective flag set, from its newest
 *                          agent.flag_state span (UC-070 BR-005 anchor).
 *   3. Intervention      — applied and blocked tallies from VictoriaMetrics (real games first, shadow games on their own line, #2294)
 *      overlay             (`n/a` when it is unreachable).
 *   4. Decide feed       — (#1233) one row per agent.decide span from Jaeger:
 *                          backend/model, answer, confidence vs threshold,
 *                          outcome, rationale, plus a "current backend" strip so
 *                          a live decision.backend switch is visible on the next
 *                          decision. Polled and rendered independently of 1-3.
 *
 * Mirrors ExperimentsView's lifecycle exactly: setError / markStale /
 * updateFreshness / rendered, with the same STALE_AFTER_MS staleness model so the
 * operator is never fooled by frozen numbers when a poll is failing.
 */
import { renderFreshness } from "../freshness.js";
import { html, setHtml, type SafeHtml } from "../html.js";
import type {
  AgentView as AgentViewData,
  DecideCurrent,
  DecideFeed,
  DecideRow,
  DecisionFeedRow,
  FlagPanel,
  InterventionOverlay,
  LinkedDecision,
} from "../agent.js";
import { confidenceBar, decisionTargetLabel, jaegerTraceUrl } from "../agent.js";
import { FLAG_STATE_NO_DATA } from "../degrade.js";

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

  private readonly decideStripEl: HTMLElement | null;
  private readonly decideFeedEl: HTMLElement | null;
  private readonly decideFreshnessEl: HTMLElement | null;
  private decideRendered = false;
  private decideUpdatedMs: number | null = null;
  /** Span keys already on screen: new ones get a one-shot highlight. */
  private seenDecideKeys: Set<string> | null = null;
  /** backend|model|threshold of the strip, to flash it when a switch lands. */
  private lastStripKey: string | null = null;
  /** Signature of the rendered rows; unchanged -> the DOM is left alone. */
  private lastRowsKey: string | null = null;
  // The HTML each panel was last given: an unchanged poll leaves the DOM alone (#2387).
  private readonly lastHtml = new WeakMap<HTMLElement, string>();

  constructor(
    feedId: string,
    flagsId: string,
    overlayId: string,
    freshnessId?: string,
    decideIds?: { strip: string; feed: string; freshness?: string },
  ) {
    this.feedEl = requireEl(feedId);
    this.flagsEl = requireEl(flagsId);
    this.overlayEl = requireEl(overlayId);
    this.freshnessEl = freshnessId ? document.getElementById(freshnessId) : null;
    this.decideStripEl = decideIds ? requireEl(decideIds.strip) : null;
    this.decideFeedEl = decideIds ? requireEl(decideIds.feed) : null;
    this.decideFreshnessEl = decideIds?.freshness
      ? document.getElementById(decideIds.freshness)
      : null;
  }

  /* ---------------- agent.decide feed (#1233) ---------------- */

  get decideHasRendered(): boolean {
    return this.decideRendered;
  }

  setDecideError(message: string) {
    if (!this.decideFeedEl || !this.decideStripEl) return;
    setHtml(this.decideFeedEl, html`<div class="loading">${message}</div>`);
    setHtml(this.decideStripEl, html``);
    this.decideRendered = true;
  }

  updateDecideFreshness() {
    const el = this.decideFreshnessEl;
    if (!el || this.decideUpdatedMs === null) return;
    renderFreshness(el, this.decideUpdatedMs, STALE_AFTER_MS);
  }

  renderDecide(feed: DecideFeed) {
    if (!this.decideFeedEl || !this.decideStripEl) return;
    this.decideRendered = true;
    this.decideUpdatedMs = performance.now();
    this.updateDecideFreshness();

    // Leave the DOM alone when nothing changed: rebuilding it would restart the
    // highlight animations and drop an open hover tooltip on every poll.
    const stripKey = feed.current ? decideStripKey(feed.current) : "";
    if (stripKey !== this.lastStripKey) {
      const switched = !!this.lastStripKey && !!stripKey;
      setHtml(this.decideStripEl, decideStripHtml(feed.current, switched));
      this.lastStripKey = stripKey;
    }

    const rowsKey = decideRowsKey(feed.rows);
    if (rowsKey === this.lastRowsKey) return;
    this.lastRowsKey = rowsKey;

    if (feed.rows.length === 0) {
      setHtml(
        this.decideFeedEl,
        html`<div class="loading">No agent.decide spans found in Jaeger yet. Rows appear here as soon as the agent decides in an FFA game.</div>`,
      );
      return;
    }
    // First render: nothing is "new". Afterwards, highlight rows not seen before.
    const previous = this.seenDecideKeys;
    const keys = new Set<string>();
    const rowsHtml = feed.rows.map((row) => {
      const key = `${row.traceId}/${row.spanId}`;
      keys.add(key);
      return decideRowHtml(row, previous !== null && !previous.has(key));
    });
    this.seenDecideKeys = previous ? new Set([...previous, ...keys]) : keys;
    // Bound the memory of a long-running dashboard.
    if (this.seenDecideKeys.size > 2000) this.seenDecideKeys = keys;
    setHtml(this.decideFeedEl, html`${rowsHtml}`);
  }

  /** Write a panel only when its HTML differs: a rewrite drops an open hover tooltip. */
  private setPanel(el: HTMLElement, markup: SafeHtml) {
    const text = markup.toString();
    if (this.lastHtml.get(el) === text) return;
    this.lastHtml.set(el, text);
    setHtml(el, markup);
  }

  setError(message: string) {
    const markup = html`<div class="loading">${message}</div>`;
    this.setPanel(this.feedEl, markup);
    this.setPanel(this.flagsEl, markup);
    this.setPanel(this.overlayEl, markup);
    this.hasRendered = true;
  }

  /**
   * Jaeger failed while VictoriaMetrics answered (UC-035 A3 step 2): the
   * Jaeger-fed panels (activity feed, flag panel) name Jaeger, the overlay is
   * left to renderOverlay.
   */
  setJaegerError(message: string) {
    const markup = html`<div class="loading">${message}</div>`;
    this.setPanel(this.feedEl, markup);
    this.setPanel(this.flagsEl, markup);
    this.hasRendered = true;
  }

  /** A poll just failed; keep the last good render and let the hint go stale. */
  markStale() {
    this.updateFreshness();
  }

  updateFreshness() {
    const el = this.freshnessEl;
    if (!el || this.lastUpdatedMs === null) return;
    renderFreshness(el, this.lastUpdatedMs, STALE_AFTER_MS);
  }

  get rendered(): boolean {
    return this.hasRendered;
  }

  render(view: AgentViewData) {
    this.hasRendered = true;
    this.lastUpdatedMs = performance.now();
    this.updateFreshness();

    this.renderFeed(view.feed);
    this.renderFlags(view.flags);
    this.renderOverlay(view.overlay);
  }

  private renderFeed(feed: DecisionFeedRow[]) {
    if (feed.length === 0) {
      this.setPanel(
        this.feedEl,
        html`<div class="loading">No blocked agent.decision or rollout spans in Jaeger yet. Allowed decisions appear in the decide feed below and in the Jaeger trace (link above); this feed shows blocked decisions and rollout activity.</div>`,
      );
      return;
    }
    this.setPanel(this.feedEl, html`${feed.map((row) => this.renderFeedRow(row))}`);
  }

  private renderFeedRow(row: DecisionFeedRow): SafeHtml {
    const time = formatTime(row.tsMs);
    const session = row.sessionId
      ? html`<span class="agent-feed-session" title="${row.sessionId}">${shortId(row.sessionId)}</span>`
      : "";
    const action = row.action
      ? html`<span class="agent-feed-action">${row.action}</span>`
      : "";
    const target = row.target
      ? html`<span class="decide-arrow">→</span> <span class="decide-target">${row.target}</span>`
      : "";
    const reason = row.reason
      ? html`<span class="agent-feed-reason" title="${row.reason}">${row.reason}</span>`
      : "";
    const badge = row.blocked
      ? html`<span class="agent-badge agent-badge-blocked">blocked</span>`
      : html`<span class="agent-badge agent-badge-${row.kind}">${row.kind}</span>`;
    return html`
      <div class="agent-feed-row agent-feed-${row.kind}">
        <span class="agent-feed-time">${time}</span>
        ${badge}
        <span class="agent-feed-signal">${row.signal}</span>
        ${action}
        ${target}
        ${session}
        ${reason}
      </div>
    `;
  }

  private renderFlags(flags: FlagPanel | null) {
    this.setPanel(this.flagsEl, flagPanelHtml(flags));
  }

  /** The VictoriaMetrics overlay alone; leaves the Jaeger panels and their freshness. */
  renderOverlay(overlay: InterventionOverlay) {
    const applied =
      overlay.appliedOk === null
        ? html`<span class="agent-stat-val agent-stat-na" title="VictoriaMetrics unreachable">n/a</span>`
        : html`<span class="agent-stat-val agent-stat-applied">${overlay.appliedOk}</span>`;
    const appliedErr =
      overlay.appliedError && overlay.appliedError > 0
        ? html`<span class="agent-stat-sub" title="failed flag-file writes">(${overlay.appliedError} failed)</span>`
        : "";

    const shadowCell = (n: number | null, extra: SafeHtml | "" = "") =>
      n === null
        ? html`<span class="agent-stat-val agent-stat-na" title="VictoriaMetrics unreachable">n/a</span>`
        : html`<span class="agent-stat-val agent-stat-shadow">${n}</span>${extra}`;
    const shadowErr =
      overlay.shadowAppliedError && overlay.shadowAppliedError > 0
        ? html` <span class="agent-stat-sub" title="failed flag-file writes">(${overlay.shadowAppliedError} failed)</span>`
        : "";

    const blocked =
      overlay.totalBlocked === null
        ? html`<span class="agent-stat-val agent-stat-na" title="VictoriaMetrics unreachable">n/a</span>`
        : html`<span class="agent-stat-val agent-stat-blocked">${overlay.totalBlocked}</span>`;

    const byAction =
      overlay.blockedByAction.length > 0
        ? overlay.blockedByAction.map(
            (t) => html`
              <div class="agent-tally">
                <span class="agent-tally-label">${t.action || "(unknown)"}</span>
                <span class="agent-tally-count agent-tally-blocked">${t.blocked}</span>
              </div>`,
          )
        : overlay.totalBlocked === null
          ? html`<div class="agent-tally-empty">n/a</div>`
          : html`<div class="agent-tally-empty">none blocked</div>`;

    const byReason =
      overlay.blockedByReason.length > 0
        ? overlay.blockedByReason.map(
            (r) => html`
              <div class="agent-tally">
                <span class="agent-tally-label">${r.reason}</span>
                <span class="agent-tally-count">${r.count}</span>
              </div>`,
          )
        : html`<div class="agent-tally-empty">&mdash;</div>`;

    this.setPanel(this.overlayEl, html`
      <div class="agent-overlay-stats">
        <div class="agent-stat">
          <span class="agent-stat-label">applied (10m)</span>
          ${applied} ${appliedErr}
        </div>
        <div class="agent-stat">
          <span class="agent-stat-label">blocked (10m)</span>
          ${blocked}
        </div>
      </div>
      <div class="agent-overlay-stats agent-overlay-shadow" title="Shadow (evaluation) games, not player-facing">
        <div class="agent-stat">
          <span class="agent-stat-label">shadow applied</span>
          ${shadowCell(overlay.shadowAppliedOk, shadowErr)}
        </div>
        <div class="agent-stat">
          <span class="agent-stat-label">shadow blocked (10m)</span>
          ${shadowCell(overlay.shadowBlocked)}
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
    `);
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

/** On / off / unknown for a boolean flag. */
function onOff(v: boolean | null): string {
  if (v === null) return "—";
  return v ? "on" : "off";
}

/**
 * The flag panel (#2059): the followed game's newest agent.flag_state. Pure and
 * exported for tests. A missing span is the no-data hint, never an error.
 */
export function renderFlagPanel(flags: FlagPanel | null): string {
  return flagPanelHtml(flags).toString();
}

function flagPanelHtml(flags: FlagPanel | null): SafeHtml {
  if (!flags) return html`<div class="loading">${FLAG_STATE_NO_DATA}</div>`;
  const rows: [string, string][] = [
    ["agent enabled (kill switch)", onOff(flags.enabled)],
    ["decision backend", flags.decisionBackend || "—"],
    ["act confidence threshold", flags.actThreshold === null ? "—" : flags.actThreshold.toFixed(2)],
    ["battery threshold", flags.batteryThreshold || "—"],
    ["max interventions/min", flags.maxPerMinute || "—"],
    ["interventions allowed", flags.interventionsAllowed || "—"],
    ["interventions enabled (write gate)", onOff(flags.interventionsEnabled)],
    ["experiments enabled", onOff(flags.experimentsEnabled)],
    ["LLM eligible game kinds", flags.eligibleGameKinds || "—"],
    ["objectives", flags.objectives || "—"],
    ["game mode", flags.gameMode || "—"],
  ];
  const body = rows.map(
    ([k, v]) => html`
        <div class="agent-flag">
          <span class="agent-flag-key">${k}</span>
          <span class="agent-flag-val">${v}</span>
        </div>`,
  );
  // #2093: enabled=on with the write gate off looks live but writes nothing.
  const gateHint =
    flags.enabled === true && flags.interventionsEnabled === false
      ? html`<div class="agent-flags-warn" role="status">${WRITE_GATE_OFF_HINT}</div>`
      : "";
  return html`
      ${gateHint}
      <div class="agent-flags-grid">${body}</div>
      <div class="agent-flags-note">
        Effective flags of ${renderGameBadge(flags.gameId, flags.gameKind)} from its newest
        <a href="${jaegerTraceUrl(flags.traceId, flags.spanId)}" target="_blank" rel="noreferrer"><code>agent.flag_state</code></a>
        span (${flags.trigger || "?"}, ${formatTime(flags.tsMs)}).
      </div>
    `;
}

/* ---------------- agent.decide rendering (#1233) ---------------- */
/*
 * Pure builders on the `html` tag (the exported render* wrappers return the string for tests):
 * every interpolation is escaped, which matters because the rationale and even the answers are model output.
 */

/** Visible rationale length; the full (already span-capped) text is the tooltip. */
const RATIONALE_VISIBLE_CHARS = 140;

const OUTCOME_LABEL: Record<DecideRow["outcome"], string> = {
  act: "act",
  held: "held",
  noop: "noop",
  invalid: "invalid",
  fallback: "fallback",
  discarded: "discarded",
  unknown: "?",
};

/** Shown in the flag panel while the agent is enabled but the write gate is off (#2093). */
export const WRITE_GATE_OFF_HINT =
  "Agent is enabled but interventions_enabled is off: decisions are made and recorded, but no intervention is written.";

/** decision.not_applied_reason -> text; an unknown reason is shown as is. */
const NOT_APPLIED_LABEL: Record<string, string> = {
  write_gate_off: "write gate off (interventions_enabled)",
  write_failed: "write failed",
};

/** The status of one agent.decision chip: blocked, not applied (with reason) or allowed. */
export function decisionStatusLabel(d: Pick<LinkedDecision, "blocked" | "applied" | "notAppliedReason">): string {
  if (d.blocked) return "blocked";
  if (d.applied === false) {
    const reason = d.notAppliedReason ? (NOT_APPLIED_LABEL[d.notAppliedReason] ?? d.notAppliedReason) : "";
    return reason ? `not applied: ${reason}` : "not applied";
  }
  return "allowed";
}

/** What the strip shows; a change re-renders (and flashes) it. */
export function decideStripKey(c: DecideCurrent): string {
  return `${c.backend}|${c.requested}|${c.model}|${c.threshold ?? ""}|${c.thresholdFromOlder}|${c.gameId}`;
}

/**
 * Signature of a row list: the row keys plus the fields that can change when a
 * span is re-fetched (linked decisions arrive after their decide).
 */
export function decideRowsKey(rows: DecideRow[]): string {
  return rows.map((r) => `${r.traceId}/${r.spanId}:${r.decisions.length}:${r.outcome}`).join(",");
}

/** Short game id for a row: game_<12 hex> -> its last 6 chars. */
export function shortGameId(id: string): string {
  if (!id) return "?";
  return id.length > 8 ? `…${id.slice(-6)}` : id;
}

/** Game badge: short id + kind (real / shadow / unknown). */
function renderGameBadge(gameId: string, gameKind: string): SafeHtml {
  const kind = gameKind === "shadow" ? "shadow" : gameKind ? "real" : "unknown";
  const label = gameKind || "?";
  return html`<span class="decide-game decide-game-${kind}" title="${gameId || "unknown game"}">${label} ${shortGameId(gameId)}</span>`;
}

function fmtConfidence(v: number | null): string {
  return v === null ? "—" : v.toFixed(2);
}

/** Truncate on a code-point boundary, adding an ellipsis. */
export function truncateText(text: string, max: number): string {
  const chars = Array.from(text);
  if (chars.length <= max) return text;
  return chars.slice(0, max - 1).join("") + "…";
}

/** The "current backend" header strip. */
export function renderDecideStrip(current: DecideCurrent | null, switched = false): string {
  return decideStripHtml(current, switched).toString();
}

function decideStripHtml(current: DecideCurrent | null, switched: boolean): SafeHtml {
  if (!current) {
    return html`<div class="decide-strip decide-strip-empty">current backend: waiting for the first agent.decide span…</div>`;
  }
  const requestedDiffers = current.requested && current.requested !== current.backend;
  const requested = requestedDiffers
    ? html`<span class="decide-strip-requested">requested <b>${current.requested}</b></span>`
    : "";
  // A rules decide (incl. a fallback, whose span still names the requested
  // backend's model) is decided by the rules engine: no model.
  const model = current.backend === "rules" ? "rules engine" : current.model || "—";
  const threshold =
    current.threshold === null
      ? html`<span class="decide-strip-val" title="no confidence routing recorded yet (rules does not route by confidence)">—</span>`
      : html`<span class="decide-strip-val${current.thresholdFromOlder ? " decide-strip-dim" : ""}"${
          current.thresholdFromOlder ? html` title="from an earlier decision; the newest one did not route by confidence"` : ""
        }>${current.threshold.toFixed(2)}</span>`;
  return html`
    <div class="decide-strip${switched ? " decide-strip-switched" : ""}">
      <div class="decide-strip-item">
        <span class="decide-strip-key">backend</span>
        <span class="decide-strip-val decide-backend-${backendClass(current.backend)}">${current.backend || "—"}</span>
        ${requested}
      </div>
      <div class="decide-strip-item">
        <span class="decide-strip-key">model</span>
        <span class="decide-strip-val decide-strip-model">${model}</span>
      </div>
      <div class="decide-strip-item">
        <span class="decide-strip-key">act threshold</span>
        ${threshold}
      </div>
      <div class="decide-strip-item decide-strip-time">${renderGameBadge(current.gameId, current.gameKind)} as of ${formatTime(current.tsMs)}</div>
    </div>`;
}

/** CSS-safe class suffix for a backend name (rules / claude / openai / jev / other). */
function backendClass(backend: string): string {
  return ["rules", "claude", "openai", "jev"].includes(backend) ? backend : "other";
}

/** The confidence bar with the threshold tick. */
export function renderConfidenceBar(confidence: number | null, threshold: number | null): string {
  return confidenceBarHtml(confidence, threshold).toString();
}

function confidenceBarHtml(confidence: number | null, threshold: number | null): SafeHtml {
  const bar = confidenceBar(confidence, threshold);
  const state = bar.pass === null ? "na" : bar.pass ? "pass" : "fail";
  const tick =
    bar.thresholdPct === null
      ? ""
      : html`<span class="decide-conf-tick" style="left:${bar.thresholdPct}%"></span>`;
  const cmp =
    confidence === null
      ? html`no confidence`
      : threshold === null
        ? html`conf ${fmtConfidence(confidence)}`
        : html`conf ${fmtConfidence(confidence)} ${bar.pass ? html`&ge;` : html`&lt;`} ${fmtConfidence(threshold)}`;
  return html`
    <div class="decide-conf decide-conf-${state}">
      <div class="decide-conf-track">
        <span class="decide-conf-fill" style="width:${bar.fillPct}%"></span>
        ${tick}
      </div>
      <span class="decide-conf-text">${cmp}</span>
    </div>`;
}

/** One decide feed row. */
export function renderDecideRow(row: DecideRow, isNew = false): string {
  return decideRowHtml(row, isNew).toString();
}

function decideRowHtml(row: DecideRow, isNew: boolean): SafeHtml {
  const outcome = row.outcome;
  const backendDiffers = row.requested && row.requested !== row.backend;
  const model = row.model
    ? html`<span class="decide-model${backendDiffers ? " decide-backend-struck" : ""}">${row.model}</span>`
    : "";
  // On a fallback the model belongs to the REQUESTED backend (struck through),
  // not to rules, which has none.
  const backend = backendDiffers
    ? html`<span class="decide-backend decide-backend-${backendClass(row.requested)} decide-backend-struck" title="requested backend">${row.requested}</span>${model}<span class="decide-arrow">→</span><span class="decide-backend decide-backend-${backendClass(row.backend)}">${row.backend}</span>`
    : html`<span class="decide-backend decide-backend-${backendClass(row.backend)}">${row.backend || "—"}</span>${row.backend === "rules" ? "" : model}`;
  const fallback = row.fallbackReason
    ? html`<span class="decide-fallback-reason" title="decide.fallback_reason">${row.fallbackReason}${
        row.errorType && row.errorType !== row.fallbackReason ? html` · ${row.errorType}` : ""
      }</span>`
    : "";
  const answer = row.intervention
    ? html`<span class="decide-answer">${row.intervention}${
        row.target && row.target !== "none"
          ? html` <span class="decide-arrow">→</span> <span class="decide-target">${row.target}</span>`
          : ""
      }</span>`
    : html`<span class="decide-answer decide-answer-none">no answer</span>`;
  const outcomeReason = row.outcomeReason
    ? html`<span class="decide-outcome-reason">${row.outcomeReason}</span>`
    : "";
  const rationale = row.rationale
    ? html`<div class="decide-why" title="${row.rationale}"><span class="decide-why-label">why</span> ${truncateText(row.rationale, RATIONALE_VISIBLE_CHARS)}</div>`
    : "";
  const decisions =
    row.decisions.length > 0
      ? html`<div class="decide-decisions">${row.decisions.map((d) => {
          // #1295: name the targeted player (display name, else serial).
          const target = decisionTargetLabel(d);
          const targetHtml = target
            ? html` <span class="decide-arrow">→</span> <span class="decide-target"${
                d.targetName && d.targetSerial ? html` title="${d.targetSerial}"` : ""
              }>${target}</span>`
            : "";
          const status = decisionStatusLabel(d);
          const notWritten = d.blocked || d.applied === false;
          return html`<span class="decide-decision ${notWritten ? "decide-decision-blocked" : "decide-decision-allowed"}"${
            d.reason ? html` title="${d.reason}"` : ""
          }>agent.decision: ${d.action || "?"}${targetHtml} · ${status}</span>`;
        })}</div>`
      : "";
  return html`
    <div class="decide-row decide-row-${outcome}${isNew ? " decide-row-new" : ""}">
      <div class="decide-line">
        <span class="agent-feed-time">${formatTime(row.tsMs)}</span>
        <span class="decide-outcome decide-outcome-${outcome}">${OUTCOME_LABEL[outcome]}</span>
        ${renderGameBadge(row.gameId, row.gameKind)}
        ${backend}
        ${fallback}
        ${answer}
        ${outcomeReason}
        <span class="decide-latency">${Math.round(row.latencyMs)} ms</span>
        <a class="decide-trace" href="${jaegerTraceUrl(row.traceId, row.spanId)}" target="_blank" rel="noreferrer" title="open this decision in Jaeger">trace ↗</a>
      </div>
      ${confidenceBarHtml(row.confidence, row.threshold)}
      ${rationale}
      ${decisions}
    </div>`;
}
