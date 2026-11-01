/**
 * Dashboard Agent tab: the prediction a decision carried and how it was scored
 * (#2479, UC-040 A44, BR-034). The decide span has the decide.prediction.* tags; the
 * agent.prediction.outcome span sits in the same game trace and is joined by id.
 */
import { describe, expect, it } from "vitest";
import { DECIDE_OVERLAP_MS, DecideStore, parseDecideTraces, predictionStripFrom } from "../agent.js";
import type { DecideRow, JaegerTracesResponse } from "../agent.js";
import { predictionText, renderDecideRow, renderPredictionStrip } from "../components/AgentView.js";
import actFixture from "./fixtures/jaeger-decide-remote-act.json";

type Tag = { key: string; type: string; value: string | number | boolean };
const str = (key: string, value: string): Tag => ({ key, type: "string", value });
const num = (key: string, value: number): Tag => ({ key, type: "float64", value });

/** The remote-act trace with prediction tags on its decide span and the given extra spans. */
function trace(
  decideTags: Tag[],
  outcomes: { id: string; tags: Tag[] }[] = [],
): JaegerTracesResponse {
  const body = structuredClone(actFixture) as unknown as JaegerTracesResponse;
  const t = body.data![0];
  const decide = t.spans.find((s) => s.operationName === "agent.decide")!;
  decide.tags!.push(...decideTags);
  outcomes.forEach((o, i) => {
    t.spans.push({
      traceID: t.traceID,
      spanID: `o${i}`,
      operationName: "agent.prediction.outcome",
      references: [{ refType: "CHILD_OF", traceID: t.traceID, spanID: "g1" }],
      startTime: decide.startTime + 20_000_000 + i,
      duration: 1,
      tags: [str("prediction.id", o.id), ...o.tags],
      logs: [],
      processID: "p1",
    } as never);
  });
  return body;
}

const R4_TAGS: Tag[] = [
  str("decide.prediction.id", "pred-1"),
  str("decide.prediction.template", "survives"),
  str("decide.prediction.characteristic", "survival"),
  str("decide.prediction.scope", "target"),
  num("decide.prediction.horizon_s", 15),
  num("decide.prediction.p", 0.7),
  str("decide.prediction.source", "rules"),
  str("decide.prediction.rule", "R4"),
];

const scored = (outcome: string, extra: Tag[] = []): Tag[] => [
  str("prediction.outcome", outcome),
  str("prediction.source", "rules"),
  str("prediction.template", "survives"),
  str("game.id", "game_abc"),
  str("game.kind", "real"),
  { key: "prediction.acted", type: "bool", value: true },
  ...extra,
];

function only(body: JaegerTracesResponse): DecideRow {
  const { rows } = parseDecideTraces(body);
  expect(rows).toHaveLength(1);
  return rows[0];
}

