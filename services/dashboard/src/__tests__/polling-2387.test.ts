// @vitest-environment happy-dom
/**
 * Dashboard polling and clock behaviour (#2387): monotonic elapsed time, no DOM
 * rewrite when nothing changed, the mode list retried until the menu answers,
 * and Connect error code and message kept on unary failures.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentView } from "../components/AgentView.js";
import { ControllerGrid } from "../components/ControllerGrid.js";
import { FlagFallbackPacer, FLAG_STATE_FALLBACK_REPEAT_MS, QueryWindow } from "../agent.js";
import { unaryRpcError } from "../connect.js";
import { controllerClient } from "../client.js";
import { FALLBACK_CHOICES, loadModeChoicesWithRetry } from "../modes.js";
import { createPoller } from "../poller.js";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("UC-035: main step 2, elapsed time on the monotonic clock (#2387)", () => {
  it("UC-035/main the fallback pacer and decide window ignore a wall-clock jump", () => {
    let mono = 1000;
    vi.spyOn(performance, "now").mockImplementation(() => mono);
    let wall = 1_700_000_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => wall);

    const pacer = new FlagFallbackPacer();
    const window = new QueryWindow();
    expect(pacer.take("k")).toBe(true);
    window.observe([{ tsMs: 5000 }]);

    wall += 3_600_000; // NTP step on the Pi-fed laptop
    mono += 1000;
    expect(pacer.take("k")).toBe(false);
    expect(window.startMs()).not.toBe(5001);

    mono += FLAG_STATE_FALLBACK_REPEAT_MS;
    expect(pacer.take("k")).toBe(true);
  });

  it("UC-035/main the poller's slow-call test ignores a wall-clock jump", async () => {
    let wall = 1_700_000_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => wall);
    const poller = createPoller({
      baseMs: 1000,
      maxMs: 8000,
      timeoutMs: 5000,
      isActive: () => true,
      run: async () => {
        wall += 3_600_000;
      },
    });
    poller.poke();
    await vi.waitFor(() => expect(wall).toBeGreaterThan(1_700_000_000_000));
    await new Promise((r) => setTimeout(r, 0));
    expect(poller.delayMs).toBe(1000);
    poller.stop();
  });
});

describe("UC-035: main step 2, Agent panels are not rewritten when nothing changed (#2387)", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<div id="f"></div><div id="g"></div><div id="o"></div><div id="s"></div><div id="d"></div>';
  });

  it("UC-035/main keeps the overlay and feed DOM nodes across an identical poll", () => {
    const view = new AgentView("f", "g", "o", undefined, { strip: "s", feed: "d" });
    const data = {
      feed: [],
      flags: null,
      overlay: {
        blockedByAction: [{ action: "x", blocked: 2 }],
        blockedByReason: [],
        totalBlocked: 2,
        shadowBlocked: 0,
        appliedOk: 1,
        appliedError: 0,
        shadowAppliedOk: 0,
        shadowAppliedError: 0,
      },
    };
    view.render(data as never);
    const overlayNode = document.getElementById("o")!.firstElementChild;
    const feedNode = document.getElementById("f")!.firstElementChild;
    expect(overlayNode).not.toBeNull();

    view.render(data as never);
    expect(document.getElementById("o")!.firstElementChild).toBe(overlayNode);
    expect(document.getElementById("f")!.firstElementChild).toBe(feedNode);

    view.render({ ...data, overlay: { ...data.overlay, totalBlocked: 3 } } as never);
    expect(document.getElementById("o")!.firstElementChild).not.toBe(overlayNode);
  });
});

describe("UC-035: BR-009 adapter badges are not rewritten when unchanged (#2387)", () => {
  it("UC-035/BR-009 leaves the badge text node alone on repeated renders", () => {
    document.body.innerHTML = '<div id="grid"></div><div id="counts"></div>';
    const grid = new ControllerGrid("grid", "counts");
    const frame = (serial: string) => ({ serial }) as never;
    grid.setAdapters(new Map([["AA", "hci0"], ["BB", "hci1"]]));
    grid.render([frame("AA"), frame("BB")]);
    const badge = document.querySelector(".adapter-badge")!;
    const counts = document.getElementById("counts")!;
    const textNode = badge.firstChild;
    const countsNode = counts.firstChild;
    expect(textNode?.textContent).toBe("hci0");

    grid.render([frame("AA"), frame("BB")]);
    grid.setAdapters(new Map([["AA", "hci0"], ["BB", "hci1"]]));
    expect(badge.firstChild).toBe(textNode);
    expect(counts.firstChild).toBe(countsNode);

    grid.setAdapters(new Map([["AA", "hci2"], ["BB", "hci1"]]));
    expect(badge.textContent).toBe("hci2");
  });
});

describe("UC-011: BR-002 the mode list is retried until the menu answers (#2387)", () => {
  it("UC-011/BR-002 offers FFA, retries with growing backoff, then applies the real modes", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const list = vi
      .fn()
      .mockRejectedValueOnce(new Error("down"))
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue({ gameModes: ["JoustFFA", "NonStop"], defaultMode: "JoustFFA" });
    const applied: string[][] = [];
    const sleeps: number[] = [];

    await loadModeChoicesWithRetry(list, (c) => applied.push(c.options.map((o) => o.value)), {
      sleep: async (ms) => void sleeps.push(ms),
    });

    expect(list).toHaveBeenCalledTimes(3);
    expect(applied).toEqual([FALLBACK_CHOICES.options.map((o) => o.value), ["JoustFFA", "NonStop"]]);
    expect(sleeps).toEqual([2000, 4000]);
  });

  it("UC-011/BR-002 stops retrying when the page is no longer active", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const list = vi.fn().mockRejectedValue(new Error("down"));
    let active = true;
    await loadModeChoicesWithRetry(list, () => {}, {
      sleep: async () => void (active = false),
      isActive: () => active,
    });
    expect(list).toHaveBeenCalledTimes(1);
  });
});

describe("UC-035: unary RPC errors keep the Connect code and message (#2387)", () => {
  const res = (status: number, statusText: string, body: string) => ({ status, statusText, text: async () => body });

  it("UC-035/main parses a Connect error body", async () => {
    const err = await unaryRpcError(res(503, "Service Unavailable", '{"code":"unavailable","message":"menu down"}'));
    expect(err.code).toBe("unavailable");
    expect(err.message).toContain("unavailable");
    expect(err.message).toContain("menu down");
  });

  it("UC-035/main falls back to the HTTP status for a non-JSON body", async () => {
    const err = await unaryRpcError(res(502, "Bad Gateway", "<html>"));
    expect(err.code).toBe("http_502");
    expect(err.message).toContain("Bad Gateway");
  });

  it("UC-035/main the real client throws it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, statusText: "Not Found", text: async () => '{"code":"not_found","message":"no such controller"}' }),
    );
    await expect(controllerClient.identifyController({ serial: "x" } as never)).rejects.toMatchObject({
      code: "not_found",
      message: expect.stringContaining("no such controller"),
    });
  });
});
