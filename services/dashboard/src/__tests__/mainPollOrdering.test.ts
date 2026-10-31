// @vitest-environment happy-dom
/**
 * UC-035 A6 step 2 (#2386): a follow-poll answer older than a game event is dropped,
 * so a stale RUNNING cannot re-enable Stop after `game_ended`.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { GameEventClock } from "../follow.js";

const getGameState = vi.fn();
const keepGameEventStreamOpen = vi.fn().mockResolvedValue(undefined);

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
vi.mock("../stream.js", () => ({ keepGameEventStreamOpen: (...a: unknown[]) => keepGameEventStreamOpen(...a) }));

const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");

describe("UC-035: A6 step 2 ordering of the follow poll and game events", () => {
  it("UC-035/A6 GameEventClock: an answer is current only if nothing bumped since its stamp", () => {
    const clock = new GameEventClock();
    const stamp = clock.stamp();
    expect(clock.isCurrent(stamp)).toBe(true);
    clock.bump();
    expect(clock.isCurrent(stamp)).toBe(false);
    expect(clock.isCurrent(clock.stamp())).toBe(true);
  });

  it("UC-035/A6 a RUNNING answer in flight when game_ended arrives does not re-enable Stop", async () => {
    document.body.innerHTML = html
      .slice(html.indexOf("<body"), html.indexOf("</body>"))
      .replace(/<(script|iframe)[\s\S]*?<\/\1>/g, "");
    let answer!: (v: unknown) => void;
    getGameState.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}), text: async () => "" })));
    for (const m of ["log", "warn", "error"] as const) vi.spyOn(console, m).mockImplementation(() => {});

    await import("../main.js");
    await vi.waitFor(() => expect(getGameState).toHaveBeenCalled());
    await vi.waitFor(() => expect(keepGameEventStreamOpen).toHaveBeenCalled());

    const onEvent = keepGameEventStreamOpen.mock.calls[0][1] as (e: unknown) => void;
    onEvent({ eventType: "game_ended", data: {}, timestamp: 0n });
    answer({ gameInfo: { gameId: "game_1", state: "RUNNING" } });
    await new Promise((r) => setTimeout(r, 20));

    expect((document.getElementById("stop-btn") as HTMLButtonElement).disabled).toBe(true);
    expect((document.getElementById("start-btn") as HTMLButtonElement).disabled).toBe(false);
    expect(document.getElementById("game-state")!.textContent).not.toContain("Running");
  });
});
