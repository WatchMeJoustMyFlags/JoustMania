/**
 * Simple Connect-protocol client for JoustMania services
 * Uses fetch with JSON encoding (Connect protocol supports JSON)
 */

import type {
  GameplayStreamRequest,
  GameplayDataUpdate,
  IdentifyControllerRequest,
  IdentifyControllerResponse,
} from "./gen/controller_manager_pb.js";
import type {
  ForceEndGameRequest,
  ForceEndGameResponse,
  StreamEventsRequest,
  GameEvent,
} from "./gen/game_coordinator_pb.js";
import type {
  ProcessInputRequest,
  ProcessInputResponse,
} from "./gen/menu_pb.js";
import type { GetGameStateResponseLike, ListGamesResponseLike } from "./follow.js";

const BASE_URL = import.meta.env.DEV ? "" : globalThis.location.origin;

// Generic unary call
async function unaryCall<Req, Res>(
  service: string,
  method: string,
  request: Req,
  signal?: AbortSignal
): Promise<Res> {
  const url = `${BASE_URL}/${service}/${method}`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    throw new Error(`RPC failed: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

// Generic server streaming call (uses Server-Sent Events style)
async function* serverStream<Req, Res>(
  service: string,
  method: string,
  request: Req,
  signal?: AbortSignal
): AsyncGenerator<Res> {
  const url = `${BASE_URL}/${service}/${method}`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    throw new Error(`RPC failed: ${response.status} ${response.statusText}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("No response body");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Parse newline-delimited JSON messages
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.trim()) {
        try {
          const msg = JSON.parse(line);
          // Connect protocol wraps messages in { "result": ... } for streaming
          yield (msg.result || msg) as Res;
        } catch {
          // Skip malformed lines
        }
      }
    }
  }
}

// Controller Manager Client
export const controllerClient = {
  streamGameplayData(request: GameplayStreamRequest) {
    return serverStream<GameplayStreamRequest, GameplayDataUpdate>(
      "joustmania.controller_manager.ControllerManagerService",
      "StreamGameplayData",
      request
    );
  },
  // UC-004: flash + rumble one controller so the operator can find it
  identifyController(request: IdentifyControllerRequest, signal?: AbortSignal): Promise<IdentifyControllerResponse> {
    return unaryCall(
      "joustmania.controller_manager.ControllerManagerService",
      "IdentifyController",
      request,
      signal
    );
  },
};

// Game Coordinator Client
export const gameClient = {
  forceEndGame(request: ForceEndGameRequest): Promise<ForceEndGameResponse> {
    return unaryCall(
      "joustmania.game_coordinator.GameCoordinatorService",
      "ForceEndGame",
      request
    );
  },
  // `gameId` subscribes to one game's bus (a shadow game, UC-070 BR-005);
  // `signal` ends the stream when the dashboard stops following that game.
  streamGameEvents(request: StreamEventsRequest, signal?: AbortSignal) {
    return serverStream<StreamEventsRequest, GameEvent>(
      "joustmania.game_coordinator.GameCoordinatorService",
      "StreamGameEvents",
      request,
      signal
    );
  },
  // Empty gameId = the primary (real) game's slot.
  getGameState(request: { gameId?: string }, signal?: AbortSignal): Promise<GetGameStateResponseLike> {
    return unaryCall(
      "joustmania.game_coordinator.GameCoordinatorService",
      "GetGameState",
      request,
      signal
    );
  },
  listGames(signal?: AbortSignal): Promise<ListGamesResponseLike> {
    return unaryCall(
      "joustmania.game_coordinator.GameCoordinatorService",
      "ListGames",
      {},
      signal
    );
  },
};

// Menu Client
export const menuClient = {
  processInput(request: ProcessInputRequest): Promise<ProcessInputResponse> {
    return unaryCall(
      "joustmania.menu.MenuService",
      "ProcessInput",
      request
    );
  },
};
