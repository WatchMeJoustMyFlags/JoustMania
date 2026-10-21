/**
 * Agent shield state for the controller tiles (#1301, UC-040 A16 / A17).
 *
 * The LED colour itself (including the shield fade and last-second blink) comes
 * live from the controller stream (`GameplayData.color`, the colour last written
 * to the LED). This module adds what the colour alone cannot say: that the white
 * is an agent shield, which kind, and how long it has left. It folds the game
 * events `player_shielded`, `player_shield_charged`, `player_shield_charge_ended`,
 * `player_death`, `game_start`, `game_ended` and `game_force_ended` (charge: #1837, UC-040 A30).
 */

export type ShieldKind = "full" | "toughness_boost";

export interface ShieldState {
  kind: ShieldKind;
  /** Wall-clock end of the protection, ms since epoch (from the event). */
  untilMs: number;
}

export interface GameEventLike {
  eventType: string;
  data: { [key: string]: string };
}

/** Seconds of the shield that blink on the controller (UC-040 BR-008). */
export const SHIELD_BLINK_WINDOW_S = 1;

/** Largest charge expiry the coordinator allows (`shield.charge_expiry_seconds` clamp), plus slack. */
export const CHARGE_MAX_COUNTDOWN_MS = 130_000;

/** A held shield charge (UC-040 A30, #1837). */
export interface ChargeState {
  /** Wall-clock expiry in ms since epoch, or null: never expires, or the clock is not trusted (A11). */
  expiresMs: number | null;
}

/** What a controller holds: a running shield, a waiting charge, or both. */
export interface ShieldView {
  active: ShieldState | null;
  charge: ChargeState | null;
}

export class ShieldStore {
  private readonly shields = new Map<string, ShieldState>();
  private readonly charges = new Map<string, ChargeState>();

  /** Drop every shield and charge (a new game, a reopened event stream). */
  clear(): boolean {
    const had = this.shields.size > 0 || this.charges.size > 0;
    this.shields.clear();
    this.charges.clear();
    return had;
  }

  /** Fold one game event; returns true when the shield state changed. */
  apply(event: GameEventLike, nowMs: number = Date.now()): boolean {
    switch (event.eventType) {
      case "player_shielded": {
        const serial = event.data["serial"];
        const untilMs = Number(event.data["until_ms"]);
        if (!serial || !Number.isFinite(untilMs)) return false;
        // "partial" is the pre-#1838 name of the toughness boost kind: an older coordinator
        // must not render as an immune full shield.
        const rawKind = event.data["kind"];
        const kind: ShieldKind = rawKind === "toughness_boost" || rawKind === "partial" ? "toughness_boost" : "full";
        this.shields.set(serial, { kind, untilMs });
        // The activation of a charge consumes it (UC-030 A6); the coordinator also sends charge_ended.
        if (event.data["activation"] === "trigger") this.charges.delete(serial);
        return true;
      }
      case "player_shield_charged": {
        const serial = event.data["serial"];
        if (!serial) return false;
        const expiresMs = Number(event.data["expires_ms"]);
        // "0" = never. An expiry in the past or beyond the largest allowed window means the
        // browser and coordinator clocks disagree: show "Charged" without a countdown (A11).
        const trusted =
          Number.isFinite(expiresMs) &&
          expiresMs > 0 &&
          expiresMs > nowMs &&
          expiresMs <= nowMs + CHARGE_MAX_COUNTDOWN_MS;
        this.charges.set(serial, { expiresMs: trusted ? expiresMs : null });
        return true;
      }
      case "player_shield_charge_ended": {
        const serial = event.data["serial"];
        return serial ? this.charges.delete(serial) : false;
      }
      case "player_death": {
        const serial = event.data["serial"];
        if (!serial) return false;
        const hadShield = this.shields.delete(serial);
        const hadCharge = this.charges.delete(serial);
        return hadShield || hadCharge;
      }
      case "game_start":
      case "game_starting":
      case "game_ended":
      case "game_force_ended":
      case "game_error":
        return this.clear();
      default:
        return false;
    }
  }

  /** What a controller holds at `nowMs`, or null (expired entries are dropped). */
  get(serial: string, nowMs: number): ShieldView | null {
    let active: ShieldState | null = this.shields.get(serial) ?? null;
    if (active && active.untilMs <= nowMs) {
      this.shields.delete(serial);
      active = null;
    }
    let charge: ChargeState | null = this.charges.get(serial) ?? null;
    if (charge && charge.expiresMs !== null && charge.expiresMs <= nowMs) {
      this.charges.delete(serial);
      charge = null;
    }
    return active || charge ? { active, charge } : null;
  }
}

export interface ShieldBadge {
  /** Badge text, e.g. "Shield 4.2s" / "Boost 1.0s" / "Charged". */
  text: string;
  /** CSS classes for the badge (and the tile ring). */
  className: string;
}

function activeBadge(shield: ShieldState, nowMs: number): ShieldBadge | null {
  const remainingS = Math.max(0, (shield.untilMs - nowMs) / 1000);
  if (remainingS <= 0) return null;
  const label = shield.kind === "toughness_boost" ? "Boost" : "Shield";
  const classes = ["shield-badge", `shield-${shield.kind}`];
  if (remainingS <= SHIELD_BLINK_WINDOW_S) classes.push("shield-ending");
  return { text: `${label} ${remainingS.toFixed(1)}s`, className: classes.join(" ") };
}

function chargeText(charge: ChargeState, nowMs: number): string {
  if (charge.expiresMs === null) return "Charged";
  return `Charged ${Math.max(1, Math.ceil((charge.expiresMs - nowMs) / 1000))}s`;
}

/** What a tile shows for a controller's shield at `nowMs`; null when there is none. */
export function shieldBadge(view: ShieldView | null, nowMs: number): ShieldBadge | null {
  if (!view) return null;
  const active = view.active ? activeBadge(view.active, nowMs) : null;
  const charge = view.charge && (view.charge.expiresMs === null || view.charge.expiresMs > nowMs) ? view.charge : null;
  if (active && charge) {
    return { text: `${active.text} · Charged`, className: `${active.className} shield-charged` };
  }
  if (active) return active;
  if (charge) return { text: chargeText(charge, nowMs), className: "shield-badge shield-charged" };
  return null;
}
