/**
 * Unit tests for the agent.decide feed (#1233): Jaeger search JSON (fromOtlp shape) ->
 * DecideRow folding, the current-backend strip, threshold-bar maths, and the
 * escaping of span-derived (untrusted, model-written) text.
 *
 * Fixtures in ./fixtures are shaped like Jaeger's HTTP API response and follow
 * the span shape in services/agent/decision/decide_hook.go:
 *   rules       agent.signal_received -> { agent.decide, agent.decision x2 }
 *   remote-act  gameplay_phase -> agent.decide -> { chat {model}, agent.decision }
 *   held        agent.decide (model only on the chat child) below threshold
 *   fallback    claude timed out, rules answered
 *   discarded   act answer dropped (game_ended), truncated answers, hostile rationale
 */
import { escapeHtml } from "../html.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DecideStore,
  DECIDE_OVERLAP_MS,
  confidenceBar,
  decisionTargetLabel,
  decideQueryParams,
  decideQueryStartMs,
  fetchDecideFeed,
  jaegerTraceUrl,
  parseDecideAnswers,
  parseDecideTraces,
} from "../agent.js";
import { createPoller } from "../poller.js";
import { COLD_START_TIME_MIN, SEARCH_TIME_MAX } from "../jaeger.js";
import { toOtlp } from "./otlp.js";
import type { DecideRow, JaegerTracesResponse } from "../agent.js";
import {
  AgentView,
  renderConfidenceBar,
  renderDecideRow,
  renderDecideStrip,
  truncateText,
} from "../components/AgentView.js";
import rulesFixture from "./fixtures/jaeger-decide-rules.json";
import actFixture from "./fixtures/jaeger-decide-remote-act.json";
import heldFixture from "./fixtures/jaeger-decide-held.json";
import fallbackFixture from "./fixtures/jaeger-decide-fallback.json";
import discardedFixture from "./fixtures/jaeger-decide-discarded.json";
import multiFixture from "./fixtures/jaeger-decide-multi.json";
import shadowFixture from "./fixtures/jaeger-decide-shadow.json";

const asResp = (f: unknown) => f as JaegerTracesResponse;

function only(f: unknown): DecideRow {
  const { rows } = parseDecideTraces(asResp(f));
  expect(rows).toHaveLength(1);
  return rows[0];
}

/** Merge fixtures into one search response (Jaeger returns several traces). */
function merged(...fs: unknown[]): JaegerTracesResponse {
  return { data: fs.flatMap((f) => asResp(f).data ?? []) };
}

describe("UC-035: main step 3, parseDecideTraces — rules path", () => {
  const row = only(rulesFixture);

  it("reads backend, answer and confidence; no model, no threshold", () => {
    expect(row.backend).toBe("rules");
    expect(row.requested).toBe("rules");
    expect(row.model).toBe("");
    expect(row.intervention).toBe("grant_shield");
    expect(row.target).toBe("MOCK0002");
    expect(row.confidence).toBeCloseTo(0.7);
    expect(row.threshold).toBeNull();
    expect(row.outcome).toBe("act");
    expect(row.latencyMs).toBe(0);
  });

  it("links the sibling agent.decision spans under agent.signal_received", () => {
    expect(row.decisions.map((d) => [d.action, d.blocked])).toEqual([
      ["grant_shield", false],
      ["adjust_speed", true],
    ]);
  });
});

