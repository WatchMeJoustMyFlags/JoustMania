/**
 * UC-035 main step 2: what the operator reads on the Controllers view besides the LED
 * colour: the agent-shield badge with its countdown (FR-084) and the alive count.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ShieldStore, shieldBadge } from "../shield.js";
import { GameStatus } from "../components/GameStatus.js";
import { reduceGameEvent } from "../gameEvents.js";
import type { GameFeedState } from "../gameEvents.js";

const NOW = 1_760_000_000_000;

describe("UC-035: main step 2, agent-shield badge with countdown", () => {
  it("UC-035 main step 2 counts the shield down while it runs and drops the badge when it ends", () => {
    const store = new ShieldStore();
    store.apply({
      eventType: "player_shielded",
      data: { serial: "MOCK0001", kind: "full", seconds: "5.0", until_ms: String(NOW + 5000) },
    });

    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)?.text).toBe("Shield 5.0s");
    expect(shieldBadge(store.get("MOCK0001", NOW + 3000), NOW + 3000)?.text).toBe("Shield 2.0s");
    expect(shieldBadge(store.get("MOCK0001", NOW + 5000), NOW + 5000)).toBeNull();
  });

  it("UC-035 main step 2 a controller with no shield shows no badge", () => {
    const store = new ShieldStore();
    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)).toBeNull();
  });
});

describe("UC-035: main step 2, game state and alive count", () => {
  const elements: Record<string, { innerHTML: string }> = {};

  beforeEach(() => {
    for (const id of ["game-state", "player-count", "alive-count", "event-log"]) elements[id] = { innerHTML: "" };
    vi.stubGlobal("document", { getElementById: (id: string) => elements[id] });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("UC-035 main step 2 shows the alive count next to the connected players", () => {
    const status = new GameStatus();
    status.updatePlayerCount(4, 3);
    expect(elements["player-count"].innerHTML).toBe("Players: <strong>4</strong> connected");
    expect(elements["alive-count"].innerHTML).toBe("Alive: <strong>3</strong>");
  });

  it("UC-035 main step 2 shows a dash when nobody is alive (no game running)", () => {
    const status = new GameStatus();
    status.updatePlayerCount(4, 0);
    expect(elements["alive-count"].innerHTML).toBe("Alive: <strong>-</strong>");
  });

  it("UC-035 main step 2 shows the game state", () => {
    new GameStatus().updateState("Running");
    expect(elements["game-state"].innerHTML).toBe("Game: <strong>Running</strong>");
  });
});

const idle: GameFeedState = { gameState: "Idle", alivePlayers: 0, totalPlayers: 4, winner: null };

function fold(events: [string, Record<string, string>][], from: GameFeedState = idle) {
  let state = from;
  const feed: string[] = [];
  for (const [eventType, data] of events) {
    const effect = reduceGameEvent(state, { eventType, data });
    state = effect.state;
    if (effect.feedLine !== null) feed.push(effect.feedLine);
  }
  return { state, feed };
}

describe("UC-035: step 2 game event feed", () => {
  it("UC-035/main step 2 game_start shows the Running state and every player alive", () => {
    const { state, feed } = fold([["game_start", {}]]);
    expect(state).toMatchObject({ gameState: "Running", alivePlayers: 4 });
    expect(feed).toEqual(["Game started!"]);
  });

  it("UC-035/main step 2 game_ended shows the Ended state and the winner", () => {
    const { state, feed } = fold([
      ["game_start", {}],
      ["game_winner", { serial: "00:06:f7:aa:bb:cc" }],
      ["game_ended", { outcome: "winner" }],
    ]);
    expect(state.gameState).toBe("Ended");
    expect(feed.at(-1)).toBe("Game ended! Winner: b:cc");
  });

  it("UC-035/main step 2 game_ended without a winner still ends the game", () => {
    const { state, feed } = fold([["game_start", {}], ["game_ended", { outcome: "tie" }]]);
    expect(state.gameState).toBe("Ended");
    expect(feed.at(-1)).toBe("Game ended!");
  });

  it("UC-035/main step 2 game_force_ended shows the Ended state", () => {
    const effect = reduceGameEvent(
      { ...idle, gameState: "Running" },
      { eventType: "game_force_ended", data: { reason: "Dashboard stop button" } },
    );
    expect(effect.state.gameState).toBe("Ended");
    expect(effect.gameRunning).toBe(false);
    expect(effect.feedLine).toBe("Game stopped");
  });

  it("UC-035/main step 2 countdown_start shows the Starting state", () => {
    const { state, feed } = fold([["countdown_start", { phase_duration_ms: "3000" }]]);
    expect(state.gameState).toBe("Starting");
    expect(feed).toEqual(["Countdown..."]);
  });

  it("UC-035/main step 2 countdown_end then game_started shows Running", () => {
    const { state } = fold([["countdown_start", {}], ["countdown_end", {}], ["game_started", {}]]);
    expect(state.gameState).toBe("Running");
  });

  it("UC-035/main step 2 player_death lowers the alive count and names the player", () => {
    const { state, feed } = fold([["game_start", {}], ["player_death", { serial: "aa:bb:cc:dd" }]]);
    expect(state.alivePlayers).toBe(3);
    expect(feed.at(-1)).toBe("Player c:dd eliminated");
  });

  it("UC-035/main step 2 player_shielded adds an agent shield line", () => {
    const { feed } = fold([["player_shielded", { serial: "aa:bb:cc:dd", kind: "partial", seconds: "2" }]]);
    expect(feed).toEqual(["Agent: partial shield for c:dd (2s)"]);
  });

  it("UC-035/main step 2 game_start_error ends the attempt with the error in the feed", () => {
    const effect = reduceGameEvent(
      { ...idle, gameState: "Running" },
      { eventType: "game_start_error", data: { error: "not enough players" } },
    );
    expect(effect.state.gameState).toBe("Ended");
    expect(effect.gameRunning).toBe(false);
    expect(effect.feedLine).toBe("Game failed to start: not enough players");
  });
});
