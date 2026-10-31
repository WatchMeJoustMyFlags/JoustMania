/**
 * Agent shield on the controller tiles (#1301): the shield badge folded from
 * game events, and the live LED colour (shield fade included) rendered as CSS.
 */
import { describe, expect, it } from "vitest";
import { ShieldStore, shieldBadge, type GameEventLike } from "../shield.js";
import { ledColorCss } from "../components/ControllerCard.js";

const NOW = 1_760_000_000_000;

function shielded(serial: string, kind: string, untilMs: number) {
  return {
    eventType: "player_shielded",
    data: { serial, kind, seconds: String((untilMs - NOW) / 1000), until_ms: String(untilMs) },
  };
}

describe("UC-040: A16 shield badge on the controller tile", () => {
  it("UC-040/A16 shows a full shield with its remaining time", () => {
    const store = new ShieldStore();
    expect(store.apply(shielded("MOCK0001", "full", NOW + 5000), NOW, NOW)).toBe(true);

    const badge = shieldBadge(store.get("MOCK0001", NOW, NOW), NOW, NOW);
    expect(badge).toEqual({ text: "Shield 5.0s", className: "shield-badge shield-full" });
    expect(store.get("MOCK0002", NOW, NOW)).toBeNull();
  });

  it("UC-040/A16 BR-009 shows the toughness boost as the weaker badge", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0002", "toughness_boost", NOW + 3000), NOW, NOW);

    const badge = shieldBadge(store.get("MOCK0002", NOW, NOW), NOW, NOW);
    expect(badge?.text).toBe("Boost 3.0s");
    expect(badge?.className).toContain("shield-toughness_boost");
  });

  it("UC-040/A16 an older coordinator's kind partial still renders as the boost badge, never a full shield", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0002", "partial", NOW + 3000), NOW, NOW);

    const badge = shieldBadge(store.get("MOCK0002", NOW, NOW), NOW, NOW);
    expect(badge?.text).toBe("Boost 3.0s");
    expect(badge?.className).toContain("shield-toughness_boost");
  });

  it("UC-040/A16 BR-008 marks the last second as ending (the LED blinks)", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000), NOW, NOW);

    expect(shieldBadge(store.get("MOCK0001", NOW + 2500, NOW + 2500), NOW + 2500, NOW + 2500)?.className).not.toContain("shield-ending");
    const ending = shieldBadge(store.get("MOCK0001", NOW + 4200, NOW + 4200), NOW + 4200, NOW + 4200);
    expect(ending?.text).toBe("Shield 0.8s");
    expect(ending?.className).toContain("shield-ending");
  });

  it("UC-040/A16 BR-010 an extension replaces the shield and restarts the countdown", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 2000), NOW, NOW);
    store.apply(shielded("MOCK0001", "full", NOW + 8000), NOW, NOW);

    expect(shieldBadge(store.get("MOCK0001", NOW + 1000, NOW + 1000), NOW + 1000, NOW + 1000)?.text).toBe("Shield 7.0s");
  });

  it("UC-040/A16 the badge disappears when the protection ends", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000), NOW, NOW);

    expect(store.get("MOCK0001", NOW + 5000, NOW + 5000)).toBeNull();
    expect(shieldBadge(null, NOW)).toBeNull();
  });

  it("UC-040/A16 ignores malformed events", () => {
    const store = new ShieldStore();
    expect(store.apply({ eventType: "player_shielded", data: { serial: "MOCK0001", until_ms: "soon" } })).toBe(false);
    expect(store.apply({ eventType: "game_started", data: {} })).toBe(false);
    expect(store.get("MOCK0001", NOW, NOW)).toBeNull();
  });
});

describe("UC-040: A16 shield badge under Pi/browser clock skew (#2387)", () => {
  it.each([-3_600_000, 3_600_000])("UC-040/A16 counts down `seconds` on the monotonic clock with a Pi %i ms off", (skewMs) => {
    const store = new ShieldStore();
    // The Pi's until_ms is an hour off the browser's wall clock; seconds is skew-free.
    const event = { eventType: "player_shielded", data: { serial: "MOCK0001", kind: "full", seconds: "5.0", until_ms: String(NOW + skewMs + 5000) } };
    store.apply(event, NOW, 1000);

    expect(shieldBadge(store.get("MOCK0001", NOW, 1000), NOW, 1000)?.text).toBe("Shield 5.0s");
    // 2 s later on the monotonic clock (the wall clock may even jump)
    expect(shieldBadge(store.get("MOCK0001", NOW + 99, 3000), NOW + 99, 3000)?.text).toBe("Shield 3.0s");
    expect(store.get("MOCK0001", NOW, 6001)).toBeNull();
  });

  it("UC-040/A16 falls back to until_ms against the browser clock when the event has no seconds", () => {
    const store = new ShieldStore();
    store.apply({ eventType: "player_shielded", data: { serial: "MOCK0001", kind: "full", until_ms: String(NOW + 5000) } }, NOW, 1000);
    expect(shieldBadge(store.get("MOCK0001", NOW + 1000, 9999), NOW + 1000, 9999)?.text).toBe("Shield 4.0s");
  });
});

describe("UC-040: A17 shield badge ends early", () => {
  it("UC-040/A17 clears a player's badge on player_death", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000), NOW, NOW);
    store.apply(shielded("MOCK0002", "full", NOW + 5000), NOW, NOW);

    expect(store.apply({ eventType: "player_death", data: { serial: "MOCK0001" } })).toBe(true);
    expect(store.get("MOCK0001", NOW, NOW)).toBeNull();
    expect(store.get("MOCK0002", NOW, NOW)).not.toBeNull();
  });

  it.each(["game_ended", "game_force_ended"])("UC-040/A17 clears every badge on %s", (eventType) => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000), NOW, NOW);
    store.apply(shielded("MOCK0002", "toughness_boost", NOW + 5000), NOW, NOW);

    expect(store.apply({ eventType, data: {} })).toBe(true);
    expect(store.get("MOCK0001", NOW, NOW)).toBeNull();
    expect(store.get("MOCK0002", NOW, NOW)).toBeNull();
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