describe("UC-035: main step 3, parseDecideTraces — remote path", () => {
  it("act: model from gen_ai.request.model, child agent.decision linked", () => {
    const row = only(actFixture);
    expect(row.backend).toBe("claude");
    expect(row.model).toBe("claude-sonnet-5-5");
    expect(row.intervention).toBe("send_controller_effect");
    expect(row.target).toBe("MOCK0003");
    expect(row.confidence).toBeCloseTo(0.91);
    expect(row.threshold).toBeCloseTo(0.8);
    expect(row.outcome).toBe("act");
    expect(row.latencyMs).toBe(1312);
    expect(row.rationale).toContain("rumble");
    expect(row.decisions).toEqual([
      expect.objectContaining({ action: "send_controller_effect", blocked: false }),
    ]);
    // gameplay_phase's other children must not leak in.
    expect(row.traceId).toBe("bbbb000000000002");
    expect(row.spanId).toBe("d1");
  });

  it("held: model falls back to the child chat span", () => {
    const row = only(heldFixture);
    expect(row.outcome).toBe("held");
    expect(row.model).toBe("claude-haiku-4-5-20251001");
    expect(row.decisions).toEqual([]);
  });

  it("fallback: requested claude, used rules, with reason and error type", () => {
    const row = only(fallbackFixture);
    expect(row.outcome).toBe("fallback");
    expect(row.requested).toBe("claude");
    expect(row.backend).toBe("rules");
    expect(row.fallbackReason).toBe("timeout");
    expect(row.errorType).toBe("timeout");
    expect(row.intervention).toBe("noop");
    expect(row.target).toBe("none");
    expect(row.threshold).toBeNull();
  });

  it("discarded: reason recorded; truncated answers JSON yields blanks, not a throw", () => {
    const row = only(discardedFixture);
    expect(row.outcome).toBe("discarded");
    expect(row.outcomeReason).toBe("game_ended");
    expect(row.intervention).toBe("");
    expect(row.confidence).toBeCloseTo(0.93);
  });
});

describe("parseDecideTraces — ordering, dedupe, current strip", () => {
  it("newest first, deduped by trace/span", () => {
    const body = merged(rulesFixture, actFixture, heldFixture, fallbackFixture, discardedFixture, actFixture);
    const { rows } = parseDecideTraces(body);
    expect(rows.map((r) => r.spanId)).toEqual(["x1", "f1", "h1", "d1", "r1"]);
  });

  it("current strip reflects the newest decide span (the switch lands next decision)", () => {
    const before = parseDecideTraces(merged(rulesFixture)).current;
    expect(before?.backend).toBe("rules");
    const after = parseDecideTraces(merged(rulesFixture, actFixture)).current;
    expect(after?.backend).toBe("claude");
    expect(after?.model).toBe("claude-sonnet-5-5");
    expect(after?.threshold).toBeCloseTo(0.8);
    expect(after?.thresholdFromOlder).toBe(false);
  });

  it("current threshold falls back to the newest span that carried one", () => {
    // fallback (newest) has no threshold; held (older) does.
    const cur = parseDecideTraces(merged(heldFixture, fallbackFixture)).current;
    expect(cur?.backend).toBe("rules");
    expect(cur?.requested).toBe("claude");
    expect(cur?.threshold).toBeCloseTo(0.8);
    expect(cur?.thresholdFromOlder).toBe(true);
  });

  it("empty / null data", () => {
    expect(parseDecideTraces({ data: null })).toEqual({ rows: [], current: null, flagStates: [] });
    expect(parseDecideTraces({})).toEqual({ rows: [], current: null, flagStates: [] });
  });
});

describe("parseDecideAnswers", () => {
  it("ignores malformed entries", () => {
    expect(parseDecideAnswers('[null, 3, {"question_id":"ffa.intervention","choice":7}]')).toEqual({
      intervention: "",
      target: "",
    });
    expect(parseDecideAnswers('{"not":"an array"}')).toEqual({ intervention: "", target: "" });
    expect(parseDecideAnswers("")).toEqual({ intervention: "", target: "" });
  });
});

