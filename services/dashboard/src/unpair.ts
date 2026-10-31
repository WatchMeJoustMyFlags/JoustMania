/**
 * Unpair a controller from its card (UC-005): confirm, then POST
 * /pairing/unpair (envoy -> connect-proxy -> pairing daemon).
 */

/** DOM event a card dispatches with the unpair result, shown in the event feed. */
export const UNPAIR_NOTICE_EVENT = "joustmania:unpair-notice";

const BT_ADDRESS = /^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$/;
// Real controller serials are the address without separators (controller-manager and rust-hid strip the colons).
const BARE_BT_ADDRESS = /^[0-9A-Fa-f]{12}$/;

/** Only real controllers (serial = Bluetooth address) can be unpaired; mock serials cannot (UC-005 A5). */
export function canUnpair(serial: string): boolean {
  return BT_ADDRESS.test(serial) || BARE_BT_ADDRESS.test(serial);
}

/** The colon form the connect-proxy and pairing daemon expect (`E0AE5EE112E4` -> `E0:AE:5E:E1:12:E4`). */
export function toBluetoothAddress(serial: string): string {
  const hex = serial.replace(/[:-]/g, "").toUpperCase();
  return hex.match(/.{2}/g)!.join(":");
}

/** Confirmation text (UC-005 step 2, A2: a player in a running game is eliminated). */
export function unpairConfirmMessage(serial: string): string {
  return (
    `Unpair controller ${serial}?\n\n` +
    "It is removed from every Bluetooth adapter and disconnects now; " +
    "a player in a running game is eliminated. " +
    "If it is plugged in over USB, it is not paired again until you unplug it and plug it back in."
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
      body: JSON.stringify({ address: toBluetoothAddress(serial) }),
    });
  } catch (err) {
    return { kind: "failed", status: 0, message: `Unpair failed: ${String(err)}` };
  }

  let body: {
    error?: string;
    removed?: number;
    already_absent?: number;
    failed?: number;
    adapters?: string[];
    on_usb?: boolean;
  } = {};
  try {
    body = await response.json();
  } catch {
    // non-JSON error page; fall back to the status text
  }

  if (!response.ok) {
    const partial = body.removed !== undefined ? ` (removed ${body.removed}, failed ${body.failed ?? 0})` : "";
    return {
      kind: "failed",
      status: response.status,
      message: (body.error ?? `Unpair failed: ${response.status} ${response.statusText}`) + partial,
    };
  }
  const removed = body.removed ?? 0;
  const adapters = body.adapters?.length ? ` (${body.adapters.join(", ")})` : "";
  const absent = body.already_absent ? `, ${body.already_absent} already absent` : "";
  const usb = body.on_usb ? "; still on USB: unplug it, it is paired again only after you plug it back in" : "";
  return { kind: "unpaired", message: `Unpaired ${serial}: ${removed} registration(s) removed${adapters}${absent}${usb}` };
}
