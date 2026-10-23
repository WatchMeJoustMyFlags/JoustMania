/**
 * When the tab-bound pollers may run (#2059): only while their tab is shown, the
 * page is visible and the page is not parked in the back/forward cache. A hidden
 * page or a page left for the back/forward cache polls nothing; coming back
 * resumes the pollers at once.
 */

/** The parts of `document` the gate reads. */
export interface DocumentLike {
  readonly hidden: boolean;
  addEventListener(type: "visibilitychange", listener: () => void): void;
}

/** The parts of `window` the gate reads. */
export interface WindowLike {
  addEventListener(type: "pagehide" | "pageshow", listener: () => void): void;
}

/** A poller the gate pauses and resumes (poller.ts). */
export interface PausablePoller {
  stop(): void;
  poke(): void;
}

export class PageGate {
  /** The tab currently shown (the tab buttons keep it current). */
  activeTab: string;
  private pageShown = true;

  constructor(
    private readonly doc: DocumentLike,
    win: WindowLike,
    initialTab: string,
    private readonly pollers: () => Array<PausablePoller | null> = () => [],
  ) {
    this.activeTab = initialTab;
    // pageShown flips BEFORE the pollers resume: poke() asks isActive().
    win.addEventListener("pagehide", () => {
      this.pageShown = false;
      this.pause();
    });
    win.addEventListener("pageshow", () => {
      this.pageShown = true;
      this.resume();
    });
    doc.addEventListener("visibilitychange", () => (doc.hidden ? this.pause() : this.resume()));
  }

  /** A tab's pollers run only while it is shown and the page is visible. */
  isActive(tab: string): boolean {
    return this.activeTab === tab && !this.doc.hidden && this.pageShown;
  }

  private pause(): void {
    for (const p of this.pollers()) p?.stop();
  }

  private resume(): void {
    for (const p of this.pollers()) p?.poke();
  }
}
