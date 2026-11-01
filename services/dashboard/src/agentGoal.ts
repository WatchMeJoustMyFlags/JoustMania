import { searchTraces, spanStartMs, spanTags, tagNumber, tagString } from "./jaeger.js";
import type { JaegerTracesResponse } from "./jaeger.js";

/**
 * Dashboard Agent tab: the game's goal profile and how far the game reached it
 * (#2479, #2498, UC-042 A12; decide-contract §14.4). The agent.game.summary span
 * carries summary.goal.profile / .source / .attainment, set at game end. The span is
 * its own root (or a child of agent.experiment), not part of the game's decide trace,
 * so it has its own small search.
 */

/** Jaeger operation of the game summary span. */
export const GAME_SUMMARY_OPERATION = "agent.game.summary";

/** Summaries asked per poll: the newest few games are all the strip can show. */
const GOAL_SEARCH_LIMIT = 10;

/** Bound on the goals kept (a game is one entry). */
const MAX_GOALS = 100;

/** A game's goal: profile, how it was chosen and the attainment 0..1 (null: no term measured). */
export interface GameGoal {
  gameId: string;
  gameKind: string;
  profile: string;
  /** named (goal.profile variant) | from_objectives (derived from the objectives) | "" when absent. */
  source: string;
  attainment: number | null;
  tsMs: number;
}

/** Fold a Jaeger search response into the games' goals; a summary without a goal profile is skipped. */
export function parseGoalSummaries(body: JaegerTracesResponse): GameGoal[] {
  const out: GameGoal[] = [];
  for (const trace of body.data ?? []) {
    for (const span of trace.spans ?? []) {
      if (span.operationName !== GAME_SUMMARY_OPERATION) continue;
      const tags = spanTags(span);
      const gameId = tagString(tags, "game.id");
      const profile = tagString(tags, "summary.goal.profile");
      if (!gameId || !profile) continue;
      out.push({
        gameId,
        gameKind: tagString(tags, "game.kind"),
        profile,
        source: tagString(tags, "summary.goal.source"),
        attainment: tagNumber(tags, "summary.goal.attainment"),
        tsMs: spanStartMs(span),
      });
    }
  }
  return out;
}

/** The goals of the games seen, newest kept, bounded. */
export class GoalStore {
  private readonly goals = new Map<string, GameGoal>();

  merge(goals: GameGoal[]): void {
    for (const g of goals) this.goals.set(g.gameId, g);
    if (this.goals.size > MAX_GOALS) {
      const keep = [...this.goals.values()].sort((a, b) => b.tsMs - a.tsMs).slice(0, MAX_GOALS);
      this.goals.clear();
      for (const g of keep) this.goals.set(g.gameId, g);
    }
  }

  /** The goal of `gameId`; null before its summary (a game has none until it ends). */
  forGame(gameId: string): GameGoal | null {
    return this.goals.get(gameId) ?? null;
  }
}

/** One search for the newest game summaries; throws JaegerUnavailableError like the other polls. */
export async function fetchGoals(signal?: AbortSignal): Promise<GameGoal[]> {
  const body = await searchTraces(
    { operation: GAME_SUMMARY_OPERATION, limit: GOAL_SEARCH_LIMIT, startMs: null },
    signal,
  );
  return parseGoalSummaries(body);
}
