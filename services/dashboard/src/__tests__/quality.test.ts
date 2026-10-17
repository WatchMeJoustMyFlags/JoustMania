/**
 * Connection-quality badge on the controller tiles (UC-035 BR-004, #1431).
 */
import { describe, expect, it } from "vitest";
import { qualityBadge } from "../quality.js";

describe("UC-035: BR-004 connection-quality badge on the controller tile", () => {
  it("UC-035/BR-004 shows a good link in green with both gaps in the tooltip", () => {
    const badge = qualityBadge({ level: "good", reportGapP95Ms: 10, reportGapMaxMs: 20 });
    expect(badge).toEqual({
      text: "Link good",
      className: "quality-badge quality-good",
      title: "Good connection (last 10 s): p95 gap 10.0 ms, longest gap 20.0 ms",
    });
  });

  it("UC-035/BR-004 colours fair and poor links", () => {
    expect(qualityBadge({ level: "fair", reportGapP95Ms: 24.5, reportGapMaxMs: 41 })?.className).toBe(
      "quality-badge quality-fair",
    );
    const poor = qualityBadge({ level: "poor", reportGapP95Ms: 31, reportGapMaxMs: 250 });
    expect(poor?.className).toBe("quality-badge quality-poor");
    expect(poor?.title).toContain("longest gap 250.0 ms");
  });

  it("UC-035/BR-004 proto3 JSON omits zero gaps; they read as 0 ms", () => {
    expect(qualityBadge({ level: "good" })?.title).toBe(
      "Good connection (last 10 s): p95 gap 0.0 ms, longest gap 0.0 ms",
    );
  });

  it("UC-035/BR-004 shows no badge while the quality is unknown", () => {
    expect(qualityBadge(undefined)).toBeNull();
    expect(qualityBadge({})).toBeNull();
    expect(qualityBadge({ level: "excellent", reportGapP95Ms: 5 })).toBeNull();
  });
});
