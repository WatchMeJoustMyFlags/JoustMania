/** Which game the dashboard follows: the shadow-game test, the follow state and the anchor pick. Split out of agent.ts (#2388). */


/** Shadow games are the experiment loop's headless games (game.kind="shadow"). */
export function isShadowGame(kind: string): boolean {
  return kind === "shadow";
}

/**
 * What the dashboard follows (UC-070 BR-005, follow.ts): whether a real game is
 * live (null = unknown), the followed shadow game ("" = none) and the primary
 * (real) game's id ("" = unknown; only the flag panel uses it).
 */
export interface DecideFollow {
  realGameLive: boolean | null;
  followGameId: string;
  primaryGameId?: string;
}

/**
 * The followed game's newest item (UC-070 BR-005), shared by the current-backend
 * strip and the flag panel. By default the newest NON-shadow game (the one on
 * stage); when no real game is live, the followed shadow game, else the newest
 * item of any kind. Without any non-shadow item, the newest item. `items` must be
 * newest first; undefined when empty.
 */
export function pickAnchor<T extends { gameId: string; gameKind: string }>(
  items: T[],
  follow?: DecideFollow,
): T | undefined {
  if (items.length === 0) return undefined;
  const followed = follow?.followGameId ? items.find((r) => r.gameId === follow.followGameId) : undefined;
  // No real game live and nothing followed with items: the newest item of any kind.
  const newestOverall = follow?.realGameLive === false ? items[0] : undefined;
  return followed ?? newestOverall ?? items.find((r) => !isShadowGame(r.gameKind)) ?? items[0];
}
