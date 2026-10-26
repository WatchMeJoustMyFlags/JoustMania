/**
 * Controller-card numbers from a gameplay frame (UC-035, #1604).
 *
 * Proto3 JSON omits zero-valued scalars, so `battery` and every vector
 * axis arrive as `undefined` when they are 0. Default them to 0 here so the card
 * never renders NaN and a real 0 replaces the previous position.
 */
import type { GameplayData, Vector3 } from "./gen/controller_manager_pb.js";

type Axes = Partial<Pick<Vector3, "x" | "y" | "z">> | undefined;

export function normalizeVector(v: Axes): Vector3 {
  return { x: v?.x ?? 0, y: v?.y ?? 0, z: v?.z ?? 0 };
}

/**
 * The frame with the numeric fields the card reads set to a number. `team` is left
 * alone: the controller manager never sets it, so omitted means unset (alive), not 0.
 */
export function normalizeGameplay(data: Partial<GameplayData> & { serial: string }): GameplayData {
  return {
    ...data,
    team: data.team as number,
    moveNum: data.moveNum ?? 0,
    battery: data.battery ?? 0,
    rssi: data.rssi ?? 0,
    accel: normalizeVector(data.accel),
    gyro: normalizeVector(data.gyro),
  };
}

/**
 * Battery as 0-100, like `battery_to_pct` in the controller manager: the Rust HID
 * path reports a percentage in steps of 20, the psmoveapi path a 0-5 level
 * (UC-035 BR-007, #2111). 5 or less is a level times 20, anything above is
 * already a percentage; clamped to 0-100.
 */
export function batteryPercent(level: number | undefined): number {
  const raw = level ?? 0;
  const percent = raw <= 5 ? raw * 20 : raw;
  return Math.min(100, Math.max(0, percent));
}

/** Battery bar: the level as a percentage and its colour class. */
export function batteryView(level: number | undefined): { percent: number; className: "high" | "medium" | "low" } {
  const percent = batteryPercent(level);
  return { percent, className: percent > 50 ? "high" : percent > 20 ? "medium" : "low" };
}

/** Accelerometer dot offset in pixels from the centre (z drives the vertical). */
export function accelDotOffset(accel: Axes): { x: number; y: number } {
  const maxOffset = 15;
  const clamp = (n: number) => Math.max(-maxOffset, Math.min(maxOffset, n * 10));
  const v = normalizeVector(accel);
  return { x: clamp(v.x), y: clamp(v.z) };
}

export type StatusView = { state: "admin" | "dead" | "alive"; text: string };

/**
 * Status badge. The followed game's roster (`alive`) decides alive/dead (UC-070
 * BR-005). Without it the controller's team does: -1 = admin, > 0 = "Team n", and
 * 0 or omitted = alive (the controller manager always sends team 0, which proto3
 * JSON omits; 0 is not "dead"). #1604
 */
export function statusView(team: number | undefined, alive?: boolean): StatusView {
  if (alive !== undefined) return alive ? { state: "alive", text: "Alive" } : { state: "dead", text: "Dead" };
  if (team === -1) return { state: "admin", text: "Admin" };
  return { state: "alive", text: team !== undefined && team > 0 ? `Team ${team}` : "Alive" };
}
