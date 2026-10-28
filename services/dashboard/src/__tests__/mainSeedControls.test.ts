// @vitest-environment happy-dom
/**
 * UC-035 A6 (#2386): a dashboard opened mid-game seeds Start / Stop and the status
 * line from the follow poll's GetGameState, because the event bus has no replay.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { seedFromPoll } from "../follow.js";

const getGameState = vi.fn();

vi.mock("../client.js", () => ({
  controllerClient: { watchGameplayData: vi.fn(), identifyController: vi.fn() },
  gameClient: {
    streamGameEvents: vi.fn(),
    getGameState: (...a: unknown[]) => getGameState(...a),
    listGames: vi.fn().mockResolvedValue({ games: [] }),
    forceEndGame: vi.fn(),
  },
  menuClient: {
    listGameModes: vi.fn().mockResolvedValue({ modes: [] }),
    getMenuStatus: vi.fn().mockRejectedValue(new Error("down")),
    processInput: vi.fn(),
  },
}));
vi.mock("../controllerStream.js", async (orig) => ({
  ...(await orig<typeof import("../controllerStream.js")>()),
  keepControllerStreamOpen: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../stream.js", () => ({ keepGameEventStreamOpen: vi.fn().mockResolvedValue(undefined) }));

const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");

describe("UC-035: A6 the follow poll seeds Start / Stop and the status", () => {
  it("UC-035/A6 seedFromPoll: first answer and live-ness changes seed, repeats do not", () => {
    const running = { gameId: "g1", state: "RUNNING" };
    expect(seedFromPoll(running, null, "Idle")).toEqual({ running: true, status: "Running" });
    expect(seedFromPoll({ gameId: "g1", state: 1 }, null, "Idle")).toEqual({ running: true, status: "Starting" });
    expect(seedFromPoll(running, true, "Running")).toBeNull();
    expect(seedFromPoll({ gameId: "g1", state: "ENDED" }, true, "Running")).toEqual({ running: false, status: "Ended" });
    expect(seedFromPoll(undefined, null, "Idle")).toEqual({ running: false, status: null });
    expect(seedFromPoll(undefined, true, "Ended")).toEqual({ running: false, status: null });
  });

  it("UC-035/A6 reload mid-game: Stop is enabled and the status reads Running", async () => {
    document.body.innerHTML = html
      .slice(html.indexOf("<body"), html.indexOf("</body>"))
      .replace(/<(script|iframe)[\s\S]*?<\/\1>/g, "");
    getGameState.mockResolvedValue({ gameInfo: { gameId: "game_1", state: "RUNNING" } });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}), text: async () => "" })));
    for (const m of ["log", "warn", "error"] as const) vi.spyOn(console, m).mockImplementation(() => {});

    await import("../main.js");

    await vi.waitFor(() => expect((document.getElementById("stop-btn") as HTMLButtonElement).disabled).toBe(false));
    expect((document.getElementById("start-btn") as HTMLButtonElement).disabled).toBe(true);
    expect(document.getElementById("game-state")!.textContent).toContain("Running");
  });
});
