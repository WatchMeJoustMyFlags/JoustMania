/**
 * TypeScript types for menu.proto messages used by the dashboard.
 * Hand-written (not buf output); keep in sync with the .proto by hand.
 */

// Process input request
export interface ProcessInputRequest {
  inputType: string;
  data: { [key: string]: string };
}

// Process input response (Connect JSON omits false / empty fields)
export interface ProcessInputResponse {
  success?: boolean;
  error?: string;
}

// List game modes request
export type ListGameModesRequest = Record<string, never>;

// List game modes response (Connect JSON omits an empty list)
export interface ListGameModesResponse {
  gameModes: string[];
  defaultMode: string;
}

// GetMenuStatus (read-only snapshot, UC-015 A5). Connect JSON omits zero / empty fields and
// writes int32 as numbers; only the fields the stage-settings panel reads are typed.
export interface GetMenuStatusResponseLike {
  sensitivity?: number;
  numTeams?: number;
  shieldActivation?: string;
  operatorSettings?: { [key: string]: string };
}
