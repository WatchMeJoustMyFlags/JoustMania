/**
 * Dashboard game event feed (UC-035 main step 2, D-D1, #1725).
 *
 * The event names are the coordinator's `GameEvent` strings (lib/types.py).
 */
import { describe, expect, it } from "vitest";
import { reduceGameEvent } from "../gameEvents.js";
import type { GameFeedState } from "../gameEvents.js";

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
});
