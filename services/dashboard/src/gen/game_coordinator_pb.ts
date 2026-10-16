/**
 * TypeScript types for game_coordinator.proto messages used by the dashboard.
 * Hand-written (not buf output); keep in sync with the .proto by hand.
 */

// Force end game request
export interface ForceEndGameRequest {
  reason: string;
  gameId?: string;  // empty = primary (legacy)
}

// Force end game response
export interface ForceEndGameResponse {
  success: boolean;
  error: string;
}

// Stream events request
export interface StreamEventsRequest {
  gameId?: string;  // subscribe to a specific game; empty = primary (legacy)
}

// Game event
export interface GameEvent {
  eventType: string;
  data: { [key: string]: string };
  timestamp: bigint;
  gameId: string;  // game this event belongs to (empty if no game bound)
}
