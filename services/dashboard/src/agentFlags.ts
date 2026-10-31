/** Flag panel: agent.flag_state spans, their store, the fallback search and its pacer. Split out of agent.ts (#2388). */

import { searchTraces, spanStartMs, spanStartUs, spanTags, spansNamed, tagBoolOrNull, tagNumber, tagString } from "./jaeger.js";
import type { JaegerSpan, JaegerTracesResponse } from "./jaeger.js";
import { isShadowGame, pickAnchor } from "./agentAnchor.js";
import type { DecideFollow } from "./agentAnchor.js";

/** Jaeger operation of the effective flag set per game partition (#2059). */
export const FLAG_STATE_OPERATION = "agent.flag_state";

/** Traces of the flag-state fallback search (one-span traces or small game traces). */
export const FLAG_STATE_FALLBACK_LIMIT = 2;

/** The effective flag set of one game, from its newest agent.flag_state span. */
export interface FlagPanel {
  /** Span start (agent clock). */
  tsMs: number;
  /** Span start in µs (agent clock): the ordering key. */
  startUs: number;
  traceId: string;
  spanId: string;
  gameId: string;
  gameKind: string;
  gameMode: string;
  /** agent.flag_state.trigger: game_start | changed. */
  trigger: string;
  /** decide.backend.requested: the decision.backend flag value. */
  decisionBackend: string;
  /** decide.act_confidence_threshold, null when absent. */
  actThreshold: number | null;
  batteryThreshold: string;
  maxPerMinute: string;
  /** agent.enabled (the kill switch), null when absent. */
  enabled: boolean | null;
  /** agent.objectives summary ("k=v,..."). */
  objectives: string;
  /** interventions.allowed summary. */
  interventionsAllowed: string;
  /** interventions.enabled, the write gate (#2093); null on an older agent image. */
  interventionsEnabled: boolean | null;
  /** experiments.enabled (#2291): the stage fallback (UC-070 A9) turns it on; null on an older agent image. */
  experimentsEnabled: boolean | null;
  /** llm.eligible_game_kinds (#2291), comma joined; "" on an older agent image. */
  eligibleGameKinds: string;
}

/* ======================================================================== */
/* agent.flag_state: the flag panel (#2059)                                  */
/* ======================================================================== */

/** Fold one agent.flag_state span. */
export function flagPanelFrom(span: JaegerSpan): FlagPanel {
  const tags = spanTags(span);
  return {
    tsMs: spanStartMs(span),
    startUs: spanStartUs(span),
    traceId: span.traceID,
    spanId: span.spanID,
    gameId: tagString(tags, "game.id"),
    gameKind: tagString(tags, "game.kind"),
    gameMode: tagString(tags, "game.mode"),
    trigger: tagString(tags, "agent.flag_state.trigger"),
    decisionBackend: tagString(tags, "decide.backend.requested"),
    actThreshold: tagNumber(tags, "decide.act_confidence_threshold"),
    batteryThreshold: tagString(tags, "policy.battery_threshold"),
    maxPerMinute: tagString(tags, "policy.max_interventions_per_minute"),
    enabled: tagBoolOrNull(tags, "agent.enabled"),
    objectives: tagString(tags, "agent.objectives"),
    interventionsAllowed: tagString(tags, "interventions.allowed"),
    interventionsEnabled: tagBoolOrNull(tags, "interventions.enabled"),
    experimentsEnabled: tagBoolOrNull(tags, "experiments.enabled"),
    eligibleGameKinds: tagString(tags, "llm.eligible_game_kinds"),
  };
}

/** Every agent.flag_state span of a Jaeger response, folded. */
export function parseFlagStates(body: JaegerTracesResponse): FlagPanel[] {
  return spansNamed(body, FLAG_STATE_OPERATION).map(flagPanelFrom);
}

/** Newest first by span time in µs (two flag states can share a ms), then span id. */
function flagNewestFirst(a: FlagPanel, b: FlagPanel): number {
  return b.startUs - a.startUs || b.spanId.localeCompare(a.spanId);
}

/**
 * The newest flag state per game across polls. The panel shows the followed game
 * (UC-070 BR-005) chosen by the same anchor rule as the current-backend strip
 * (`pickAnchor`), so a shadow game's flag state never replaces the stage game's.
 */
export class FlagStateStore {
  private readonly byGame = new Map<string, FlagPanel>();

  constructor(private readonly maxGames = 20) {}

