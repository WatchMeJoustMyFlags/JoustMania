/**
 * Screen-only fallback (#1316): the dashboard follows a live shadow game when no
 * real game is live, for the current-backend strip and the controller tiles.
 */
import { describe, expect, it } from "vitest";
import { NO_FOLLOW, chooseFollow, followBanner, isLiveGame, visibleTiles } from "../follow.js";
import type { GameInfoLike } from "../follow.js";
import { DecideStore, currentFrom } from "../agent.js";
import type { DecideRow } from "../agent.js";

function game(gameId: string, state: string, startTimeMs: number, players: [string, boolean][]): GameInfoLike {
  return {
    gameId,
    gameMode: "JoustFFA",
    state,
    startTimeMs: String(startTimeMs),
    // proto3 JSON omits alive=false
    players: players.map(([serial, alive]) => (alive ? { serial, alive } : { serial })),
  };
}

const REAL = game("game_real00000001", "RUNNING", 1000, [
  ["00:06:f7:aa:bb:01", true],
  ["00:06:f7:aa:bb:02", true],
]);
const SHADOW_OLD = game("game_shadow00001", "RUNNING", 2000, [
  ["MOCK0001", true],
  ["MOCK0002", false],
]);
const SHADOW_NEW = game("game_shadow00002", "RUNNING", 3000, [
  ["MOCK0003", true],
  ["MOCK0004", true],
]);
const SHADOW_ENDED = game("game_shadow00003", "ENDED", 4000, [["MOCK0005", false]]);

const tiles = [
  "00:06:f7:aa:bb:01",
  "00:06:f7:aa:bb:02",
  "MOCK0001",
  "MOCK0002",
  "MOCK0003",
  "MOCK0004",
  "MOCK0009",
].map((serial) => ({ serial }));

function row(spanId: string, tsMs: number, gameId: string, gameKind: string, backend: string): DecideRow {
  return {
    traceId: `t-${spanId}`,
    spanId,
    tsMs,
    latencyMs: 300,
    backend,
    requested: backend,
    fallbackReason: "",
    model: backend === "jev" ? "jev-latest" : "",
    intervention: "",
    target: "",
    confidence: null,
    threshold: backend === "jev" ? 0.6 : null,
    outcome: "noop",
    outcomeReason: "",
    rationale: "",
    errorType: "",
    gameId,
    gameKind,
    decisions: [],
  };
}

describe("UC-070: A9 BR-005 which game the dashboard follows", () => {
  it("UC-070/BR-005 a live real game wins: no shadow game is followed", () => {
    const f = chooseFollow(REAL, [REAL, SHADOW_OLD, SHADOW_NEW], "");
    expect(f.realGameLive).toBe(true);
    expect(f.shadowGameId).toBe("");
  });

  it("UC-070/A9 no real game live: the newest live shadow game is followed", () => {
    const f = chooseFollow({ gameId: "", state: "IDLE" }, [SHADOW_OLD, SHADOW_NEW, SHADOW_ENDED], "");
    expect(f.realGameLive).toBe(false);
    expect(f.shadowGameId).toBe("game_shadow00002");
    expect(f.shadowGameMode).toBe("JoustFFA");
  });

  it("UC-070/A9 an ended real game does not hold the view", () => {
    const ended = { ...REAL, state: "ENDED" };
    const f = chooseFollow(ended, [ended, SHADOW_OLD], "");
    expect(f.realGameLive).toBe(false);
    expect(f.shadowGameId).toBe("game_shadow00001");
  });

  it("UC-070/BR-005 the followed shadow game is kept while live (no flicker)", () => {
    const f = chooseFollow(undefined, [SHADOW_OLD, SHADOW_NEW], "game_shadow00001");
    expect(f.shadowGameId).toBe("game_shadow00001");
  });

  it("UC-070/BR-005 an ended followed game hands over to the newest live one", () => {
    const f = chooseFollow(undefined, [SHADOW_ENDED, SHADOW_OLD], "game_shadow00003");
    expect(f.shadowGameId).toBe("game_shadow00001");
  });

  it("UC-070/A9 nothing live: nothing followed", () => {
    const f = chooseFollow(undefined, [SHADOW_ENDED], "");
    expect(f.shadowGameId).toBe("");
    expect(followBanner(f)).toBe("");
  });

  it("UC-070/BR-005 live states accept enum names and numbers", () => {
    expect(isLiveGame({ gameId: "g", state: "STARTING" })).toBe(true);
    expect(isLiveGame({ gameId: "g", state: 2 })).toBe(true);
    expect(isLiveGame({ gameId: "g", state: "ENDING" })).toBe(false);
    expect(isLiveGame({ gameId: "", state: "RUNNING" })).toBe(false);
  });
});