describe("confidenceBar", () => {
  it("fill and tick positions in percent", () => {
    expect(confidenceBar(0.91, 0.8)).toEqual({ fillPct: 91, thresholdPct: 80, pass: true });
    expect(confidenceBar(0.55, 0.8)).toEqual({ fillPct: 55, thresholdPct: 80, pass: false });
  });

  it("equal to the threshold passes (the agent routes with >=)", () => {
    expect(confidenceBar(0.8, 0.8).pass).toBe(true);
  });

  it("clamps out-of-range values", () => {
    expect(confidenceBar(1.4, -0.2)).toEqual({ fillPct: 100, thresholdPct: 0, pass: true });
    expect(confidenceBar(Number.NaN, 0.5).fillPct).toBe(0);
  });

  it("unknown sides", () => {
    expect(confidenceBar(0.7, null)).toEqual({ fillPct: 70, thresholdPct: null, pass: null });
    expect(confidenceBar(null, 0.8)).toEqual({ fillPct: 0, thresholdPct: 80, pass: null });
  });

  it("renders tick only with a threshold", () => {
    expect(renderConfidenceBar(0.91, 0.8)).toContain("left:80%");
    expect(renderConfidenceBar(0.91, 0.8)).toContain("decide-conf-pass");
    expect(renderConfidenceBar(0.55, 0.8)).toContain("conf 0.55 &lt; 0.80");
    expect(renderConfidenceBar(0.7, null)).not.toContain("decide-conf-tick");
  });
});

describe("rendering + escaping", () => {
  it("escapes hostile rationale in both text and title attribute", () => {
    const html = renderDecideRow(only(discardedFixture));
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).toContain("&quot;MOCK0004&quot; &amp; &#39;save&#39;");
    expect(html).toContain("decide-outcome-discarded");
    expect(html).toContain("game_ended");
  });

  it("escapes every span-derived field", () => {
    const evil = '"><script>x</script>';
    const row: DecideRow = {
      ...only(actFixture),
      backend: evil,
      requested: evil + "2",
      model: evil,
      fallbackReason: evil,
      intervention: evil,
      target: evil,
      outcomeReason: evil,
      decisions: [
        { spanId: "z", tsMs: 0, action: evil, blocked: true, reason: evil, blockReason: evil, applied: null, notAppliedReason: "", targetSerial: evil, targetName: evil + "n" },
      ],
    };
    const html = renderDecideRow(row);
    expect(html).not.toContain("<script>");
    expect(renderDecideStrip({ tsMs: 0, backend: evil, requested: evil, model: evil, threshold: null, thresholdFromOlder: false, gameId: evil, gameKind: evil })).not.toContain(
      "<script>",
    );
  });

  it("shows requested -> used on a fallback row", () => {
    const html = renderDecideRow(only(fallbackFixture));
    // The model sits with the REQUESTED (struck) backend, not with rules.
    expect(html).toMatch(
      /decide-backend-struck[^>]*>claude<\/span><span class="decide-model decide-backend-struck">claude-opus-5-5<\/span><span class="decide-arrow">→<\/span><span class="decide-backend decide-backend-rules">rules/,
    );
    expect(html).toContain("timeout");
  });

  it("deep link to the span in Jaeger", () => {
    expect(jaegerTraceUrl("bbbb000000000002", "d1")).toBe("/jaeger/trace/bbbb000000000002?uiFind=d1");
    expect(renderDecideRow(only(actFixture))).toContain('href="/jaeger/trace/bbbb000000000002?uiFind=d1"');
  });

  it("strip flags a switch", () => {
    const cur = parseDecideTraces(merged(actFixture)).current;
    expect(renderDecideStrip(cur, true)).toContain("decide-strip-switched");
    expect(renderDecideStrip(cur, false)).not.toContain("decide-strip-switched");
    expect(renderDecideStrip(null)).toContain("waiting");
  });

  it("truncateText and escapeHtml basics", () => {
    expect(truncateText("abcdef", 4)).toBe("abc…");
    expect(truncateText("abc", 4)).toBe("abc");
    expect(escapeHtml(`<a href='x'>&"`)).toBe("&lt;a href=&#39;x&#39;&gt;&amp;&quot;");
  });
});

