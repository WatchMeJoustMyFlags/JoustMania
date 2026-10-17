/**
 * Connection-quality badge for the controller tiles (UC-035 BR-004, #1431).
 *
 * The controller manager classifies each controller from the gaps between fresh
 * frames over a rolling 10 s window: p95 gap <= 22 / <= 30 / > 30 ms and longest
 * gap <= 30 / <= 50 / > 50 ms rate good / fair / poor, the class is the worse
 * of the two. The class arrives with the gameplay stream; this module only turns
 * it into the badge (colour + tooltip with both gaps).
 */
import type { ConnectionQuality } from "./gen/controller_manager_pb.js";

export type QualityLevel = "good" | "fair" | "poor";

const LABELS: Record<QualityLevel, string> = { good: "Good", fair: "Fair", poor: "Poor" };

export interface QualityBadge {
  /** Short badge text, e.g. "Link good". */
  text: string;
  /** CSS classes: `quality-badge quality-<level>`. */
  className: string;
  /** Tooltip with the p95 and longest gap. */
  title: string;
}

function isLevel(level: string | undefined): level is QualityLevel {
  return level === "good" || level === "fair" || level === "poor";
}

/** What a tile shows for a controller's connection quality; null while unknown. */
export function qualityBadge(quality: ConnectionQuality | undefined): QualityBadge | null {
  if (!quality || !isLevel(quality.level)) return null;
  const p95 = quality.reportGapP95Ms ?? 0;
  const max = quality.reportGapMaxMs ?? 0;
  return {
    text: `Link ${LABELS[quality.level].toLowerCase()}`,
    className: `quality-badge quality-${quality.level}`,
    title: `${LABELS[quality.level]} connection (last 10 s): p95 gap ${p95.toFixed(1)} ms, longest gap ${max.toFixed(1)} ms`,
  };
}
