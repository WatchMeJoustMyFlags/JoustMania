/**
 * Controller card numbers when proto3 JSON omits zero scalars (#1604).
 */
import { describe, expect, it } from "vitest";
import { accelDotOffset, batteryView, normalizeGameplay } from "../motion.js";

describe("UC-035: controller card reads omitted proto3-JSON zero scalars as 0", () => {
  it("UC-035 main step 3 a frame without battery, team and axes renders 0, not NaN", () => {
    // What the server sends for battery 0, team 0, accel (0, 0.5, 0), gyro all zero
    const frame = normalizeGameplay({ serial: "AA", accel: { y: 0.5 } as never, gyro: {} as never });
    expect(frame.battery).toBe(0);
    expect(frame.team).toBe(0);
    expect(frame.rssi).toBe(0);
    expect(frame.accel).toEqual({ x: 0, y: 0.5, z: 0 });
    expect(frame.gyro).toEqual({ x: 0, y: 0, z: 0 });
    expect(batteryView(frame.battery)).toEqual({ percent: 0, className: "low" });
    expect(accelDotOffset(frame.accel)).toEqual({ x: 0, y: 0 });
  });

  it("UC-035 main step 3 an omitted accel vector puts the dot back at the centre", () => {
    expect(accelDotOffset(undefined)).toEqual({ x: 0, y: 0 });
    expect(accelDotOffset({ z: 1 })).toEqual({ x: 0, y: 10 });
    expect(Number.isNaN(accelDotOffset({}).x)).toBe(false);
  });

  it("UC-035 main step 3 a full battery still maps to 100 %", () => {
    expect(batteryView(5)).toEqual({ percent: 100, className: "high" });
  });
});
