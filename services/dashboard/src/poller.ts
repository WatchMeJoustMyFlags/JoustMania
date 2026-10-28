/**
 * Single-chain poller with per-request timeout and exponential backoff (#1233).
 *
 * Guarantees at most ONE poll chain and ONE request in flight: `poke()` (tab
 * shown, page visible again) is a no-op while a request is in flight or a tick is
 * already scheduled, and a finishing request reschedules only if nothing else
 * did. That closes the race where a visibility toggle during a slow fetch
 * started a second chain.
 *
 * Backoff: the delay starts at `baseMs` and doubles (capped at `maxMs`) after an
 * error, a timeout, or a response slower than `slowMs`; it resets to `baseMs`
 * after a fast success. A successful `run` may resolve to a minimum wait (ms) for
 * the next tick (the decide poll's payload pace, #2287).
 */
export interface PollerOptions {
  /**
   * One poll. Must honour the signal (aborted after `timeoutMs`). May resolve to
   * the minimum wait before the next tick (ms); the backoff can still exceed it.
   */
  run: (signal: AbortSignal) => Promise<void | number>;
  /** Whether polling should continue (tab shown, page visible). */
  isActive: () => boolean;
  onError?: (error: unknown) => void;
  baseMs: number;
  maxMs: number;
  timeoutMs: number;
  /** A success slower than this also backs off. Defaults to timeoutMs / 2. */
  slowMs?: number;
}

export interface Poller {
  /** Poll now if active and idle; otherwise do nothing. */
  poke(): void;
  /** Cancel the scheduled tick (an in-flight request settles, then stops). */
  stop(): void;
  /** Delay the next tick will wait (for tests and diagnostics). */
  readonly delayMs: number;
  readonly inFlight: boolean;
}

export function createPoller(opts: PollerOptions): Poller {
  const slowMs = opts.slowMs ?? opts.timeoutMs / 2;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight = false;
  let delayMs = opts.baseMs;

  const schedule = (wait: number) => {
    if (timer !== null || inFlight || !opts.isActive()) return;
    timer = setTimeout(tick, wait);
  };

  const tick = async () => {
    timer = null;
    if (inFlight || !opts.isActive()) return;
    inFlight = true;
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), opts.timeoutMs);
    const started = Date.now();
    let ok = false;
    let minNextMs = 0;
    try {
      const hint = await opts.run(controller.signal);
      if (typeof hint === "number" && Number.isFinite(hint)) minNextMs = hint;
      ok = true;
    } catch (error) {
      opts.onError?.(error);
    } finally {
      clearTimeout(abortTimer);
      inFlight = false;
    }
    const fast = Date.now() - started <= slowMs;
    delayMs = ok && fast ? opts.baseMs : Math.min(delayMs * 2, opts.maxMs);
    const wait = Math.max(delayMs, minNextMs);
    schedule(wait);
  };

  return {
    poke() {
      if (inFlight || timer !== null || !opts.isActive()) return;
      void tick();
    },
    stop() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },
    get delayMs() {
      return delayMs;
    },
    get inFlight() {
      return inFlight;
    },
  };
}
