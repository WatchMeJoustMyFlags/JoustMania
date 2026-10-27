/**
 * UC-035 BR-009: the HCI adapter on each controller card (#2133), read from
 * VictoriaMetrics' controller_adapter_info. No DOM library is installed, so the
 * card and grid run against a minimal fake DOM.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ADAPTER_QUERY, adapterCounts, adapterCountsText, fetchAdapters, parseAdapters, showAdapterBadge } from "../adapters.js";
import type { VmQueryResponse } from "../vm.js";

function body(...series: Array<[string, string, number]>): VmQueryResponse {
  return {
    status: "success",
    data: {
      resultType: "vector",
      result: series.map(([serial, adapter, ts]) => ({ metric: { serial, adapter }, value: [ts, String(ts)] as [number, string] })),
    },
  };
}

describe("UC-035 BR-009 adapter on the card: parse", () => {
  it("UC-035/BR-009 two serials on two adapters", () => {
    const map = parseAdapters(body(["AA", "hci0", 100], ["BB", "hci1", 100]));
    expect(Object.fromEntries(map)).toEqual({ AA: "hci0", BB: "hci1" });
  });

  it("UC-035/BR-009 a serial with two series (a move) takes the newest timestamp, in either order", () => {
    expect(parseAdapters(body(["AA", "hci0", 100], ["AA", "hci1", 130])).get("AA")).toBe("hci1");
    expect(parseAdapters(body(["AA", "hci1", 130], ["AA", "hci0", 100])).get("AA")).toBe("hci1");
  });

  it("UC-035/BR-009 an empty result gives no label", () => {
    expect(parseAdapters(body()).size).toBe(0);
  });

  it("UC-035/BR-009 a malformed or failed body gives an empty map", () => {
    expect(parseAdapters(null).size).toBe(0);
    expect(parseAdapters({ status: "error" }).size).toBe(0);
    expect(parseAdapters({ status: "success", data: { resultType: "vector", result: "x" as never } }).size).toBe(0);
    expect(parseAdapters({ status: "success", data: { resultType: "vector", result: [{ metric: {}, value: [1, "1"] }] } }).size).toBe(0);
    expect(parseAdapters({ status: "success", data: { resultType: "vector", result: [{ metric: { serial: "AA", adapter: "hci0" }, value: [1, "nan?"] }] } }).size).toBe(0);
  });

  it("UC-035/BR-009 counts per adapter, header text and the single-adapter rule", () => {
    const map = parseAdapters(body(["AA", "hci1", 1], ["BB", "hci0", 1], ["CC", "hci0", 1]));
    const counts = adapterCounts(map);
    expect(counts).toEqual([["hci0", 2], ["hci1", 1]]);
    expect(adapterCountsText(counts)).toBe("hci0: 2 · hci1: 1");
    expect(showAdapterBadge(counts)).toBe(true);
    // only controllers still on screen are counted
    expect(adapterCounts(map, ["AA", "ZZ"])).toEqual([["hci1", 1]]);
    expect(showAdapterBadge([["hci0", 9]])).toBe(false);
    expect(adapterCountsText([["hci0", 9]])).toBe("hci0: 9");
  });
});

describe("UC-035 BR-009 adapter on the card: query", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("UC-035/BR-009 asks VictoriaMetrics for timestamp(controller_adapter_info) with nocache=1", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(body(["AA", "hci0", 5])), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("location", { origin: "http://pi" });

    const map = await fetchAdapters();

    const url = new URL(String((fetchMock.mock.calls[0] as unknown[])[0]), "http://pi");
    expect(url.pathname).toBe("/victoria-metrics/api/v1/query");
    expect(url.searchParams.get("query")).toBe(ADAPTER_QUERY);
    expect(url.searchParams.get("query")).toContain("controller_adapter_info");
    expect(url.searchParams.get("nocache")).toBe("1");
    expect(map?.get("AA")).toBe("hci0");
  });

  it("UC-035/BR-009 VictoriaMetrics unreachable gives null, never a throw", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    vi.stubGlobal("location", { origin: "http://pi" });
    expect(await fetchAdapters()).toBeNull();
  });
});

class FakeElement {
  hidden = false;
  disabled = false;
  textContent = "";
  className = "";
  title = "";
  style: Record<string, unknown> = { setProperty: () => {} };
  private readonly classes = new Set<string>();
  readonly children = new Map<string, FakeElement>();
  classList = {
    toggle: (name: string, force?: boolean) => {
      const on = force ?? !this.classes.has(name);
      if (on) this.classes.add(name);
      else this.classes.delete(name);
      return on;
    },
    contains: (name: string) => this.classes.has(name),
    add: (name: string) => void this.classes.add(name),
    remove: (name: string) => void this.classes.delete(name),
  };
  set innerHTML(_html: string) {}
  addEventListener() {}
  appendChild() {}
  remove() {}
  querySelector(selector: string): FakeElement {
    let child = this.children.get(selector);
    if (!child) {
      child = new FakeElement();
      this.children.set(selector, child);
    }
    return child;
  }
}

describe("UC-035 BR-009 adapter on the card: rendering", () => {
  const byId = new Map<string, FakeElement>();

  beforeEach(() => {
    byId.clear();
    byId.set("grid", new FakeElement());
    byId.set("counts", new FakeElement());
    vi.stubGlobal("document", { createElement: () => new FakeElement(), getElementById: (id: string) => byId.get(id) ?? null });
    vi.stubGlobal("location", { origin: "http://pi" });
  });
  afterEach(() => vi.unstubAllGlobals());

  const frame = (serial: string) => ({ serial }) as never;

  async function newGrid() {
    const { ControllerGrid } = await import("../components/ControllerGrid.js");
    return new ControllerGrid("grid", "counts");
  }
  const badge = (grid: unknown, serial: string) => {
    const card = (grid as { cards: Map<string, { element: unknown }> }).cards.get(serial)!;
    return (card.element as FakeElement).querySelector(".adapter-badge");
  };

  it("UC-035/BR-009 a badge names the adapter and the header counts per adapter", async () => {
    const grid = await newGrid();
    grid.render([frame("AA"), frame("BB"), frame("CC")]);
    grid.setAdapters(new Map([["AA", "hci0"], ["BB", "hci1"], ["CC", "hci1"]]));

    expect(badge(grid, "BB").textContent).toBe("hci1");
    expect(badge(grid, "BB").hidden).toBe(false);
    expect(byId.get("counts")!.textContent).toBe("hci0: 1 · hci1: 2");
  });

  it("UC-035/BR-009 a controller without a series has no badge; one adapter hides the badges but not the header", async () => {
    const grid = await newGrid();
    grid.render([frame("AA"), frame("BB")]);
    grid.setAdapters(new Map([["AA", "hci0"]]));

    expect(badge(grid, "AA").hidden).toBe(true);
    expect(badge(grid, "BB").hidden).toBe(true);
    expect(byId.get("counts")!.textContent).toBe("hci0: 1");
  });

  it("UC-035/BR-009 a card that appears later gets its badge on the next render", async () => {
    const grid = await newGrid();
    grid.render([frame("AA")]);
    grid.setAdapters(new Map([["AA", "hci0"], ["BB", "hci1"]]));
    grid.render([frame("AA"), frame("BB")]);

    expect(badge(grid, "BB").textContent).toBe("hci1");
  });

  it("UC-035/A3 VictoriaMetrics down keeps the last labels and marks them stale; before any render none show", async () => {
    const grid = await newGrid();
    grid.render([frame("AA"), frame("BB")]);
    grid.setAdapters(null);
    expect(badge(grid, "AA").hidden).toBe(true);
    expect(byId.get("counts")!.hidden).toBe(true);

    grid.setAdapters(new Map([["AA", "hci0"], ["BB", "hci1"]]));
    grid.setAdapters(null);

    expect(badge(grid, "AA").textContent).toBe("hci0");
    expect(badge(grid, "AA").classList.contains("stale")).toBe(true);
    expect(badge(grid, "AA").title).toContain("stale");
    expect(byId.get("counts")!.textContent).toBe("hci0: 1 · hci1: 1 · stale");
  });
});
