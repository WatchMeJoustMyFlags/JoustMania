/**
 * JoustMania Dashboard - Main Entry Point
 *
 * Real-time controller visualization and game controls using Connect-Web.
 */
import { keepStreamOpen } from "./stream.js";
import { controllerClient, gameClient, menuClient } from "./client.js";
import { ControllerGrid } from "./components/ControllerGrid.js";
import { UNPAIR_NOTICE_EVENT } from "./unpair.js";
import { GameStatus } from "./components/GameStatus.js";
import { Controls } from "./components/Controls.js";
import { ExperimentsView } from "./components/ExperimentsView.js";
import { fetchExperiments } from "./experiments.js";
import { AgentView } from "./components/AgentView.js";
import { DecideStore, fetchAgentView, fetchDecideFeed } from "./agent.js";
import { createPoller } from "./poller.js";
import { ShieldStore } from "./shield.js";
import { FollowTracker, followBanner, visibleTiles } from "./follow.js";
import { loadModeChoices, startGameEvent } from "./modes.js";
import type { Poller } from "./poller.js";
import type { GameplayData } from "./gen/controller_manager_pb.js";

// Poll cadence for the agent experiment view (Loki query). Matches the slow,
// human-readable refresh rate of the embedded observability panels rather than
// the 30Hz controller stream — experiment state moves on the order of games.
const EXPERIMENTS_POLL_MS = 5000;

// Poll cadence for the agent decision/flag/intervention view (Loki + VM query).
// Faster than the experiment view (state moves per decision, not per game) but
// still well under the agent's signal rate — this is an observability tail, not
// a live stream.
const AGENT_POLL_MS = 2000;

// Poll cadence for the agent.decide feed (#1233, Jaeger query). Faster than the
// Loki view: a live decision.backend switch must show on the very next decision,
// and the remote cadence (decision.min_interval_ms) is 1.5-3s. Errors and slow
// responses back off (doubling) to DECIDE_MAX_BACKOFF_MS; each request is aborted
// after DECIDE_TIMEOUT_MS so a struggling Jaeger is not piled onto.
const DECIDE_POLL_MS = 1500;
const DECIDE_MAX_BACKOFF_MS = 10_000;
const DECIDE_TIMEOUT_MS = 4000;

// Poll cadence for the followed game (UC-070 BR-005, #1316): ListGames +
// GetGameState on the game coordinator, two small unary calls. Decides whether
// the dashboard follows a shadow game (no real game live) and which one.
const FOLLOW_POLL_MS = 2000;
const FOLLOW_MAX_BACKOFF_MS = 15_000;
const FOLLOW_TIMEOUT_MS = 3000;

// State
interface AppState {
  controllers: Map<string, GameplayData>;
  gameState: string;
  alivePlayers: number;
  totalPlayers: number;
  events: string[];
  isStreaming: boolean;
}

const state: AppState = {
  controllers: new Map(),
  gameState: "Idle",
  alivePlayers: 0,
  totalPlayers: 0,
  events: [],
  isStreaming: false,
};

// Agent shields per controller, from player_shielded game events (#1301)
const shields = new ShieldStore();

// The followed game (UC-070 BR-005): a live shadow game when no real game is.
// Its shields come from its own event stream, kept apart from the real game's.
const followTracker = new FollowTracker();
const shadowShields = new ShieldStore();
// The game shadowShields belongs to: cleared only when the followed game changes.
let shadowShieldsFor = "";
let shadowStream: { gameId: string; abort: AbortController } | null = null;

// agent.decide rows across polls; the follow poller tells it which game to describe.
const decideStore = new DecideStore();

// Components
let controllerGrid: ControllerGrid;
let gameStatus: GameStatus;
let controls: Controls;
let experimentsView: ExperimentsView;
let agentView: AgentView;