describe("multi-decide game trace (#1195 shape)", () => {
  const { rows } = parseDecideTraces(asResp(multiFixture));
  const byId = new Map(rows.map((r) => [r.spanId, r]));

  it("folds every decide in the game trace", () => {
    expect(rows.map((r) => r.spanId)).toEqual(["D2", "D1", "R1"]);
  });

  it("rules decide links only its signal_received siblings", () => {
    expect(byId.get("R1")?.decisions.map((d) => d.action)).toEqual(["grant_shield"]);
  });

  it("remote decides link only their own children, never gameplay_phase's", () => {
    expect(byId.get("D1")?.decisions.map((d) => d.action)).toEqual(["send_controller_effect"]);
    // D2 has no children; the stray agent.decision under gameplay_phase must not attach.
    expect(byId.get("D2")?.decisions).toEqual([]);
  });

  it("row shows a short game id and kind badge", () => {
    const html = renderDecideRow(byId.get("D1") as DecideRow);
    expect(html).toContain("decide-game-real");
    expect(html).toContain("real …00a1b2");
  });
});

describe("current strip with real + shadow games", () => {
  it("a newer shadow rules decide does not flip the strip", () => {
    const { rows } = parseDecideTraces(merged(multiFixture, shadowFixture));
    expect(rows[0].spanId).toBe("SH1");
    const cur = parseDecideTraces(merged(multiFixture, shadowFixture)).current;
    expect(cur?.gameKind).toBe("real");
    expect(cur?.backend).toBe("claude");
    expect(cur?.model).toBe("claude-sonnet-5-5");
    // D2 (noop) carries no threshold; it comes from D1 of the same game.
    expect(cur?.threshold).toBeCloseTo(0.8);
    expect(cur?.thresholdFromOlder).toBe(true);
  });

  it("only shadow games -> the newest row's game", () => {
    const cur = parseDecideTraces(merged(shadowFixture)).current;
    expect(cur?.gameKind).toBe("shadow");
    expect(cur?.backend).toBe("rules");
  });

  it("strip says rules engine (no model) when rules decided, even on a fallback", () => {
    const cur = parseDecideTraces(merged(fallbackFixture)).current;
    expect(cur?.model).toBe("claude-opus-5-5");
    const html = renderDecideStrip(cur);
    expect(html).toContain("rules engine");
    expect(html).not.toContain("claude-opus-5-5");
  });
});

