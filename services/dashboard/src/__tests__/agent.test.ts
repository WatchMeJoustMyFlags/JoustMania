/**
 * Agent tab data from spans (#2059; UC-035 step 3, UC-040 A21, UC-070 BR-005).
 *
 * Fixtures are in the folds' input shape (the legacy /api/traces JSON); fetch stubs
 * serve them as Jaeger v3 OTLP answers through ./otlp.ts toOtlp (#2283):
 *   jaeger-flag-state-games   two game traces (a real game, then a newer shadow
 *                             game): game_cycle -> agent.flag_state, and
 *                             agent.signal_received -> agent.decide + agent.decision
 *                             (one blocked, one allowed)
 *   jaeger-rollout-*          one-span agent.infrastructure.decision traces
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BLOCKED_UNAVAILABLE,
  DecideStore,
  FLAG_STATE_FALLBACK_LIMIT,
  FlagStateStore,
  ROLLOUT_ACTIONS,
  ROLLOUT_OVERLAP_MS,
  RolloutStore,
  activityFeed,
  blockedRowsFrom,
  pollDecides,
  fetchFlagStates,
  fetchInterventionCounts,
  fetchRollouts,
  needsFlagStateFallback,
  parseAppliedCounts,
  APPLIED_UNAVAILABLE,
  parseBlockedCounts,
  parseDecideTraces,
  parseFlagStates,
  rolloutRowFrom,
} from "../agent.js";
import type { JaegerTracesResponse, VmQueryResponse } from "../agent.js";
import { WRITE_GATE_OFF_HINT, renderFlagPanel } from "../components/AgentView.js";
import { FLAG_STATE_NO_DATA } from "../degrade.js";
import { COLD_START_TIME_MIN, SEARCH_TIME_MAX } from "../jaeger.js";
import { toOtlp } from "./otlp.js";
import gamesFixture from "./fixtures/jaeger-flag-state-games.json";
import expandFixture from "./fixtures/jaeger-rollout-expand.json";
import rollbackFixture from "./fixtures/jaeger-rollout-rollback.json";

const games = gamesFixture as unknown as JaegerTracesResponse;
const expand = expandFixture as unknown as JaegerTracesResponse;
const rollback = rollbackFixture as unknown as JaegerTracesResponse;

const T0_MS = 1_760_000_000_000;

function vm(result: { metric: Record<string, string>; value: [number, string] }[]): VmQueryResponse {
  return { status: "success", data: { resultType: "vector", result } };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Stub fetch; returns every requested URL. */
function stubFetch(answer: (url: string) => Response | Promise<Response>): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      return answer(url);
    }),
  );
  return urls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("UC-035: main step 3, flag panel from agent.flag_state", () => {
  it("UC-035/main step 3 folds the flag set of an agent.flag_state span, booleans included", () => {
    const states = parseFlagStates(games);
    const real = states.find((s) => s.gameId === "game_real000001");
    expect(real).toMatchObject({
      gameKind: "real",
      gameMode: "JoustFFA",
      trigger: "game_start",
      decisionBackend: "claude",
      actThreshold: 0.7,
      batteryThreshold: "20",
      maxPerMinute: "6",
      enabled: true,
      objectives: "engagement=1.00",
      interventionsAllowed: "grant_shield,end_game",
    });
    expect(states.find((s) => s.gameId === "game_shadow00001")?.enabled).toBe(false);
  });

  it("UC-035/main step 3 the decide poll's game traces carry the flag states (no extra request)", () => {
    const feed = parseDecideTraces(games);
    expect(feed.flagStates?.map((s) => s.gameId).sort()).toEqual(["game_real000001", "game_shadow00001"]);
  });

  it("UC-035/main step 3 the panel shows the kill switch, objectives and allow-list, and no variance window", () => {
    const html = renderFlagPanel(parseFlagStates(games)[0]);
    expect(html).toContain("agent enabled (kill switch)");
    expect(html).toContain("interventions allowed");
    expect(html).toContain("objectives");
    expect(html).not.toContain("variance window");
  });

  it("UC-035/main step 3 enabled=on with interventions_enabled=off shows the write-gate hint (#2093)", () => {
    const base = parseFlagStates(games)[0];
    const off = renderFlagPanel({ ...base, enabled: true, interventionsEnabled: false });
    expect(off).toContain("interventions enabled (write gate)");
    expect(off).toContain(WRITE_GATE_OFF_HINT);
    for (const quiet of [
      { enabled: true, interventionsEnabled: true },
      { enabled: true, interventionsEnabled: null },
      { enabled: false, interventionsEnabled: false },
    ]) {
      expect(renderFlagPanel({ ...base, ...quiet })).not.toContain(WRITE_GATE_OFF_HINT);
    }
  });

  it("UC-035/main step 3 interventions.enabled is read from the agent.flag_state span (#2093)", () => {
    const body = structuredClone(games);
    for (const trace of body.data ?? []) {
      for (const span of trace.spans) {
        if (span.operationName === "agent.flag_state") {
          span.tags?.push({ key: "interventions.enabled", type: "bool", value: false });
        }
      }
    }
    expect(parseFlagStates(body).every((s) => s.interventionsEnabled === false)).toBe(true);
    expect(parseFlagStates(games).every((s) => s.interventionsEnabled === null)).toBe(true);
  });

  it("UC-035/A3 no agent.flag_state span (an agent image before #2059) is a hint, not an error", () => {
    const html = renderFlagPanel(null);
    expect(html).toContain("No agent.flag_state spans in Jaeger yet");
    expect(html).toContain(FLAG_STATE_NO_DATA.slice(0, 40));
    expect(html).not.toMatch(/Could not reach/);
  });
});

