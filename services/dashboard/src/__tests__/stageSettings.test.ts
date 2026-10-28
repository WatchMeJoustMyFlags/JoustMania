/**
 * UC-035 BR-010: the stage settings strip (#2289). Mocked VictoriaMetrics and
 * GetMenuStatus answers; no DOM library, so the component runs on a fake element.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { STAGE_QUERY, STAGE_TS_QUERY, fetchStageSettings, parseGauges, parseMenu, stageRows } from "../stageSettings.js";
import type { VmQueryResponse } from "../vm.js";

function vm(...series: Array<[Record<string, string>, number]>): VmQueryResponse {
  return {
    status: "success",
    data: { resultType: "vector", result: series.map(([metric, v]) => ({ metric, value: [1, String(v)] as [number, string] })) },
  };
}

const HOLD = vm(
  [{ __name__: "game_shield_activation_mode", mode: "hold" }, 1],
  [{ __name__: "game_sensitivity" }, 3],
  [{ __name__: "game_shield_hold_setting", setting: "shield.energy_budget_seconds" }, 20],
  [{ __name__: "game_shield_hold_setting", setting: "shield.energy_max_seconds" }, 6],
  [{ __name__: "game_shield_hold_setting", setting: "shield.min_burst_seconds" }, 0.5],
);

describe("UC-035 BR-010 stage settings", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("UC-035/BR-010 gauges give shield mode, hold values (max first) and sensitivity", () => {
    expect(parseGauges(HOLD)).toEqual({ sensitivity: 3, shieldMode: "hold", hold: [["max", 6], ["budget", 20]] });
  });

  it("UC-035/BR-010 a live game wins over the menu; teams and origin come from the menu", () => {
    const menu = parseMenu({ lifecycleLive: true, sensitivity: 1, numTeams: 3, shieldActivation: "trigger", operatorSettings: { sensitivity: "automatic", num_teams: "three", "shield.activation": "hold" } });
    const rows = stageRows({ gauges: parseGauges(HOLD), menu });
    expect(rows).toEqual([
      { label: "Shield", value: "hold (max 6s, budget 20s)", origin: "operator" },
      { label: "Sensitivity", value: "fast", origin: "automatic" },
      { label: "Teams", value: "3", origin: "operator" },
    ]);
  });

  it("UC-035/BR-010 in the lobby (no gauge) the menu values show", () => {
    const rows = stageRows({ gauges: parseGauges(vm()), menu: parseMenu({ numTeams: 2, shieldActivation: "instant" }) });
    expect(rows.map((r) => r.value)).toEqual(["instant", "ultra slow", "2"]);
    expect(rows.every((r) => r.origin === "")).toBe(true);
  });

  it("UC-035/BR-010 two game_ids: the game with the newest sample wins, values are never mixed", () => {
    const values = vm(
      [{ __name__: "game_shield_activation_mode", mode: "instant", game_id: "old" }, 1],
      [{ __name__: "game_sensitivity", game_id: "old" }, 4],
      [{ __name__: "game_shield_hold_setting", setting: "shield.energy_max_seconds", game_id: "old" }, 9],
      [{ __name__: "game_shield_activation_mode", mode: "trigger", game_id: "new" }, 1],
      [{ __name__: "game_sensitivity", game_id: "new" }, 1],
    );
    const ts = vm([{ game_id: "old" }, 100], [{ game_id: "new" }, 160], [{ game_id: "new" }, 150]);
    expect(parseGauges(values, ts)).toEqual({ sensitivity: 1, shieldMode: "trigger", hold: [] });
    // result order must not matter
    const swapped = vm([{ game_id: "new" }, 100], [{ game_id: "old" }, 160]);
    expect(parseGauges(values, swapped)).toEqual({ sensitivity: 4, shieldMode: "instant", hold: [["max", 9]] });
    // two games and no timestamps: nothing is picked rather than a guess
    expect(parseGauges(values, null)).toEqual({ sensitivity: null, shieldMode: null, hold: [] });
    expect(STAGE_TS_QUERY).toContain("tlast_over_time");
  });

  it("UC-035/BR-010 lobby after a game: the menu wins over a lingering game gauge", () => {
    const menu = parseMenu({ sensitivity: 2, numTeams: 2, shieldActivation: "instant" });
    expect(menu?.lifecycleLive).toBe(false);
    const rows = stageRows({ gauges: parseGauges(HOLD), menu });
    expect(rows.map((r) => r.value)).toEqual(["instant", "medium", "2"]);
    // a live lifecycle keeps the gauge
    const live = stageRows({ gauges: parseGauges(HOLD), menu: parseMenu({ lifecycleLive: true, shieldActivation: "instant" }) });
    expect(live[0].value).toContain("hold");
  });

  it("UC-035/BR-010 missing data shows a dash", () => {
    const rows = stageRows({ gauges: parseGauges({ status: "error" }), menu: null });
    expect(rows.map((r) => r.value)).toEqual(["—", "—", "—"]);
    expect(parseMenu({ shieldActivation: "" })?.shieldMode).toBeNull();
    expect(parseMenu({})?.numTeams).toBeNull();
  });

  it("UC-035/BR-010 queries VictoriaMetrics for the primary game with nocache and asks the menu", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(HOLD), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("location", { origin: "http://pi" });
    const menu = vi.fn(async () => ({ numTeams: 4 }));

    const data = await fetchStageSettings(menu);

    const url = new URL(String((fetchMock.mock.calls[0] as unknown[])[0]), "http://pi");
    expect(url.searchParams.get("query")).toBe(STAGE_QUERY);
    expect(STAGE_QUERY).toContain("game_sensitivity");
    expect(url.searchParams.get("nocache")).toBe("1");
    expect(menu).toHaveBeenCalledTimes(1);
    expect(data.menu?.numTeams).toBe(4);
  });

  it("UC-035/BR-010 one source down keeps the other; both down throws", async () => {
    vi.stubGlobal("location", { origin: "http://pi" });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const onlyMenu = await fetchStageSettings(async () => ({ numTeams: 5 }));
    expect(onlyMenu.gauges).toBeNull();
    expect(stageRows(onlyMenu)[2].value).toBe("5");
    await expect(fetchStageSettings(async () => { throw new Error("503"); })).rejects.toThrow();

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(HOLD), { status: 200 })));
    const onlyVm = await fetchStageSettings(async () => { throw new Error("503"); });
    expect(onlyVm.menu).toBeNull();
    expect(stageRows(onlyVm)[0].value).toContain("hold");
    expect(stageRows(onlyVm)[2].value).toBe("—");
  });
});
