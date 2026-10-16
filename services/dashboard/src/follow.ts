/**
 * Which game the dashboard follows (UC-070 A9, BR-005, #1316).
 *
 * The talk's screen-only fallback: when Bluetooth fails at the venue there is no
 * real game, but the experiment loop keeps archetype shadow games running on
 * pattern-driven mock controllers (UC-046). The dashboard then follows the newest
 * live shadow game: the current-backend strip describes it (agent.ts
 * `currentFrom`) and the controller grid shows only its players.
 *
 * TILE VISIBILITY. The controller-manager gameplay stream the grid reads carries
 * EVERY tracked controller, reserved pattern controllers included (the reserved
 * flag only hides them from the button stream, i.e. the lobby and the menu). The
 * stream has no game id per controller, so the grid joins it with the game
 * coordinator's `ListGames` / `GetGameState` rosters:
 *   - following a shadow game: only that game's players, alive or dead from its
 *     `PlayerInfo.alive`;
 *   - otherwise (a real game, or nothing live): every controller except the
 *     players of live shadow games, so shadow tiles never mix into the real view.
 * Nothing changes for the lobby: the filter is dashboard-only.
 *
 * The game coordinator answers in proto3 JSON (connect-proxy): camelCase names,
 * enums as names, int64 as strings, and false / zero fields omitted.
 */

/** GameInfo, the subset the dashboard reads (local type: no gen/ dependency). */
export interface GameInfoLike {
  gameId?: string;
  gameMode?: string;
  /** "RUNNING", or the enum number. */
  state?: string | number;
  /** int64: a string in proto3 JSON. */
  startTimeMs?: string | number;
  players?: PlayerInfoLike[];
}

export interface PlayerInfoLike {
  serial?: string;
  /** Omitted when false (proto3 JSON). */
  alive?: boolean;
}

export interface GetGameStateResponseLike {
  success?: boolean;
  gameInfo?: GameInfoLike;
}

export interface ListGamesResponseLike {
  success?: boolean;
  games?: GameInfoLike[];
}

/** What the dashboard follows right now. */
export interface FollowState {
  /** A real (primary) game is STARTING or RUNNING; null = unknown (no answer yet). */
  realGameLive: boolean | null;
  /** The followed shadow game ("" when following none). */
  shadowGameId: string;
  shadowGameMode: string;
  /** Players of the followed shadow game: serial -> alive. */
  shadowPlayers: Map<string, boolean>;
  /** Players of every live shadow game (hidden from the real-game view). */
  liveShadowSerials: Set<string>;
}

export const NO_FOLLOW: FollowState = {
  realGameLive: null,
  shadowGameId: "",
  shadowGameMode: "",
  shadowPlayers: new Map(),
  liveShadowSerials: new Set(),
};

const LIVE_STATES = new Set(["STARTING", "RUNNING", "1", "2"]);

/** STARTING or RUNNING (a game in ENDING / ENDED / IDLE is not followed). */
export function isLiveGame(game: GameInfoLike | undefined | null): boolean {
  if (!game || !game.gameId) return false;
  return LIVE_STATES.has(String(game.state ?? ""));
}

function startMs(game: GameInfoLike): number {
  const n = Number(game.startTimeMs ?? 0);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Pick the followed game (UC-070 BR-005). Pure.
 *
 * `primary` is GetGameState() without a game id (the real game's slot), `games`
 * is ListGames (primary + shadow), `previous` the shadow game followed so far.
 * A real game that is live wins: nothing shadow is followed. Otherwise the
 * previous shadow game is kept while it is live (concurrent shadow games must not
 * make the view flicker), else the newest live shadow game by start time.
 */
export function chooseFollow(
  primary: GameInfoLike | null | undefined,
  games: GameInfoLike[],
  previous: string,
): FollowState {
  const primaryId = primary?.gameId ?? "";
  const realGameLive = isLiveGame(primary);
  const liveShadows = games
    .filter((g) => isLiveGame(g) && g.gameId !== primaryId)
    .sort((a, b) => startMs(b) - startMs(a) || String(b.gameId).localeCompare(String(a.gameId)));

  const liveShadowSerials = new Set<string>();
  for (const g of liveShadows) {
    for (const p of g.players ?? []) if (p.serial) liveShadowSerials.add(p.serial);
  }

  const followed = realGameLive
    ? undefined
    : (liveShadows.find((g) => g.gameId === previous) ?? liveShadows[0]);

  const shadowPlayers = new Map<string, boolean>();
  for (const p of followed?.players ?? []) {
    if (p.serial) shadowPlayers.set(p.serial, p.alive === true);
  }

  return {
    realGameLive,
    shadowGameId: followed?.gameId ?? "",
    shadowGameMode: followed?.gameMode ?? "",
    shadowPlayers,
    liveShadowSerials,
  };
}

/** One tile to draw, with the alive override of a followed shadow game. */
export interface Tile<T> {
  controller: T;
  /** Alive (true) or dead (false) from the followed game; undefined = the controller's own team. */
  alive: boolean | undefined;
}

/**
 * The controller tiles to draw (UC-070 BR-005). Pure. Following a shadow game:
 * only its players. Otherwise: everything but the players of live shadow games.
 */
export function visibleTiles<T extends { serial: string }>(controllers: T[], follow: FollowState): Tile<T>[] {
  if (follow.shadowGameId) {
    return controllers
      .filter((c) => follow.shadowPlayers.has(c.serial))
      .map((c) => ({ controller: c, alive: follow.shadowPlayers.get(c.serial) }));
  }
  return controllers
    .filter((c) => !follow.liveShadowSerials.has(c.serial))
    .map((c) => ({ controller: c, alive: undefined }));
}

/** Banner text above the grid while a shadow game is followed ("" when none). */
export function followBanner(follow: FollowState): string {
  if (!follow.shadowGameId) return "";
  const alive = Array.from(follow.shadowPlayers.values()).filter(Boolean).length;
  const mode = follow.shadowGameMode ? `${follow.shadowGameMode}, ` : "";
  return `No real game live: following shadow game ${follow.shadowGameId} (${mode}${alive}/${follow.shadowPlayers.size} alive)`;
}
