/**
 * JoustMania Dashboard - Main Entry Point
 *
 * Real-time controller visualization and game controls using Connect-Web.
 */
import { keepGameEventStreamOpen } from "./stream.js";
import { keepControllerStreamOpen } from "./controllerStream.js";
import { controllerClient, gameClient, menuClient } from "./client.js";
import { ControllerGrid } from "./components/ControllerGrid.js";
import { UNPAIR_NOTICE_EVENT } from "./unpair.js";
import { GameStatus } from "./components/GameStatus.js";
import { Controls } from "./components/Controls.js";
import { ExperimentsView } from "./components/ExperimentsView.js";
import { ExperimentStore, pollExperiments } from "./experiments.js";
import { AgentView } from "./components/AgentView.js";
import {
  DecideStore,
  FlagStateStore,
  RolloutStore,
  activityFeed,
  blockedRowsFrom,
  fetchFlagStates,
  fetchInterventionCounts,
  fetchRollouts,
  needsFlagStateFallback,
  pollDecides,
  FlagFallbackPacer,
  flagFallbackKey,
} from "./agent.js";
import type { FlagPanel, InterventionOverlay } from "./agent.js";
import { fetchAdapters } from "./adapters.js";
import { createPoller } from "./poller.js";
import { PageGate } from "./pageGate.js";
import { ShieldStore } from "./shield.js";
import { reduceGameEvent } from "./gameEvents.js";
import {
  onAgentPollFailed,
  onDecidePollFailed,
  onExperimentsPollFailed,
  paintAgentPanels,
  settleAgentPoll,
} from "./degrade.js";
import { FollowTracker, followBanner, visibleTiles } from "./follow.js";
import { loadModeChoices, startGameEvent } from "./modes.js";
import type { Poller } from "./poller.js";
import type { GameplayData } from "./gen/controller_manager_pb.js";

// Every telemetry poller below is a createPoller chain (poller.ts): at most one
// request in flight, a per-request timeout, doubling backoff on errors and slow
// answers, and it runs only while its tab is shown and the page is visible. The
// Jaeger searches never carry a browser-clock time (jaeger.ts, CLOCK SKEW).

// Experiments tab (#2059: experiment.progress in Jaeger). Experiment state moves
// on the order of games.
const EXPERIMENTS_POLL_MS = 5000;
const EXPERIMENTS_MAX_BACKOFF_MS = 15_000;
const EXPERIMENTS_TIMEOUT_MS = 4000;

// Agent tab, everything but the decide feed: VictoriaMetrics counts and, from
// Jaeger, the flag-state fallback search (only when the decide poll brought no
// flag state for its game) and the rollout searches (every ROLLOUT_EVERY_TICKS).
const AGENT_POLL_MS = 5000;
const AGENT_MAX_BACKOFF_MS = 15_000;
const AGENT_TIMEOUT_MS = 4000;
const ROLLOUT_EVERY_TICKS = 2;

// Poll cadence for the agent.decide feed (#1233, Jaeger query). Faster than the
// other agent panels: a live decision.backend switch must show on the very next decision,
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
  winner: string | null;
  totalPlayers: number;
  events: string[];
  isStreaming: boolean;
}

const state: AppState = {
  controllers: new Map(),
  gameState: "Idle",
  alivePlayers: 0,
  winner: null,
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
// Agent-tab span state across polls (#2059): flag states per game, rollout rows.
const flagStore = new FlagStateStore();
const rolloutStore = new RolloutStore();
// The VictoriaMetrics half of the overlay; null until the agent poller ran once.
let interventionCounts: InterventionOverlay | null = null;
/** False while the last Agent poll's Jaeger leg failed (UC-035 A3 step 2). */
let agentJaegerOk = true;
// Newest experiment.progress snapshot per experiment.
const experimentStore = new ExperimentStore();

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
  controllerGrid = new ControllerGrid("controller-grid", "adapter-counts");
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

  // Start polling the controllers' HCI adapters (VictoriaMetrics, UC-035 BR-009)
  startAdapterPolling();

  // Start polling the experiment.progress spans (Jaeger)
  startExperimentsPolling();

  // Start polling the agent flag / activity / intervention telemetry (Jaeger + VM)
  startAgentPolling();

  // Start polling the agent.decide spans (Jaeger)
  startDecidePolling();

  // Start following the live game (real, else the newest live shadow game)
  startFollowPolling();

  console.log("Dashboard initialized");
}