describe("UC-040 A44 BR-034: a decision's prediction and its scored outcome", () => {
  it("UC-040/A44 reads decide.prediction.* into DecideRow.prediction", () => {
    const row = only(trace(R4_TAGS));
    expect(row.prediction).toMatchObject({
      id: "pred-1",
      template: "survives",
      scope: "target",
      horizonS: 15,
      p: 0.7,
      source: "rules",
      rule: "R4",
      merged: false,
    });
  });

  it("UC-040/A44 a decision without a prediction has none", () => {
    expect(only(trace([])).prediction).toBeNull();
  });

  it("UC-040/A44 no outcome span yet reads pending", () => {
    expect(only(trace(R4_TAGS)).prediction?.outcome).toBe("pending");
  });

  it.each(["hit", "miss"])("UC-040/A44 the outcome span of the same trace and id gives %s", (outcome) => {
    expect(only(trace(R4_TAGS, [{ id: "pred-1", tags: scored(outcome) }])).prediction?.outcome).toBe(outcome);
  });

  it("UC-040/A44 unscorable keeps its reason", () => {
    const row = only(
      trace(R4_TAGS, [{ id: "pred-1", tags: scored("unscorable", [str("prediction.unscorable_reason", "game_ended")]) }]),
    );
    expect(row.prediction?.outcome).toBe("unscorable");
    expect(row.prediction?.unscorableReason).toBe("game_ended");
  });

  it("UC-040/A44 an outcome with another id is not joined", () => {
    expect(only(trace(R4_TAGS, [{ id: "other", tags: scored("hit") }])).prediction?.outcome).toBe("pending");
  });

  it("UC-040/A44 an outcome in another trace is not joined", () => {
    const body = trace(R4_TAGS);
    const other = trace([], [{ id: "pred-1", tags: scored("hit") }]).data![0];
    other.traceID = "cccc000000000009";
    other.spans = other.spans.map((s) => ({ ...s, traceID: "cccc000000000009" }));
    body.data!.push({ ...other, spans: other.spans.filter((s) => s.operationName === "agent.prediction.outcome") });
    expect(parseDecideTraces(body).rows[0].prediction?.outcome).toBe("pending");
  });

  it("UC-040/A44 the row reads 'expects: survives 15 s (p 0.70, rules R4) → hit'", () => {
    const row = only(trace(R4_TAGS, [{ id: "pred-1", tags: scored("hit") }]));
    expect(predictionText(row.prediction!)).toBe("survives 15 s (p 0.70, rules R4)");
    const html = renderDecideRow(row);
    expect(html).toContain("expects:");
    expect(html).toContain("survives 15 s (p 0.70, rules R4)");
    expect(html).toMatch(/decide-prediction-outcome[^>]*>hit</);
    expect(renderDecideRow(only(trace(R4_TAGS)))).toMatch(/decide-prediction-outcome[^>]*>pending</);
    expect(renderDecideRow(only(trace([])))).not.toContain("expects:");
  });

  it("UC-040/A44 span-derived text is escaped", () => {
    const tags = R4_TAGS.map((t) => (t.key === "decide.prediction.rule" ? str(t.key, "<img src=x>") : t));
    const html = renderDecideRow(only(trace(tags)));
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  it("UC-040/A44 a row re-fetched with its outcome replaces the pending one", () => {
    const store = new DecideStore();
    store.merge(parseDecideTraces(trace(R4_TAGS)).rows);
    expect(store.feed().rows[0].prediction?.outcome).toBe("pending");
    store.merge(parseDecideTraces(trace(R4_TAGS, [{ id: "pred-1", tags: scored("miss") }])).rows);
    expect(store.feed().rows).toHaveLength(1);
    expect(store.feed().rows[0].prediction?.outcome).toBe("miss");
  });
});

describe("UC-040 A44: a pending prediction the query window left behind reads unknown", () => {
  it("UC-040/A44 pending stays pending while the window is near, then reads unknown", () => {
    const store = new DecideStore();
    const old = only(trace(R4_TAGS));
    store.merge([old]);
    expect(store.feed().rows[0].prediction?.outcome).toBe("pending");
    // A decide DECIDE_OVERLAP_MS + 1 ms newer arrives (another trace, no outcome for the old one).
    store.merge([{ ...old, spanId: "later", tsMs: old.tsMs + DECIDE_OVERLAP_MS + 1, prediction: null }]);
    const rows = store.feed().rows;
    expect(rows.find((r) => r.spanId === old.spanId)?.prediction?.outcome).toBe("unknown");
    expect(renderDecideRow(rows.find((r) => r.spanId === old.spanId)!)).toMatch(/decide-prediction-outcome[^>]*>unknown</);
  });

  it("UC-040/A44 a scored prediction is never turned into unknown", () => {
    const store = new DecideStore();
    const old = only(trace(R4_TAGS, [{ id: "pred-1", tags: scored("hit") }]));
    store.merge([old, { ...old, spanId: "later", tsMs: old.tsMs + DECIDE_OVERLAP_MS + 1, prediction: null }]);
    expect(store.feed().rows.find((r) => r.spanId === old.spanId)?.prediction?.outcome).toBe("hit");
  });
});

describe("UC-040 A44 BR-034: hit rate against the control base rate per game", () => {
  const span = (id: string, source: string, template: string, outcome: string, acted = true, gameId = "game_abc") => ({
    id,
    traceId: "t",
    tsMs: 1,
    gameId,
    gameKind: "real",
    backend: "rules",
    source,
    template,
    p: 0.7,
    acted,
    outcome: outcome as "hit" | "miss" | "unscorable",
    unscorableReason: "",
  });

  it("UC-040/A44 compares a source with the controls of its own templates", () => {
    const strip = predictionStripFrom(
      [
        span("r1", "rules", "survives", "hit"),
        span("r2", "rules", "survives", "hit"),
        span("r3", "rules", "survives", "miss"),
        span("r4", "rules", "survives", "unscorable"),
        span("c1", "control", "survives", "hit"),
        span("c2", "control", "survives", "miss"),
        span("c3", "control", "game_ends", "hit"),
        span("c4", "control", "survives", "unscorable"),
      ],
      "game_abc",
    )!;
    expect(strip.sources).toHaveLength(1);
    const rules = strip.sources[0];
    expect(rules).toMatchObject({ source: "rules", hits: 2, misses: 1, unscorable: 1 });
    expect(rules.hitRate).toBeCloseTo(2 / 3);
    // Only the scored survives controls count for a source that predicted survives.
    expect(rules.baseRate).toBeCloseTo(0.5);
  });

  it("UC-040/A44 BR-034 counterfactual predictions stay out of the rates", () => {
    const strip = predictionStripFrom(
      [span("m1", "model", "survives", "hit", false), span("m2", "model", "survives", "hit", true)],
      "game_abc",
    )!;
    expect(strip.sources[0]).toMatchObject({ source: "model", hits: 1, misses: 0 });
    expect(strip.counterfactual).toBe(1);
  });

  it("UC-040/A44 no control of the template: the base rate is n/a, not zero", () => {
    const strip = predictionStripFrom([span("r1", "rules", "survives", "hit")], "game_abc")!;
    expect(strip.sources[0].baseRate).toBeNull();
    expect(renderPredictionStrip(strip)).toContain("vs controls n/a");
  });

  it("UC-040/A44 only the followed game counts, and none gives no strip", () => {
    const spans = [span("r1", "rules", "survives", "hit", true, "game_other")];
    expect(predictionStripFrom(spans, "game_abc")).toBeNull();
    expect(renderPredictionStrip(null)).toBe("");
  });

  it("UC-040/A44 the feed carries the strip of the followed game", () => {
    const game = "game_0a1b2c3d4e5f"; // the game of the remote-act fixture
    const outcome = (source: string, outcome: string): Tag[] => [
      str("prediction.outcome", outcome),
      str("prediction.source", source),
      str("prediction.template", "survives"),
      str("game.id", game),
      str("game.kind", "real"),
      { key: "prediction.acted", type: "bool", value: true },
    ];
    const body = trace(R4_TAGS, [
      { id: "pred-1", tags: outcome("rules", "hit") },
      { id: "ctl-1", tags: outcome("control", "miss") },
    ]);
    const store = new DecideStore();
    const feed = parseDecideTraces(body);
    store.merge(feed.rows);
    store.mergeOutcomes(feed.outcomes ?? []);
    expect(feed.predictionStrip?.gameId).toBe(game);
    const strip = store.feed().predictionStrip;
    expect(strip?.gameId).toBe(game);
    expect(strip?.sources).toHaveLength(1);
    expect(strip?.sources[0]).toMatchObject({ source: "rules", hits: 1, misses: 0, hitRate: 1, baseRate: 0 });
  });
});
