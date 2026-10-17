/**
 * Identify a controller from its dashboard card (UC-004, FR-121, #1430): the
 * card calls IdentifyController and shows what the controller manager answered.
 */
import { describe, expect, it, vi } from "vitest";
import { IDENTIFY_DEFAULT_MS, identifyController, identifyStatus } from "../identify.js";

describe("UC-004: identify a controller from its card", () => {
  it("UC-004 main sends the card's serial and shows Identifying for the effect", async () => {
    const call = vi.fn().mockResolvedValue({ success: true, outcome: "started" });

    const status = await identifyController("MOCK0001", call);

    expect(call).toHaveBeenCalledWith({ serial: "MOCK0001", durationMs: IDENTIFY_DEFAULT_MS });
    expect(status).toEqual({ text: "Identifying", ok: true, showForMs: IDENTIFY_DEFAULT_MS });
  });

  it("UC-004/A1 shows Not connected when the controller is gone", async () => {
    const call = vi.fn().mockResolvedValue({ outcome: "not_connected", error: "Controller MOCK0001 is not connected" });

    const status = await identifyController("MOCK0001", call);

    expect(status.text).toBe("Not connected");
    expect(status.ok).toBe(false);
  });

  it("UC-004/A2 shows In game for a player of a running game", () => {
    expect(identifyStatus({ outcome: "in_game" })).toMatchObject({ text: "In game", ok: false });
  });

  it("UC-004/A3 shows Reserved for a shadow-game controller", () => {
    expect(identifyStatus({ outcome: "reserved" })).toMatchObject({ text: "Reserved", ok: false });
  });

  it("UC-004/A6 shows Invalid for an empty serial", () => {
    expect(identifyStatus({ outcome: "invalid" })).toMatchObject({ text: "Invalid", ok: false });
  });

  it("UC-004 shows Failed when the RPC itself fails or the outcome is unknown", async () => {
    const call = vi.fn().mockRejectedValue(new Error("RPC failed: 500"));
    expect(await identifyController("MOCK0001", call)).toMatchObject({ text: "Failed", ok: false });
    // Proto3 JSON omits default fields: an empty object is not a success.
    expect(identifyStatus({})).toMatchObject({ text: "Failed", ok: false });
  });
});
