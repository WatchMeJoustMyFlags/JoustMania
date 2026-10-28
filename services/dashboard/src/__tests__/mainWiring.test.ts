// @vitest-environment happy-dom
/**
 * UC-035: main.ts wiring smoke test (#2291): init() builds the components, the Start
 * button reaches the menu, and the Experiments tab pokes its Jaeger poll.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const processInput = vi.fn().mockResolvedValue({});
const forceEndGame = vi.fn().mockResolvedValue({});

vi.mock("../client.js", () => ({
  controllerClient: { watchGameplayData: vi.fn(), identifyController: vi.fn() },
  gameClient: {
    streamGameEvents: vi.fn(),
    getGameState: vi.fn().mockRejectedValue(new Error("down")),
    listGames: vi.fn().mockRejectedValue(new Error("down")),
    forceEndGame: (...a: unknown[]) => forceEndGame(...a),
  },
  menuClient: {
    listGameModes: vi.fn().mockResolvedValue({ modes: [] }),
    getMenuStatus: vi.fn().mockRejectedValue(new Error("down")),
    processInput: (...a: unknown[]) => processInput(...a),
  },
}));
vi.mock("../controllerStream.js", async (orig) => ({
  ...(await orig<typeof import("../controllerStream.js")>()),
  keepControllerStreamOpen: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("../stream.js", () => ({ keepGameEventStreamOpen: vi.fn().mockResolvedValue(undefined) }));

const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");

describe("UC-035: main.ts wiring", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("UC-035/main step 4 init wires Start to the menu and the Experiments tab to the experiment.progress search", async () => {
    document.body.innerHTML = html.slice(html.indexOf("<body"), html.indexOf("</body>")).replace(/<(script|iframe)[\s\S]*?<\/\1>/g, "");
    const searches: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        searches.push(String(url));
        return { ok: false, status: 503, json: async () => ({}), text: async () => "" };
      }),
    );
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});

    await import("../main.js");

    document.getElementById("start-btn")!.click();
    await vi.waitFor(() => expect(processInput).toHaveBeenCalledTimes(1));
    expect(processInput.mock.calls[0][0]).toMatchObject({ data: { command: "start_game" } });

    (document.querySelector('.tab-btn[data-tab="experiments"]') as HTMLElement).click();
    expect(document.getElementById("tab-experiments")!.classList.contains("active")).toBe(true);
    await vi.waitFor(() => expect(searches.some((u) => u.includes("experiment.progress"))).toBe(true));
  });
});
