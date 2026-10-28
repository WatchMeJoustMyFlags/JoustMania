/**
 * The decide poll's Jaeger cost (#2287, UC-035 BR-010): Jaeger's search returns
 * whole traces, so the poll paces itself on the answer size, stops re-fetching a
 * settled game's trace, asks for the live real game by game.id (shadow traces
 * cannot crowd it out, #2255) and polls nothing while the page is hidden.
 *
 * The fetch mock is a small Jaeger: it filters agent.decide spans by the
 * `tags` and `start` params and returns the `limit` newest WHOLE traces.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DECIDE_BYTES_PER_SEC,
  DECIDE_MAX_PACE_MS,
  DECIDE_OVERLAP_MS,
  DecideStore,
  FLAG_STATE_FALLBACK_REPEAT_MS,
  FlagFallbackPacer,
  decidePaceMs,
  flagFallbackKey,
  pollDecides,
} from "../agent.js";
import type { JaegerSpan, JaegerTrace, JaegerTracesResponse } from "../agent.js";
import { createPoller } from "../poller.js";
import { PageGate } from "../pageGate.js";
import multiFixture from "./fixtures/jaeger-decide-multi.json";
import shadowFixture from "./fixtures/jaeger-decide-shadow.json";

const REAL_GAME = "game_00000000a1b2";

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

function realTrace(): JaegerTrace {
  return clone((multiFixture as JaegerTracesResponse).data![0]);
}

/** A shadow game trace whose decide is `offsetS` seconds after the fixture's. */
function shadowTrace(i: number, offsetS: number): JaegerTrace {
  const t = clone((shadowFixture as JaegerTracesResponse).data![0]);
  t.traceID = `shadow${i}`;
  for (const s of t.spans) {
    s.traceID = t.traceID;
    s.spanID = `${s.spanID}-${i}`;
    s.references = (s.references ?? []).map((r) => ({ ...r, traceID: t.traceID, spanID: `${r.spanID}-${i}` }));
    s.startTime += offsetS * 1_000_000;
    for (const tag of s.tags ?? []) if (tag.key === "game.id") tag.value = `game_shadow${i}`;
  }
  return t;
}

/** Pad a trace (a long game: many phase and player spans) to about `chars`. */
function pad(t: JaegerTrace, chars: number): JaegerTrace {
  t.spans[0].tags = [...(t.spans[0].tags ?? []), { key: "padding", type: "string", value: "x".repeat(chars) }];
  return t;
}

function tagValue(s: JaegerSpan, key: string): unknown {
  return (s.tags ?? []).find((t) => t.key === key)?.value;
}

interface Request {
  params: URLSearchParams;
  bytes: number;
}

