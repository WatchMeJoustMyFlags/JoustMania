/**
 * Unpair a controller from its dashboard card (UC-005).
 */
import { describe, expect, it, vi } from "vitest";
import { canUnpair, requestUnpair, unpairConfirmMessage } from "../unpair.js";

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

  it("UC-005/A6 shows the 'being paired' refusal", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(409, { error: "This controller is being paired over USB" }));

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
