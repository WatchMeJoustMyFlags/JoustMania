/**
 * Dashboard Agent tab: the game's goal profile, its source and its attainment
 * (#2479, #2498, UC-042 A12). The agent.game.summary span carries the
 * summary.goal.* attributes; the strip follows the followed game.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { DecideStore, GOAL_POLL_MS, createGoalRun, GAME_SUMMARY_OPERATION, fetchGoal, parseDecideTraces, parseGoalSummaries } from "../agent.js";
import type { JaegerTracesResponse } from "../agent.js";
import { renderGoalStrip } from "../components/AgentView.js";
import { toOtlp } from "./otlp.js";
import actFixture from "./fixtures/jaeger-decide-remote-act.json";

type Tag = { key: string; type: string; value: string | number | boolean };
const str = (key: string, value: string): Tag => ({ key, type: "string", value });
const num = (key: string, value: number): Tag => ({ key, type: "float64", value });

function summary(tags: Tag[], gameId = "game_abc", startTime = 1_700_000_000_000_000): JaegerTracesResponse {
  return {
    data: [
      {
        traceID: `tr${gameId}`,
        processes: { p1: { serviceName: "agent" } },
        spans: [
          {
            traceID: `tr${gameId}`,
            spanID: "s1",
            operationName: GAME_SUMMARY_OPERATION,
            references: [],
            startTime,
            duration: 5,
            tags: [str("game.id", gameId), str("game.kind", "real"), ...tags],
            logs: [],
            processID: "p1",
          },
        ],
      },
    ],
  } as unknown as JaegerTracesResponse;
}

const NAMED: Tag[] = [
  str("summary.goal.profile", "quick_game"),
  str("summary.goal.source", "named"),
  num("summary.goal.attainment", 0.62),
];

afterEach(() => vi.unstubAllGlobals());

describe("UC-042 A12: the game's goal profile, source and attainment", () => {
  it("UC-042/A12 reads summary.goal.* of the agent.game.summary span", () => {
    expect(parseGoalSummaries(summary(NAMED))).toMatchObject([
      { gameId: "game_abc", gameKind: "real", profile: "quick_game", source: "named", attainment: 0.62 },
    ]);
  });

  it("UC-042/A12 a derived profile reads from_objectives; no measured term reads attainment null", () => {
    const goals = parseGoalSummaries(
      summary([str("summary.goal.profile", "chaos_focused"), str("summary.goal.source", "from_objectives")]),
    );
    expect(goals[0]).toMatchObject({ source: "from_objectives", attainment: null });
  });

  it("UC-042/A12 a summary without a goal gives no goal", () => {
    expect(parseGoalSummaries(summary([]))).toEqual([]);
  });

  it("UC-042/A12 the strip reads 'goal quick_game (named profile) attainment 62%'", () => {
    const html = renderGoalStrip(parseGoalSummaries(summary(NAMED))[0]);
    expect(html).toContain("quick_game");
    expect(html).toContain("named profile");
    expect(html).toContain("attainment <b>62%</b>");
    const derived = renderGoalStrip(
      parseGoalSummaries(summary([str("summary.goal.profile", "chaos_focused"), str("summary.goal.source", "from_objectives")]))[0],
    );
    expect(derived).toContain("from the objectives");
    expect(derived).toContain("attainment n/a");
    expect(renderGoalStrip(null)).toBe("");
  });

  it("UC-042/A12 span-derived text is escaped", () => {
    const html = renderGoalStrip(parseGoalSummaries(summary([str("summary.goal.profile", "<img src=x>"), str("summary.goal.source", "named")]))[0]);
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  it("UC-042/A12 the feed carries the goal of the followed game only", () => {
    const store = new DecideStore();
    const decides = parseDecideTraces(structuredClone(actFixture) as unknown as JaegerTracesResponse);
    store.merge(decides.rows);
    const gameId = decides.current!.gameId;
    store.mergeGoals(parseGoalSummaries(summary(NAMED, "other_game")));
    expect(store.feed().goal).toBeNull();
    store.mergeGoals(parseGoalSummaries(summary(NAMED, gameId)));
    expect(store.feed().goal).toMatchObject({ gameId, profile: "quick_game" });
  });

  it("UC-042/A12 fetchGoal searches agent.game.summary by game.id", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return new Response(JSON.stringify(toOtlp(summary(NAMED) as never)), { status: 200 });
      }),
    );
    const { goals } = await fetchGoal("game_abc");
    const params = new URL(urls[0], "http://pi").searchParams;
    expect(params.get("query.operationName")).toBe(GAME_SUMMARY_OPERATION);
    expect(JSON.parse(params.get("query.attributes")!)).toEqual({ "game.id": "game_abc" });
    expect(goals).toHaveLength(1);
  });

  it("UC-042/A12 the goal poll searches the followed game, stops once its goal is known, restarts on a new game", async () => {
    const games = new Map<string, JaegerTracesResponse>();
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const params = new URL(String(input), "http://pi").searchParams;
        const id = (JSON.parse(params.get("query.attributes") ?? "{}") as Record<string, string>)["game.id"];
        requests.push(id);
        const body = games.get(id);
        if (!body) return new Response(JSON.stringify({ error: { httpCode: 404, message: "No traces found" } }), { status: 404 });
        return new Response(JSON.stringify(toOtlp(body as never)), { status: 200 });
      }),
    );
    const store = new DecideStore();
    const first = parseDecideTraces(structuredClone(actFixture) as unknown as JaegerTracesResponse);
    store.merge(first.rows);
    const gameA = first.current!.gameId;
    let rendered = 0;
    const run = createGoalRun(store, () => rendered++);

    expect(await run()).toBe(GOAL_POLL_MS); // game A has no summary yet
    expect(requests).toEqual([gameA]);
    expect(rendered).toBe(0);

    games.set(gameA, summary(NAMED, gameA));
    await run();
    expect(requests).toEqual([gameA, gameA]);
    expect(rendered).toBe(1);
    expect(store.feed().goal?.gameId).toBe(gameA);

    await run();
    await run();
    expect(requests).toHaveLength(2); // goal known: no further search

    // A newer game becomes the followed one: the poll starts again for it.
    const next = structuredClone(actFixture) as unknown as JaegerTracesResponse;
    for (const t of next.data!) for (const s of t.spans) {
      s.startTime += 3_600_000_000;
      for (const tag of s.tags ?? []) if (tag.key === "game.id") tag.value = "game_next";
    }
    store.merge(parseDecideTraces(next).rows);
    await run();
    expect(requests.at(-1)).toBe("game_next");
    expect(requests).toHaveLength(3);
  });
});