/** A Jaeger search over `traces()` (evaluated per request), recording each request. */
function fakeJaeger(traces: () => JaegerTrace[]) {
  const requests: Request[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const params = new URL(String(input), "http://pi").searchParams;
    const tags = JSON.parse(params.get("tags") ?? "{}") as Record<string, string>;
    const startUs = params.has("start") ? Number(params.get("start")) : -Infinity;
    const limit = Number(params.get("limit"));
    const newestMatch = (t: JaegerTrace) =>
      Math.max(
        ...t.spans
          .filter(
            (s) =>
              s.operationName === params.get("operation") &&
              s.startTime >= startUs &&
              Object.entries(tags).every(([k, v]) => tagValue(s, k) === v),
          )
          .map((s) => s.startTime),
      );
    const data = traces()
      .map((t) => ({ t, newest: newestMatch(t) }))
      .filter((x) => Number.isFinite(x.newest))
      .sort((a, b) => b.newest - a.newest)
      .slice(0, limit)
      .map((x) => x.t);
    const text = JSON.stringify({ data });
    requests.push({ params, bytes: text.length });
    return new Response(text, { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return requests;
}

/** The decide poller as main.ts wires it. */
function decidePoller(store: DecideStore, isActive: () => boolean = () => true) {
  let tick = 0;
  return createPoller({
    baseMs: 1500,
    maxMs: 10_000,
    timeoutMs: 4000,
    isActive,
    run: async (signal) => {
      const polled = await pollDecides(store, tick, signal);
      tick += 1;
      return decidePaceMs(polled.bytes);
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("UC-035: BR-010, the decide poll paces itself on the Jaeger answer size", () => {
  it("UC-035/BR-010 pace: small answers keep the cadence, large ones wait bytes / budget, capped", () => {
    expect(decidePaceMs(0)).toBe(0);
    expect(decidePaceMs(100_000)).toBeLessThan(1500);
    expect(decidePaceMs(4.9 * 1024 * 1024)).toBe(Math.ceil((4.9 * 1024 * 1024 * 1000) / DECIDE_BYTES_PER_SEC));
    expect(decidePaceMs(1e9)).toBe(DECIDE_MAX_PACE_MS);
  });

  it("UC-035/BR-010 a run's minimum wait delays the next tick", async () => {
    vi.useFakeTimers();
    let runs = 0;
    const poller = createPoller({
      baseMs: 1500,
      maxMs: 10_000,
      timeoutMs: 4000,
      isActive: () => true,
      run: async () => {
        runs += 1;
        return 6000;
      },
    });
    poller.poke();
    await vi.advanceTimersByTimeAsync(0);
    expect(runs).toBe(1);
    await vi.advanceTimersByTimeAsync(5900);
    expect(runs).toBe(1);
    await vi.advanceTimersByTimeAsync(200);
    expect(runs).toBe(2);
    poller.stop();
  });

  it("UC-035/BR-010 a live 2 MB game trace over 60 s: Jaeger sends at most the budget, not 40 full traces", async () => {
    vi.useFakeTimers();
    const t0 = Date.now();
    // The real game grows: one more decide every 2 s (agent clock = fixture time + elapsed).
    const requests = fakeJaeger(() => {
      const t = pad(realTrace(), 2 * 1024 * 1024);
      const decide = t.spans.find((s) => s.spanID === "D2")!;
      const n = Math.floor((Date.now() - t0) / 2000);
      for (let k = 1; k <= n; k += 1) {
        t.spans.push({ ...clone(decide), spanID: `L${k}`, startTime: decide.startTime + k * 2_000_000 });
      }
      return [t];
    });
    const store = new DecideStore();
    store.setFollow({ realGameLive: true, followGameId: "", primaryGameId: REAL_GAME });
    const poller = decidePoller(store);
    poller.poke();
    await vi.advanceTimersByTimeAsync(60_000);
    poller.stop();

    const gameRequests = requests.filter((r) => r.params.get("tags")?.includes(REAL_GAME));
    const totalBytes = requests.reduce((sum, r) => sum + r.bytes, 0);
    // Before #2287: one 2 MB request every 1.5 s = 40 requests, ~80 MB per minute.
    expect(gameRequests.length).toBeLessThanOrEqual(17);
    expect(totalBytes).toBeLessThanOrEqual(DECIDE_BYTES_PER_SEC * 60 * 1.1 + 2.2 * 1024 * 1024);
    // The feed still shows the newest decides of the live game.
    const newest = store.feed().rows[0];
    expect(newest.gameId).toBe(REAL_GAME);
    expect(newest.spanId).toMatch(/^L\d+$/);
  });

  it("UC-035/BR-010 settle: an ended game's trace is not re-fetched once its newest decide is 30 s old", async () => {
    vi.useFakeTimers();
    const requests = fakeJaeger(() => [pad(realTrace(), 200_000)]);
    const store = new DecideStore();
    const poller = decidePoller(store);
    poller.poke();
    await vi.advanceTimersByTimeAsync(DECIDE_OVERLAP_MS + 5000);
    poller.stop();

    const d2Ms = 1_760_000_014_000;
    const last = requests[requests.length - 1];
    expect(last.params.get("start")).toBe(String((d2Ms + 1) * 1000));
    expect(last.bytes).toBeLessThan(100);
    // Before settling the window overlapped the newest decide and re-fetched the trace.
    expect(requests[1].params.get("start")).toBe(String((d2Ms - DECIDE_OVERLAP_MS) * 1000));
    expect(requests[1].bytes).toBeGreaterThan(200_000);
    expect(store.feed().rows.map((r) => r.spanId)).toEqual(["D2", "D1", "R1"]);
  });
});

describe("UC-035: BR-010, the live real game's decides are searched by game.id (#2255)", () => {
  const traces = () => [realTrace(), ...[1, 2, 3, 4, 5].map((i) => shadowTrace(i, 10 + i))];

  it("UC-035/BR-010 without a live real game, five newer shadow traces fill limit=5", async () => {
    fakeJaeger(traces);
    const store = new DecideStore();
    store.setFollow({ realGameLive: false, followGameId: "" });
    await pollDecides(store, 0);
    expect(store.feed().rows.every((r) => r.gameKind === "shadow")).toBe(true);
  });

  it("UC-035/BR-010 with a live real game, its decides stay on the strip and the shadow rows still come", async () => {
    const requests = fakeJaeger(traces);
    const store = new DecideStore();
    store.setFollow({ realGameLive: true, followGameId: "", primaryGameId: REAL_GAME });
    await pollDecides(store, 0);

    const ids = store.feed().rows.map((r) => r.spanId);
    expect(ids).toEqual(expect.arrayContaining(["D2", "D1", "R1", "SH1-1", "SH1-5"]));
    expect(store.feed().current?.gameId).toBe(REAL_GAME);
    expect(requests.map((r) => r.params.get("tags"))).toEqual([
      JSON.stringify({ "game.id": REAL_GAME }),
      JSON.stringify({ "game.kind": "shadow" }),
    ]);
    // Between the shadow searches only the real game is asked for.
    await pollDecides(store, 1);
    expect(requests).toHaveLength(3);
  });
});

describe("UC-035: BR-010, nothing is polled while the page is hidden", () => {
  it("UC-035/BR-010 hidden page: no Jaeger request for a minute, one at once when shown again", async () => {
    vi.useFakeTimers();
    const requests = fakeJaeger(() => [realTrace()]);
    const listeners: Record<string, () => void> = {};
    const doc = {
      hidden: false,
      addEventListener: (type: string, l: () => void) => (listeners[type] = l),
    };
    const win = { addEventListener: (type: string, l: () => void) => (listeners[type] = l) };
    let poller: ReturnType<typeof decidePoller> | null = null;
    const gate = new PageGate(doc, win, "agent", () => [poller]);
    poller = decidePoller(new DecideStore(), () => gate.isActive("agent"));
    poller.poke();
    await vi.advanceTimersByTimeAsync(4000);
    const visible = requests.length;
    expect(visible).toBeGreaterThanOrEqual(2);

    doc.hidden = true;
    listeners.visibilitychange();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(requests).toHaveLength(visible);

    doc.hidden = false;
    listeners.visibilitychange();
    await vi.advanceTimersByTimeAsync(0);
    expect(requests).toHaveLength(visible + 1);
    poller.stop();
  });
});

describe("UC-035: BR-010, the flag-state fallback search is paced per game", () => {
  beforeEach(() => vi.useFakeTimers());

  it("UC-035/BR-010 the same game at most every 30 s, another game at once, a failed search again at once", () => {
    const pacer = new FlagFallbackPacer();
    const live = { realGameLive: true, followGameId: "", primaryGameId: REAL_GAME };
    const key = flagFallbackKey(null, live);
    expect(pacer.take(key)).toBe(true);
    vi.advanceTimersByTime(5000);
    expect(pacer.take(key)).toBe(false);
    expect(pacer.take(flagFallbackKey("game_other", { realGameLive: false, followGameId: "" }))).toBe(true);
    expect(pacer.take(key)).toBe(true);
    vi.advanceTimersByTime(FLAG_STATE_FALLBACK_REPEAT_MS - 1);
    expect(pacer.take(key)).toBe(false);
    vi.advanceTimersByTime(1);
    expect(pacer.take(key)).toBe(true);
    pacer.forget();
    expect(pacer.take(key)).toBe(true);
  });
});
