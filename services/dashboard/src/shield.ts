/**
 * Agent shield state for the controller tiles (#1301, UC-040 A14 / A15).
 *
 * The LED colour itself (including the shield fade and last-second blink) comes
 * live from the controller stream (`GameplayData.color`, the colour last written
 * to the LED). This module adds what the colour alone cannot say: that the white
 * is an agent shield, which kind, and how long it has left. It folds the game
 * events `player_shielded`, `player_death`, `game_ended` and `game_force_ended`.
 */

export type ShieldKind = "full" | "partial";

export interface ShieldState {
  kind: ShieldKind;
  /** Wall-clock end of the protection, ms since epoch (from the event). */
  untilMs: number;
}

export interface GameEventLike {
  eventType: string;
  data: { [key: string]: string };
}

/** Seconds of the shield that blink on the controller (UC-040 BR-006). */
export const SHIELD_BLINK_WINDOW_S = 1;

export class ShieldStore {
  private readonly shields = new Map<string, ShieldState>();

  /** Fold one game event; returns true when the shield state changed. */
  apply(event: GameEventLike): boolean {
    switch (event.eventType) {
      case "player_shielded": {
        const serial = event.data["serial"];
        const untilMs = Number(event.data["until_ms"]);
        if (!serial || !Number.isFinite(untilMs)) return false;
        const kind: ShieldKind = event.data["kind"] === "partial" ? "partial" : "full";
        this.shields.set(serial, { kind, untilMs });
        return true;
      }
      case "player_death": {
        const serial = event.data["serial"];
        return serial ? this.shields.delete(serial) : false;
      }
      case "game_ended":
      case "game_force_ended":
      case "game_error": {
        const had = this.shields.size > 0;
        this.shields.clear();
        return had;
      }
      default:
        return false;
    }
  }

  /** The active shield of a controller at `nowMs`, or null (expired entries are dropped). */
  get(serial: string, nowMs: number): ShieldState | null {
    const shield = this.shields.get(serial);
    if (!shield) return null;
    if (shield.untilMs <= nowMs) {
      this.shields.delete(serial);
      return null;
    }
    return shield;
  }
}

export interface ShieldBadge {
  /** Badge text, e.g. "Shield 4.2s" / "Partial 1.0s". */
  text: string;
  /** CSS classes for the badge (and the tile ring). */
  className: string;
}

/** What a tile shows for a shield at `nowMs`; null when there is none. */
export function shieldBadge(shield: ShieldState | null, nowMs: number): ShieldBadge | null {
  if (!shield) return null;
  const remainingS = Math.max(0, (shield.untilMs - nowMs) / 1000);
  if (remainingS <= 0) return null;
  const label = shield.kind === "partial" ? "Partial" : "Shield";
  const classes = ["shield-badge", `shield-${shield.kind}`];
  if (remainingS <= SHIELD_BLINK_WINDOW_S) classes.push("shield-ending");
  return { text: `${label} ${remainingS.toFixed(1)}s`, className: classes.join(" ") };
}