describe("incremental query window", () => {
  it("cold start: the epoch lower bound, the fixed upper bound (#2283)", () => {
    expect(decideQueryStartMs(null)).toBeNull();
    const q = decideQueryParams(null);
    expect(q.get("query.startTimeMin")).toBe(COLD_START_TIME_MIN);
    expect(q.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
    expect(q.get("query.searchDepth")).toBe("5");
  });

  it("warm: the overlap behind the newest decide seen, RFC 3339, the fixed upper bound", () => {
    const newest = 1_760_000_012_000;
    expect(decideQueryStartMs(newest)).toBe(newest - DECIDE_OVERLAP_MS);
    const q = decideQueryParams(decideQueryStartMs(newest));
    expect(q.get("query.startTimeMin")).toBe(new Date(newest - DECIDE_OVERLAP_MS).toISOString());
    expect(q.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
  });

  it("DecideStore tracks the newest decide, replaces re-fetched rows, and stays bounded", () => {
    const store = new DecideStore(3);
    expect(store.queryStartMs()).toBeNull();
    const multi = parseDecideTraces(asResp(multiFixture)).rows;
    store.merge(multi);
    const newest = Math.max(...multi.map((r) => r.tsMs));
    expect(store.queryStartMs()).toBe(newest - DECIDE_OVERLAP_MS);
    // Re-fetch of the same spans: no duplicates.
    store.merge(multi);
    expect(store.size).toBe(3);
    // More rows than the bound: the oldest go.
    store.merge(parseDecideTraces(asResp(shadowFixture)).rows);
    expect(store.size).toBe(3);
    expect(store.feed().rows.map((r) => r.spanId)).toEqual(["SH1", "D2", "D1"]);
    expect(store.feed().current?.gameKind).toBe("real");
  });
});

describe("poller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function deferred() {
    let resolve!: () => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<void>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  it("a poke during a slow fetch does not start a second chain", async () => {
    const calls: ReturnType<typeof deferred>[] = [];
    const poller = createPoller({
      baseMs: 1500,
      maxMs: 10_000,
      timeoutMs: 4000,
      isActive: () => true,
      run: () => {
        const d = deferred();
        calls.push(d);
        return d.promise;
      },
    });
    poller.poke();
    expect(calls).toHaveLength(1);
    // Visibility / tab toggles while the request is in flight.
    poller.stop();
    poller.poke();
    poller.poke();
    expect(calls).toHaveLength(1);
    calls[0].resolve();
    await vi.advanceTimersByTimeAsync(0);
    // Exactly one tick scheduled, at the base cadence.
    await vi.advanceTimersByTimeAsync(1500);
    expect(calls).toHaveLength(2);
    calls[1].resolve();
    await vi.advanceTimersByTimeAsync(1500);
    expect(calls).toHaveLength(3);
  });

  it("backs off (doubling, capped) on errors and resets on a fast success", async () => {
    let fail = true;
    let runs = 0;
    const poller = createPoller({
      baseMs: 1500,
      maxMs: 10_000,
      timeoutMs: 4000,
      isActive: () => true,
      run: async () => {
        runs += 1;
        if (fail) throw new Error("jaeger down");
      },
    });
    poller.poke();
    await vi.advanceTimersByTimeAsync(0);
    expect(poller.delayMs).toBe(3000);
    await vi.advanceTimersByTimeAsync(3000);
    expect(poller.delayMs).toBe(6000);
    await vi.advanceTimersByTimeAsync(6000);
    expect(poller.delayMs).toBe(10_000);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(poller.delayMs).toBe(10_000);
    fail = false;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(poller.delayMs).toBe(1500);
    expect(runs).toBe(5);
  });

  it("aborts a request after the timeout and backs off", async () => {
    let aborted = false;
    const poller = createPoller({
      baseMs: 1500,
      maxMs: 10_000,
      timeoutMs: 4000,
      isActive: () => true,
      run: (signal) =>
        new Promise<void>((_, reject) => {
          signal.addEventListener("abort", () => {
            aborted = true;
            reject(new Error("aborted"));
          });
        }),
    });
    poller.poke();
    await vi.advanceTimersByTimeAsync(4000);
    expect(aborted).toBe(true);
    expect(poller.inFlight).toBe(false);
    expect(poller.delayMs).toBe(3000);
  });

  it("does not poll while inactive and resumes on poke", async () => {
    let active = false;
    let runs = 0;
    const poller = createPoller({
      baseMs: 1500,
      maxMs: 10_000,
      timeoutMs: 4000,
      isActive: () => active,
      run: async () => {
        runs += 1;
      },
    });
    poller.poke();
    await vi.advanceTimersByTimeAsync(5000);
    expect(runs).toBe(0);
    active = true;
    poller.poke();
    await vi.advanceTimersByTimeAsync(0);
    expect(runs).toBe(1);
    active = false;
    await vi.advanceTimersByTimeAsync(5000);
    expect(runs).toBe(1);
  });
});

describe("AgentView.renderDecide skips unchanged renders", () => {
  /** Minimal DOM stand-in: counts innerHTML writes per element. */
  function fakeDocument() {
    const writes: Record<string, number> = {};
    const els: Record<string, unknown> = {};
    const make = (id: string) => {
      let html = "";
      writes[id] = 0;
      return {
        get innerHTML() {
          return html;
        },
        set innerHTML(v: string) {
          writes[id] += 1;
          html = v;
        },
        hidden: true,
        textContent: "",
        classList: { toggle() {} },
      };
    };
    for (const id of ["feed", "flags", "overlay", "strip", "dfeed"]) els[id] = make(id);
    return { writes, doc: { getElementById: (id: string) => els[id] ?? null } };
  }

  let restore: unknown;
  beforeEach(() => {
    restore = (globalThis as { document?: unknown }).document;
  });
  afterEach(() => {
    (globalThis as { document?: unknown }).document = restore;
  });

  it("does not rewrite the strip or rows when nothing changed", () => {
    const { writes, doc } = fakeDocument();
    (globalThis as { document?: unknown }).document = doc;
    const view = new AgentView("feed", "flags", "overlay", undefined, { strip: "strip", feed: "dfeed" });
    const feed = parseDecideTraces(merged(multiFixture));
    view.renderDecide(feed);
    expect(writes.strip).toBe(1);
    expect(writes.dfeed).toBe(1);
    // Same content again (a new object, as every poll produces).
    view.renderDecide(parseDecideTraces(merged(multiFixture)));
    expect(writes.strip).toBe(1);
    expect(writes.dfeed).toBe(1);
    // A newer shadow decide: rows change, the strip (real game) does not.
    view.renderDecide(parseDecideTraces(merged(multiFixture, shadowFixture)));
    expect(writes.strip).toBe(1);
    expect(writes.dfeed).toBe(2);
  });
});

describe("clock skew: browser clock hours off the agent/Jaeger clock", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("never sends browser-derived times, and the rows still render", async () => {
    vi.useFakeTimers();
    // Fixture spans are at ~1_760_000_000_000 ms; the presenter laptop is 7h ahead.
    const browserNow = 1_760_000_000_000 + 7 * 3_600_000;
    vi.setSystemTime(browserNow);
    const urls: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      urls.push(url);
      return new Response(JSON.stringify(toOtlp(merged(multiFixture))), { status: 200 });
    });

    const store = new DecideStore();
    const first = await fetchDecideFeed(store.queryStartMs());
    store.merge(first.rows);
    await fetchDecideFeed(store.queryStartMs());

    const [cold, warm] = urls.map((u) => new URLSearchParams(u.split("?")[1]));
    expect(cold.get("query.startTimeMin")).toBe(COLD_START_TIME_MIN);
    expect(cold.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
    const newest = Math.max(...first.rows.map((r) => r.tsMs));
    expect(warm.get("query.startTimeMin")).toBe(new Date(newest - DECIDE_OVERLAP_MS).toISOString());
    expect(warm.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
    // The only times in either query are fixed bounds or span-derived (agent clock).
    const keys = ["query.operationName", "query.searchDepth", "query.serviceName", "query.startTimeMax", "query.startTimeMin"];
    expect([...cold.keys()].sort()).toEqual(keys);
    expect([...warm.keys()].sort()).toEqual(keys);
    expect(Date.parse(warm.get("query.startTimeMin")!)).toBeLessThan(browserNow - 6 * 3_600_000);

    const feed = store.feed();
    expect(feed.rows.map((r) => r.spanId)).toEqual(["D2", "D1", "R1"]);
    expect(renderDecideRow(feed.rows[0])).toContain("decide-outcome-noop");
    expect(feed.current?.backend).toBe("claude");
  });
});

