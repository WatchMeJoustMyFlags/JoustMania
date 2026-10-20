/**
 * What each panel does when its telemetry backend poll fails (UC-035 A3 step 2):
 * a panel that has rendered keeps its data and goes stale; one that never
 * rendered says which backend it could not reach.
 */

export const AGENT_LOKI_DOWN = "Could not reach Loki for agent telemetry.";
export const EXPERIMENTS_LOKI_DOWN = "Could not reach Loki for experiment telemetry.";
export const DECIDE_JAEGER_DOWN = "Could not reach Jaeger for agent.decide spans.";

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
  else view.setError(AGENT_LOKI_DOWN);
}

export function onExperimentsPollFailed(view: StaleCapable): void {
  if (view.rendered) view.markStale();
  else view.setError(EXPERIMENTS_LOKI_DOWN);
}

export function onDecidePollFailed(view: DecideCapable): void {
  if (!view.decideHasRendered) view.setDecideError(DECIDE_JAEGER_DOWN);
}
