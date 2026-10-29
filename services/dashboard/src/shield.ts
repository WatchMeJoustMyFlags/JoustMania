/**
 * Agent shield state for the controller tiles (#1301, UC-040 A16 / A17).
 *
 * The LED colour itself (including the shield fade and last-second blink) comes
 * live from the controller stream (`GameplayData.color`, the colour last written
 * to the LED). This module adds what the colour alone cannot say: that the white
 * is an agent shield, which kind, and how long it has left. It folds the game
 * events `player_shielded`, `player_shield_charged`, `player_shield_charge_ended`,
 * `player_death`, `game_start`, `game_ended` and `game_force_ended` (charge: #1837, UC-040 A30),
 * and in a hold game `player_shield_energy` for the energy bar (#2096, UC-035 BR-008).
 */

export type ShieldKind = "full" | "toughness_boost";

export interface ShieldState {
  kind: ShieldKind;
  /**
   * End of the protection on the browser's monotonic clock: receipt time plus the event's
   * `seconds` (#2387), so a Pi/browser wall-clock skew cannot move it. Without a usable
   * `seconds` it is the event's wall-clock `until_ms` and `wall` is true.
   */
  untilMs: number;
  /** untilMs is a wall-clock time (legacy event without `seconds`). */
  wall?: boolean;
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

/** `state` of a `player_shield_energy` event (telemetry.md §5). */
export type HoldState = "active" | "charged" | "recharging" | "empty" | "exhausted";
/** Where the energy came from last: an agent grant or the recharge. */
export type HoldSource = "agent" | "recharge";

const HOLD_STATES: readonly HoldState[] = ["active", "charged", "recharging", "empty", "exhausted"];

/** The last `player_shield_energy` event of a player, with its monotonic receipt time. */
export interface HoldEnergyState {
  state: HoldState;
  energyMs: number;
  fullMs: number;
  capMs: number;
  minBurstMs: number;
  budgetMs: number;
  budgetLeftMs: number;
  /** Signed: negative while active (draining), positive while recharging. */
  rateMsPerS: number;
  /** Time until the running activation ends (0 when idle). */
  remainingMs: number;
  source: HoldSource | null;
  /** `performance.now()` at receipt: never compared with a coordinator time. */
  atMonoMs: number;
}

/** The bar's level, as the controller LED shows it (UC-040 BR-026), or `active`. */
export type HoldLevel = "empty" | "low" | "charged" | "full" | "active";

/** A hold pool at a moment, interpolated from the last event (UC-035 BR-008). */
export interface HoldEnergyView {
  level: HoldLevel;
  /** `state` now: an activation whose time ran out shows the idle state it ended in. */
  state: HoldState;
  energyMs: number;
  capMs: number;
  budgetMs: number;
  budgetLeftMs: number;
  source: HoldSource | null;
}

/** What a controller holds: a running shield, a waiting charge, a hold pool, or several. */
export interface ShieldView {
  active: ShieldState | null;
  charge: ChargeState | null;
  /** The hold pool (hold games only, UC-035 BR-008); absent outside a hold game. */
  energy?: HoldEnergyView | null;
}

/** The browser's monotonic clock (UC-035 BR-008: the bar never trusts the wall clock). */
export function monotonicNowMs(): number {
  return performance.now();
}

function parseHoldEnergy(data: { [key: string]: string }, atMonoMs: number, previous: HoldEnergyState | undefined): HoldEnergyState | null {
  const state = data["state"] as HoldState;
  if (!HOLD_STATES.includes(state)) return null;
  const num = (key: string): number => Number(data[key]);
  const fields = {
    energyMs: num("energy_ms"),
    fullMs: num("full_ms"),
    capMs: num("cap_ms"),
    minBurstMs: num("min_burst_ms"),
    budgetMs: num("budget_ms"),
    budgetLeftMs: num("budget_remaining_ms"),
    rateMsPerS: num("rate_ms_per_s"),
    remainingMs: num("remaining_ms"),
  };
  if (!Object.values(fields).every(Number.isFinite) || fields.capMs <= 0) return null;
  const rawSource = data["source"];
  // "none": keep the marker of the last source (BR-008: "the last source").
  const source: HoldSource | null =
    rawSource === "agent" || rawSource === "recharge" ? rawSource : (previous?.source ?? null);
  return { state, ...fields, source, atMonoMs };
}

function idleState(energyMs: number, budgetLeftMs: number, minBurstMs: number): HoldState {
  if (budgetLeftMs < minBurstMs) return "exhausted";
  return energyMs < minBurstMs ? "empty" : "charged";
}

/** Interpolate a hold pool to `monoMs` (UC-035 BR-008): linear in the event's rate, clamped. */
export function holdEnergyView(hold: HoldEnergyState, monoMs: number): HoldEnergyView {
  const elapsedMs = Math.max(0, monoMs - hold.atMonoMs);
  let energyMs = hold.energyMs;
  let budgetLeftMs = hold.budgetLeftMs;
  let state = hold.state;
  if (state === "active") {
    const drainedMs = Math.min(elapsedMs, Math.max(0, hold.remainingMs)) * (Math.abs(hold.rateMsPerS) / 1000);
    energyMs -= drainedMs;
    budgetLeftMs -= drainedMs;
    // The activation ended and no event said so (a dropped one): show the idle state it ended in.
    if (elapsedMs >= hold.remainingMs) state = idleState(energyMs, budgetLeftMs, hold.minBurstMs);
  } else if (state === "recharging" && hold.rateMsPerS > 0) {
    energyMs = Math.min(hold.fullMs, energyMs + (hold.rateMsPerS * elapsedMs) / 1000);
    if (energyMs >= hold.fullMs) state = idleState(energyMs, budgetLeftMs, hold.minBurstMs);
  }
  energyMs = Math.min(hold.capMs, Math.max(0, energyMs));
  budgetLeftMs = Math.min(hold.budgetMs, Math.max(0, budgetLeftMs));
  const fullMs = Math.min(hold.capMs, budgetLeftMs);
  let level: HoldLevel;
  if (state === "active") level = "active";
  else if (state === "exhausted" || energyMs < hold.minBurstMs) level = "empty";
  else if (energyMs >= fullMs - 1) level = "full";
  else if (energyMs < hold.capMs / 3) level = "low";
  else level = "charged";
  return {
    level,
    state,
    energyMs,
    capMs: hold.capMs,
    budgetMs: hold.budgetMs,
    budgetLeftMs,
    source: hold.source,
  };
}

export class ShieldStore {
  private readonly shields = new Map<string, ShieldState>();
  private readonly charges = new Map<string, ChargeState>();
  private readonly energies = new Map<string, HoldEnergyState>();