// Initialize the dashboard
async function init() {
  console.log("JoustMania Dashboard initializing...");

  // Initialize components
  controllerGrid = new ControllerGrid("controller-grid");
  gameStatus = new GameStatus();
  controls = new Controls({
    onStartGame: handleStartGame,
    onStopGame: handleStopGame,
    onModeChange: handleModeChange,
  });
  // The playable modes come from the menu (#1278); FFA stays preselected.
  loadModeChoices(() => menuClient.listGameModes())
    .then((choices) => controls.setModeChoices(choices))
    .catch(console.error);
  experimentsView = new ExperimentsView("experiments-grid", "experiments-freshness");
  agentView = new AgentView("agent-feed", "agent-flags", "agent-overlay", "agent-freshness", {
    strip: "decide-strip",
    feed: "decide-feed",
    freshness: "decide-freshness",
  });

  // Set up tab navigation
  setupTabs();

  // Start streaming controller data
  startControllerStream();

  // Start streaming game events
  startGameEventStream();

  // Start polling the agent experiment telemetry (Loki)
  startExperimentsPolling();

  // Start polling the agent decision/flag/intervention telemetry (Loki + VM)
  startAgentPolling();

  // Start polling the agent.decide spans (Jaeger), independently of Loki
  startDecidePolling();

  // Start following the live game (real, else the newest live shadow game)
  startFollowPolling();

  console.log("Dashboard initialized");
}

// The tab currently shown (setupTabs keeps it current). Pollers that only feed
// one tab check it so a hidden tab costs no queries.
let activeTab =
  (document.querySelector(".tab-btn.active") as HTMLElement | null)?.dataset.tab ?? "";
let decidePoller: Poller | null = null;

// Tab navigation
function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const tabPanels = document.querySelectorAll(".tab-panel");

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const tabId = (btn as HTMLElement).dataset.tab;
      if (!tabId) return;

      // Update button states
      tabButtons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");

      // Update panel visibility
      tabPanels.forEach((panel) => {
        panel.classList.remove("active");
        if (panel.id === `tab-${tabId}`) {
          panel.classList.add("active");
        }
      });

      activeTab = tabId;
      if (tabId === "agent") decidePoller?.poke();

      console.log(`Switched to tab: ${tabId}`);
    });
  });
}

// Stream controller data at 30Hz
async function startControllerStream() {
  if (state.isStreaming) return;
  state.isStreaming = true;

  controllerGrid.setLoading(false);

  try {
    const stream = controllerClient.watchGameplayData({
      updateFrequencyHz: 30,
    });

    for await (const update of stream) {
      // Update controller state
      state.controllers.clear();
      for (const controller of update.controllers ?? []) {
        state.controllers.set(controller.serial, controller);
      }

      // Render controllers; the count covers the tiles shown (UC-070 BR-005)
      updatePlayerCount(renderGrid());
    }
    // A clean end (controller manager restarted) also needs a new stream.
    throw new Error("controller stream ended");
  } catch (error) {
    console.error("Controller stream error:", error);
    state.isStreaming = false;
    controllerGrid.setError("Connection lost. Retrying...");

    // Retry after delay
    setTimeout(startControllerStream, 2000);
  }
}

// Stream game events
function startGameEventStream() {
  // Reopened after a failure and after a clean end (UC-035 A4, #1706).
  return keepStreamOpen({
    open: () => gameClient.streamGameEvents({}),
    onEvent: handleGameEvent,
    onLost: (error) => console.warn("Game event stream lost, reopening:", error ?? "ended"),
  });
}

// Poll the agent's experiment telemetry from Loki and refresh the view.
// Self-rescheduling timeout (not setInterval) so a slow query can never stack
// overlapping requests. Polling pauses while the tab is hidden (no point hitting
// Loki for a view nobody is looking at) and resumes on visibility change.
let experimentsPollTimer: ReturnType<typeof setTimeout> | null = null;

function startExperimentsPolling() {
  const tick = async () => {
    experimentsPollTimer = null;
    // Don't burn a Loki query on a hidden tab; the visibility handler resumes.
    if (document.hidden) return;
    try {
      const experiments = await fetchExperiments();
      experimentsView.render(experiments);
    } catch (error) {
      console.error("Experiments poll error:", error);
      // Keep whatever is on screen if we've rendered before (and flag it stale);
      // otherwise show why.
      if (experimentsView.rendered) {
        experimentsView.markStale();
      } else {
        experimentsView.setError("Could not reach Loki for experiment telemetry.");
      }
    } finally {
      // Only reschedule while visible; visibilitychange restarts an idle loop.
      if (!document.hidden) {
        experimentsPollTimer = setTimeout(tick, EXPERIMENTS_POLL_MS);
      }
    }
  };

  // Keep the "updated Ns ago / stale" hint counting up between polls (even while
  // polls are failing or the loop is paused). The dashboard lives for the page
  // session, so this interval is never cleared.
  setInterval(() => experimentsView.updateFreshness(), 1000);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      // Pause: cancel any pending poll. An in-flight fetch is allowed to settle.
      if (experimentsPollTimer !== null) {
        clearTimeout(experimentsPollTimer);
        experimentsPollTimer = null;
      }
    } else if (experimentsPollTimer === null) {
      // Resume: poll immediately so the operator sees fresh data on return.
      tick();
    }
  });

  tick();
}

