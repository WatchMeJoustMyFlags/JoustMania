/**
 * Controller battery roster (UC-035 A5, BR-007; UC-004 BR-006, #2111): sorting,
 * the level mapping, the stale state and the Identify wiring.
 */
import { describe, expect, it, vi } from "vitest";
import { batteryPercent, batteryView } from "../motion.js";
import {
  ROSTER_STALE_TEXT,
  ROSTER_UNAVAILABLE_TEXT,
  RosterModel,
  renderRoster,
  rosterRows,
} from "../roster.js";
import { identifyController } from "../identify.js";

describe("UC-035 battery roster (A5, BR-007)", () => {
  it("UC-035 BR-007 maps the Rust percentage and the 0-5 level to one scale", () => {
    expect(batteryPercent(100)).toBe(100);
    expect(batteryPercent(60)).toBe(60);
    expect(batteryPercent(20)).toBe(20);
    expect(batteryPercent(5)).toBe(100);
    expect(batteryPercent(3)).toBe(60);
    expect(batteryPercent(0)).toBe(0);
    expect(batteryPercent(undefined)).toBe(0);
    expect(batteryPercent(250)).toBe(100);
    expect(batteryPercent(-3)).toBe(0);
  });

  it("UC-035 BR-007 colours the bar high above 50, medium above 20, otherwise low", () => {
    expect(batteryView(100).className).toBe("high");
    expect(batteryView(60).className).toBe("high");
    expect(batteryView(40).className).toBe("medium");
    expect(batteryView(20).className).toBe("low");
    expect(batteryView(0).className).toBe("low");
    // 0-5 levels keep their colour (the game tile)
    expect(batteryView(4)).toEqual({ percent: 80, className: "high" });
  });

  it("UC-035 A5 sorts lowest battery first, then by name and serial", () => {
    const rows = rosterRows([
      { serial: "C", name: "Full", battery: 100 },
      { serial: "B", name: "Zed", battery: 20 },
      { serial: "A", name: "Amy", battery: 20 },
      { serial: "D", name: "Half", battery: 3 },
    ]);
    expect(rows.map((r) => r.serial)).toEqual(["A", "B", "D", "C"]);
    expect(rows.map((r) => r.label)).toEqual(["20%", "20%", "60%", "100%"]);
  });

  it("UC-035 A5 shows a controller with no reported battery as 0% and names it by serial", () => {
    const [row] = rosterRows([{ serial: "S1" }]);
    expect(row).toMatchObject({ name: "S1", percent: 0, className: "low", label: "0%" });
  });

  it("UC-035 A5 marks the roster stale on a failed poll and keeps the rows, a good poll clears it", () => {
    const model = new RosterModel();
    model.update({ controllers: [{ serial: "A", name: "Amy", battery: 80 }] });
    expect(model.stale).toBe(false);
    expect(renderRoster(model)).not.toContain(ROSTER_STALE_TEXT);

    model.fail();
    const html = renderRoster(model);
    expect(model.stale).toBe(true);
    expect(html).toContain(ROSTER_STALE_TEXT);
    expect(html).toContain("roster-list stale");
    expect(html).toContain("Amy");

    model.update({ controllers: [] });
    expect(model.stale).toBe(false);
    expect(renderRoster(model)).toContain("No controllers connected");
  });

  it("UC-035 A5 names the failure when no roster was ever received", () => {
    const model = new RosterModel();
    expect(renderRoster(model)).toContain("Loading");
    model.fail();
    expect(renderRoster(model)).toContain(ROSTER_UNAVAILABLE_TEXT);
    expect(renderRoster(model)).not.toContain("<li");
  });

  it("UC-035 A5 escapes controller names", () => {
    const model = new RosterModel();
    model.update({ controllers: [{ serial: "A", name: "<img src=x onerror=1>", battery: 60 }] });
    const html = renderRoster(model);
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  it("UC-004 BR-006 each row carries an Identify button for its serial", async () => {
    const model = new RosterModel();
    model.update({ controllers: [{ serial: "AA:BB", name: "Amy", battery: 40 }] });
    expect(renderRoster(model)).toContain('class="identify-btn roster-identify" data-serial="AA:BB"');

    // The click handler uses the card's helper with the row's serial.
    const call = vi.fn().mockResolvedValue({ success: false, outcome: "in_game" });
    const status = await identifyController("AA:BB", call);
    expect(call).toHaveBeenCalledWith({ serial: "AA:BB", durationMs: 3000 }, expect.any(AbortSignal));
    expect(status).toMatchObject({ text: "In game", ok: false });
  });
});
