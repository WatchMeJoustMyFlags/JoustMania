/**
 * UC-035 BR-010: the StageSettings component on a fake element (no DOM library).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { StageSettings } from "../components/StageSettings.js";

function fakeElement() {
  const classes = new Set<string>();
  return {
    hidden: true,
    textContent: "",
    classList: {
      toggle: (name: string, on: boolean) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name: string) => classes.has(name),
    },
  };
}

describe("UC-035 BR-010 StageSettings component", () => {
  afterEach(() => vi.unstubAllGlobals());

  function mount() {
    const el = fakeElement();
    vi.stubGlobal("document", { getElementById: (id: string) => (id === "stage-settings" ? el : null) });
    return { el, strip: new StageSettings() };
  }

  it("UC-035/BR-010 update shows the strip with origin tags and no stale marker", () => {
    const { el, strip } = mount();
    strip.update({
      gauges: { sensitivity: 3, shieldMode: "trigger", hold: [] },
      menu: { lifecycleLive: true, sensitivity: 3, numTeams: 3, shieldMode: null, operator: { sensitivity: "fast", num_teams: "three" } },
    });
    expect(el.hidden).toBe(false);
    expect(el.classList.contains("stale")).toBe(false);
    expect(el.textContent).toBe("Shield: trigger · Sensitivity: fast (operator) · Teams: 3 (operator)");
  });

  it("UC-035/BR-010 a failed poll keeps the last data marked stale; with none it shows dashes", () => {
    const { el, strip } = mount();
    strip.markStale();
    expect(el.hidden).toBe(false);
    expect(el.textContent).toBe("Shield: — · Sensitivity: — · Teams: — · stale");
    expect(el.classList.contains("stale")).toBe(true);

    strip.update({ gauges: { sensitivity: 0, shieldMode: "instant", hold: [] }, menu: null });
    strip.markStale();
    expect(el.textContent).toBe("Shield: instant · Sensitivity: ultra slow · Teams: — · stale");
    strip.update({ gauges: { sensitivity: 0, shieldMode: "instant", hold: [] }, menu: null });
    expect(el.classList.contains("stale")).toBe(false);
    expect(el.textContent).not.toContain("stale");
  });
});
