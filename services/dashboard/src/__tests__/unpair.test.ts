/**
 * Unpair a controller from its dashboard card (UC-005).
 */
import { describe, expect, it, vi } from "vitest";
import { canUnpair, requestUnpair, toBluetoothAddress, unpairConfirmMessage } from "../unpair.js";

const SERIAL = "00:06:F7:AA:BB:CC";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("UC-005: Unpair a Controller", () => {
  it("UC-005/main confirms, posts the address and reports the removed registrations", async () => {
    const confirm = vi.fn(() => true);
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, { address: SERIAL, removed: 2, adapters: ["AA:00:00:00:00:00", "BB:00:00:00:00:00"] }),
    );

    const outcome = await requestUnpair(SERIAL, { confirm, fetch: fetchMock as typeof fetch, baseUrl: "http://pi" });

    expect(confirm).toHaveBeenCalledWith(unpairConfirmMessage(SERIAL));
    expect(fetchMock).toHaveBeenCalledWith("http://pi/pairing/unpair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address: SERIAL }),
    });
    expect(outcome).toEqual({
      kind: "unpaired",
      message: `Unpaired ${SERIAL}: 2 registration(s) removed (AA:00:00:00:00:00, BB:00:00:00:00:00)`,
    });
  });

  it("UC-005/A4 all registrations already absent is still success (idempotent)", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, { address: SERIAL, removed: 0, already_absent: 2, failed: 0, adapters: [], on_usb: false }),
    );

    const outcome = await requestUnpair(SERIAL, { confirm: () => true, fetch: fetchMock as typeof fetch });

    expect(outcome).toEqual({ kind: "unpaired", message: `Unpaired ${SERIAL}: 0 registration(s) removed, 2 already absent` });
  });

  it("UC-005/A8 a controller still on USB is told to unplug and replug", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(200, { address: SERIAL, removed: 1, already_absent: 0, failed: 0, adapters: ["AA:00:00:00:00:00"], on_usb: true }),
    );

    const outcome = await requestUnpair(SERIAL, { confirm: () => true, fetch: fetchMock as typeof fetch });

    expect(outcome.kind).toBe("unpaired");
    expect(outcome.kind === "unpaired" && outcome.message).toContain("still on USB");
    expect(unpairConfirmMessage(SERIAL)).toContain("unplug it and plug it back in");
  });

  it("UC-005/A7 a partial failure shows the counts", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(503, { error: "1 registration(s) could not be removed", removed: 1, already_absent: 0, failed: 1 }),
    );

    const outcome = await requestUnpair(SERIAL, { confirm: () => true, fetch: fetchMock as typeof fetch });

    expect(outcome).toEqual({
      kind: "failed",
      status: 503,
      message: "1 registration(s) could not be removed (removed 1, failed 1)",
    });
  });

  it("UC-005/A1 cancelling the confirmation sends nothing", async () => {
    const fetchMock = vi.fn();

    const outcome = await requestUnpair(SERIAL, { confirm: () => false, fetch: fetchMock as typeof fetch });

    expect(outcome).toEqual({ kind: "cancelled" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("UC-005/A2 the confirmation warns that a player in a running game is eliminated", () => {
    const message = unpairConfirmMessage(SERIAL);
    expect(message).toContain(SERIAL);
    expect(message).toContain("disconnects now");
    expect(message).toContain("running game is eliminated");
  });

  it("UC-005/A4 shows the daemon's 'not registered' error", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse(404, { error: `Controller ${SERIAL} is not registered on any adapter.` }),
    );

    const outcome = await requestUnpair(SERIAL, { confirm: () => true, fetch: fetchMock as typeof fetch });

    expect(outcome).toEqual({ kind: "failed", status: 404, message: `Controller ${SERIAL} is not registered on any adapter.` });
  });

  it("UC-005/A5 mock serials have no unpair action and never reach the network", async () => {
    expect(canUnpair(SERIAL)).toBe(true);
    expect(canUnpair("00-06-f7-aa-bb-cc")).toBe(true);
    expect(canUnpair("MOCK0001")).toBe(false);
    const confirm = vi.fn(() => true);
    const fetchMock = vi.fn();

    const outcome = await requestUnpair("MOCK0001", { confirm, fetch: fetchMock as typeof fetch });

    expect(outcome.kind).toBe("failed");
    expect(confirm).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("UC-005/main a bare 12-hex serial (as real controllers report it) can be unpaired and is sent with colons", async () => {
    const bare = "E0AE5EE112E4";
    expect(canUnpair(bare)).toBe(true);
    expect(canUnpair("e0ae5ee112e4")).toBe(true);
    expect(canUnpair("E0AE5EE112E")).toBe(false);
    expect(canUnpair("E0AE5EE112E4F")).toBe(false);
    expect(toBluetoothAddress("e0-ae-5e-e1-12-e4")).toBe("E0:AE:5E:E1:12:E4");
    const fetchMock = vi.fn(async () => jsonResponse(200, { removed: 1 }));

    await requestUnpair(bare, { confirm: () => true, fetch: fetchMock as typeof fetch });

    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(JSON.parse(init.body as string)).toEqual({ address: "E0:AE:5E:E1:12:E4" });
  });

  it("UC-005/A6 shows the 'being paired' refusal", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(409, { error: "A USB pairing or another unpair is running; try again in a few seconds." }));

    const outcome = await requestUnpair(SERIAL, { confirm: () => true, fetch: fetchMock as typeof fetch });

    expect(outcome).toMatchObject({ kind: "failed", status: 409 });
  });

  it("UC-005/A7 reports an unreachable pairing daemon or network failure", async () => {
    const badGateway = vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    expect(await requestUnpair(SERIAL, { confirm: () => true, fetch: badGateway as typeof fetch })).toEqual({
      kind: "failed",
      status: 502,
      message: "Unpair failed: 502 Bad Gateway",
    });

    const offline = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    expect(await requestUnpair(SERIAL, { confirm: () => true, fetch: offline as typeof fetch })).toMatchObject({
      kind: "failed",
      status: 0,
    });
  });
});
