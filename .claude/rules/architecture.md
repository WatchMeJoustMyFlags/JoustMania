# Architecture

| Service | Port | Role |
|---------|------|------|
| Controller Manager | 50052 | PS Move hardware, buttons, motion |
| Game Coordinator | 50053 | Game lifecycle, death detection, scoring |
| Menu | 50054 | Lobby, game selection, ready state |
| Audio | 50056 | Sound effects, music, announcements |

Dependencies: Audio & ControllerManager (no deps) -> GameCoordinator -> Menu

Streams (bidir): `StreamButtonEvents` (buttons + LEDs), `StreamGameplayData` (motion + effects). Server-stream: `StreamGameEvents`, `StreamMenuEvents`.

States - Controller: DISCONNECTED -> CONNECTED (dim LED) -> READY (bright LED). Game: IDLE -> STARTING -> RUNNING -> ENDING -> ENDED.

Motion: 1000Hz hardware poll -> 60Hz stream (active) / 10Hz (idle). EMA filter. Death threshold by sensitivity 0-4.
