/**
 * Controller-card numbers from a gameplay frame (UC-035, #1604).
 *
 * Proto3 JSON omits zero-valued scalars, so `battery`, `team` and every vector
 * axis arrive as `undefined` when they are 0. Default them to 0 here so the card
 * never renders NaN and a real 0 replaces the previous position.
 */
import type { GameplayData, Vector3 } from "./gen/controller_manager_pb.js";

type Axes = Partial<Pick<Vector3, "x" | "y" | "z">> | undefined;

export function normalizeVector(v: Axes): Vector3 {
  return { x: v?.x ?? 0, y: v?.y ?? 0, z: v?.z ?? 0 };
}

/** The frame with every numeric field the card reads set to a number. */
export function normalizeGameplay(data: Partial<GameplayData> & { serial: string }): GameplayData {
  return {
    ...data,
    moveNum: data.moveNum ?? 0,
    battery: data.battery ?? 0,
    team: data.team ?? 0,
    rssi: data.rssi ?? 0,
    accel: normalizeVector(data.accel),
    gyro: normalizeVector(data.gyro),
  };
}

/** Battery bar: level 0-5 as a percentage and its colour class. */
export function batteryView(level: number | undefined): { percent: number; className: "high" | "medium" | "low" } {
  const percent = Math.min(100, Math.max(0, ((level ?? 0) / 5) * 100));
  return { percent, className: percent > 50 ? "high" : percent > 20 ? "medium" : "low" };
}

/** Accelerometer dot offset in pixels from the centre (z drives the vertical). */
export function accelDotOffset(accel: Axes): { x: number; y: number } {
  const maxOffset = 15;
  const clamp = (n: number) => Math.max(-maxOffset, Math.min(maxOffset, n * 10));
  const v = normalizeVector(accel);
  return { x: clamp(v.x), y: clamp(v.z) };
}