  /**
   * Fold new flag states in. Over `maxGames` the oldest go, but never the live
   * primary game's (`follow`): a burst of newer shadow games must not evict the
   * flag state the panel shows (UC-070 BR-005).
   */
  merge(states: FlagPanel[], follow?: DecideFollow): void {
    for (const s of states) {
      const held = this.byGame.get(s.gameId);
      if (!held || flagNewestFirst(s, held) < 0) this.byGame.set(s.gameId, s);
    }
    if (this.byGame.size > this.maxGames) {
      const pinned =
        follow?.realGameLive === true && follow.primaryGameId ? this.byGame.get(follow.primaryGameId) : undefined;
      const rest = this.sorted().filter((s) => s !== pinned);
      const keep = pinned ? [pinned, ...rest.slice(0, this.maxGames - 1)] : rest.slice(0, this.maxGames);
      this.byGame.clear();
      for (const s of keep) this.byGame.set(s.gameId, s);
    }
  }

  get size(): number {
    return this.byGame.size;
  }

  has(gameId: string): boolean {
    return this.byGame.has(gameId);
  }

  /**
   * The followed game's flag state (null before any agent.flag_state span).
   * While a real game is live it is that game's (`primaryGameId`) or null (the
   * hint until its flag state arrives), never an older game's or a shadow game's.
   */
  current(follow?: DecideFollow): FlagPanel | null {
    if (follow?.realGameLive === true) {
      if (follow.primaryGameId) return this.byGame.get(follow.primaryGameId) ?? null;
      return this.sorted().find((s) => !isShadowGame(s.gameKind)) ?? null;
    }
    return pickAnchor(this.sorted(), follow) ?? null;
  }

  private sorted(): FlagPanel[] {
    return Array.from(this.byGame.values()).sort(flagNewestFirst);
  }
}

/**
 * Whether the flag panel needs its own agent.flag_state search: no flag state is
 * held, or none for the followed game. While a real game is live that is the
 * primary game (`follow.primaryGameId`, follow.ts): a new game that has not
 * decided yet still shows the previous game on the decide strip, so the strip's
 * game is not enough. Otherwise it is the game the decide strip describes
 * (`decideGameId`, the UC-070 BR-005 anchor), e.g. a quiet game with no
 * agent.decide spans, whose game trace the decide poll never fetched. While
 * decides flow for a game whose flag state is held, the decide poll's traces are
 * enough.
 */
export function needsFlagStateFallback(
  store: FlagStateStore,
  decideGameId: string | null,
  follow?: DecideFollow,
): boolean {
  if (store.size === 0) return true;
  if (follow?.realGameLive === true && follow.primaryGameId) return !store.has(follow.primaryGameId);
  return decideGameId === null || !store.has(decideGameId);
}

/**
 * The flag-state fallback search: the newest FLAG_STATE_FALLBACK_LIMIT traces,
 * with no `start` (Jaeger's own window; a flag state is emitted once per game and
 * on change, so an incremental window would miss a long quiet game's). While a
 * real game is live the search asks for that game's `game.id`: shadow games
 * started after it would otherwise fill the limit.
 */
export async function fetchFlagStates(signal?: AbortSignal, follow?: DecideFollow): Promise<FlagPanel[]> {
  const primary = follow?.realGameLive === true ? follow.primaryGameId : "";
  const body = await searchTraces(
    {
      operation: FLAG_STATE_OPERATION,
      limit: FLAG_STATE_FALLBACK_LIMIT,
      startMs: null,
      ...(primary ? { tags: { "game.id": primary } } : {}),
    },
    signal,
  );
  return parseFlagStates(body);
}

/**
 * Pacing of the flag-state fallback search (#2287): a search for the same game
 * (the same `key`) is repeated at most every FLAG_STATE_FALLBACK_REPEAT_MS
 * (browser-elapsed time). Each search can return whole game traces, and while a
 * game has no agent.flag_state span the answer does not change from one 5 s
 * agent poll to the next. A new key (another game) searches at once.
 */
export const FLAG_STATE_FALLBACK_REPEAT_MS = 30_000;

export class FlagFallbackPacer {
  private lastKey: string | null = null;
  private lastMs = 0;

  constructor(private readonly now: () => number = () => performance.now()) {}

  /** Whether a fallback search for `key` may run now; records it when it may. */
  take(key: string): boolean {
    const now = this.now();
    if (key === this.lastKey && now - this.lastMs < FLAG_STATE_FALLBACK_REPEAT_MS) return false;
    this.lastKey = key;
    this.lastMs = now;
    return true;
  }

  /** Drop the last search (it failed): the next take() searches at once. */
  forget(): void {
    this.lastKey = null;
  }
}

/** The fallback pacer key: the game the flag panel needs ("" = any). */
export function flagFallbackKey(decideGameId: string | null, follow?: DecideFollow): string {
  if (follow?.realGameLive === true && follow.primaryGameId) return `primary:${follow.primaryGameId}`;
  return `decide:${decideGameId ?? ""}`;
}