describe("UC-070: BR-005 the flag panel follows the stage game", () => {
  it("UC-070/BR-005 a newer shadow game's flag state never replaces the real game's", () => {
    const store = new FlagStateStore();
    store.merge(parseFlagStates(games));
    // The shadow game's flag state is newer, but no follow state / a real game live.
    expect(store.current()?.gameId).toBe("game_real000001");
    expect(store.current({ realGameLive: true, followGameId: "" })?.gameId).toBe("game_real000001");
  });

  it("UC-070/BR-005 with no real game live the followed shadow game's flags show", () => {
    const store = new FlagStateStore();
    store.merge(parseFlagStates(games));
    expect(store.current({ realGameLive: false, followGameId: "game_shadow00001" })?.gameId).toBe(
      "game_shadow00001",
    );
  });

  it("UC-070/BR-005 a newer flag state of the same game replaces the older one", () => {
    const store = new FlagStateStore();
    const [first] = parseFlagStates(games).filter((s) => s.gameId === "game_real000001");
    store.merge([
      { ...first, tsMs: first.tsMs + 5000, startUs: first.startUs + 5_000_000, spanId: "later", trigger: "changed", enabled: false },
    ]);
    store.merge([first]);
    expect(store.current()).toMatchObject({ spanId: "later", trigger: "changed", enabled: false });
  });

  it("UC-070/BR-005 a flag state later in the same ms replaces the older one, whatever the span ids", () => {
    const store = new FlagStateStore();
    const [first] = parseFlagStates(games).filter((s) => s.gameId === "game_real000001");
    const older = { ...first, startUs: first.startUs + 100, spanId: "zzzz", trigger: "game_start" };
    const newer = { ...first, startUs: first.startUs + 400, spanId: "aaaa", trigger: "changed" };
    expect(older.tsMs).toBe(newer.tsMs);
    store.merge([newer]);
    store.merge([older]);
    expect(store.current()).toMatchObject({ spanId: "aaaa", trigger: "changed" });
  });

  it("UC-070/BR-005 a new real game that has not decided yet: fallback search, never the old game's flags", () => {
    const store = new FlagStateStore();
    store.merge(parseFlagStates(games)); // game_real000001 (decided) + a newer shadow game
    // Game 2 is live and quiet: the decide strip still shows game 1.
    const follow = { realGameLive: true, followGameId: "", primaryGameId: "game_real000002" };
    expect(needsFlagStateFallback(store, "game_real000001", follow)).toBe(true);
    expect(store.current(follow)).toBeNull();
    // Even with shadow_policy=allow, a live real game never shows a shadow game's flags.
    const shadowOnly = new FlagStateStore();
    shadowOnly.merge(parseFlagStates(games).filter((s) => s.gameKind === "shadow"));
    expect(shadowOnly.current({ realGameLive: true, followGameId: "" })).toBeNull();
    // Game 2's flag state arrives (the fallback search found it).
    const [first] = parseFlagStates(games).filter((s) => s.gameId === "game_real000001");
    store.merge([{ ...first, gameId: "game_real000002", spanId: "g2" }]);
    expect(needsFlagStateFallback(store, "game_real000001", follow)).toBe(false);
    expect(store.current(follow)?.gameId).toBe("game_real000002");
  });

  it("UC-035/main step 3 the fallback search runs only when the strip's game has no flag state", () => {
    const store = new FlagStateStore();
    expect(needsFlagStateFallback(store, null)).toBe(true);
    store.merge(parseFlagStates(games));
    expect(needsFlagStateFallback(store, "game_real000001")).toBe(false);
    expect(needsFlagStateFallback(store, "game_other")).toBe(true);
    expect(needsFlagStateFallback(store, null)).toBe(true);
  });

  it("UC-035/main step 3 the fallback search asks for agent.flag_state, limit 2, without a browser-clock time", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-01-01T00:00:00Z")); // a browser clock far off the agent's
    const urls = stubFetch(() => json(toOtlp(games)));
    const states = await fetchFlagStates();
    expect(states.length).toBe(2);
    const params = new URL(urls[0], "http://x").searchParams;
    expect(params.get("query.serviceName")).toBe("agent");
    expect(params.get("query.operationName")).toBe("agent.flag_state");
    expect(params.get("query.searchDepth")).toBe(String(FLAG_STATE_FALLBACK_LIMIT));
    expect(params.get("query.startTimeMin")).toBe(COLD_START_TIME_MIN);
    expect(params.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
  });
});

