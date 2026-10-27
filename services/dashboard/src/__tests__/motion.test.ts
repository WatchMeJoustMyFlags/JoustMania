/**
 * Controller card numbers when proto3 JSON omits zero scalars (#1604).
 */
import { describe, expect, it } from "vitest";
import { accelDotOffset, batteryPercent, batteryView, normalizeGameplay, statusView } from "../motion.js";

describe("UC-035: controller card reads omitted proto3-JSON zero scalars as 0", () => {
  it("UC-035 main step 2 a frame without battery and axes renders 0 values, not NaN", () => {
    // What the server sends for battery 0, accel (0, 0.5, 0), gyro all zero
    const frame = normalizeGameplay({ serial: "AA", accel: { y: 0.5 } as never, gyro: {} as never });
    expect(frame.battery).toBe(0);
    expect(frame.team).toBeUndefined();
    expect(frame.rssi).toBe(0);
    expect(frame.accel).toEqual({ x: 0, y: 0.5, z: 0 });
    expect(frame.gyro).toEqual({ x: 0, y: 0, z: 0 });
    expect(batteryView(frame.battery)).toEqual({ percent: 0, className: "low" });
    expect(accelDotOffset(frame.accel)).toEqual({ x: 0, y: 0 });
  });

  it("UC-035 main step 2 an omitted accel vector puts the dot back at the centre", () => {
    expect(accelDotOffset(undefined)).toEqual({ x: 0, y: 0 });
    expect(accelDotOffset({ z: 1 })).toEqual({ x: 0, y: 10 });
    expect(Number.isNaN(accelDotOffset({}).x)).toBe(false);
  });

  it("UC-035 main step 2 a full battery still maps to 100 %", () => {
    expect(batteryView(5)).toEqual({ percent: 100, className: "high" });
  });

  it("UC-035 main step 2 battery undefined reads as empty, not NaN", () => {
    expect(batteryView(undefined)).toEqual({ percent: 0, className: "low" });
  });

  it("UC-035 main step 2 a frame with no team (always team 0 on the wire) renders Alive, not Dead", () => {
    const frame = normalizeGameplay({ serial: "AA" });
    expect(statusView(frame.team)).toEqual({ state: "alive", text: "Alive" });
    expect(statusView(0)).toEqual({ state: "alive", text: "Alive" });
  });

  it("UC-035 main step 2 only the followed roster marks a card Dead; admin and teams keep their badges", () => {
    expect(statusView(undefined, false)).toEqual({ state: "dead", text: "Dead" });
    expect(statusView(undefined, true)).toEqual({ state: "alive", text: "Alive" });
    expect(statusView(-1)).toEqual({ state: "admin", text: "Admin" });
    expect(statusView(2)).toEqual({ state: "alive", text: "Team 2" });
    expect(statusView(2, true)).toEqual({ state: "alive", text: "Alive" });
  });
});

describe("UC-035 BR-007: the card battery reads both scales (#2111, #2132)", () => {
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

  it("UC-035 BR-007 a Rust 40 % battery is not shown as 100 %", () => {
    expect(batteryView(40)).toEqual({ percent: 40, className: "medium" });
    expect(batteryView(100).className).toBe("high");
    expect(batteryView(20).className).toBe("low");
    expect(batteryView(4)).toEqual({ percent: 80, className: "high" });
  });
});
