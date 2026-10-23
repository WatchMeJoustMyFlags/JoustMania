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
 */

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