describe("UC-070: BR-005 the flag-state search and store keep the live real game", () => {
  const live = { realGameLive: true, followGameId: "", primaryGameId: "game_real000002" };

  it("UC-035/main step 3 while a real game is live the fallback search filters by its game.id", async () => {
    const urls = stubFetch(() => json(toOtlp(games)));
    await fetchFlagStates(undefined, live);
    await fetchFlagStates(undefined, { realGameLive: false, followGameId: "game_shadow00001", primaryGameId: "game_real000002" });
    const [withGame, without] = urls.map((u) => new URL(u, "http://x").searchParams);
    expect(JSON.parse(withGame.get("query.attributes") ?? "null")).toEqual({ "game.id": "game_real000002" });
    expect(withGame.get("query.operationName")).toBe("agent.flag_state");
    expect(without.has("query.attributes")).toBe(false);
  });

  it("UC-070/BR-005 newer shadow games never evict the live primary game's flag state", () => {
    const store = new FlagStateStore(2);
    const [real] = parseFlagStates(games).filter((s) => s.gameId === "game_real000001");
    store.merge([{ ...real, gameId: "game_real000002", spanId: "primary" }], live);
    const shadows = [1, 2, 3].map((i) => ({
      ...real,
      gameId: `game_shadow0000${i}`,
      gameKind: "shadow",
      spanId: `s${i}`,
      startUs: real.startUs + i * 1_000_000,
      tsMs: real.tsMs + i * 1000,
    }));
    store.merge(shadows, live);
    expect(store.size).toBe(2);
    expect(store.current(live)?.spanId).toBe("primary");
    expect(store.has("game_shadow00003")).toBe(true);
  });
});