let decidePoller: Poller | null = null;
let agentPoller: Poller | null = null;
let experimentsPoller: Poller | null = null;
let adapterPoller: Poller | null = null;
// Pollers that only feed one tab run while it is shown and the page is visible
// (pageGate.ts): a hidden tab, a hidden page or a page in the back/forward cache
// costs no queries; coming back resumes them.
const pageGate = new PageGate(
  document,
  globalThis,
  (document.querySelector(".tab-btn.active") as HTMLElement | null)?.dataset.tab ?? "",
  () => [decidePoller, agentPoller, experimentsPoller, adapterPoller],
);

function tabActive(tab: string): boolean {
  return pageGate.isActive(tab);
}

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

      pageGate.activeTab = tabId;
      if (tabId === "agent") {
        decidePoller?.poke();
        agentPoller?.poke();
      }
      if (tabId === "experiments") experimentsPoller?.poke();
      if (tabId === "controllers") adapterPoller?.poke();

      console.log(`Switched to tab: ${tabId}`);
    });
  });
}

// Stream controller data at 30Hz; reopened 2 s after a failure or a clean end
// (controller manager restarted), UC-035 A4.
function startControllerStream() {
  if (state.isStreaming) return;
  void keepControllerStreamOpen({
    open: () => controllerClient.watchGameplayData({ updateFrequencyHz: 30 }),
    onUpdate: (update) => {
      // Update controller state
      state.controllers.clear();
      for (const controller of update.controllers ?? []) {
        state.controllers.set(controller.serial, controller);
      }

      // Render controllers; the count covers the tiles shown (UC-070 BR-005)
      updatePlayerCount(renderGrid());
    },
    grid: controllerGrid,
    setStreaming: (streaming) => {
      state.isStreaming = streaming;
    },
  });
}

// Stream game events
function startGameEventStream() {
  // Reopened after a failure and after a clean end (UC-035 A4, #1706).
  return keepGameEventStreamOpen(() => gameClient.streamGameEvents({}), handleGameEvent, undefined, () => shields.clear());
}

// Poll the experiment.progress spans (#2059): cold start without `start`, then
// incremental from the newest snapshot held (ExperimentStore).
function startExperimentsPolling() {
  experimentsPoller = createPoller({
    baseMs: EXPERIMENTS_POLL_MS,
    maxMs: EXPERIMENTS_MAX_BACKOFF_MS,
    timeoutMs: EXPERIMENTS_TIMEOUT_MS,
    isActive: () => tabActive("experiments"),
    run: async (signal) => {
      experimentsView.render(await pollExperiments(experimentStore, signal));
    },
    onError: (error) => {
      console.error("Experiments poll error:", error);
      // Keep what is on screen (flagged stale), else say Jaeger is unreachable.
      onExperimentsPollFailed(experimentsView);
    },
  });

  // Keep the "updated Ns ago / stale" hint counting up between polls.
  setInterval(() => experimentsView.updateFreshness(), 1000);
  experimentsPoller.poke();
}

/** The followed game's flag state (UC-070 BR-005, the strip's anchor rule). */
function currentFlags(): FlagPanel | null {
  return flagStore.current(decideStore.followState);
}

// Render the activity feed, flag panel and overlay from the stores.
// While the Agent poll's Jaeger leg fails only the overlay renders (UC-035 A3).
function renderAgentPanels() {
  paintAgentPanels(agentView, interventionCounts, agentJaegerOk, () => ({
    feed: activityFeed(blockedRowsFrom(decideStore.feed().rows), rolloutStore.list()),
    flags: currentFlags(),
  }));
}

