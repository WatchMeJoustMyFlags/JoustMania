/**
 * Keep a server-stream open (UC-035 A4, #1706).
 *
 * The stream is reopened after a failure AND after a clean end (connect-proxy
 * ends the game event stream cleanly when the coordinator's stream ends), so
 * the next game's events still show. The delay is fixed (2 s by default), the
 * same as the controller stream's loss handling.
 */
export interface KeepStreamOpenOptions<T> {
  open: () => AsyncIterable<T>;
  onEvent: (event: T) => void;
  /** Called with the failure, or null for a clean end, before the retry is scheduled. */
  onLost?: (error: unknown | null) => void;
  retryMs?: number;
  /** Test seam; defaults to setTimeout. */
  schedule?: (fn: () => void, ms: number) => void;
}

export async function keepStreamOpen<T>(opts: KeepStreamOpenOptions<T>): Promise<void> {
  const schedule = opts.schedule ?? ((fn, ms) => void setTimeout(fn, ms));
  let lost: unknown | null = null;
  try {
    for await (const event of opts.open()) {
      opts.onEvent(event);
    }
  } catch (error) {
    lost = error ?? new Error("stream failed");
  }
  opts.onLost?.(lost);
  schedule(() => void keepStreamOpen(opts), opts.retryMs ?? 2000);
}

/**
 * The game event stream's loss handling (UC-035 A4): the loss is only logged,
 * the event feed and the shield badges get nothing (they stop updating without
 * a message), the badges are cleared (the bus has no replay, so a badge would
 * outlive a missed end), and the stream is reopened every 2 s.
 */
export function keepGameEventStreamOpen<T>(
  open: () => AsyncIterable<T>,
  onEvent: (event: T) => void,
  schedule?: (fn: () => void, ms: number) => void,
  onReopen?: () => void,
): Promise<void> {
  return keepStreamOpen({
    open,
    onEvent,
    onLost: (error) => {
      console.warn("Game event stream lost, reopening:", error ?? "ended");
      // The event bus has no replay: a missed charge_ended must not leave a badge behind.
      onReopen?.();
    },
    schedule,
  });
}
