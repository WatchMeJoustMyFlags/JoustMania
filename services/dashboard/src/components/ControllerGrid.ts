/**
 * Controller Grid Component
 *
 * Displays a grid of controller cards with real-time updates.
 */
import { html, setHtml } from "../html.js";
import { ControllerCard } from "./ControllerCard.js";
import type { GameplayData } from "../gen/controller_manager_pb.js";
import type { ShieldStore } from "../shield.js";
import { adapterCounts, adapterCountsText, showAdapterBadge } from "../adapters.js";
import type { AdapterMap } from "../adapters.js";

export class ControllerGrid {
  private readonly container: HTMLElement;
  private readonly cards: Map<string, ControllerCard> = new Map();
  private adapters: AdapterMap = new Map();
  private adaptersStale = false;
  private readonly countsElement: HTMLElement | null;

  constructor(containerId: string, countsId?: string) {
    const container = document.getElementById(containerId);
    if (!container) {
      throw new Error(`Container element not found: ${containerId}`);
    }
    this.container = container;
    this.countsElement = countsId ? document.getElementById(countsId) : null;
  }

  /**
   * The adapters from VictoriaMetrics (UC-035 BR-009). `null` = VictoriaMetrics
   * unreachable: keep the last map and mark it stale (A3).
   */
  setAdapters(adapters: AdapterMap | null) {
    if (adapters) {
      this.adapters = adapters;
      this.adaptersStale = false;
    } else {
      this.adaptersStale = true;
    }
    this.renderAdapters();
  }

  private renderAdapters() {
    const counts = adapterCounts(this.adapters, this.cards.keys());
    const badges = showAdapterBadge(counts);
    for (const [serial, card] of this.cards) {
      card.updateAdapter(badges ? this.adapters.get(serial) : undefined, this.adaptersStale);
    }
    if (!this.countsElement) return;
    const text = adapterCountsText(counts);
    const shown = text && this.adaptersStale ? `${text} · stale` : text;
    if (this.countsElement.hidden === (text === "") && this.countsElement.textContent === shown) return;
    this.countsElement.hidden = text === "";
    this.countsElement.textContent = shown;
    this.countsElement.classList.toggle("stale", this.adaptersStale);
  }

  setLoading(loading: boolean) {
    if (loading) {
      setHtml(this.container, html`<div class="loading">Connecting to controller manager...</div>`);
    }
  }

  setError(message: string) {
    setHtml(this.container, html`<div class="loading">${message}</div>`);
  }

  /**
   * `alive` overrides the alive/dead badge per serial: the followed shadow
   * game's roster (UC-070 BR-005); without an entry the controller's team decides.
   */
  render(
    controllers: GameplayData[],
    shields: ShieldStore | null = null,
    nowMs: number = Date.now(),
    alive: ReadonlyMap<string, boolean> | null = null,
  ) {
    // A placeholder (connecting, connection lost, no controllers) never outlives a frame.
    // With no controllers the empty placeholder is kept as is, not rebuilt every frame.
    if (controllers.length > 0) {
      for (const placeholder of this.container.querySelectorAll(":scope > .loading")) placeholder.remove();
    }

    // Track which controllers are still present
    const presentSerials = new Set(controllers.map((c) => c.serial));

    // Remove cards for disconnected controllers
    for (const [serial, card] of this.cards.entries()) {
      if (!presentSerials.has(serial)) {
        card.remove();
        this.cards.delete(serial);
      }
    }

    // Update or create cards for each controller
    for (const controller of controllers) {
      let card = this.cards.get(controller.serial);

      if (!card) {
        // Create new card
        card = new ControllerCard(controller.serial);
        this.cards.set(controller.serial, card);
      }
      // setError wiped the container but kept the cards: put known cards back (#2285).
      if (card.element.parentElement !== this.container) this.container.appendChild(card.element);

      // Update card with new data
      card.update(controller, shields?.get(controller.serial, nowMs) ?? null, nowMs, alive?.get(controller.serial));
    }

    this.renderAdapters();

    // Show empty state if no controllers
    if (controllers.length === 0) {
      const existing = this.container.querySelector(":scope > .loading");
      if (existing?.textContent === "No controllers connected") return;
      // Drop any other placeholder (connecting, connection lost) before the empty state.
      setHtml(this.container, html`<div class="loading">No controllers connected</div>`);
    }
  }
}
