/**
 * Simple Connect-protocol client for JoustMania services
 * Unary calls: fetch with plain JSON. Server streams: Connect streaming
 * framing (application/connect+json envelopes, connect.ts, #1542).
 */

import type {
  GameplayStreamConfig,
  GameplayDataUpdate,
  IdentifyControllerRequest,
  IdentifyControllerResponse,
  GetConnectedControllersResponse,
} from "./gen/controller_manager_pb.js";
import type {
  ForceEndGameRequest,
  ForceEndGameResponse,
  StreamEventsRequest,
  GameEvent,
} from "./gen/game_coordinator_pb.js";
import type {
  ListGameModesRequest,
  ListGameModesResponse,
  ProcessInputRequest,
  ProcessInputResponse,
} from "./gen/menu_pb.js";
import type { GetGameStateResponseLike, ListGamesResponseLike } from "./follow.js";
import { serverStream as connectServerStream } from "./connect.js";

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

// Generic server streaming call: Connect streaming framing (#1542), see connect.ts
function serverStream<Req, Res>(
  service: string,
  method: string,
  request: Req,
  signal?: AbortSignal
): AsyncGenerator<Res> {
  return connectServerStream<Req, Res>(`${BASE_URL}/${service}/${method}`, request, signal);
}

// Controller Manager Client
export const controllerClient = {
  // Server stream relayed by connect-proxy from the bidi StreamGameplayData
  // (#1595); the bidi call itself gets 505 over envoy's HTTP/1.1 hop.
  watchGameplayData(request: GameplayStreamConfig, signal?: AbortSignal) {
    return serverStream<GameplayStreamConfig, GameplayDataUpdate>(
      "joustmania.controller_manager.ControllerManagerService",
      "WatchGameplayData",
      request,
      signal
    );
  },
  // UC-035 A5: the live roster with the last-known battery (#2111)
  getConnectedControllers(signal?: AbortSignal): Promise<GetConnectedControllersResponse> {
    return unaryCall(
      "joustmania.controller_manager.ControllerManagerService",
      "GetConnectedControllers",
      {},
      signal
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
  listGameModes(): Promise<Partial<ListGameModesResponse>> {
    return unaryCall<ListGameModesRequest, Partial<ListGameModesResponse>>(
      "joustmania.menu.MenuService",
      "ListGameModes",
      {}
    );
  },
};
