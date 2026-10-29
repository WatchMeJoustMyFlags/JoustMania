/**
 * Mode selector options (UC-011 BR-002, #1278): built from the menu's
 * ListGameModes, so the dashboard offers exactly the playable modes the menu
 * accepts (UC-011 BR-001) instead of a hardcoded list.
 */
import type { ListGameModesResponse, ProcessInputResponse } from "./gen/menu_pb.js";

export const DEFAULT_MODE = "JoustFFA";

export interface ModeOption {
  value: string;
  label: string;
}

export interface ModeChoices {
  options: ModeOption[];
  selected: string;
}

// Display names; a mode without one shows its Games enum name.
const MODE_LABELS: Record<string, string> = {
  JoustFFA: "Joust FFA",
  JoustTeams: "Joust Teams",
  JoustRandomTeams: "Random Teams",
  FightClub: "Fight Club",
  NonStop: "Non Stop",
};

export function modeLabel(mode: string): string {
  return MODE_LABELS[mode] ?? mode;
}

/** Selector options for a ListGameModes response; the default mode is preselected. */
export function modeChoices(response: Partial<ListGameModesResponse>): ModeChoices {
  const modes = response.gameModes ?? [];
  if (modes.length === 0) return FALLBACK_CHOICES;
  const preferred = response.defaultMode || DEFAULT_MODE;
  return {
    options: modes.map((mode) => ({ value: mode, label: modeLabel(mode) })),
    selected: modes.includes(preferred) ? preferred : modes[0],
  };
}

/**
 * Event-log line for a start_game answer. Connect JSON omits `success: false`,
 * so anything but an explicit `success: true` is a rejection.
 */
export function startGameEvent(mode: string, response: Partial<ProcessInputResponse>): string {
  if (!response.success) return `Start rejected: ${response.error || "unknown error"}`;
  return `Starting ${mode}...`;
}

/** Only the default mode, while the menu is unreachable: Start still works. */
export const FALLBACK_CHOICES: ModeChoices = {
  options: [{ value: DEFAULT_MODE, label: modeLabel(DEFAULT_MODE) }],
  selected: DEFAULT_MODE,
};

/** Fetch the choices; any failure falls back to the default mode alone. */
export async function loadModeChoices(
  listGameModes: () => Promise<Partial<ListGameModesResponse>>
): Promise<ModeChoices> {
  try {
    return modeChoices(await listGameModes());
  } catch (error) {
    console.error("ListGameModes failed, offering the default mode only:", error);
    return FALLBACK_CHOICES;
  }
}

const RETRY_BASE_MS = 2000;
const RETRY_MAX_MS = 30_000;

/**
 * Offer the default mode at once, then retry ListGameModes with backoff until the
 * menu answers with modes (#2387): a dashboard opened while the menu is down no
 * longer stays FFA-only. `apply` gets the fallback on the first failure and the
 * real choices on success. `isActive` ends the loop (the page went away).
 */
export async function loadModeChoicesWithRetry(
  listGameModes: () => Promise<Partial<ListGameModesResponse>>,
  apply: (choices: ModeChoices) => void,
  options: { sleep?: (ms: number) => Promise<void>; isActive?: () => boolean } = {},
): Promise<void> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const isActive = options.isActive ?? (() => true);
  let delayMs = RETRY_BASE_MS;
  let first = true;
  while (isActive()) {
    try {
      const response = await listGameModes();
      if ((response.gameModes ?? []).length > 0) {
        apply(modeChoices(response));
        return;
      }
      console.error("ListGameModes returned no modes, retrying");
    } catch (error) {
      console.error("ListGameModes failed, offering the default mode until it answers:", error);
    }
    if (first) apply(FALLBACK_CHOICES);
    first = false;
    await sleep(delayMs);
    delayMs = Math.min(delayMs * 2, RETRY_MAX_MS);
  }
}
