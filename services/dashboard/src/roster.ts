/**
 * Controller battery roster (UC-035 A5 / BR-007, #2111): every connected
 * controller with its last-known battery, lowest first, so the host knows which
 * one to charge, in the lobby too (the gameplay stream has no idle frames).
 * Pure logic: the poll state, the sort, the level mapping and the markup; the
 * component (components/BatteryRoster.ts) only wires it to the DOM and the RPCs.
 */
import { batteryView } from "./motion.js";
import type { ConnectedController, GetConnectedControllersResponse } from "./gen/controller_manager_pb.js";

/** Poll cadence while the page is visible. */
export const ROSTER_POLL_MS = 5000;

export interface RosterRow {
  serial: string;
  name: string;
  percent: number;
  className: "high" | "medium" | "low";
  /** Text next to the bar, for example `80%`. */
  label: string;
}

/** Rows lowest battery first; equal batteries by name, then serial. */
export function rosterRows(controllers: readonly ConnectedController[]): RosterRow[] {
  return controllers
    .filter((c) => !!c.serial)
    .map((c) => {
      const { percent, className } = batteryView(c.battery);
      return {
        serial: c.serial as string,
        name: c.name || (c.serial as string),
        percent,
        className,
        label: `${Math.round(percent)}%`,
      };
    })
    .sort((a, b) => a.percent - b.percent || a.name.localeCompare(b.name) || a.serial.localeCompare(b.serial));
}

/**
 * What the panel knows. `rows` is the last good roster; `stale` is set by a failed
 * poll and cleared by the next good one, so old data is never shown as live (A5).
 */
export class RosterModel {
  rows: RosterRow[] = [];
  /** A roster has been received at least once. */
  loaded = false;
  stale = false;

  /** A poll answered. */
  update(response: GetConnectedControllersResponse): void {
    this.rows = rosterRows(response.controllers ?? []);
    this.loaded = true;
    this.stale = false;
  }

  /** A poll failed or timed out: keep the rows, mark them stale. */
  fail(): void {
    this.stale = true;
  }
}

export const ROSTER_STALE_TEXT = "Roster unavailable - showing the last known batteries";
export const ROSTER_UNAVAILABLE_TEXT = "Roster unavailable";
export const ROSTER_EMPTY_TEXT = "No controllers connected";

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** Markup of the panel body for the model's current state. */
export function renderRoster(model: RosterModel): string {
  if (!model.loaded) {
    return model.stale ? `<div class="roster-note stale">${ROSTER_UNAVAILABLE_TEXT}</div>` : '<div class="loading">Loading controllers...</div>';
  }
  const note = model.stale ? `<div class="roster-note stale">${ROSTER_STALE_TEXT}</div>` : "";
  if (model.rows.length === 0) return `${note}<div class="roster-note">${ROSTER_EMPTY_TEXT}</div>`;
  const rows = model.rows
    .map(
      (row) => `<li class="roster-row" data-serial="${escapeHtml(row.serial)}">
  <span class="roster-name">${escapeHtml(row.name)}</span>
  <span class="roster-serial">${escapeHtml(row.serial)}</span>
  <span class="battery-bar roster-bar"><span class="battery-level ${row.className}" style="width: ${row.percent}%"></span></span>
  <span class="roster-level ${row.className}">${row.label}</span>
  <button type="button" class="identify-btn roster-identify" data-serial="${escapeHtml(row.serial)}" title="Flash and rumble this controller">Identify</button>
  <span class="identify-status roster-status" hidden></span>
</li>`,
    )
    .join("\n");
  return `${note}<ul class="roster-list${model.stale ? " stale" : ""}">${rows}</ul>`;
}
