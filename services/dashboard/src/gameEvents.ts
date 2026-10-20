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
      const winner = data["winner"] ? shortSerial(data["winner"]) : state.winner;
      feedLine = winner ? `Game ended! Winner: ${winner}` : "Game ended!";
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
      const kind = data["kind"] === "partial" ? "partial shield" : "shield";
      feedLine = `Agent: ${kind} for ${shortSerial(data["serial"])} (${data["seconds"] ?? "?"}s)`;
      break;
    }

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
