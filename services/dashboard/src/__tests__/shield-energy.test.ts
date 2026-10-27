/**
 * The hold-mode energy bar (#2096, UC-035 BR-008): `ShieldStore` folds
 * `player_shield_energy`, interpolates on the monotonic clock and clears with the
 * game; `holdEnergyBar` is what the controller tile renders.
 */
import { describe, expect, it } from "vitest";
import { ShieldStore, holdEnergyBar } from "../shield.js";

const SERIAL = "00:06:F7:AA:BB:CC";
const WALL = 1_700_000_000_000;

function energyEvent(fields: Record<string, string> = {}) {
  return {
    eventType: "player_shield_energy",
    data: {
      serial: SERIAL,
      state: "charged",
      source: "none",
      energy_ms: "2000",
      full_ms: "5000",
      cap_ms: "5000",
      min_burst_ms: "300",
      budget_ms: "15000",
      budget_remaining_ms: "12000",
      rate_ms_per_s: "0",
      remaining_ms: "0",
      ...fields,
    },
  };
}

function barAt(store: ShieldStore, monoMs: number) {
  return holdEnergyBar(store.get(SERIAL, WALL, monoMs));
}

describe("UC-035 BR-008: hold energy bar levels", () => {
  it.each([
    ["empty", { state: "empty", energy_ms: "100" }, 2],
    ["low", { energy_ms: "1000" }, 20],
    ["charged", { energy_ms: "3000" }, 60],
    ["full", { energy_ms: "5000" }, 100],
    ["active", { state: "active", energy_ms: "4000", rate_ms_per_s: "-1000", remaining_ms: "4000" }, 80],
  ])("renders %s", (level, fields, fill) => {
    const store = new ShieldStore();
    expect(store.apply(energyEvent(fields), WALL, 0)).toBe(true);
    const bar = barAt(store, 0)!;
    expect(bar.className.split(" ")).toContain(`hold-${level}`);
    expect(bar.fillPercent).toBe(fill);
    expect(bar.budgetPercent).toBe(80);
    expect(bar.label).toBe(level);
  });

  it("shows the energy, cap and budget as text", () => {
    const store = new ShieldStore();
    store.apply(energyEvent({ energy_ms: "2400", budget_remaining_ms: "9100" }), WALL, 0);
    expect(barAt(store, 0)!.text).toBe("Hold 2.4s/5s · budget 9.1s");
  });

  it("names a used-up budget and the last source", () => {
    const store = new ShieldStore();
    store.apply(energyEvent({ state: "exhausted", energy_ms: "0", budget_remaining_ms: "0", source: "agent" }), WALL, 0);
    const bar = barAt(store, 0)!;
    expect(bar.label).toBe("budget used up");
    expect(bar.className).toContain("hold-exhausted");
    expect(bar.source).toBe("agent");
  });

  it("keeps the last source when an event names none", () => {
    const store = new ShieldStore();
    store.apply(energyEvent({ source: "agent" }), WALL, 0);
    store.apply(energyEvent({ state: "active", rate_ms_per_s: "-1000", remaining_ms: "2000" }), WALL, 10);
    expect(barAt(store, 10)!.source).toBe("agent");
  });

  it("shows no bar without an energy event (not a hold game)", () => {
    const store = new ShieldStore();
    expect(holdEnergyBar(store.get(SERIAL, WALL, 0))).toBeNull();
    store.apply({ eventType: "player_shielded", data: { serial: SERIAL, kind: "full", until_ms: String(WALL + 5000) } }, WALL, 0);
    expect(store.get(SERIAL, WALL, 0)?.active).not.toBeNull();
    expect(holdEnergyBar(store.get(SERIAL, WALL, 0))).toBeNull();
  });

  it("ignores a malformed event", () => {
    const store = new ShieldStore();
    expect(store.apply(energyEvent({ state: "bogus" }), WALL, 0)).toBe(false);
    expect(store.apply(energyEvent({ energy_ms: "x" }), WALL, 0)).toBe(false);
    expect(store.apply(energyEvent({ cap_ms: "0" }), WALL, 0)).toBe(false);
    expect(store.get(SERIAL, WALL, 0)).toBeNull();
  });
});