describe("UC-070: A9 BR-005 controller tiles of the followed shadow game", () => {
  it("UC-070/A9 following a shadow game shows only its players, alive or dead from its roster", () => {
    const f = chooseFollow(undefined, [SHADOW_OLD, SHADOW_NEW], "game_shadow00001");
    const shown = visibleTiles(tiles, f);
    expect(shown.map((t) => t.controller.serial)).toEqual(["MOCK0001", "MOCK0002"]);
    expect(shown.map((t) => t.alive)).toEqual([true, false]);
    expect(followBanner(f)).toBe(
      "No real game live: following shadow game game_shadow00001 (JoustFFA, 1/2 alive)",
    );
  });

  it("UC-070/BR-005 with a real game live, live shadow players are hidden from the grid", () => {
    const f = chooseFollow(REAL, [REAL, SHADOW_OLD, SHADOW_NEW], "");
    const shown = visibleTiles(tiles, f);
    expect(shown.map((t) => t.controller.serial)).toEqual([
      "00:06:f7:aa:bb:01",
      "00:06:f7:aa:bb:02",
      "MOCK0009",
    ]);
    expect(shown.every((t) => t.alive === undefined)).toBe(true);
  });

  it("UC-070/BR-005 unknown follow state (coordinator unreachable) shows every tile", () => {
    expect(visibleTiles(tiles, NO_FOLLOW)).toHaveLength(tiles.length);
  });
});

describe("UC-070: A9 BR-005 current-backend strip anchors on the followed game", () => {
  // newest first: a newer shadow decide on rules, an older real game on jev
  const rows = [
    row("S2", 5000, "game_shadow00002", "shadow", "rules"),
    row("S1", 4000, "game_shadow00001", "shadow", "jev"),
    row("R1", 3000, "game_real00000001", "real", "jev"),
  ];

  it("UC-070/BR-005 without follow information the newest real game still anchors (stage default)", () => {
    expect(currentFrom(rows)?.gameId).toBe("game_real00000001");
    expect(currentFrom(rows, { realGameLive: true, followGameId: "" })?.gameKind).toBe("real");
  });

  it("UC-070/A9 the followed shadow game anchors the strip with its backend", () => {
    const cur = currentFrom(rows, { realGameLive: false, followGameId: "game_shadow00001" });
    expect(cur?.gameId).toBe("game_shadow00001");
    expect(cur?.gameKind).toBe("shadow");
    expect(cur?.backend).toBe("jev");
    expect(cur?.threshold).toBeCloseTo(0.6);
  });

  it("UC-070/A9 no real game live and the followed game has no decide yet: newest shadow row", () => {
    const cur = currentFrom(rows, { realGameLive: false, followGameId: "game_shadow09999" });
    expect(cur?.gameId).toBe("game_shadow00002");
  });

  it("UC-070/A9 the decide store applies the follow state", () => {
    const store = new DecideStore();
    store.merge(rows);
    expect(store.feed().current?.gameId).toBe("game_real00000001");
    store.setFollow({ realGameLive: false, followGameId: "game_shadow00001" });
    expect(store.feed().current?.gameId).toBe("game_shadow00001");
    store.setFollow({ realGameLive: true, followGameId: "" });
    expect(store.feed().current?.gameId).toBe("game_real00000001");
  });
});
