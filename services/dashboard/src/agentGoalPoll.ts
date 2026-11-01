import { DECIDE_MAX_PACE_MS, decidePaceMs } from "./agentDecide.js";
import type { DecideStore } from "./agentDecide.js";
import { fetchGoal } from "./agentGoal.js";

/** Wait between goal polls (also while idle); the answer size can only lengthen it. */
export const GOAL_POLL_MS = 10_000;

/**
 * The goal poller's `run` (#2498, UC-035 BR-011): searches the followed game's
 * agent.game.summary by game.id, and nothing at all while the followed game's goal is
 * known (or there is no followed game) until the game id changes. A game without a goal
 * has no summary goal, so it is searched again every GOAL_POLL_MS until it ends. The
 * strip is end-of-game and static: a failed poll keeps it as it is, with no stale marker.
 * `onGoal` renders after a new goal was merged.
 */
export function createGoalRun(
  store: DecideStore,
  onGoal: () => void,
): (signal?: AbortSignal) => Promise<number> {
  return async (signal) => {
    const gameId = store.feed().current?.gameId ?? "";
    if (!gameId || store.hasGoal(gameId)) return GOAL_POLL_MS;
    const { goals, bytes } = await fetchGoal(gameId, signal);
    store.mergeGoals(goals);
    if (store.hasGoal(gameId)) onGoal();
    return Math.max(GOAL_POLL_MS, decidePaceMs(bytes, DECIDE_MAX_PACE_MS));
  };
}
