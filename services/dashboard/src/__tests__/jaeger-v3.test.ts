/**
 * Jaeger query API v3 (#2283): the dashboard's search URL, the OTLP JSON parse
 * and the "no traces" answer, against answers recorded from the stage Pi
 * (Jaeger 2.19, `fixtures/jaeger-v3-*.json`, spans trimmed to one decide cycle).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  COLD_START_TIME_MIN,
  JaegerUnavailableError,
  SEARCH_TIME_MAX,
  fromOtlp,
  parentSpanId,
  searchParams,
  searchTraces,
  spanEvents,
  spanTags,
  spansNamed,
} from "../jaeger.js";
import type { JaegerTracesResponse, OtlpTracesResponse } from "../jaeger.js";
import { parseDecideTraces, parseFlagStates } from "../agent.js";
import { parseExperimentSnapshots } from "../experiments.js";
import { jsonResponse, toOtlp } from "./otlp.js";
import recorded from "./fixtures/jaeger-v3-decide-trace.json";
import noTraces from "./fixtures/jaeger-v3-no-traces.json";
import rulesFixture from "./fixtures/jaeger-decide-rules.json";
import actFixture from "./fixtures/jaeger-decide-remote-act.json";
import multiFixture from "./fixtures/jaeger-decide-multi.json";
import flagStateFixture from "./fixtures/jaeger-flag-state-games.json";
import progressFixture from "./fixtures/jaeger-experiment-progress.json";

const RECORDED_TRACE = "6941c034ab0b93c2af2e439d02580244";
const RECORDED_DECIDE = "c66f7c2a844e006d";

function stubFetch(response: () => Response | Promise<Response>): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return response();
    }),
  );
  return urls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UC-035: main step 3, the v3 search URL never carries a browser-clock time (#2283)", () => {
  it("UC-035/main step 3 cold start: epoch to the fixed upper bound, service, operation, depth, attributes", () => {
    const q = searchParams({ operation: "agent.decide", limit: 5, startMs: null, tags: { "game.id": "g1" } });
    expect(Object.fromEntries(q)).toEqual({
      "query.serviceName": "agent",
      "query.operationName": "agent.decide",
      "query.searchDepth": "5",
      "query.startTimeMin": COLD_START_TIME_MIN,
      "query.startTimeMax": SEARCH_TIME_MAX,
      "query.attributes": '{"game.id":"g1"}',
    });
  });

  it("UC-035/main step 3 warm: the lower bound is the span-derived start (RFC 3339, UTC), the upper one stays fixed", () => {
    const q = searchParams({ operation: "agent.decide", limit: 5, startMs: 1_793_139_266_961.6 });
    expect(q.get("query.startTimeMin")).toBe("2026-10-27T22:14:26.961Z");
    expect(q.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
    expect(q.has("query.attributes")).toBe(false);
  });

  it("UC-035/main step 3 searches /jaeger/api/v3/traces, never the legacy /api/traces", async () => {
    const urls = stubFetch(() => jsonResponse(200, recorded));
    await searchTraces({ operation: "agent.decide", limit: 5, startMs: null });
    expect(urls).toHaveLength(1);
    expect(new URL(urls[0], "http://x").pathname).toBe("/jaeger/api/v3/traces");
  });
});

describe("UC-035: main step 3, the recorded v3 answer folds like the legacy one (#2283)", () => {
  const body = fromOtlp(recorded as OtlpTracesResponse);

  it("UC-035/main step 3 groups the resource spans of both services into one trace", () => {
    expect(body.data?.map((t) => t.traceID)).toEqual([RECORDED_TRACE]);
    expect(body.data?.[0].spans.map((s) => s.operationName).sort()).toEqual([
      "Joust Free-for-All",
      "agent.action",
      "agent.decide",
      "agent.decision",
      "agent.flag_state",
      "agent.signal_received",
    ]);
  });

  it("UC-035/main step 3 maps ids, parent, times (ns strings to µs) and typed attributes", () => {
    const [decide] = spansNamed(body, "agent.decide");
    expect(decide.spanID).toBe(RECORDED_DECIDE);
    expect(decide.traceID).toBe(RECORDED_TRACE);
    expect(decide.startTime).toBe(1_793_139_266_961_628);
    expect(decide.duration).toBe(206);
    const signal = spansNamed(body, "agent.signal_received")[0];
    expect(parentSpanId(decide)).toBe(signal.spanID);
    const tags = spanTags(decide);
    expect(tags.get("decide.backend")).toBe("rules");
    expect(tags.get("decide.blocked")).toBe(true);
    expect(tags.get("decide.latency_ms")).toBe(0);
    expect(tags.get("decide.confidence")).toBeCloseTo(0.8331, 4);
  });

  it("UC-035/main step 3 keeps links as FOLLOWS_FROM (not a parent) and span events as logs", () => {
    const [decision] = spansNamed(body, "agent.decision");
    expect(decision.references?.map((r) => r.refType)).toEqual(["CHILD_OF", "FOLLOWS_FROM"]);
    const [flagState] = spansNamed(body, "agent.flag_state");
    const evals = spanEvents(flagState, "feature_flag.evaluation");
    expect(evals.length).toBeGreaterThan(1);
    expect(evals[0].get("feature_flag.key")).toBe("enabled");
    expect(evals[0].get("feature_flag.result.value")).toBe(false);
  });

  it("UC-035/main step 3 gives the decide row and the flag panel", () => {
    const feed = parseDecideTraces(body);
    expect(feed.rows).toHaveLength(1);
    expect(feed.rows[0]).toMatchObject({
      spanId: RECORDED_DECIDE,
      backend: "rules",
      outcome: "fallback",
      gameId: "game_d1a30cc80da4",
      gameKind: "shadow",
    });
    expect(feed.rows[0].decisions.length).toBe(1);
    const flags = parseFlagStates(body);
    expect(flags).toHaveLength(1);
    expect(flags[0].traceId).toBe(RECORDED_TRACE);
  });

  it("UC-035/main step 3 every legacy-shaped fixture folds the same after a v3 round trip", () => {
    const strip = (r: JaegerTracesResponse) => JSON.parse(JSON.stringify(r)) as JaegerTracesResponse;
    for (const f of [rulesFixture, actFixture, multiFixture, flagStateFixture]) {
      const legacy = strip(f as unknown as JaegerTracesResponse);
      expect(parseDecideTraces(fromOtlp(toOtlp(legacy)))).toEqual(parseDecideTraces(legacy));
      expect(parseFlagStates(fromOtlp(toOtlp(legacy)))).toEqual(parseFlagStates(legacy));
    }
    const progress = progressFixture as unknown as JaegerTracesResponse;
    expect(parseExperimentSnapshots(fromOtlp(toOtlp(progress)))).toEqual(parseExperimentSnapshots(progress));
  });
});

describe("UC-035: A3, no trace is data, a broken Jaeger is not (#2283)", () => {
  it("UC-035/A3 v3's 404 'No traces found' is an empty answer", async () => {
    stubFetch(() => jsonResponse(404, noTraces));
    await expect(searchTraces({ operation: "agent.decide", limit: 5, startMs: null })).resolves.toEqual({ data: [] });
  });

  it("UC-035/A3 a 404 that is not Jaeger's (no route, plain text) is unavailable", async () => {
    stubFetch(() => new Response("404 page not found", { status: 404 }));
    await expect(searchTraces({ operation: "agent.decide", limit: 5, startMs: null })).rejects.toBeInstanceOf(
      JaegerUnavailableError,
    );
  });

  it("UC-035/A3 a v3 error answer is unavailable", async () => {
    stubFetch(() => jsonResponse(500, { error: { httpCode: 500, message: "badger: closed" } }));
    await expect(searchTraces({ operation: "agent.decide", limit: 5, startMs: null })).rejects.toThrow(/500/);
  });
});
