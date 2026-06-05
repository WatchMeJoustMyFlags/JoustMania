# Research: Shadow Games — Mock-Only Parallel Games for the Agent (#774)

Analysis of what is missing for the agent to start games with only mock
controllers — possibly several in parallel — alongside a real game with real
players, and how the same capability benefits integration tests. Informs the
ACT layer of the agent initiative (#722) and is sequenced after the
intervention API (#730).

## 1. Scope

A **shadow game** is a game started programmatically (by the agent or a test)
using only mock controllers, running on the same stack — same
controller-manager, same game-coordinator — as the real, menu-driven game.
Use cases:

- The agent evaluates intervention strategies against a synthetic game
  without touching the live game
- The agent exercises game modes end-to-end as a health check
- Integration tests run multiple game lifecycles concurrently against one
  compose stack

The headline finding: **most of the infrastructure already exists**. Mock
controllers, headless game starts, and a multi-subscriber motion-data plane
are all in place. The gaps are concentrated in four places: the
game-coordinator's single-game state machine, missing `game_id` routing in
the proto, menu interference with agent-owned controllers, and the agent's
lack of any game-control client.

## 2. What Already Exists

### 2.1 Mock controller infrastructure (complete)

| Piece | Location |
|---|---|
| Mock RPC service (port 50062): `AddController(s)`, `SimulateButton/Movement/Death`, `SetColor/GetColor`, `StreamObservability`, `ListMockControllers` | [controller_manager_mock.proto](../../proto/controller_manager_mock.proto), [mock_control_service.py](../../services/controller_manager/mock_control_service.py) |
| Mock adapter: in-memory controller state with sensor noise, battery drain, LED/rumble tracking, observer events | [mock_adapter.py](../../services/controller_manager/multiplexer/mock_adapter.py) |
| Mock adapter **always injected** alongside hardware adapters ("Mock starts with 0 controllers; tests/demos add them via AddController") | [backend_factory.py:151-154](../../services/controller_manager/backend_factory.py) |

The last row matters most: because the mock adapter rides along even when the
backend flag is `python` or `rust`, mock controllers can be added **on a
production deployment with real controllers connected**. No flag flip or
restart is needed for the agent to create synthetic players.

### 2.2 Headless game start (exists)

`StreamGameEvents(StreamEventsRequest{start_config})` starts a game directly,
bypassing the menu entirely
([game_coordinator.proto:13-15](../../proto/game_coordinator.proto),
[servicer.py:320-341](../../services/game_coordinator/servicer.py)). The menu
itself uses this same path. So "start a game without the lobby flow" works
today — for **one** game.

### 2.3 Multi-subscriber motion data plane (exists)

`StreamGameplayData` supports multiple concurrent subscribers, each with its
own per-stream serial filter (`current_filter` is per-invocation state,
[servicer.py:315-346](../../services/controller_manager/servicer.py);
subscriber queues at
[servicer.py:72](../../services/controller_manager/servicer.py)). Two game
instances reading disjoint controller sets can stream motion concurrently
with **zero controller-manager changes**.

`StreamButtonEvents` also supports multiple subscribers but has **no
per-subscriber filtering** — every subscriber gets every event. The menu is
the only button-stream consumer today; this asymmetry shapes the reservation
design in §5.

### 2.4 Integration test harness (exists, single-game)

CI runs the full stack with `backend=mock`
([controller.ci.json](../../services/flagd/controller.ci.json),
[docker-compose.ci.yml](../../docker-compose.ci.yml)) and drives 14
parametrized game modes end-to-end through the menu flow
([test_full_game_lifecycle.py](../../tests/integration/test_full_game_lifecycle.py),
[helpers.py](../../tests/integration/helpers.py)). Tests are strictly
sequential: a fixture force-ends "the" game between tests because there is
only one.

## 3. The Four Blockers

### 3.1 Single-game state machine in the game coordinator

[servicer.py](../../services/game_coordinator/servicer.py) holds exactly one
game: `self.current_game`, `self.game_state`, `self.game_id` (:43-51), one
global `EventBus` (:58), and a hard gate:

```python
if self.game_state in [STARTING, RUNNING]:
    return False, "Game already in progress"   # servicer.py:101-105
```

Two latent bugs become **load-bearing** under parallelism:

1. **game_id collision** — `game_id = f"game_{int(time.time())}"`
   ([servicer.py:115](../../services/game_coordinator/servicer.py)) collides
   at second resolution. Two games started near-simultaneously (exactly the
   parallel-test scenario) get the same id.
2. **Metrics cross-wipe** — `clear_all_player_analytics()`
   ([metrics.py:258-282](../../services/game_coordinator/metrics.py)) does a
   global `_metrics.clear()` on every player gauge and zeroes game-state
   gauges. With two concurrent games, the first to end wipes the live game's
   dashboard. A targeted alternative already exists:
   `clear_player_analytics(serial, game_id)`
   ([metrics.py:231](../../services/game_coordinator/metrics.py)).

One thing that **helps**: each game already runs in its own background thread
with its own asyncio loop and its own `GrpcClientManager` created inside that
loop ([servicer.py:151-181](../../services/game_coordinator/servicer.py)).
The per-game isolation primitive exists; only the coordinator's bookkeeping
is singular.

### 3.2 No game_id routing in the proto

[game_coordinator.proto](../../proto/game_coordinator.proto):
`StreamEventsRequest` (:115-119) carries only an optional `start_config`;
`GameEvent` (:121-125) has no `game_id`; `ForceEndGameRequest` (:106-108) and
`GetGameStateRequest` (:128) operate on "the" game. A headless starter cannot
even learn the id of the game it started — today it is only recorded as a
span attribute ([servicer.py:340](../../services/game_coordinator/servicer.py)).

### 3.3 Menu interference — no controller reservation

The menu learns about controllers exclusively from button-stream connect
events: `_send_initial_connection_events` announces **every** tracked
controller to every new subscriber
([servicer.py:155-181](../../services/controller_manager/servicer.py)), and
each `ButtonEvent` carries a `connected_serials` roster of all serials
([servicer.py:172](../../services/controller_manager/servicer.py)). The menu
tracks all of them in its state manager, writes lobby LEDs (last-write-wins,
no arbitration —
[led.py:103-133](../../services/menu/utils/led.py)), and counts trigger
presses toward ready state.

Consequence: if the agent adds four mock controllers for a shadow game, the
menu shows four phantom players in the lobby, fights the shadow game over
their LEDs, and a simulated trigger press doubles as a lobby ready-up.

A serial-prefix convention (mock serials are `MOCK%04d` /
`mock_controller_N`,
[mock_adapter.py:82,200](../../services/controller_manager/multiplexer/mock_adapter.py))
**cannot** fix this: integration tests legitimately drive *menu-flow* games
with unreserved mock controllers, so the prefix cannot distinguish
"agent-reserved mock" from "test's lobby mock".

### 3.4 Agent has no game-control capability

[services/agent](../../services/agent) (Go) is observation-only today: an
OTLP receiver with span/metric processing (`receiver.go`, `decision/`,
`gamecontext/`, `gate/`). It has no proto dependency and no gRPC clients
toward the game stack. Game-start is an ACT-layer capability, adjacent to but
distinct from the #730 intervention API.

## 4. Design Decisions

### 4.1 Multi-game: in-process `GameSession` dict (not multi-container)

Promote the coordinator's singular fields into a `GameSession` object
(`game_id`, `game_name`, `players`, config, state, `current_game`,
`event_bus`, thread, running flag, parent trace context) and hold
`dict[game_id, GameSession]`.

Rejected alternative — one game-coordinator container per game: would
multiply OTLP pipelines, flagd connections, and audio fan-out, and require a
router/registry plus dynamic orchestration. Since each game already owns its
thread + loop + clients (§3.1), the dict refactor is a state-container move,
not a concurrency redesign. If the agent ever needs many (>~4) concurrent
shadow games, revisit. A configurable max-concurrent-games cap bounds
resource use.

### 4.2 EventBus: per-session bus + persistent primary bus

Each `GameSession` gets its own `EventBus` — the bus's state-sync callback
mutates *that game's* state
([servicer.py:72-84](../../services/game_coordinator/servicer.py)), so a
shared game_id-tagged bus would reintroduce routing coupling.

Backward compatibility: today a subscriber with no `start_config` attaches
before any game exists and receives "the" game's events. Preserve this with a
**persistent primary bus** owned by the servicer that the menu-driven
(primary) session publishes to; agent/shadow sessions get their own buses.
Zero-arg subscribes bind to the primary bus → **the menu needs no changes**.

### 4.3 Metrics: `game_kind` label (decision: shadow games are visible)

Add a `game_kind` label (`primary` / `shadow`) to lifecycle metrics
(`active_game`, `active_players`, `games_started_total`,
`game_duration_seconds`, …) so shadow games are observable in their own
series rather than invisible. Two consequences to handle:

- Grafana queries that aggregate (`sum()`) over game metrics need a
  `game_kind` filter review when this lands
- `clear_all_player_analytics()` must become per-session targeted cleanup
  (via the existing `clear_player_analytics(serial, game_id)`) so a shadow
  game ending never touches the primary game's gauges

### 4.4 Reservation: explicit flag + announce-suppression

Add `bool reserved` + `string tag` to `AddController(s)` in
[controller_manager_mock.proto](../../proto/controller_manager_mock.proto);
the mock adapter stores it; the controller-manager suppresses reserved
controllers at **both** leak paths toward button-stream consumers:

1. connect events (initial snapshot *and* live connect path)
2. the `connected_serials` roster inside every `ButtonEvent`

Reserved controllers remain fully usable — gameplay-data streams (per-stream
serial filter) and explicit-serial LED writes still work; they are simply
never *announced*. The `tag` identifies the owning agent/game, enabling
orphan sweeps if the agent crashes.

Rejected alternatives:

- *Per-subscriber button-stream filtering* (mirroring `FilterUpdate`): more
  general, but the menu is the only button consumer and we want reserved
  controllers globally invisible to lobby logic — filtering adds identity
  plumbing for no current consumer
- *Menu-side serial-prefix exclusion*: brittle, and impossible per §3.3
- *Suppress at the menu via flagd*: leaves the roster leak and pushes
  mock-awareness into the menu

### 4.5 Agent path: direct gRPC clients, after #730

The agent calls `MockControllerService` and `GameCoordinatorService` with
generated Go stubs — no new purpose-built RPC, since the headless start path
already exists. Sequencing decision: **after #730**, so the shadow-game
runner reuses the intervention API's transport/client plumbing instead of
building a parallel client stack.

Module wiring to confirm at implementation time: Go gen lands under the
connect-proxy go_package
(`github.com/joustmania/connect-proxy/gen/...`), while the agent module is
`github.com/joustmania/agent` — either depend on the connect-proxy gen module
(`replace` directive) or add an agent-local buf output. The Python services
serve plain gRPC, so use grpc-go stubs (not Connect HTTP).

## 5. Phases

| Phase | Issue | Area | Summary |
|---|---|---|---|
| 1 | [#775](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/775) | game_coordinator | Characterization tests, `GameSession` dict, per-session EventBus + primary bus, uuid game_id, `game_kind` metrics, targeted analytics cleanup, concurrency cap |
| 2 | [#776](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/776) | proto + game_coordinator | `optional game_id` on requests, `game_id` on `GameEvent`, `ListGames` RPC; empty id = primary game (menu unchanged) |
| 3 | [#777](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/777) | controller_manager | `reserved`/`tag` on mock AddController(s); suppress connect events + roster for reserved controllers |
| 4 | [#778](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/778) | agent (Go) | `ShadowGameRunner`: reserve mocks → headless start → capture game_id → drive via Simulate\* → force-end + cleanup. Depends on #730 |
| 5 | [#779](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/779) | tests/integration | Headless-start helper, game_id-aware collectors, fixture split, concurrent + isolation tests |

Phases 1–2 are the load-bearing refactor; 3 unblocks "alongside a live menu
game"; 4 is the agent capability; 5 is the payoff. Phase 3 is independent of
1–2 and can land in parallel.

## 6. Integration Test Impact

Yes, this directly helps the test suite (related: #770, CI speed):

- **Concurrent lifecycle tests** — each test starts its own headless game
  with its own mock controller set and game_id; no shared global game. The
  data plane already multiplexes (§2.3), so pytest-xdist becomes viable for
  non-menu tests
- **Per-game cleanup** — the global force-end-between-tests fixture splits
  into a menu-scoped one (sequential menu-flow tests) and per-test
  game_id-scoped cleanup (force-end by id, remove reserved controllers)
- **Direct regression test for this feature** — run a menu-flow game with
  unreserved mocks *and* a headless game with reserved mocks simultaneously;
  assert the lobby never sees the reserved controllers and ready-count is
  unaffected

Constraint that does **not** change: the menu remains single-lobby, so
menu-flow tests stay sequential.
