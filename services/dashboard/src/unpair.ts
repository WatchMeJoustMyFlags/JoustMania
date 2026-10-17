/**
 * Unpair a controller from its card (UC-005): confirm, then POST
 * /pairing/unpair (envoy -> connect-proxy -> pairing daemon).
 */

const BT_ADDRESS = /^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$/;

/** Only real controllers (serial = Bluetooth address) can be unpaired; mock serials cannot (UC-005 A5). */
export function canUnpair(serial: string): boolean {
  return BT_ADDRESS.test(serial);
}

/** Confirmation text (UC-005 step 2, A2: a player in a running game is eliminated). */
export function unpairConfirmMessage(serial: string): string {
  return (
    `Unpair controller ${serial}?\n\n` +
    "It is removed from every Bluetooth adapter and disconnects now; " +
    "a player in a running game is eliminated. " +
    "Plug it in over USB to pair it again."
  );
}

export type UnpairOutcome =
  | { kind: "cancelled" }
  | { kind: "unpaired"; message: string }
  | { kind: "failed"; status: number; message: string };

export interface UnpairDeps {
  confirm: (message: string) => boolean;
  fetch: typeof fetch;
  baseUrl?: string;
}

/** Ask for confirmation, send the unpair request and describe the result (UC-005 steps 2-7, A1, A4-A7). */
export async function requestUnpair(serial: string, deps: UnpairDeps): Promise<UnpairOutcome> {
  if (!canUnpair(serial)) {
    return { kind: "failed", status: 400, message: `${serial} is not a Bluetooth controller` };
  }
  if (!deps.confirm(unpairConfirmMessage(serial))) {
    return { kind: "cancelled" };
  }

  let response: Response;
  try {
    response = await deps.fetch(`${deps.baseUrl ?? ""}/pairing/unpair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address: serial }),
    });
  } catch (err) {
    return { kind: "failed", status: 0, message: `Unpair failed: ${String(err)}` };
  }

  let body: { error?: string; removed?: number; adapters?: string[] } = {};
  try {
    body = await response.json();
  } catch {
    // non-JSON error page; fall back to the status text
  }

  if (!response.ok) {
    return {
      kind: "failed",
      status: response.status,
      message: body.error ?? `Unpair failed: ${response.status} ${response.statusText}`,
    };
  }
  const removed = body.removed ?? 0;
  const adapters = body.adapters?.length ? ` (${body.adapters.join(", ")})` : "";
  return { kind: "unpaired", message: `Unpaired ${serial}: ${removed} registration(s) removed${adapters}` };
}