describe("UC-035: main step 3, activity feed (blocked decisions + rollout)", () => {
  it("UC-035/main step 3 blocked rows come from the decide poll's agent.decision spans", () => {
    const store = new DecideStore();
    store.merge(parseDecideTraces(games).rows);
    const rows = blockedRowsFrom(store.feed().rows);
    expect(rows.map((r) => [r.sessionId, r.action, r.reason, r.target])).toEqual([
      ["game_shadow00001", "grant_shield", "not_allowed", "Ana"],
      ["game_real000001", "grant_shield", "rate_limit", "Ana"],
    ]);
    // Allowed decisions stay in the decide feed only.
    expect(rows.every((r) => r.blocked && r.kind === "blocked")).toBe(true);
  });

  it("UC-035/main step 3 rollout rows come from agent.infrastructure.decision by remediation.action", () => {
    const row = rolloutRowFrom(rollback.data![0].spans[0]);
    expect(row).toMatchObject({ kind: "rollout", signal: "rollout rollback (dry run)", action: "bluez", reason: "event_gap_ms>40" });
    const none = { ...expand.data![0].spans[0], tags: [{ key: "remediation.action", value: "none" }] };
    expect(rolloutRowFrom(none)).toBeNull();
  });

  it("UC-035/main step 3 one tag search per action; later searches start at the newest span minus the overlap", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-01-01T00:00:00Z"));
    const store = new RolloutStore();
    const urls = stubFetch((url) => {
      const tags = new URL(url, "http://x").searchParams.get("query.attributes") ?? "";
      if (tags.includes('"expand"')) return json(toOtlp(expand));
      if (tags.includes('"rollback"')) return json(toOtlp(rollback));
      return json({ data: [] });
    });

    await fetchRollouts(store);
    const first = urls.map((u) => new URL(u, "http://x").searchParams);
    expect(first.map((p) => JSON.parse(p.get("query.attributes")!)["remediation.action"])).toEqual([...ROLLOUT_ACTIONS]);
    expect(
      first.every(
        (p) =>
          p.get("query.operationName") === "agent.infrastructure.decision" &&
          p.get("query.startTimeMin") === COLD_START_TIME_MIN &&
          p.get("query.startTimeMax") === SEARCH_TIME_MAX,
      ),
    ).toBe(true);

    urls.length = 0;
    await fetchRollouts(store);
    const second = new Map(urls.map((u) => {
      const p = new URL(u, "http://x").searchParams;
      return [JSON.parse(p.get("query.attributes")!)["remediation.action"], p.get("query.startTimeMin")];
    }));
    expect(second.get("expand")).toBe(new Date(T0_MS + 70_000 - ROLLOUT_OVERLAP_MS).toISOString());
    expect(second.get("rollback")).toBe(new Date(T0_MS + 80_000 - ROLLOUT_OVERLAP_MS).toISOString());
    expect(second.get("recommended_only")).toBe(COLD_START_TIME_MIN);
    expect(store.list().map((r) => r.signal)).toEqual(["rollout rollback (dry run)", "rollout expand"]);
  });

  it("UC-035/main step 3 the feed merges both, newest first", () => {
    const decide = new DecideStore();
    decide.merge(parseDecideTraces(games).rows);
    const rollouts = new RolloutStore();
    rollouts.merge("expand", expand);
    const feed = activityFeed(blockedRowsFrom(decide.feed().rows), rollouts.list());
    expect(feed.map((r) => r.kind)).toEqual(["rollout", "blocked", "blocked"]);
  });
});

describe("UC-035: main step 3, no browser clock in a Jaeger request", () => {
  it("UC-035/main step 3 the decide search sends neither the browser's now nor an end", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-01-01T00:00:00Z"));
    const browserDay = new Date().toISOString().slice(0, 10);
    const urls = stubFetch(() => json(toOtlp(games)));
    const store = new DecideStore();
    await pollDecides(store, 0);
    await pollDecides(store, 0);
    expect(new URL(urls[0], "http://x").searchParams.get("query.startTimeMin")).toBe(COLD_START_TIME_MIN);
    for (const u of urls) {
      const params = new URL(u, "http://x").searchParams;
      expect(params.get("query.startTimeMin")).not.toContain(browserDay);
      expect(params.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
    }
  });
});

describe("UC-035: main step 3, parseAppliedCounts", () => {
  it("sums result series across namespaced jobs", () => {
    const out = parseAppliedCounts(
      vm([
        { metric: { result: "ok", job: "a/agent" }, value: [1, "3"] },
        { metric: { result: "ok", job: "b/agent" }, value: [1, "2"] },
        { metric: { result: "error", job: "a/agent" }, value: [1, "1"] },
      ]),
    );
    expect(out).toEqual({ appliedOk: 5, appliedError: 1, shadowAppliedOk: 0, shadowAppliedError: 0 });
  });

  it("UC-035/A3 game_kind splits real (primary) from shadow; an unlabelled series counts as real", () => {
    const out = parseAppliedCounts(
      vm([
        { metric: { result: "ok", game_kind: "primary" }, value: [1, "4"] },
        { metric: { result: "ok" }, value: [1, "1"] },
        { metric: { result: "ok", game_kind: "shadow" }, value: [1, "9"] },
        { metric: { result: "error", game_kind: "shadow" }, value: [1, "2"] },
      ]),
    );
    expect(out).toEqual({ appliedOk: 5, appliedError: 0, shadowAppliedOk: 9, shadowAppliedError: 2 });
  });

  it("UC-035/A3 the applied query groups by game_kind", async () => {
    const urls = stubFetch(() => json(games));
    await fetchInterventionCounts();
    const q = new URL(urls.find((u) => u.includes("agent_intervention_writes_total"))!, "http://x").searchParams.get("query") ?? "";
    expect(q).toContain("game_kind");
  });

  it("UC-035/A3 an empty answer is zero applied, not n/a; a malformed body is n/a", () => {
    expect(parseAppliedCounts(vm([]))).toEqual({ appliedOk: 0, appliedError: 0, shadowAppliedOk: 0, shadowAppliedError: 0 });
    expect(parseAppliedCounts({ status: "error" })).toEqual(APPLIED_UNAVAILABLE);
  });
});

