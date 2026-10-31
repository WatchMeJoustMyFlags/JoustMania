import { stageRows } from "../stageSettings.js";
import type { StageSettings as Data } from "../stageSettings.js";

/** Compact strip of the live game settings (UC-035 BR-010). Keeps the last data, marked stale, on a failed poll. */
export class StageSettings {
  private readonly element: HTMLElement;
  private last: Data | null = null;

  constructor() {
    this.element = document.getElementById("stage-settings")!;
  }

  update(data: Data) {
    this.last = data;
    this.render(false);
  }

  /** A failed poll: keep the last data marked stale; with none, show dashes. */
  markStale() {
    this.render(true);
  }

  private render(stale: boolean) {
    const rows = stageRows(this.last ?? { gauges: null, menu: null });
    this.element.hidden = false;
    this.element.classList.toggle("stale", stale);
    this.element.textContent = rows
      .map((r) => `${r.label}: ${r.value}${r.origin && r.value !== "—" ? ` (${r.origin})` : ""}`)
      .join(" · ") + (stale ? " · stale" : "");
  }
}
