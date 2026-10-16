/**
 * TypeScript types for menu.proto messages used by the dashboard.
 * Hand-written (not buf output); keep in sync with the .proto by hand.
 */

// Process input request
export interface ProcessInputRequest {
  inputType: string;
  data: { [key: string]: string };
}

// Process input response
export interface ProcessInputResponse {
  success: boolean;
  error: string;
}
