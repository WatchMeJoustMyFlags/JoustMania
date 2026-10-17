/**
 * Identify a controller from its dashboard card (UC-004, FR-121, #1430).
 *
 * The card calls IdentifyController; the controller manager flashes that
 * controller white and rumbles it, or refuses (not connected, in game,
 * reserved, invalid). This module maps the answer to the card's status text.
 */
import type {
  IdentifyControllerRequest,
  IdentifyControllerResponse,
} from "./gen/controller_manager_pb.js";

/** Effect length the card asks for; the controller manager's default (UC-004 BR-001). */
export const IDENTIFY_DEFAULT_MS = 3000;

/** The card gives up waiting after this, so the button never stays disabled. */
export const IDENTIFY_TIMEOUT_MS = 5000;

/** How long a refusal stays on the card. */
export const IDENTIFY_REFUSAL_SHOW_MS = 3000;

export interface IdentifyStatus {
  text: string;
  ok: boolean;
  showForMs: number;
}

const REFUSALS: Record<string, string> = {
  not_connected: "Not connected",
  in_game: "In game",
  reserved: "Reserved",
  invalid: "Invalid",
};

/** Card status for an IdentifyController answer (proto3 JSON omits default fields). */
export function identifyStatus(
  response: Partial<IdentifyControllerResponse>,
  durationMs: number = IDENTIFY_DEFAULT_MS,
): IdentifyStatus {
  if (response.success && response.outcome === "started") {
    // The server reports the duration it used (default / clamped, UC-004 BR-001).
    return { text: "Identifying", ok: true, showForMs: response.durationMs || durationMs };
  }
  const text = (response.outcome && REFUSALS[response.outcome]) || "Failed";
  return { text, ok: false, showForMs: IDENTIFY_REFUSAL_SHOW_MS };
}

export type IdentifyCall = (
  request: IdentifyControllerRequest,
  signal: AbortSignal,
) => Promise<Partial<IdentifyControllerResponse>>;

/** Ask the controller manager to identify `serial`; never throws, gives up after IDENTIFY_TIMEOUT_MS. */
export async function identifyController(
  serial: string,
  call: IdentifyCall,
  timeoutMs: number = IDENTIFY_TIMEOUT_MS,
): Promise<IdentifyStatus> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("identify timed out"));
    }, timeoutMs);
  });
  try {
    const response = await Promise.race([call({ serial, durationMs: IDENTIFY_DEFAULT_MS }, controller.signal), timeout]);
    return identifyStatus(response, IDENTIFY_DEFAULT_MS);
  } catch {
    return { text: "Failed", ok: false, showForMs: IDENTIFY_REFUSAL_SHOW_MS };
  } finally {
    clearTimeout(timer);
  }
}
