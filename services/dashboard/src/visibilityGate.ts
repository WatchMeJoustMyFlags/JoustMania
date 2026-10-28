/**
 * Background-load rules of the dashboard (UC-035 BR-012, #2290).
 *
 * VisibilityGate: a stream that runs only while the page is visible. Hiding the
 * page aborts the current `signal`; showing it hands out a fresh signal and
 * releases `whenVisible()`.
 *
 * EmbedGate: the Grafana and Jaeger iframes get their `src` on first use of
 * their tab. Grafana is unloaded 60 s after its tab is left (a quick return keeps
 * it loaded) and at once on a hidden page, so a hidden embed costs no queries.
 */

export interface VisibilityDocument {
  readonly hidden: boolean;
  addEventListener(type: "visibilitychange", listener: () => void): void;
}

/** The parts of `window` the gate reads (back/forward cache, like PageGate). */
export interface VisibilityWindow {
  addEventListener(type: "pagehide" | "pageshow", listener: () => void): void;
}

export class VisibilityGate {
  private controller = new AbortController();
  private waiters: Array<() => void> = [];
  private pageShown = true;
  private pauses = 0;

  constructor(
    private readonly doc: VisibilityDocument,
    win?: VisibilityWindow,
  ) {
    if (this.hidden) this.controller.abort();
    doc.addEventListener("visibilitychange", () => this.refresh());
    win?.addEventListener("pagehide", () => {
      this.pageShown = false;
      this.refresh();
    });
    win?.addEventListener("pageshow", () => {
      this.pageShown = true;
      this.refresh();
    });
  }

  private refresh(): void {
    if (this.hidden) {
      if (!this.controller.signal.aborted) {
        this.pauses++;
        this.controller.abort();
      }
      return;
    }
    if (this.controller.signal.aborted) this.controller = new AbortController();
    const waiters = this.waiters;
    this.waiters = [];
    for (const wake of waiters) wake();
  }

  /** True while the page is hidden or parked in the back/forward cache. */
  get hidden(): boolean {
    return this.doc.hidden || !this.pageShown;
  }

  /** Aborted while the page is hidden. */
  get signal(): AbortSignal {
    return this.controller.signal;
  }

  /**
   * Counts the pauses. A stream that sees a different value after its loop ended
   * was aborted by a pause, even when the page is visible again by then.
   */
  get generation(): number {
    return this.pauses;
  }

  whenVisible(): Promise<void> {
    if (!this.hidden) return Promise.resolve();
    return new Promise((resolve) => this.waiters.push(resolve));
  }
}

export interface EmbedSpec {
  frame: { src: string };
  /** The tab that shows the iframe. */
  tab: string;
  src: string;
  /** Clear the src 60 s after the tab is left, or at once when the page is hidden (a refreshing dashboard). */
  unloadWhenAway: boolean;
}

/** Grafana is unloaded after this long away from its tab (returning sooner keeps it loaded). */
export const EMBED_UNLOAD_GRACE_MS = 60_000;

export class EmbedGate {
  /** Embeds whose real src is set (frame.src reads back resolved, so track it here). */
  private readonly loaded = new Set<EmbedSpec>();
  private readonly timers = new Map<EmbedSpec, ReturnType<typeof setTimeout>>();

  constructor(
    private readonly embeds: EmbedSpec[],
    private readonly graceMs: number = EMBED_UNLOAD_GRACE_MS,
  ) {}

  /** Call on tab changes and visibility changes. */
  sync(activeTab: string, pageHidden: boolean): void {
    for (const e of this.embeds) {
      const shown = e.tab === activeTab && !pageHidden;
      if (shown) {
        this.cancelUnload(e);
        if (!this.loaded.has(e)) {
          e.frame.src = e.src;
          this.loaded.add(e);
        }
      } else if (e.unloadWhenAway && this.loaded.has(e)) {
        if (pageHidden) this.unload(e);
        else if (!this.timers.has(e)) {
          this.timers.set(
            e,
            setTimeout(() => this.unload(e), this.graceMs),
          );
        }
      }
    }
  }

  private cancelUnload(e: EmbedSpec): void {
    const timer = this.timers.get(e);
    if (timer !== undefined) clearTimeout(timer);
    this.timers.delete(e);
  }

  private unload(e: EmbedSpec): void {
    this.cancelUnload(e);
    if (!this.loaded.has(e)) return;
    e.frame.src = "about:blank";
    this.loaded.delete(e);
  }
}
