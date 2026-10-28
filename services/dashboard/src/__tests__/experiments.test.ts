/**
 * Experiments tab from experiment.progress spans (#2059; UC-035 step 4).
 *
 * jaeger-experiment-progress holds one-span traces (own roots):
 *   exp_alpha  running: game_assigned snapshot, then a newer game_concluded one
 *              with an interim `inconclusive` verdict
 *   exp_beta   discarded (terminal) with verdict discard and an end reason
 *   exp_gamma  aborted (terminal) without a verdict
 *   exp_delta  done (terminal) with verdict promote
 *   exp_epsilon aborted (terminal) whose snapshot still carries the interim
 *              `inconclusive` verdict (journal KindInterimVerdict)
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EXPERIMENT_COLD_LIMIT,
  EXPERIMENT_OVERLAP_MS,
  ExperimentStore,
  parseExperimentSnapshots,
  pollExperiments,
  resolveVerdict,
} from "../experiments.js";
import { COLD_START_TIME_MIN, SEARCH_TIME_MAX } from "../jaeger.js";
import type { JaegerTracesResponse } from "../jaeger.js";
import { ExperimentsView } from "../components/ExperimentsView.js";
import { toOtlp } from "./otlp.js";
import progressFixture from "./fixtures/jaeger-experiment-progress.json";

const progress = progressFixture as unknown as JaegerTracesResponse;
const T0_MS = 1_760_000_000_000;

function byId(store: ExperimentStore, id: string) {
  return store.list().find((e) => e.experimentId === id);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("UC-035: main step 4, newest experiment.progress snapshot per experiment", () => {
  it("UC-035/main step 4 keeps the newest snapshot of each experiment, whatever the order", () => {
    const snaps = parseExperimentSnapshots(progress);
    const store = new ExperimentStore();
    store.merge([...snaps].reverse());
    const alpha = byId(store, "exp_alpha");
    expect(alpha?.lastUpdatedMs).toBe(T0_MS + 30_000);
    expect(alpha?.arms).toEqual([
      { arm: "experimental", sampleCount: 3, meanFitness: 0.62, inFlight: 1 },
      { arm: "control", sampleCount: 2, meanFitness: 0.5, inFlight: 2 },
    ]);
  });

  it("UC-035/main step 4 Δ is the experimental mean minus the control mean, only with both arms sampled", () => {
    const store = new ExperimentStore();
    store.merge(parseExperimentSnapshots(progress));
    expect(byId(store, "exp_alpha")?.delta).toBeCloseTo(0.12, 10);
    expect(byId(store, "exp_gamma")?.delta).toBeNull();
  });

  it("UC-035/main step 4 the verdict is verdict.outcome once finished, else the status, else running", () => {
    const store = new ExperimentStore();
    store.merge(parseExperimentSnapshots(progress));
    expect(byId(store, "exp_alpha")).toMatchObject({ verdict: "running", live: true });
    expect(byId(store, "exp_beta")).toMatchObject({ verdict: "discard", live: false, reason: "paired: d_z=-0.81 (pairs=6)" });
    expect(byId(store, "exp_gamma")).toMatchObject({ verdict: "aborted", live: false, reason: "kill_switch" });
    expect(byId(store, "exp_delta")).toMatchObject({ verdict: "promote", live: false });
    expect(resolveVerdict("promote", "concluded")).toBe("promote");
    expect(resolveVerdict("discard", "discarded")).toBe("discard");
    expect(resolveVerdict("", "promoting")).toBe("promoting");
    expect(resolveVerdict("", "")).toBe("running");
  });

  it("UC-035/main step 4 an aborted experiment shows aborted, its interim verdict only as a note", () => {
    const store = new ExperimentStore();
    store.merge(parseExperimentSnapshots(progress));
    expect(byId(store, "exp_epsilon")).toMatchObject({
      verdict: "aborted (interim: inconclusive)",
      live: false,
      reason: "capacity_reclaim",
    });
    expect(resolveVerdict("inconclusive", "aborted")).toBe("aborted (interim: inconclusive)");
    expect(resolveVerdict("", "aborted")).toBe("aborted");
  });

  it("UC-035/main step 4 two snapshots in the same ms: the later one (µs) wins, whatever the span ids", () => {
    const [beta] = parseExperimentSnapshots(progress).filter((e) => e.experimentId === "exp_beta");
    // game_concluded then terminal: only a journal append lies between them.
    const older = { ...beta, status: "running", verdict: "running", live: true, startUs: beta.startUs + 100, spanId: "zzzz" };
    const newer = { ...beta, startUs: beta.startUs + 400, spanId: "aaaa" };
    expect(older.lastUpdatedMs).toBe(newer.lastUpdatedMs);
    for (const order of [[older, newer], [newer, older]]) {
      const store = new ExperimentStore();
      for (const snap of order) store.merge([snap]);
      expect(byId(store, "exp_beta")).toMatchObject({ status: "discarded", verdict: "discard", spanId: "aaaa" });
    }
  });

  it("UC-035/main step 4 two snapshots in the same µs: the later lifecycle status wins", () => {
    const [beta] = parseExperimentSnapshots(progress).filter((e) => e.experimentId === "exp_beta");
    const concluded = { ...beta, status: "concluded", spanId: "zzzz" };
    const terminal = { ...beta, spanId: "aaaa" };
    for (const order of [[concluded, terminal], [terminal, concluded]]) {
      const store = new ExperimentStore();
      for (const snap of order) store.merge([snap]);
      expect(byId(store, "exp_beta")?.status).toBe("discarded");
    }
  });

  it("UC-035/main step 4 keeps every live experiment and only the newest N finished ones", () => {
    const store = new ExperimentStore(2);
    store.merge(parseExperimentSnapshots(progress));
    expect(store.list().map((e) => e.experimentId)).toEqual(["exp_gamma", "exp_beta", "exp_alpha"]);
  });
});

describe("UC-035: main step 4, the search never uses the browser clock", () => {
  it("UC-035/main step 4 cold start: limit 200, no start, no end; then newest span minus the overlap", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-01-01T00:00:00Z")); // hours off the agent clock
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return new Response(JSON.stringify(toOtlp(progress)), { status: 200 });
      }),
    );
    const store = new ExperimentStore();

    await pollExperiments(store);
    await pollExperiments(store);

    const cold = new URL(urls[0], "http://x").searchParams;
    expect(cold.get("query.operationName")).toBe("experiment.progress");
    expect(cold.get("query.serviceName")).toBe("agent");
    expect(cold.get("query.searchDepth")).toBe(String(EXPERIMENT_COLD_LIMIT));
    expect(cold.get("query.startTimeMin")).toBe(COLD_START_TIME_MIN);
    expect(cold.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
    const next = new URL(urls[1], "http://x").searchParams;
    expect(next.get("query.startTimeMin")).toBe(new Date(T0_MS + 50_000 - EXPERIMENT_OVERLAP_MS).toISOString());
    expect(next.get("query.startTimeMax")).toBe(SEARCH_TIME_MAX);
  });

  it("UC-035/A3 no experiment.progress span (an agent image before #2059) is an empty list, not an error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { httpCode: 404, message: "No traces found" } }), { status: 404 })));
    await expect(pollExperiments(new ExperimentStore())).resolves.toEqual([]);
  });
});

describe("UC-035: main step 4, recommendation of a refused promotion (#2276)", () => {
  function snapshot(extra: { key: string; value: string }[]): JaegerTracesResponse {
    const tag = (key: string, value: string) => ({ key, type: "string", value });
    return {
      data: [
        {
          traceID: "rec1",
          spans: [
            {
              traceID: "rec1",
              spanID: "sprec",
              operationName: "experiment.progress",
              references: [],
              startTime: 1_760_000_100_000_000,
              duration: 10,
              tags: [
                tag("experiment.id", "exp_rec"),
                tag("experiment.status", "discarded"),
                tag("experiment.flag_key", "death_grace_period_seconds"),
                ...extra.map((t) => tag(t.key, t.value)),
              ],
              logs: [],
            },
          ],
        },
      ],
    } as unknown as JaegerTracesResponse;
  }

  it("UC-035/main step 4 the snapshot's recommendation attributes are folded", () => {
    const [view] = parseExperimentSnapshots(
      snapshot([
        { key: "experiment.recommendation.flag", value: "death_grace_period_seconds" },
        { key: "experiment.recommendation.value", value: "0.5" },
        { key: "experiment.recommendation.owner", value: "operator" },
      ]),
    );
    expect(view.recommendation).toEqual({ flag: "death_grace_period_seconds", value: "0.5", owner: "operator" });
  });

  it("UC-035/main step 4 no recommendation attributes: none", () => {
    const [view] = parseExperimentSnapshots(snapshot([]));
    expect(view.recommendation).toBeNull();
  });

  it("UC-035/main step 4 the card says would promote <flag>=<value>; real value owned by <writer>", () => {
    const [view] = parseExperimentSnapshots(
      snapshot([
        { key: "experiment.recommendation.flag", value: "death_grace_period_seconds" },
        { key: "experiment.recommendation.value", value: "0.5" },
        { key: "experiment.recommendation.owner", value: "operator" },
      ]),
    );
    const grid = { innerHTML: "", hidden: false, textContent: "", classList: { toggle: () => {} } };
    vi.stubGlobal("document", { getElementById: () => grid });
    new ExperimentsView("grid").render([view]);
    expect(grid.innerHTML).toContain("would promote death_grace_period_seconds=0.5; real value owned by operator");
    grid.innerHTML = "";
    new ExperimentsView("grid").render(parseExperimentSnapshots(snapshot([])));
    expect(grid.innerHTML).not.toContain("would promote");
  });
});
