/**
 * Dashboard stream loss and telemetry-backend loss (UC-035 A4, A3; #1706, #1725).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { keepStreamOpen } from "../stream.js";
import { ExperimentsView } from "../components/ExperimentsView.js";

/** A stream that yields the events, then ends cleanly or throws. */
async function* stream(events: string[], failWith?: Error): AsyncGenerator<string> {
  for (const e of events) yield e;
  if (failWith) throw failWith;
}

describe("UC-035: A4 the dashboard loses a live stream", () => {
  it("UC-035/A4 reopens a game event stream that ended cleanly, so the next game shows", async () => {
    const seen: string[] = [];
    const delays: number[] = [];
    const queue = [["game1-start", "game1-end"], ["game2-start"]];
    const open = vi.fn(() => stream(queue.shift() ?? []));
    let reopen: (() => void) | undefined;

    await keepStreamOpen({
      open,
      onEvent: (e) => seen.push(e),
      schedule: (fn, ms) => {
        reopen = fn;
        delays.push(ms);
      },
    });
    expect(open).toHaveBeenCalledTimes(1);
    expect(delays).toEqual([2000]);

    reopen?.();
    await vi.waitFor(() => expect(open).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(seen).toContain("game2-start"));
    expect(seen).toEqual(["game1-start", "game1-end", "game2-start"]);
  });

  it("UC-035/A4 reopens a failed stream every 2 s and reports the loss", async () => {
    const lost: unknown[] = [];
    const delays: number[] = [];
    await keepStreamOpen({
      open: () => stream(["a"], new Error("proxy restarted")),
      onEvent: () => {},
      onLost: (e) => lost.push(e),
      schedule: (_fn, ms) => void delays.push(ms),
    });
    expect(delays).toEqual([2000]);
    expect((lost[0] as Error).message).toBe("proxy restarted");
  });

  it("UC-035/A4 a clean end is reported as a loss without an error", async () => {
    const lost: unknown[] = [];
    await keepStreamOpen({
      open: () => stream([]),
      onEvent: () => {},
      onLost: (e) => lost.push(e),
      schedule: () => {},
    });
    expect(lost).toEqual([null]);
  });
});

describe("UC-035: A3 a telemetry backend is down (dashboard side)", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function fakeEl() {
    const classes = new Set<string>();
    return {
      innerHTML: "",
      textContent: "",
      hidden: true,
      classList: {
        toggle: (c: string, on: boolean) => void (on ? classes.add(c) : classes.delete(c)),
        has: (c: string) => classes.has(c),
      },
    };
  }

  it("UC-035/A3 a rendered panel keeps its data and is marked stale", () => {
    vi.useFakeTimers();
    const grid = fakeEl();
    const fresh = fakeEl();
    vi.stubGlobal("document", {
      getElementById: (id: string) => (id === "grid" ? grid : fresh),
    });
    const view = new ExperimentsView("grid", "fresh");
    view.render([]);
    const painted = grid.innerHTML;

    vi.advanceTimersByTime(20_000);
    view.markStale(); // a poll failed; keep what is on screen

    expect(grid.innerHTML).toBe(painted);
    expect(fresh.textContent).toBe("stale — last updated 20s ago");
    expect(fresh.classList.has("stale")).toBe(true);
  });

  it("UC-035/A3 a panel that never rendered shows no freshness hint", () => {
    const fresh = fakeEl();
    vi.stubGlobal("document", { getElementById: () => fresh });
    const view = new ExperimentsView("grid", "fresh");
    view.markStale();
    expect(view.rendered).toBe(false);
    expect(fresh.hidden).toBe(true);
  });
});
