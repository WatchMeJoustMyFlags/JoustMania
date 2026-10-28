// @vitest-environment happy-dom
/**
 * UC-035 A4: the controller grid after a stream loss, on the real ControllerGrid and a real DOM (#2285).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ControllerGrid } from "../components/ControllerGrid.js";
import { CONTROLLER_STREAM_LOST, keepControllerStreamOpen } from "../controllerStream.js";

const frame = (serial: string) => ({ serial }) as never;

describe("UC-035: A4 controller grid after a stream reconnect", () => {
  let container: HTMLElement;
  let grid: ControllerGrid;

  beforeEach(() => {
    document.body.innerHTML = '<div id="grid"></div>';
    container = document.getElementById("grid")!;
    grid = new ControllerGrid("grid");
  });

  const serials = () => [...container.querySelectorAll(".controller-card")].length;
  const placeholders = () => [...container.querySelectorAll(".loading")].map((e) => e.textContent);

  it("UC-035/A4 shows the known cards again after an error and a reconnect, without placeholders", () => {
    grid.setLoading(true);
    grid.render([frame("AA"), frame("BB")]);
    expect(serials()).toBe(2);

    grid.setError(CONTROLLER_STREAM_LOST);
    grid.setLoading(false);
    grid.render([frame("AA"), frame("BB")]);

    expect(serials()).toBe(2);
    expect(placeholders()).toEqual([]);
  });

  it("UC-035/A4 drops the Connecting placeholder when the first frame arrives", () => {
    grid.setLoading(true);
    grid.setLoading(false);
    grid.render([frame("AA")]);
    expect(serials()).toBe(1);
    expect(placeholders()).toEqual([]);
  });

  it("UC-035/A4 drops 'No controllers connected' once a controller connects", () => {
    grid.setLoading(true);
    grid.render([]);
    expect(placeholders()).toEqual(["No controllers connected"]);
    grid.render([frame("AA")]);
    expect(serials()).toBe(1);
    expect(placeholders()).toEqual([]);
  });

  it("UC-035/A4 the stream wiring restores the cards after the stream drops and reopens", async () => {
    grid.setLoading(true);
    async function* lost(): AsyncGenerator<string[]> {
      yield ["AA", "BB"];
      throw new Error("lost");
    }
    async function* back(): AsyncGenerator<string[]> {
      yield ["AA", "BB"];
    }
    const queue = [lost, back];
    const open = () => queue.shift()!();
    let reopen: (() => void) | undefined;
    const run = () =>
      keepControllerStreamOpen<string[]>({
        open,
        onUpdate: (serialList) => grid.render(serialList.map(frame)),
        grid,
        setStreaming: () => {},
        schedule: (fn) => {
          reopen = fn;
        },
      });
    vi.spyOn(console, "error").mockImplementation(() => {});
    await run();
    expect(placeholders()).toEqual([CONTROLLER_STREAM_LOST]);

    const first = reopen;
    reopen = undefined;
    first?.();
    await vi.waitFor(() => expect(reopen).toBeDefined());
    // the clean end of the second stream shows the loss again; the next frame must restore the cards
    grid.setLoading(false);
    grid.render([frame("AA"), frame("BB")]);
    expect(serials()).toBe(2);
    expect(placeholders()).toEqual([]);
    vi.restoreAllMocks();
  });
});
