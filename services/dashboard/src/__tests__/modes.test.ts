/**
 * Dashboard mode selector (UC-011 BR-001/BR-002, #1278): the options come from
 * the menu's ListGameModes, never a hardcoded list, and FFA is preselected.
 */
import { describe, expect, it, vi } from "vitest";
import indexHtml from "../index.html?raw";
import { DEFAULT_MODE, FALLBACK_CHOICES, loadModeChoices, modeChoices, startGameEvent } from "../modes.js";

const PLAYABLE = [
  "JoustFFA",
  "JoustTeams",
  "JoustRandomTeams",
  "Traitor",
  "Werewolf",
  "Zombies",
  "Swapper",
  "FightClub",
  "Tournament",
  "NonStop",
];

describe("UC-011 BR-002: dashboard mode selector options", () => {
  it("UC-011 BR-002 offers exactly the modes ListGameModes returns, FFA preselected", () => {
    const choices = modeChoices({ gameModes: PLAYABLE, defaultMode: "JoustFFA" });

    expect(choices.options.map((o) => o.value)).toEqual(PLAYABLE);
    expect(choices.selected).toBe("JoustFFA");
    expect(choices.options[0]).toEqual({ value: "JoustFFA", label: "Joust FFA" });
    expect(choices.options.find((o) => o.value === "FightClub")?.label).toBe("Fight Club");
  });

  it("UC-011 BR-001 the static page hardcodes no mode list (no Commander / Ninja)", () => {
    const select = /<select id="game-mode">([\s\S]*?)<\/select>/.exec(indexHtml)?.[1] ?? "";
    const values = [...select.matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);

    expect(values).toEqual([DEFAULT_MODE]);
  });

  it("UC-011 BR-002 falls back to the first mode when the default is not offered", () => {
    expect(modeChoices({ gameModes: ["JoustTeams", "Tournament"], defaultMode: "JoustFFA" }).selected).toBe(
      "JoustTeams"
    );
  });

  it("UC-011 BR-002 offers FFA alone when the menu returns no modes", () => {
    expect(modeChoices({})).toEqual(FALLBACK_CHOICES);
  });

  it("UC-011 BR-002 offers FFA alone when ListGameModes fails, so Start still works", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const choices = await loadModeChoices(() => Promise.reject(new Error("RPC failed: 503")));
    error.mockRestore();

    expect(choices).toEqual({ options: [{ value: "JoustFFA", label: "Joust FFA" }], selected: "JoustFFA" });
  });

  it("UC-011 BR-002 reports a rejected start from an error-only answer (Connect omits success=false)", () => {
    expect(startGameEvent("Commander", { error: "Unknown or unimplemented game mode: Commander" })).toBe(
      "Start rejected: Unknown or unimplemented game mode: Commander"
    );
    expect(startGameEvent("Commander", {})).toBe("Start rejected: unknown error");
  });

  it("UC-011 BR-002 reports an accepted start", () => {
    expect(startGameEvent("JoustFFA", { success: true })).toBe("Starting JoustFFA...");
  });

  it("UC-011 BR-002 loads the choices from ListGameModes", async () => {
    const call = vi.fn().mockResolvedValue({ gameModes: PLAYABLE, defaultMode: "JoustFFA" });

    const choices = await loadModeChoices(call);

    expect(call).toHaveBeenCalledOnce();
    expect(choices.options).toHaveLength(10);
    expect(choices.selected).toBe("JoustFFA");
  });
});