  /** Drop every shield, charge and hold pool (a new game, a reopened event stream). */
  clear(): boolean {
    const had = this.shields.size > 0 || this.charges.size > 0 || this.energies.size > 0;
    this.shields.clear();
    this.charges.clear();
    this.energies.clear();
    return had;
  }

  /**
   * Fold one game event; returns true when the shield state changed. `monoMs` is the
   * receipt time on the monotonic clock, used only for the hold energy (UC-035 BR-008).
   */
  apply(event: GameEventLike, nowMs: number = Date.now(), monoMs: number = monotonicNowMs()): boolean {
    switch (event.eventType) {
      case "player_shield_energy": {
        const serial = event.data["serial"];
        if (!serial) return false;
        const hold = parseHoldEnergy(event.data, monoMs, this.energies.get(serial));
        if (!hold) return false;
        this.energies.set(serial, hold);
        // A hold game shows the bar, never a timed shield badge (UC-040 BR-026).
        this.shields.delete(serial);
        return true;
      }
      case "player_shielded": {
        const serial = event.data["serial"];
        const untilWallMs = Number(event.data["until_ms"]);
        const rawSeconds = event.data["seconds"];
        const seconds = rawSeconds === undefined || rawSeconds === "" ? NaN : Number(rawSeconds);
        const hasSeconds = Number.isFinite(seconds) && seconds >= 0;
        if (!serial || (!hasSeconds && !Number.isFinite(untilWallMs))) return false;
        // "partial" is the pre-#1838 name of the toughness boost kind: an older coordinator
        // must not render as an immune full shield.
        const rawKind = event.data["kind"];
        const kind: ShieldKind = rawKind === "toughness_boost" || rawKind === "partial" ? "toughness_boost" : "full";
        this.shields.set(
          serial,
          hasSeconds ? { kind, untilMs: monoMs + seconds * 1000 } : { kind, untilMs: untilWallMs, wall: true },
        );
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
        const hadEnergy = this.energies.delete(serial);
        return hadShield || hadCharge || hadEnergy;
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

  /** What a controller holds at `nowMs` (hold energy at `monoMs`), or null (expired entries are dropped). */
  get(serial: string, nowMs: number, monoMs: number = monotonicNowMs()): ShieldView | null {
    let active: ShieldState | null = this.shields.get(serial) ?? null;
    if (active && active.untilMs <= (active.wall ? nowMs : monoMs)) {
      this.shields.delete(serial);
      active = null;
    }
    let charge: ChargeState | null = this.charges.get(serial) ?? null;
    if (charge && charge.expiresMs !== null && charge.expiresMs <= nowMs) {
      this.charges.delete(serial);
      charge = null;
    }
    const hold = this.energies.get(serial);
    const energy = hold ? holdEnergyView(hold, monoMs) : null;
    return active || charge || energy ? { active, charge, energy } : null;
  }
}

export interface ShieldBadge {
  /** Badge text, e.g. "Shield 4.2s" / "Boost 1.0s" / "Charged". */
  text: string;
  /** CSS classes for the badge (and the tile ring). */
  className: string;
}

function activeBadge(shield: ShieldState, nowMs: number, monoMs: number): ShieldBadge | null {
  const remainingS = Math.max(0, (shield.untilMs - (shield.wall ? nowMs : monoMs)) / 1000);
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
export function shieldBadge(view: ShieldView | null, nowMs: number, monoMs: number = monotonicNowMs()): ShieldBadge | null {
  if (!view) return null;
  const active = view.active ? activeBadge(view.active, nowMs, monoMs) : null;
  const charge = view.charge && (view.charge.expiresMs === null || view.charge.expiresMs > nowMs) ? view.charge : null;
  if (active && charge) {
    return { text: `${active.text} · Charged`, className: `${active.className} shield-charged` };
  }
  if (active) return active;
  if (charge) return { text: chargeText(charge, nowMs), className: "shield-badge shield-charged" };
  return null;
}

export interface HoldEnergyBar {
  /** CSS classes: `hold-energy hold-<level>`, plus `hold-recharging` / `hold-exhausted`. */
  className: string;
  /** Energy against the cap, 0-100. */
  fillPercent: number;
  /** Budget remaining against the budget, 0-100. */
  budgetPercent: number;
  /** E.g. "Hold 2.4s/5s · budget 9.1s". */
  text: string;
  /** The state word, e.g. "active", "recharging", "budget used up". */
  label: string;
  /** The last source marker: "agent", "recharge" or "". */
  source: string;
}

const HOLD_LABELS: Record<HoldLevel, string> = {
  empty: "empty",
  low: "low",
  charged: "charged",
  full: "full",
  active: "active",
};

function seconds(ms: number): string {
  const s = ms / 1000;
  return Number.isInteger(s) ? `${s}` : s.toFixed(1);
}

/** What a tile shows for a hold pool (UC-035 BR-008); null when the controller has none. */
export function holdEnergyBar(view: ShieldView | null): HoldEnergyBar | null {
  const energy = view?.energy;
  if (!energy) return null;
  const classes = ["hold-energy", `hold-${energy.level}`];
  let label = HOLD_LABELS[energy.level];
  if (energy.state === "recharging") {
    classes.push("hold-recharging");
    label = "recharging";
  } else if (energy.state === "exhausted") {
    classes.push("hold-exhausted");
    label = "budget used up";
  }
  return {
    className: classes.join(" "),
    fillPercent: Math.round((energy.energyMs / energy.capMs) * 100),
    budgetPercent: energy.budgetMs > 0 ? Math.round((energy.budgetLeftMs / energy.budgetMs) * 100) : 0,
    text: `Hold ${(energy.energyMs / 1000).toFixed(1)}s/${seconds(energy.capMs)}s · budget ${(energy.budgetLeftMs / 1000).toFixed(1)}s`,
    label,
    source: energy.source ?? "",
  };
}
