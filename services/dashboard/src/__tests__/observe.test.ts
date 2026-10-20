/**
 * UC-035 main step 2: what the operator reads on the Controllers view besides the LED
 * colour: the agent-shield badge with its countdown (FR-084) and the alive count.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ShieldStore, shieldBadge } from "../shield.js";
import { GameStatus } from "../components/GameStatus.js";

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