// Poll the Agent tab's other panels (#2059): VictoriaMetrics counts (never fail),
// the agent.flag_state fallback search and the rollout searches (Jaeger; a
// failure keeps the last render, flagged stale).
function startAgentPolling() {
  let tick = 0;
  const flagFallbackPacer = new FlagFallbackPacer();
  agentPoller = createPoller({
    baseMs: AGENT_POLL_MS,
    maxMs: AGENT_MAX_BACKOFF_MS,
    timeoutMs: AGENT_TIMEOUT_MS,
    isActive: () => tabActive("agent"),
    run: async (signal) => {
      const rolloutDue = tick % ROLLOUT_EVERY_TICKS === 0;
      const decideGameId = decideStore.feed().current?.gameId ?? null;
      // Paced (#2287): the same game's fallback search repeats at most every 30 s.
      const flagsNeeded =
        needsFlagStateFallback(flagStore, decideGameId, decideStore.followState) &&
        flagFallbackPacer.take(flagFallbackKey(decideGameId, decideStore.followState));
      // Settled, not all: the VictoriaMetrics counts render even when a Jaeger
      // leg fails (UC-035 A3 step 2); the Jaeger failure still marks the poll failed.
      const settled = await Promise.allSettled([
        fetchInterventionCounts(signal),
        flagsNeeded ? fetchFlagStates(signal, decideStore.followState) : Promise.resolve([] as FlagPanel[]),
        rolloutDue ? fetchRollouts(rolloutStore, signal) : Promise.resolve(),
      ]);
      tick += 1;
      // A failed fallback search is retried on the next poll, not after the pace.
      if (flagsNeeded && settled[1].status === "rejected") flagFallbackPacer.forget();
      const result = settleAgentPoll(settled, interventionCounts, (states) =>
        flagStore.merge(states, decideStore.followState),
        { flags: flagsNeeded, rollouts: rolloutDue },
      );
      interventionCounts = result.overlay;
      agentJaegerOk = result.jaegerOk ?? agentJaegerOk;
      renderAgentPanels();
      if (result.failed) throw result.failed.reason;
    },
    onError: (error) => {
      console.error("Agent poll error:", error);
      onAgentPollFailed(agentView);
    },
  });

  setInterval(() => agentView.updateFreshness(), 1000);
  agentPoller.poke();
}

// The HCI adapter of each controller card (UC-035 BR-009): controller_adapter_info
// from VictoriaMetrics with nocache=1, only while the Controllers tab is shown and
// the page is visible. VictoriaMetrics down keeps the last labels, marked stale (A3).
const ADAPTER_POLL_MS = 10_000;
const ADAPTER_MAX_BACKOFF_MS = 30_000;
const ADAPTER_TIMEOUT_MS = 5000;

function startAdapterPolling() {
  adapterPoller = createPoller({
    baseMs: ADAPTER_POLL_MS,
    maxMs: ADAPTER_MAX_BACKOFF_MS,
    timeoutMs: ADAPTER_TIMEOUT_MS,
    isActive: () => tabActive("controllers"),
    run: async (signal) => {
      controllerGrid.setAdapters(await fetchAdapters(signal));
    },
  });
  adapterPoller.poke();
}

// Poll the agent.decide spans from Jaeger (#1233). Only while the Agent tab is
// shown AND the page is visible; one chain at most (see poller.ts), incremental
// query window per search lane (DecideStore), per-request timeout and backoff,
// paced by the answer size and targeted at the live real game (#2287). Its game traces
// also carry the blocked agent.decision and agent.flag_state spans (#2059).
function startDecidePolling() {
  const store = decideStore;
  let tick = 0;
  decidePoller = createPoller({
    baseMs: DECIDE_POLL_MS,
    maxMs: DECIDE_MAX_BACKOFF_MS,
    timeoutMs: DECIDE_TIMEOUT_MS,
    isActive: () => tabActive("agent"),
    run: async (signal) => {
      const polled = await pollDecides(store, tick, signal);
      tick += 1;
      flagStore.merge(polled.flagStates, store.followState);
      agentView.renderDecide(store.feed());
      renderAgentPanels();
      // Payload pace (#2287, UC-035 BR-011): a large game trace slows the poll.
      return polled.paceMs;
    },
    onError: (error) => {
      console.error("Decide feed poll error:", error);
      onDecidePollFailed(agentView);
    },
  });

  setInterval(() => agentView.updateDecideFreshness(), 1000);
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
  decideStore.setFollow({
    realGameLive: follow.realGameLive,
    followGameId: follow.shadowGameId,
    primaryGameId: follow.primaryGameId,
  });
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
  // Shield badges (UC-040 A16 / A17): the next controller frame re-renders them.
  shields.apply(event);

  const effect = reduceGameEvent(state, event);
  state.gameState = effect.state.gameState;
  state.alivePlayers = effect.state.alivePlayers;
  state.winner = effect.state.winner;
  if (effect.gameRunning !== null) controls.setGameRunning(effect.gameRunning);
  if (effect.feedLine !== null) addEvent(effect.feedLine);

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