// Poll the agent's decision/flag/intervention telemetry and refresh the view.
// Same self-rescheduling + tab-hidden-pause + staleness model as the experiments
// poller (#1047): a slow query can't stack, a hidden tab burns no queries, and a
// failed poll keeps the last good render on screen flagged stale.
let agentPollTimer: ReturnType<typeof setTimeout> | null = null;

function startAgentPolling() {
  const tick = async () => {
    agentPollTimer = null;
    if (document.hidden) return;
    try {
      const view = await fetchAgentView();
      agentView.render(view);
    } catch (error) {
      console.error("Agent poll error:", error);
      if (agentView.rendered) {
        agentView.markStale();
      } else {
        agentView.setError("Could not reach Loki for agent telemetry.");
      }
    } finally {
      if (!document.hidden) {
        agentPollTimer = setTimeout(tick, AGENT_POLL_MS);
      }
    }
  };

  setInterval(() => agentView.updateFreshness(), 1000);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (agentPollTimer !== null) {
        clearTimeout(agentPollTimer);
        agentPollTimer = null;
      }
    } else if (agentPollTimer === null) {
      tick();
    }
  });

  tick();
}

// Poll the agent.decide spans from Jaeger (#1233). Only while the Agent tab is
// shown AND the page is visible; one chain at most (see poller.ts), incremental
// query window (DecideStore), per-request timeout and backoff. Independent of the
// Loki poll so either backend being down leaves the other panel live.
function startDecidePolling() {
  const store = decideStore;
  decidePoller = createPoller({
    baseMs: DECIDE_POLL_MS,
    maxMs: DECIDE_MAX_BACKOFF_MS,
    timeoutMs: DECIDE_TIMEOUT_MS,
    isActive: () => activeTab === "agent" && !document.hidden,
    run: async (signal) => {
      const feed = await fetchDecideFeed(store.queryStartMs(), signal);
      store.merge(feed.rows);
      agentView.renderDecide(store.feed());
    },
    onError: (error) => {
      console.error("Decide feed poll error:", error);
      if (!agentView.decideHasRendered) {
        agentView.setDecideError("Could not reach Jaeger for agent.decide spans.");
      }
    },
  });

  setInterval(() => agentView.updateDecideFreshness(), 1000);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) decidePoller?.stop();
    else decidePoller?.poke();
  });

  decidePoller.poke();
}

// The controller tiles: everything but the players of live shadow games, plus
// the followed shadow game's players (UC-070 BR-005, follow.ts). Returns the
// number of tiles drawn (the player count shows only those).
function renderGrid(): number {
  const follow = followTracker.state;
  const tiles = visibleTiles(Array.from(state.controllers.values()), follow);
  const following = follow.shadowGameId !== "";
  controllerGrid.render(
    tiles.map((t) => t.controller),
    following ? shadowShields : shields,
    Date.now(),
    following ? follow.shadowPlayers : null,
  );
  return tiles.length;
}

function renderFollowBanner() {
  const banner = document.getElementById("follow-banner");
  if (!banner) return;
  const text = followBanner(followTracker.state);
  banner.hidden = text === "";
  banner.innerHTML = text
    ? `<span class="decide-game decide-game-shadow">shadow</span> ${escapeText(text)}`
    : "";
}

