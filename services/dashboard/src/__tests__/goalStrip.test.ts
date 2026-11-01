/**
 * Dashboard Agent tab: the game's goal profile, its source and its attainment
 * (#2479, #2498, UC-042 A12). The agent.game.summary span carries the
 * summary.goal.* attributes; the strip follows the followed game.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { DecideStore, GAME_SUMMARY_OPERATION, fetchGoals, parseDecideTraces, parseGoalSummaries } from "../agent.js";
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

  it("UC-042/A12 fetchGoals searches the agent.game.summary operation", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        return new Response(JSON.stringify(toOtlp(summary(NAMED) as never)), { status: 200 });
      }),
    );
    const goals = await fetchGoals();
    expect(new URL(urls[0], "http://pi").searchParams.get("query.operationName")).toBe(GAME_SUMMARY_OPERATION);
    expect(goals).toHaveLength(1);
  });
});
