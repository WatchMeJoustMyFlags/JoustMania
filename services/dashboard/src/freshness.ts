/**
 * The "updated Ns ago / stale" hint shared by the Agent and Experiments tabs
 * (#2388). Each tab passes its own staleness threshold.
 */

/** Write the freshness hint into `el` for data last updated at `updatedMs` (performance.now() clock). */
export function renderFreshness(el: HTMLElement, updatedMs: number, staleAfterMs: number): void {
  const ageMs = performance.now() - updatedMs;
  const ageSec = Math.max(0, Math.round(ageMs / 1000));
  const stale = ageMs >= staleAfterMs;
  el.textContent = stale ? `stale — last updated ${ageSec}s ago` : `updated ${ageSec}s ago`;
  el.classList.toggle("stale", stale);
  el.hidden = false;
}
