/**
 * Game event feed reducer (UC-035 main step 2, D-D1).
 *
 * Folds one coordinator game event (`GameEvent` in lib/types.py) into the
 * dashboard's game state, alive count and event-feed line. Pure, so the
 * event-name contract with the coordinator is testable without a DOM.
 */

export interface GameEventInput {
  eventType: string;
  data: { [key: string]: string };
}

export interface GameFeedState {
  gameState: string;
  alivePlayers: number;
  totalPlayers: number;
  /** Winner serial from a `game_winner` event, shown on the following `game_ended`. */
  winner: string | null;
}

export interface GameEventEffect {
  state: GameFeedState;
  /** Line for the event feed, or null when the event shows nothing. */
  feedLine: string | null;
  /** Start/stop button state, or null when unchanged. */
  gameRunning: boolean | null;
}

const SERIAL_RE = /^([0-9a-f]{2}:){5}[0-9a-f]{2}$/i;

/** Why a game ended without a winner: the explicit reason, else a non-result outcome (#1935). */
function noWinnerDetail(data: Record<string, string>): string {
  const detail = data["reason"] || (["winner", "completed", "tie"].includes(data["outcome"] ?? "winner") ? "" : data["outcome"]);
  return detail ? ` (${detail.replace(/_/g, "-")})` : "";
}

/**
 * `game_ended` winner: a serial is a controller, `none` is no winner, anything else a team name (#1935).
 * Modes without a `winner` field publish `winning_team` (Swapper, Traitor: a numeric team index, `-1` or
 * `none` = no winner) or `champion` (Tournament: a serial) (#1977). Teams base publishes neither.
 */
function endedLine(data: Record<string, string>, fallbackSerial: string | null): string {
  const raw = present(data["winner"]) || present(data["champion"]) || teamWinner(data["winning_team"]);
  if (raw === "none") return `Game ended! No winner${noWinnerDetail(data)}`;
  if (raw && !SERIAL_RE.test(raw)) return `Game ended! Winner: ${/^Team \d+$/.test(raw) ? raw : `team ${raw}`}`;
  const winner = raw ? shortSerial(raw) : fallbackSerial;
  return winner ? `Game ended! Winner: ${winner}` : "Game ended!";
}

/** The event bus stringifies payload values, so an unset serial arrives as "None": treat it as absent (#1977). */
function present(value: string | undefined): string | undefined {
  return value === undefined || value === "" || value === "None" ? undefined : value;
}

/** `winning_team` as an `endedLine` winner: "Team N" for an index, `none` for -1/none, a name as is. */
function teamWinner(raw: string | undefined): string | undefined {
  if (raw === undefined || raw === "") return undefined;
  if (raw === "none" || raw === "-1") return "none";
  return /^\d+$/.test(raw) ? `Team ${raw}` : raw;
}

const shortSerial = (serial: string | undefined): string => serial?.slice(-4) || "???";

export function reduceGameEvent(prev: GameFeedState, event: GameEventInput): GameEventEffect {
  const { eventType, data } = event;
  const state = { ...prev };
  let feedLine: string | null = null;
  let gameRunning: boolean | null = null;

  switch (eventType) {
    case "game_start":
      state.gameState = "Running";
      state.alivePlayers = state.totalPlayers;
      state.winner = null;
      gameRunning = true;
      feedLine = "Game started!";
      break;

    case "game_winner":
      state.winner = data["serial"] ? shortSerial(data["serial"]) : null;
      break;

    case "game_ended": {
      state.gameState = "Ended";
      gameRunning = false;
      feedLine = endedLine(data, state.winner);
      state.winner = null;
      break;
    }

    case "game_force_ended":
      state.gameState = "Ended";
      gameRunning = false;
      feedLine = "Game stopped";
      break;

    case "game_error":
      state.gameState = "Ended";
      gameRunning = false;
      feedLine = "Game ended with an error";
      break;

    case "game_start_error":
      state.gameState = "Ended";
      gameRunning = false;
      feedLine = `Game failed to start: ${data["error"] || "unknown error"}`;
      break;

    case "player_death":
      state.alivePlayers = Math.max(0, state.alivePlayers - 1);
      feedLine = `Player ${shortSerial(data["serial"])} eliminated`;
      break;

    case "player_shielded": {
      if (data["activation"] === "trigger") {
        const delayS = Number(data["charge_delay_ms"]) / 1000;
        feedLine = `${shortSerial(data["serial"])} activated the shield${Number.isFinite(delayS) ? ` (after ${delayS.toFixed(1)}s)` : ""}`;
        break;
      }
      const kind = data["kind"] === "toughness_boost" || data["kind"] === "partial" ? "toughness boost" : "shield";
      feedLine = `Agent: ${kind} for ${shortSerial(data["serial"])} (${data["seconds"] ?? "?"}s)`;
      break;
    }

    case "player_shield_charged":
      feedLine = `Agent: shield charge for ${shortSerial(data["serial"])} (${data["seconds"] ?? "?"}s)`;
      break;

    case "player_shield_charge_ended":
      // Only an expiry is news: activation and death have their own lines.
      if (data["reason"] === "expired") feedLine = `${shortSerial(data["serial"])}'s shield charge expired`;
      break;

    case "countdown_start":
      state.gameState = "Starting";
      feedLine = "Countdown...";
      break;

    // The coordinator sends game_started before the countdown (#1767), so the
    // countdown's end is what returns the state to Running.
    case "game_started":
    case "countdown_end":
      state.gameState = "Running";
      break;

    default:
      break;
  }
  return { state, feedLine, gameRunning };
}
