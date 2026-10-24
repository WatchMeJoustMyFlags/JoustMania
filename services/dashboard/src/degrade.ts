/**
 * What each panel does when its telemetry backend does not deliver (UC-035 A3
 * step 2, #2059). Three states, kept apart:
 *
 *   Jaeger down     the search failed (transport error, timeout, non-2xx, or a
 *                   Jaeger `errors` array: JaegerUnavailableError). A panel that
 *                   has rendered keeps its data and goes stale; one that never
 *                   rendered names Jaeger.
 *   no data         Jaeger answered without matching spans: not an error. The
 *                   panel names the span it is waiting for; this is also what an
 *                   agent image without agent.flag_state / experiment.progress
 *                   (older than #2059) shows.
 *   agent or        Jaeger answers but no new spans arrive: rows stop growing and
 *   collector down  the freshness hint stays "updated" (UC-035 A3: "no new agent
 *                   rows"); with nothing ever received it is the no-data state.
 *
 * VictoriaMetrics down never fails a poll: Applied and Blocked show `n/a`.
 * Jaeger down with VictoriaMetrics up: the overlay renders, the Jaeger-fed
 * panels keep their data (stale) or name Jaeger, never the no-data hints.
 */
import type { AgentView as AgentViewData, FlagPanel, InterventionOverlay } from "./agent.js";

export const AGENT_JAEGER_DOWN = "Could not reach Jaeger for agent telemetry.";
export const EXPERIMENTS_JAEGER_DOWN = "Could not reach Jaeger for experiment telemetry.";
export const DECIDE_JAEGER_DOWN = "Could not reach Jaeger for agent.decide spans.";

/** No-data hints (Jaeger up, no matching spans yet). */
export const FLAG_STATE_NO_DATA =
  "No agent.flag_state spans in Jaeger yet. The effective flags appear once the agent evaluates a game " +
  "(an agent image from before #2059 emits none).";
export const EXPERIMENT_PROGRESS_NO_DATA =
  "No experiment.progress spans in Jaeger yet. Experiments appear once the cohort framework starts one " +
  "(an agent image from before #2059 emits none).";

interface StaleCapable {
  rendered: boolean;
  markStale(): void;
  setError(message: string): void;
}

interface DecideCapable {
  decideHasRendered: boolean;
  setDecideError(message: string): void;
}

export function onAgentPollFailed(view: StaleCapable): void {
  if (view.rendered) view.markStale();
  else view.setError(AGENT_JAEGER_DOWN);
}

export function onExperimentsPollFailed(view: StaleCapable): void {
  if (view.rendered) view.markStale();
  else view.setError(EXPERIMENTS_JAEGER_DOWN);
}

export function onDecidePollFailed(view: DecideCapable): void {
  if (!view.decideHasRendered) view.setDecideError(DECIDE_JAEGER_DOWN);
}

interface AgentPanelsCapable extends StaleCapable {
  render(view: AgentViewData): void;
  renderOverlay(overlay: InterventionOverlay): void;
  setJaegerError(message: string): void;
}

/**
 * Paint the Agent tab's feed, flag panel and overlay. `jaegerOk` is false while
 * the last Agent poll's Jaeger leg (flag-state or rollout search) failed: the
 * overlay still renders, the Jaeger-fed panels keep their last data and go
 * stale, or name Jaeger when they never rendered (UC-035 A3 step 2).
 */
export function paintAgentPanels(
  view: AgentPanelsCapable,
  overlay: InterventionOverlay | null,
  jaegerOk: boolean,
  jaegerPanels: () => Pick<AgentViewData, "feed" | "flags">,
): void {
  if (jaegerOk) {
    if (overlay !== null) view.render({ ...jaegerPanels(), overlay });
    return;
  }
  if (overlay !== null) view.renderOverlay(overlay);
  if (view.rendered) view.markStale();
  else if (overlay !== null) view.setJaegerError(AGENT_JAEGER_DOWN);
  else view.setError(AGENT_JAEGER_DOWN);
}

export type AgentPollResults = [
  PromiseSettledResult<InterventionOverlay>,
  PromiseSettledResult<FlagPanel[]>,
  PromiseSettledResult<unknown>,
];

/**
 * Fold one settled Agent poll (VictoriaMetrics counts, flag-state search,
 * rollout searches) into the state. Returns the overlay to keep, whether the
 * Jaeger legs succeeded and the first failure (the poller backs off on it).
 * `ran` names the Jaeger legs this tick actually searched; a skipped leg is a
 * resolved placeholder, not evidence. `jaegerOk` is null when no leg ran, so the
 * caller keeps its previous verdict (a Jaeger outage must not clear on a tick
 * that never asked Jaeger).
 */
export function settleAgentPoll(
  [counts, flags, rollouts]: AgentPollResults,
  overlay: InterventionOverlay | null,
  mergeFlags: (states: FlagPanel[]) => void,
  ran: { flags: boolean; rollouts: boolean },
): { overlay: InterventionOverlay | null; jaegerOk: boolean | null; failed: PromiseRejectedResult | undefined } {
  if (flags.status === "fulfilled") mergeFlags(flags.value);
  const failed = [counts, flags, rollouts].find((r): r is PromiseRejectedResult => r.status === "rejected");
  return {
    overlay: counts.status === "fulfilled" ? counts.value : overlay,
    jaegerOk:
      !ran.flags && !ran.rollouts
        ? null
        : (!ran.flags || flags.status === "fulfilled") && (!ran.rollouts || rollouts.status === "fulfilled"),
    failed,
  };
}
