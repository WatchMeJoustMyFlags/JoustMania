/**
 * TypeScript types for controller_manager.proto messages used by the dashboard.
 * Hand-written (not buf output); keep in sync with the .proto by hand.
 */

// RGB color
export interface RGB {
  r: number;
  g: number;
  b: number;
}

// Vector3 for accelerometer/gyro data
export interface Vector3 {
  x: number;
  y: number;
  z: number;
}

// Gameplay data for a single controller
export interface GameplayData {
  serial: string;
  moveNum: number;
  battery: number;
  team: number;
  color?: RGB;
  accel?: Vector3;
  gyro?: Vector3;
  rssi: number;
  /** Unset until the controller's 10 s gap window has data (UC-035 BR-005). */
  connectionQuality?: ConnectionQuality;
}

// Connection quality from fresh-frame gaps over a rolling 10 s window (UC-035 BR-005).
// Proto3 JSON omits empty / zero fields.
export interface ConnectionQuality {
  level?: string;
  reportGapP95Ms?: number;
  reportGapMaxMs?: number;
}

// Gameplay data update with all controllers
export interface GameplayDataUpdate {
  controllers: GameplayData[];
  timestamp: bigint;
}

// Gameplay stream request
export interface GameplayStreamRequest {
  updateFrequencyHz: number;
}