describe("UC-035: main step 2, controller tile LED colour", () => {
  it("UC-035 main step 2 shows the stream's LED colour with a glow of the same colour", () => {
    expect(ledColorCss({ r: 10, g: 200, b: 30 })).toEqual({
      background: "rgb(10, 200, 30)",
      glow: "rgba(10, 200, 30, 0.6)",
    });
  });

  it("UC-035 main step 2 an unlit tile has a grey dot and no glow", () => {
    expect(ledColorCss(undefined)).toEqual({ background: "#333", glow: "transparent" });
  });
});

function charged(serial: string, expiresMs: number) {
  return {
    eventType: "player_shield_charged",
    data: { serial, seconds: "5.0", expires_ms: String(expiresMs) },
  };
}

describe("UC-035: main step 2, shield charge badge (FR-084, #1837)", () => {
  it("UC-035/main shows Charged with the seconds to expiry, or none when it never expires", () => {
    const store = new ShieldStore();
    store.apply(charged("MOCK0001", NOW + 12_000), NOW);
    store.apply(charged("MOCK0002", 0), NOW);

    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)).toEqual({
      text: "Charged 12s",
      className: "shield-badge shield-charged",
    });
    expect(shieldBadge(store.get("MOCK0002", NOW + 600_000), NOW + 600_000)?.text).toBe("Charged");
  });

  it("UC-035/main the countdown ends with the expiry", () => {
    const store = new ShieldStore();
    store.apply(charged("MOCK0001", NOW + 3000), NOW);
    expect(store.get("MOCK0001", NOW + 3000)).toBeNull();
  });

  it("UC-035/main shows Charged without a countdown when the clocks disagree (A11 of #1837)", () => {
    const store = new ShieldStore();
    store.apply(charged("MOCK0001", NOW - 60_000), NOW); // expiry already in the past
    store.apply(charged("MOCK0002", NOW + 131_000), NOW); // beyond the largest allowed window
    store.apply(charged("MOCK0003", NOW + 130_000), NOW); // at the limit: trusted

    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)?.text).toBe("Charged");
    expect(shieldBadge(store.get("MOCK0002", NOW + 500_000), NOW + 500_000)?.text).toBe("Charged");
    expect(shieldBadge(store.get("MOCK0003", NOW), NOW)?.text).toBe("Charged 130s");
  });

  it("UC-035/main a charge under a running shield shows both", () => {
    const store = new ShieldStore();
    store.apply(charged("MOCK0001", 0), NOW);
    store.apply(shielded("MOCK0001", "full", NOW + 4200), NOW);
    const badge = shieldBadge(store.get("MOCK0001", NOW), NOW);
    expect(badge?.text).toBe("Shield 4.2s · Charged");
    expect(badge?.className).toContain("shield-full");
    expect(badge?.className).toContain("shield-charged");
  });

  it("UC-035/main the existing shield badge output is unchanged without a charge", () => {
    const store = new ShieldStore();
    store.apply(shielded("MOCK0001", "full", NOW + 5000), NOW);
    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)).toEqual({
      text: "Shield 5.0s",
      className: "shield-badge shield-full",
    });
  });
});

describe("UC-040: A30 step 4 / A32 step 3 the dashboard badge follows the charge", () => {
  it("UC-040/A32 player_shield_charge_ended clears the badge for every reason", () => {
    for (const reason of ["expired", "death", "game_end", "activated"]) {
      const store = new ShieldStore();
      store.apply(charged("MOCK0001", 0), NOW);
      expect(store.apply({ eventType: "player_shield_charge_ended", data: { serial: "MOCK0001", reason } })).toBe(true);
      expect(store.get("MOCK0001", NOW)).toBeNull();
    }
  });

  it("UC-040/A30 activation by the trigger turns the charge into the running shield", () => {
    const store = new ShieldStore();
    store.apply(charged("MOCK0001", 0), NOW);
    store.apply({
      eventType: "player_shielded",
      data: {
        serial: "MOCK0001",
        kind: "full",
        seconds: "5.0",
        until_ms: String(NOW + 5000),
        activation: "trigger",
        charge_delay_ms: "3200",
      },
    }, NOW);
    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)?.text).toBe("Shield 5.0s");
  });

  it("UC-040/A30 step 4 shows the Charged badge text, with the seconds to expiry when there is one", () => {
    const store = new ShieldStore();
    store.apply(charged("MOCK0001", NOW + 12_000), NOW);
    store.apply(charged("MOCK0002", 0), NOW);
    expect(shieldBadge(store.get("MOCK0001", NOW), NOW)?.text).toBe("Charged 12s");
    expect(shieldBadge(store.get("MOCK0002", NOW), NOW)?.text).toBe("Charged");
  });

  it("UC-040/A32 a death, a game start and a game end clear the charge", () => {
    for (const clearing of <GameEventLike[]>[
      { eventType: "player_death", data: { serial: "MOCK0001" } },
      { eventType: "game_start", data: {} },
      { eventType: "game_ended", data: {} },
      { eventType: "game_force_ended", data: {} },
    ]) {
      const store = new ShieldStore();
      store.apply(charged("MOCK0001", 0), NOW);
      store.apply(clearing, NOW);
      expect(store.get("MOCK0001", NOW)).toBeNull();
    }
  });
});