function escapeText(text: string): string {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// Fold the followed shadow game's own events (shields, deaths) until it is no
// longer followed. The primary stream never carries a shadow game's events.
function followShadowEvents(gameId: string) {
  shadowStream?.abort.abort();
  shadowStream = null;
  if (gameId !== shadowShieldsFor) {
    // Another game: its shields are not ours. A reopen of the same game keeps them.
    shadowShields.apply({ eventType: "game_ended", data: {} });
    shadowShieldsFor = gameId;
  }
  if (!gameId) return;
  const abort = new AbortController();
  const current = { gameId, abort };
  shadowStream = current;
  (async () => {
    try {
      for await (const event of gameClient.streamGameEvents({ gameId }, abort.signal)) {
        shadowShields.apply(event);
      }
    } catch (error) {
      if (!abort.signal.aborted) console.error("Shadow game event stream error:", error);
    } finally {
      // Ended on its own: the next follow poll reopens it if still followed.
      if (shadowStream === current) shadowStream = null;
    }
  })();
}

function applyFollow() {
  const follow = followTracker.state;
  if (follow.shadowGameId !== (shadowStream?.gameId ?? "")) followShadowEvents(follow.shadowGameId);
  decideStore.setFollow({ realGameLive: follow.realGameLive, followGameId: follow.shadowGameId });
  renderFollowBanner();
  if (state.isStreaming) updatePlayerCount(renderGrid());
}

function updatePlayerCount(visible: number) {
  state.totalPlayers = visible;
  gameStatus.updatePlayerCount(state.totalPlayers, state.alivePlayers);
}

// Same single-chain poller with timeout and backoff as the decide feed: a
// coordinator outage backs off to FOLLOW_MAX_BACKOFF_MS instead of erroring
// every 2 s, and a transient error keeps the followed game (FollowTracker).
function startFollowPolling() {
  let failing = false;
  const poller = createPoller({
    baseMs: FOLLOW_POLL_MS,
    maxMs: FOLLOW_MAX_BACKOFF_MS,
    timeoutMs: FOLLOW_TIMEOUT_MS,
    isActive: () => !document.hidden,
    run: async (signal) => {
      const [primary, list] = await Promise.all([
        gameClient.getGameState({}, signal),
        gameClient.listGames(signal),
      ]);
      followTracker.update(primary.gameInfo, list.games ?? []);
      failing = false;
      applyFollow();
    },
    onError: (error) => {
      // Log the first error of an outage only; the backoff spaces the retries.
      if (!failing) console.warn("Follow poll error (retrying with backoff):", error);
      failing = true;
      followTracker.fail();
      applyFollow();
    },
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) poller.stop();
    else poller.poke();
  });
  poller.poke();
}

// Handle game events
function handleGameEvent(event: { eventType: string; data: { [key: string]: string }; timestamp: bigint }) {
  const eventType = event.eventType;
  const data = event.data;

  // Shield badges (UC-040 A16 / A17): the next controller frame re-renders them.
  shields.apply(event);

  // Update state based on event type
  switch (eventType) {
    case "game_start":
      state.gameState = "Running";
      state.alivePlayers = state.totalPlayers;
      controls.setGameRunning(true);
      addEvent("Game started!");
      break;

    case "game_end": {
      state.gameState = "Ended";
      controls.setGameRunning(false);
      const winner = data["winner"] || "Unknown";
      addEvent(`Game ended! Winner: ${winner}`);
      break;
    }

    case "player_death": {
      state.alivePlayers = Math.max(0, state.alivePlayers - 1);
      const player = data["serial"]?.slice(-4) || "???";
      addEvent(`Player ${player} eliminated`);
      break;
    }

    case "player_shielded": {
      const player = data["serial"]?.slice(-4) || "???";
      const kind = data["kind"] === "partial" ? "partial shield" : "shield";
      addEvent(`Agent: ${kind} for ${player} (${data["seconds"] ?? "?"}s)`);
      break;
    }

    case "countdown":
      state.gameState = "Starting";
      addEvent("Countdown...");
      break;

    default:
      console.log("Unknown event:", eventType, data);
  }

  gameStatus.updateState(state.gameState);
  gameStatus.updatePlayerCount(state.totalPlayers, state.alivePlayers);
}

// Unpair results from a controller card (UC-005); the card itself may vanish
document.addEventListener(UNPAIR_NOTICE_EVENT, (event) => {
  addEvent(String((event as CustomEvent<string>).detail));
});

// Add event to the log
function addEvent(text: string) {
  state.events.unshift(text);
  if (state.events.length > 5) {
    state.events.pop();
  }
  gameStatus.updateEvents(state.events);
}

// Game controls
async function handleStartGame() {
  const mode = controls.getSelectedMode();
  console.log("Starting game:", mode);

  try {
    // Use menu service to process start command
    const response = await menuClient.processInput({
      inputType: "web_command",
      data: { command: "start_game", mode },
    });
    addEvent(startGameEvent(mode, response));
  } catch (error) {
    console.error("Failed to start game:", error);
    addEvent("Failed to start game");
  }
}

async function handleStopGame() {
  console.log("Stopping game");

  try {
    await gameClient.forceEndGame({
      reason: "Dashboard stop button",
    });
    addEvent("Game stopped");
  } catch (error) {
    console.error("Failed to stop game:", error);
    addEvent("Failed to stop game");
  }
}

function handleModeChange(mode: string) {
  console.log("Mode changed to:", mode);
}

// Start the app
await init();
