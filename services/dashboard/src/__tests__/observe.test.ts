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
    }, NOW, NOW);

    expect(shieldBadge(store.get("MOCK0001", NOW, NOW), NOW, NOW)?.text).toBe("Shield 5.0s");
    expect(shieldBadge(store.get("MOCK0001", NOW + 3000, NOW + 3000), NOW + 3000, NOW + 3000)?.text).toBe("Shield 2.0s");
    expect(shieldBadge(store.get("MOCK0001", NOW + 5000, NOW + 5000), NOW + 5000, NOW + 5000)).toBeNull();
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

  it("UC-035/main step 2 game_ended with winner none shows No winner and the reason (#1935)", () => {
    const { feed } = fold([["game_start", {}], ["game_ended", { winner: "none", outcome: "force_ended", reason: "preempted" }]]);
    expect(feed.at(-1)).toBe("Game ended! No winner (preempted)");
    expect(fold([["game_ended", { winner: "none", outcome: "force_ended" }]]).feed).toEqual(["Game ended! No winner (force-ended)"]);
    expect(fold([["game_ended", { winner: "none" }]]).feed).toEqual(["Game ended! No winner"]);
  });

  it("UC-035/main step 2 game_ended with a team winner shows the team, not a serial tail (#1935)", () => {
    expect(fold([["game_ended", { winner: "werewolves", outcome: "winner" }]]).feed).toEqual(["Game ended! Winner: team werewolves"]);
  });

  it("UC-035/main step 2 game_ended with a numeric winning_team shows Team N, -1 or none no winner (#1977)", () => {
    expect(fold([["game_ended", { winning_team: "1" }]]).feed).toEqual(["Game ended! Winner: Team 1"]);
    expect(fold([["game_ended", { winning_team: "0" }]]).feed).toEqual(["Game ended! Winner: Team 0"]);
    expect(fold([["game_ended", { winning_team: "-1" }]]).feed).toEqual(["Game ended! No winner"]);
    expect(fold([["game_ended", { winning_team: "none" }]]).feed).toEqual(["Game ended! No winner"]);
  });

  it("UC-035/main step 2 game_ended with a Tournament champion shows the controller (#1977)", () => {
    expect(fold([["game_ended", { champion: "00:06:f7:aa:bb:cc", total_matches: "3" }]]).feed).toEqual(["Game ended! Winner: b:cc"]);
  });

  it("UC-035/main step 2 game_ended with a stringified None champion or winner is a plain Game ended (#1977)", () => {
    expect(fold([["game_ended", { champion: "None", total_matches: "0" }]]).feed).toEqual(["Game ended!"]);
    expect(fold([["game_ended", { winner: "None", outcome: "winner" }]]).feed).toEqual(["Game ended!"]);
    expect(fold([["game_ended", { winner: "", champion: "" }]]).feed).toEqual(["Game ended!"]);
    expect(fold([["game_ended", { winner: "None", champion: "00:06:f7:aa:bb:cc" }]]).feed).toEqual(["Game ended! Winner: b:cc"]);
  });

  it("UC-035/main step 2 game_ended with a winner field ignores winning_team (#1977)", () => {
    expect(fold([["game_ended", { winner: "werewolves", winning_team: "werewolves" }]]).feed).toEqual(["Game ended! Winner: team werewolves"]);
  });

  it("UC-035/main step 2 game_ended with a serial winner shows the controller (#1935)", () => {
    expect(fold([["game_ended", { winner: "00:06:f7:aa:bb:cc", outcome: "winner" }]]).feed).toEqual(["Game ended! Winner: b:cc"]);
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

  it("UC-035/main step 2 the coordinator's real start order ends the countdown in Running (#1767)", () => {
    const { state, feed } = fold([
      ["game_start", {}],
      ["game_starting", {}],
      ["players_initialized", {}],
      ["game_started", {}],
      ["countdown_start", { phase_duration_ms: "3000" }],
      ["countdown_end", {}],
    ]);
    expect(state.gameState).toBe("Running");
    expect(state.alivePlayers).toBe(4);
    expect(feed).toEqual(["Game started!", "Countdown..."]);
  });

  it("UC-035/main step 2 player_death lowers the alive count and names the player", () => {
    const { state, feed } = fold([["game_start", {}], ["player_death", { serial: "aa:bb:cc:dd" }]]);
    expect(state.alivePlayers).toBe(3);
    expect(feed.at(-1)).toBe("Player c:dd eliminated");
  });

  it("UC-035/main step 2 player_shielded adds an agent shield line", () => {
    const { feed } = fold([["player_shielded", { serial: "aa:bb:cc:dd", kind: "toughness_boost", seconds: "2" }]]);
    expect(feed).toEqual(["Agent: toughness boost for c:dd (2s)"]);
  });

  it("UC-035/main step 2 player_shielded with the pre-rename kind partial is still a toughness boost line", () => {
    const { feed } = fold([["player_shielded", { serial: "aa:bb:cc:dd", kind: "partial", seconds: "2" }]]);
    expect(feed).toEqual(["Agent: toughness boost for c:dd (2s)"]);
  });

  it("UC-035/main step 2 shield charge events add feed lines (#1837)", () => {
    const serial = "aa:bb:cc:dd";
    expect(fold([["player_shield_charged", { serial, seconds: "5.0", expires_ms: "0" }]]).feed).toEqual([
      "Agent: shield charge for c:dd (5.0s)",
    ]);
    expect(
      fold([["player_shielded", { serial, kind: "full", seconds: "5.0", activation: "trigger", charge_delay_ms: "3200" }]]).feed,
    ).toEqual(["c:dd activated the shield (after 3.2s)"]);
    expect(fold([["player_shield_charge_ended", { serial, reason: "expired" }]]).feed).toEqual([
      "c:dd's shield charge expired",
    ]);
    for (const reason of ["death", "game_end", "activated"]) {
      expect(fold([["player_shield_charge_ended", { serial, reason }]]).feed).toEqual([]);
    }
  });

  it("UC-035/main step 2 game_error shows the Ended state and an error line", () => {
    const effect = reduceGameEvent(
      { ...idle, gameState: "Running" },
      { eventType: "game_error", data: { error: "stream failed" } },
    );
    expect(effect.state.gameState).toBe("Ended");
    expect(effect.gameRunning).toBe(false);
    expect(effect.feedLine).toBe("Game ended with an error");
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

describe("UC-010: A7 start fails after admission", () => {
  it("UC-010/A7 game_start then game_start_error leaves Running for Ended", () => {
    const started = reduceGameEvent(idle, {
      eventType: "game_start",
      data: { game_id: "g1", game_name: "FFA", player_count: "4" },
    });
    expect(started.state.gameState).toBe("Running");
    const failed = reduceGameEvent(started.state, {
      eventType: "game_start_error",
      data: { error: "thread start failed", game_id: "g1" },
    });
    expect(failed.state.gameState).toBe("Ended");
    expect(failed.gameRunning).toBe(false);
  });
});