describe("UC-040 main: the Decisions row names the targeted player (#1295)", () => {
  /** The rules fixture with player.serial / player.name on its decision spans. */
  function withTargets(serial: string, name: string): JaegerTracesResponse {
    const f = JSON.parse(JSON.stringify(rulesFixture)) as JaegerTracesResponse;
    for (const span of f.data?.[0]?.spans ?? []) {
      if (span.operationName !== "agent.decision") continue;
      const action = (span.tags ?? []).find((t) => t.key === "decision.action")?.value;
      if (action !== "grant_shield") continue;
      span.tags = [...(span.tags ?? []), { key: "player.serial", type: "string", value: serial }];
      if (name) span.tags?.push({ key: "player.name", type: "string", value: name });
    }
    return f;
  }

  it("UC-040 main: reads player.serial and player.name off agent.decision", () => {
    const [row] = parseDecideTraces(withTargets("MOCK0002", "Calm Lion")).rows;
    const shield = row.decisions.find((d) => d.action === "grant_shield");
    expect(shield?.targetSerial).toBe("MOCK0002");
    expect(shield?.targetName).toBe("Calm Lion");
    // A session-scoped decision carries no target.
    const other = row.decisions.find((d) => d.action !== "grant_shield");
    expect(other?.targetSerial).toBe("");
    expect(other?.targetName).toBe("");
  });

  it("UC-040 main: shows the name, with the serial as the tooltip", () => {
    const html = renderDecideRow(parseDecideTraces(withTargets("MOCK0002", "Calm Lion")).rows[0]);
    expect(html).toMatch(/agent\.decision: grant_shield <span class="decide-arrow">→<\/span> <span class="decide-target" title="MOCK0002">Calm Lion<\/span> · allowed/);
  });

  it("UC-040 main: falls back to the serial when no name is known", () => {
    const html = renderDecideRow(parseDecideTraces(withTargets("MOCK0002", "")).rows[0]);
    expect(html).toContain('<span class="decide-target">MOCK0002</span> · allowed');
    expect(decisionTargetLabel({ targetSerial: "MOCK0002", targetName: "" })).toBe("MOCK0002");
    expect(decisionTargetLabel({ targetSerial: "", targetName: "" })).toBe("");
  });

  it("UC-040 A10: a blocked targeted decision still names its target", () => {
    const f = withTargets("MOCK0004", "Calm Lion");
    for (const span of f.data?.[0]?.spans ?? []) {
      for (const t of span.tags ?? []) if (t.key === "decision.blocked") t.value = true;
    }
    const html = renderDecideRow(parseDecideTraces(f).rows[0]);
    expect(html).toMatch(/grant_shield <span class="decide-arrow">→<\/span> <span class="decide-target" title="MOCK0004">Calm Lion<\/span> · blocked/);
  });
});

