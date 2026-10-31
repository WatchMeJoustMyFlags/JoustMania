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
 * coordinator's `ListGames` / `GetGameState` rosters. Players of live shadow games
 * are hidden, except the followed shadow game's players, which are added with
 * alive or dead from its `PlayerInfo.alive`. A controller that is in no live
 * shadow game (a real or lobby controller) is never hidden. The menu is unchanged:
 * the filter is dashboard-only.
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
  /** The primary (real) game's id from GetGameState ("" when unknown). */
  primaryGameId: string;
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
  primaryGameId: "",
  shadowGameId: "",
  shadowGameMode: "",
  shadowPlayers: new Map(),
  liveShadowSerials: new Set(),
};

const LIVE_STATES = new Set(["STARTING", "RUNNING", "1", "2"]);
/** The real game still holds the stage while it ends (winner ceremony). */
const REAL_LIVE_STATES = new Set([...LIVE_STATES, "ENDING", "3"]);

/** STARTING or RUNNING (a shadow game in ENDING / ENDED / IDLE is not followed). */
export function isLiveGame(game: GameInfoLike | undefined | null): boolean {
  if (!game || !game.gameId) return false;
  return LIVE_STATES.has(String(game.state ?? ""));
}

/** The real (primary) game is on stage: STARTING, RUNNING or ENDING. */
export function isRealGameLive(primary: GameInfoLike | undefined | null): boolean {
  if (!primary || !primary.gameId) return false;
  return REAL_LIVE_STATES.has(String(primary.state ?? ""));
}

/**
 * What a follow poll tells the Start / Stop buttons and the status line (UC-035 A6).
 * Events drive them while the stream is up, but the event bus has no replay, so a
 * dashboard opened mid-game (or one that missed `game_ended`) learns the state
 * only from GetGameState. Edge-triggered: only the first answer and a change of the
 * real game's live-ness yield a seed, so a late poll never undoes a newer event.
 * `status` is null when the status line should stay as it is.
 */
export interface PolledGameSeed {
  running: boolean;
  status: "Starting" | "Running" | "Ended" | null;
}

export function seedFromPoll(
  primary: GameInfoLike | undefined | null,
  previousLive: boolean | null,
  currentStatus: string,
): PolledGameSeed | null {
  const live = isRealGameLive(primary);
  if (previousLive === live) return null;
  if (live) {
    const starting = String(primary?.state ?? "") === "STARTING" || String(primary?.state ?? "") === "1";
    return { running: true, status: starting ? "Starting" : "Running" };
  }
  // Not live: the status line only changes when it still claims a live game.
  const stale = currentStatus === "Running" || currentStatus === "Starting";
  return { running: false, status: stale ? "Ended" : null };
}

/**
 * Orders the follow poll against game events (UC-035 A6 step 2, #2386). Every
 * game event and every Start / Stop click bumps the clock; a poll stamps it when
 * its request goes out and its answer seeds the controls only if no bump happened
 * since, so an answer older than a `game_ended` (or a Start click in flight) is
 * dropped instead of undoing it.
 */
export class GameEventClock {
  private n = 0;
  bump(): void {
    this.n += 1;
  }
  stamp(): number {
    return this.n;
  }
  isCurrent(stamp: number): boolean {
    return stamp === this.n;
  }
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
  const realGameLive = isRealGameLive(primary);
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
    primaryGameId: primaryId,
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
 * The controller tiles to draw (UC-070 BR-005). Pure. Every controller that is
 * in no live shadow game (real or lobby controllers) is shown; players of live
 * shadow games are hidden, except the followed shadow game's players, which are
 * shown with alive or dead from its roster.
 */
export function visibleTiles<T extends { serial: string }>(controllers: T[], follow: FollowState): Tile<T>[] {
  const tiles: Tile<T>[] = [];
  for (const c of controllers) {
    const followedAlive = follow.shadowGameId ? follow.shadowPlayers.get(c.serial) : undefined;
    if (followedAlive !== undefined) tiles.push({ controller: c, alive: followedAlive });
    else if (!follow.liveShadowSerials.has(c.serial)) tiles.push({ controller: c, alive: undefined });
  }
  return tiles;
}

/** Banner text above the grid while a shadow game is followed ("" when none). */
export function followBanner(follow: FollowState): string {
  if (!follow.shadowGameId) return "";
  const alive = Array.from(follow.shadowPlayers.values()).filter(Boolean).length;
  const mode = follow.shadowGameMode ? `${follow.shadowGameMode}, ` : "";
  return `No real game live: following shadow game ${follow.shadowGameId} (${mode}${alive}/${follow.shadowPlayers.size} alive)`;
}

/**
 * The follow state across polls (UC-070 BR-005). A transient poll error keeps the
 * last state, so the followed game does not switch; after `maxErrors` consecutive
 * errors the state becomes unknown (NO_FOLLOW, every tile shown). The previously
 * followed shadow game is remembered through errors and resumed when it is live.
 */
export class FollowTracker {
  private current: FollowState = NO_FOLLOW;
  private previous = "";
  private errors = 0;

  constructor(private readonly maxErrors = 3) {}

  get state(): FollowState {
    return this.current;
  }

  update(primary: GameInfoLike | null | undefined, games: GameInfoLike[]): FollowState {
    this.errors = 0;
    this.current = chooseFollow(primary, games, this.previous);
    if (this.current.shadowGameId) this.previous = this.current.shadowGameId;
    return this.current;
  }

  fail(): FollowState {
    this.errors += 1;
    if (this.errors >= this.maxErrors) this.current = NO_FOLLOW;
    return this.current;
  }
}
