/**
 * Background-load rules of the dashboard (UC-035 BR-012, #2290).
 *
 * VisibilityGate: a stream that runs only while the page is visible. Hiding the
 * page aborts the current `signal`; showing it hands out a fresh signal and
 * releases `whenVisible()`.
 *
 * EmbedGate: the Grafana and Jaeger iframes get their `src` on first use of
 * their tab, so a hidden embed costs no queries.
 */

export interface VisibilityDocument {
  readonly hidden: boolean;
  addEventListener(type: "visibilitychange", listener: () => void): void;
}

export class VisibilityGate {
  private controller = new AbortController();
  private waiters: Array<() => void> = [];

  constructor(private readonly doc: VisibilityDocument) {
    if (doc.hidden) this.controller.abort();
    doc.addEventListener("visibilitychange", () => {
      if (doc.hidden) {
        this.controller.abort();
        return;
      }
      if (this.controller.signal.aborted) this.controller = new AbortController();
      const waiters = this.waiters;
      this.waiters = [];
      for (const wake of waiters) wake();
    });
  }

  get hidden(): boolean {
    return this.doc.hidden;
  }

  /** Aborted while the page is hidden. */
  get signal(): AbortSignal {
    return this.controller.signal;
  }

  whenVisible(): Promise<void> {
    if (!this.doc.hidden) return Promise.resolve();
    return new Promise((resolve) => this.waiters.push(resolve));
  }
}

export interface EmbedSpec {
  frame: { src: string };
  /** The tab that shows the iframe. */
  tab: string;
  src: string;
  /** Clear the src when the tab is left or the page hidden (a refreshing dashboard). */
  unloadWhenAway: boolean;
}

export class EmbedGate {
  /** Embeds whose real src is set (frame.src reads back resolved, so track it here). */
  private readonly loaded = new Set<EmbedSpec>();

  constructor(private readonly embeds: EmbedSpec[]) {}

  /** Call on tab changes and visibility changes. */
  sync(activeTab: string, pageHidden: boolean): void {
    for (const e of this.embeds) {
      const shown = e.tab === activeTab && !pageHidden;
      if (shown) {
        if (!this.loaded.has(e)) {
          e.frame.src = e.src;
          this.loaded.add(e);
        }
      } else if (e.unloadWhenAway && this.loaded.has(e)) {
        e.frame.src = "about:blank";
        this.loaded.delete(e);
      }
    }
  }
}