describe("UC-035: A3/#2288 interventions panel windows and fetch results", () => {
  it("UC-035/A3 VM up but empty for both queries: zero applied, zero blocked", async () => {
    stubFetch(() => json(vm([])));
    const overlay = await fetchInterventionCounts();
    expect(overlay.appliedOk).toBe(0);
    expect(overlay.totalBlocked).toBe(0);
  });

  it("UC-035/A3 an HTTP error from VM is unreachable (n/a)", async () => {
    stubFetch(() => json({}, 502));
    const overlay = await fetchInterventionCounts();
    expect(overlay.appliedOk).toBeNull();
    expect(overlay.totalBlocked).toBeNull();
  });

  it("UC-035/A3 applied and blocked use the same 600 s window; blocked leaves out already_applied", async () => {
    const urls = stubFetch(() => json(vm([])));
    await fetchInterventionCounts();
    const queries = urls.map((u) => new URL(u, "http://x").searchParams.get("query") ?? "");
    const applied = queries.find((q) => q.includes("agent_intervention_writes_total")) ?? "";
    const blocked = queries.find((q) => q.includes("agent_decisions_total")) ?? "";
    expect(applied).toContain("increase(");
    expect(applied).toContain("[600s]");
    expect(blocked).toContain("[600s]");
    expect(blocked).toContain('block_reason!="already_applied"');
  });
});

describe("UC-040: A21 blocked tallies from agent_decisions_total", () => {
  it("UC-040/A21 counts repeated rate_limit refusals that emit no agent.decision span", () => {
    const out = parseBlockedCounts(
      vm([
        { metric: { action: "grant_shield", block_reason: "rate_limit" }, value: [1, "6.6"] },
        { metric: { action: "end_game", block_reason: "not_allowed" }, value: [1, "1"] },
        { metric: { action: "x", block_reason: "y" }, value: [1, "NaN"] },
      ]),
    );
    expect(out.totalBlocked).toBe(8);
    expect(out.blockedByAction[0]).toEqual({ action: "grant_shield", blocked: 7 });
  });

  it("UC-040/A21 no samples in the window is zero blocked, not n/a", () => {
    expect(parseBlockedCounts(vm([]))).toEqual({
      blockedByAction: [],
      blockedByReason: [],
      totalBlocked: 0,
      shadowBlocked: 0,
    });
  });

  it("UC-035/A3 shadow-game refusals are tallied apart and stay out of the real breakdowns", () => {
    const out = parseBlockedCounts(
      vm([
        { metric: { action: "grant_shield", block_reason: "rate_limit", game_kind: "primary" }, value: [1, "2"] },
        { metric: { action: "grant_shield", block_reason: "rate_limit", game_kind: "shadow" }, value: [1, "30"] },
      ]),
    );
    expect(out.totalBlocked).toBe(2);
    expect(out.shadowBlocked).toBe(30);
    expect(out.blockedByAction).toEqual([{ action: "grant_shield", blocked: 2 }]);
    expect(out.blockedByReason).toEqual([{ reason: "rate_limit", count: 2 }]);
  });

  it("UC-040/A21 asks VictoriaMetrics over 600 s; unreachable shows Blocked as n/a, never a span fold", async () => {
    const urls = stubFetch((url) => {
      if (url.includes("/victoria-metrics/")) throw new TypeError("Failed to fetch");
      return json(games);
    });
    const overlay = await fetchInterventionCounts();
    const query = new URL(urls.find((u) => u.includes("agent_decisions_total"))!, "http://x").searchParams.get("query") ?? "";
    expect(query).toContain('agent_decisions_total{blocked="true",block_reason!="already_applied"}');
    expect(query).toContain("[600s]");
    expect(overlay).toEqual({ ...BLOCKED_UNAVAILABLE, ...APPLIED_UNAVAILABLE });
    expect(overlay.totalBlocked).toBeNull();
    expect(urls.some((u) => u.includes("/jaeger/"))).toBe(false);
  });
});
