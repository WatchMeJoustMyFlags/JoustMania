import { html, setHtml } from "../html.js";

/**
 * Game Status Component
 *
 * Displays game state, player counts, and recent events.
 */
export class GameStatus {
  private readonly stateElement: HTMLElement;
  private readonly playerCountElement: HTMLElement;
  private readonly aliveCountElement: HTMLElement;
  private readonly eventLogElement: HTMLElement;

  constructor() {
    this.stateElement = document.getElementById("game-state")!;
    this.playerCountElement = document.getElementById("player-count")!;
    this.aliveCountElement = document.getElementById("alive-count")!;
    this.eventLogElement = document.getElementById("event-log")!;
  }

  updateState(state: string) {
    setHtml(this.stateElement, html`Game: <strong>${state}</strong>`);
  }

  updatePlayerCount(total: number, alive: number) {
    setHtml(this.playerCountElement, html`Players: <strong>${total}</strong> connected`);
    setHtml(this.aliveCountElement, html`Alive: <strong>${alive > 0 ? alive : "-"}</strong>`);
  }

  updateEvents(events: string[]) {
    // textContent, never innerHTML: event text can carry server-provided strings (#1278).
    this.eventLogElement.replaceChildren(
      ...events.map((event) => {
        const item = document.createElement("div");
        item.className = "event-item";
        item.textContent = event;
        return item;
      })
    );
  }
}
