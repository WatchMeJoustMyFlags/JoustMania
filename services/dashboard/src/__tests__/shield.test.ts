/**
 * Agent shield on the controller tiles (#1301): the shield badge folded from
 * game events, and the live LED colour (shield fade included) rendered as CSS.
 */
import { describe, expect, it } from "vitest";
import { ShieldStore, shieldBadge } from "../shield.js";
import { ledColorCss } from "../components/ControllerCard.js";

const NOW = 1_760_000_000_000;

function shielded(serial: string, kind: string, untilMs: number) {
  return {
    eventType: "player_shielded",
    data: { serial, kind, seconds: "5.0", until_ms: String(untilMs) },
  };
}

describe("UC-040: A16 shield badge on the controller tile", () => {
  it("UC-040/A16 shows a full shield with its remaining time", () => {
    const store = new ShieldStore();
    expect(store.apply(shielded("MOCK0001", "full", NOW + 5000))).toBe(true);

    const badge = shieldBadge(store.get("MOCK0001", NOW), NOW);
    expect(badge).toEqual({ text: "Shield 5.0s", className: "shield-badge shield-full" });
    expect(store.get("MOCK0002", NOW)).toBeNull();
  });

  it("UC-040/A16 BR-009 shows the partial shield as the weaker badge", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0002", "partial", NOW + 3000));

    const badge = shieldBadge(store.get("MOCK0002", NOW), NOW);
    expect(badge?.text).toBe("Partial 3.0s");
    expect(badge?.className).toContain("shield-partial");
  });

  it("UC-040/A16 BR-008 marks the last second as ending (the LED blinks)", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000));

    expect(shieldBadge(store.get("MOCK0001", NOW + 2500), NOW + 2500)?.className).not.toContain("shield-ending");
    const ending = shieldBadge(store.get("MOCK0001", NOW + 4200), NOW + 4200);
    expect(ending?.text).toBe("Shield 0.8s");
    expect(ending?.className).toContain("shield-ending");
  });

  it("UC-040/A16 BR-010 an extension replaces the shield and restarts the countdown", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 2000));
    store.apply(shielded("MOCK0001", "full", NOW + 8000));

    expect(shieldBadge(store.get("MOCK0001", NOW + 1000), NOW + 1000)?.text).toBe("Shield 7.0s");
  });

  it("UC-040/A16 the badge disappears when the protection ends", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000));

    expect(store.get("MOCK0001", NOW + 5000)).toBeNull();
    expect(shieldBadge(null, NOW)).toBeNull();
  });

  it("UC-040/A16 ignores malformed events", () => {
    const store = new ShieldStore();
    expect(store.apply({ eventType: "player_shielded", data: { serial: "MOCK0001", until_ms: "soon" } })).toBe(false);
    expect(store.apply({ eventType: "game_started", data: {} })).toBe(false);
    expect(store.get("MOCK0001", NOW)).toBeNull();
  });
});

describe("UC-040: A17 shield badge ends early", () => {
  it("UC-040/A17 clears a player's badge on player_death", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000));
    store.apply(shielded("MOCK0002", "full", NOW + 5000));

    expect(store.apply({ eventType: "player_death", data: { serial: "MOCK0001" } })).toBe(true);
    expect(store.get("MOCK0001", NOW)).toBeNull();
    expect(store.get("MOCK0002", NOW)).not.toBeNull();
  });

  it.each(["game_ended", "game_force_ended"])("UC-040/A17 clears every badge on %s", (eventType) => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000));
    store.apply(shielded("MOCK0002", "partial", NOW + 5000));

    expect(store.apply({ eventType, data: {} })).toBe(true);
    expect(store.get("MOCK0001", NOW)).toBeNull();
    expect(store.get("MOCK0002", NOW)).toBeNull();
  });
});

describe("UC-040: A16 live LED colour on the tile (shield fade without hardware)", () => {
  it("UC-040/A16 BR-008 renders the shield frames the stream carries", () => {
    // full shield colour at t=0, ~65 % mid-fade, an off frame of the blink, then the player colour
    expect(ledColorCss({ r: 255, g: 255, b: 255 }).background).toBe("rgb(255, 255, 255)");
    expect(ledColorCss({ r: 166, g: 166, b: 166 }).background).toBe("rgb(166, 166, 166)");
    expect(ledColorCss({}).background).toBe("rgb(0, 0, 0)");
    expect(ledColorCss({ g: 255 }).background).toBe("rgb(0, 255, 0)");
  });

  it("UC-040/A16 shows no LED before the first frame", () => {
    expect(ledColorCss(undefined)).toEqual({ background: "#333", glow: "transparent" });
  });
});
