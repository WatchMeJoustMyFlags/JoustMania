/**
 * Unit tests for the agent.decide feed (#1233): Jaeger /api/traces JSON ->
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
import { describe, it, expect } from "vitest";
import {
  confidenceBar,
  jaegerTraceUrl,
  parseDecideAnswers,
  parseDecideTraces,
} from "../agent.js";
import type { DecideRow, JaegerTracesResponse } from "../agent.js";
import {
  escapeHtml,
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

describe("parseDecideTraces — rules path", () => {
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

describe("parseDecideTraces — remote path", () => {
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
    expect(parseDecideTraces({ data: null })).toEqual({ rows: [], current: null });
    expect(parseDecideTraces({})).toEqual({ rows: [], current: null });
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
      decisions: [{ spanId: "z", action: evil, blocked: true, reason: evil }],
    };
    const html = renderDecideRow(row);
    expect(html).not.toContain("<script>");
    expect(renderDecideStrip({ tsMs: 0, backend: evil, requested: evil, model: evil, threshold: null, thresholdFromOlder: false })).not.toContain(
      "<script>",
    );
  });

  it("shows requested -> used on a fallback row", () => {
    const html = renderDecideRow(only(fallbackFixture));
    expect(html).toMatch(/decide-backend-struck[^>]*>claude<\/span><span class="decide-arrow">→<\/span><span class="decide-backend decide-backend-rules">rules/);
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
