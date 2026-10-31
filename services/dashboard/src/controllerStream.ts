/**
 * The controller grid's gameplay stream (UC-035 step 2 and A4).
 *
 * The stream is reopened 2 s after a failure and after a clean end (the
 * controller manager restarted); while it is down the grid shows
 * CONTROLLER_STREAM_LOST. Built on keepStreamOpen so the retry is testable.
 */
import { keepStreamOpen } from "./stream.js";
import type { VisibilityGate } from "./visibilityGate.js";

export const CONTROLLER_STREAM_LOST = "Connection lost. Retrying...";

export interface ControllerStreamOptions<T> {
  open: (signal?: AbortSignal) => AsyncIterable<T>;
  /** Pauses the stream while the page is hidden (UC-035 BR-012). */
  gate?: VisibilityGate;
  onUpdate: (update: T) => void;
  grid: { setLoading(loading: boolean): void; setError(message: string): void };
  /** Mirrors the stream state (main.ts state.isStreaming). */
  setStreaming: (streaming: boolean) => void;
  retryMs?: number;
  /** Test seam; defaults to setTimeout. */
  schedule?: (fn: () => void, ms: number) => void;
}

export function keepControllerStreamOpen<T>(opts: ControllerStreamOptions<T>): Promise<void> {
  return keepStreamOpen({
    gate: opts.gate,
    open: (signal) => {
      opts.setStreaming(true);
      opts.grid.setLoading(false);
      return opts.open(signal);
    },
    onEvent: opts.onUpdate,
    onLost: (error) => {
      console.error("Controller stream error:", error ?? new Error("controller stream ended"));
      opts.setStreaming(false);
      opts.grid.setError(CONTROLLER_STREAM_LOST);
    },
    retryMs: opts.retryMs,
    schedule: opts.schedule,
  });
}