describe("UC-035 BR-008: interpolation on the monotonic clock", () => {
  it("drains an active pool at the event's rate and stops at the activation's end", () => {
    const store = new ShieldStore();
    store.apply(
      energyEvent({ state: "active", energy_ms: "3000", rate_ms_per_s: "-1000", remaining_ms: "2000", budget_remaining_ms: "10000" }),
      WALL,
      1000,
    );
    expect(barAt(store, 2000)!.text).toBe("Hold 2.0s/5s · budget 9.0s");
    expect(barAt(store, 2000)!.label).toBe("active");
    // Past the end, no event yet (a dropped one): the idle level at the drained energy.
    const after = barAt(store, 9000)!;
    expect(after.text).toBe("Hold 1.0s/5s · budget 8.0s");
    expect(after.label).toBe("low");
  });

  it("fills a recharging pool up to the full level, then shows full", () => {
    const store = new ShieldStore();
    store.apply(energyEvent({ state: "recharging", source: "recharge", energy_ms: "1000", rate_ms_per_s: "500" }), WALL, 0);
    const filling = barAt(store, 4000)!;
    expect(filling.fillPercent).toBe(60);
    expect(filling.label).toBe("recharging");
    expect(filling.source).toBe("recharge");
    const full = barAt(store, 60_000)!;
    expect(full.fillPercent).toBe(100);
    expect(full.label).toBe("full");
  });

  it("never reads the wall clock: a wildly wrong browser clock changes nothing", () => {
    const store = new ShieldStore();
    store.apply(energyEvent({ state: "active", energy_ms: "3000", rate_ms_per_s: "-1000", remaining_ms: "3000" }), 0, 500);
    expect(holdEnergyBar(store.get(SERIAL, WALL * 2, 1500))!.text).toBe(holdEnergyBar(store.get(SERIAL, -WALL, 1500))!.text);
    expect(holdEnergyBar(store.get(SERIAL, 0, 1500))!.fillPercent).toBe(40);
  });

  it("clamps to the cap", () => {
    const store = new ShieldStore();
    store.apply(energyEvent({ energy_ms: "9000" }), WALL, 0);
    expect(barAt(store, 0)!.fillPercent).toBe(100);
  });
});

describe("UC-035 BR-008: the bar clears with the game", () => {
  it.each(["game_start", "game_starting", "game_ended", "game_force_ended", "game_error"])("on %s", (eventType) => {
    const store = new ShieldStore();
    store.apply(energyEvent(), WALL, 0);
    expect(store.apply({ eventType, data: {} }, WALL, 0)).toBe(true);
    expect(store.get(SERIAL, WALL, 0)).toBeNull();
  });

  it("on player_death", () => {
    const store = new ShieldStore();
    store.apply(energyEvent(), WALL, 0);
    expect(store.apply({ eventType: "player_death", data: { serial: SERIAL } }, WALL, 0)).toBe(true);
    expect(store.get(SERIAL, WALL, 0)).toBeNull();
  });

  it("on a lost event stream (A4: clear)", () => {
    const store = new ShieldStore();
    store.apply(energyEvent(), WALL, 0);
    expect(store.clear()).toBe(true);
    expect(store.get(SERIAL, WALL, 0)).toBeNull();
  });

  it("an energy event clears a timed shield badge of the same player", () => {
    const store = new ShieldStore();
    store.apply({ eventType: "player_shielded", data: { serial: SERIAL, kind: "full", until_ms: String(WALL + 5000) } }, WALL, 0);
    store.apply(energyEvent({ state: "active", rate_ms_per_s: "-1000", remaining_ms: "2000" }), WALL, 0);
    expect(store.get(SERIAL, WALL, 0)!.active).toBeNull();
  });
});