describe("UC-040: A11, a write the actuation gate dropped is not 'allowed' (#2093)", () => {
  /** The act fixture with extra tags on its agent.decision span. */
  function withDecisionTags(tags: { key: string; type: string; value: string | boolean }[]): DecideRow {
    const body = structuredClone(actFixture) as unknown as JaegerTracesResponse;
    for (const trace of body.data ?? []) {
      for (const span of trace.spans) {
        if (span.operationName === "agent.decision") span.tags?.push(...tags);
      }
    }
    return only(body);
  }

  it("UC-040/A11 decision.applied=false + write_gate_off reads as not applied with the reason", () => {
    const row = withDecisionTags([
      { key: "decision.applied", type: "bool", value: false },
      { key: "decision.not_applied_reason", type: "string", value: "write_gate_off" },
    ]);
    expect(row.decisions[0].applied).toBe(false);
    expect(row.decisions[0].notAppliedReason).toBe("write_gate_off");
    const html = renderDecideRow(row);
    expect(html).toContain("not applied: write gate off (interventions_enabled)");
    expect(html).not.toMatch(/agent\.decision:[^<]*· allowed/);
    expect(html).toContain("decide-decision-blocked");
  });

  it("UC-040/A27 decision.not_applied_reason=write_failed reads as not applied: write failed", () => {
    const row = withDecisionTags([
      { key: "decision.applied", type: "bool", value: false },
      { key: "decision.not_applied_reason", type: "string", value: "write_failed" },
    ]);
    const html = renderDecideRow(row);
    expect(html).toContain("not applied: write failed");
    expect(html).not.toMatch(/agent\.decision:[^<]*· allowed/);
  });

  it("UC-040/main a written decision (applied=true) and a pre-#2093 span still read allowed", () => {
    const written = withDecisionTags([{ key: "decision.applied", type: "bool", value: true }]);
    expect(renderDecideRow(written)).toMatch(/agent\.decision:[^<]*· allowed/);
    expect(renderDecideRow(only(actFixture))).toMatch(/agent\.decision:[^<]*· allowed/);
  });
});
