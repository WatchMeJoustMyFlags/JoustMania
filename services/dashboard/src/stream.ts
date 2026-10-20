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
