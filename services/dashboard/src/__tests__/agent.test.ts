/**
 * Unit tests for the pure parse + fold in agent.ts (issue #792).
 *
 * Exercise aggregateAgentLogs (decision feed + flag panel + Loki-derived blocked
 * tallies) and parseAppliedCounts (VictoriaMetrics applied count) in isolation —
 * no Loki / VM / DOM. Covers the agent.* log shapes the agent really emits
 * (services/agent/decision/decision.go):
 *   - agent.evaluate feeds the flag panel (newest wins) and never floods the feed
 *   - agent.decision_blocked folds into blocked-by-action and blocked-by-reason
 *   - agent.rollout_* appears in the feed but doesn't count as blocked
 *   - the feed is newest-first
 *   - VM applied-count summing across namespaced jobs, and the empty/unavailable
 *     path returning nulls
 */
import { describe, it, expect } from "vitest";
import { aggregateAgentLogs, parseAppliedCounts, parseBlockedCounts } from "../agent.js";
import type { VmQueryResponse } from "../agent.js";

/** Build a synthetic slog TextHandler line from key→value pairs. */
function logfmt(fields: Record<string, string>): string {
  return Object.entries(fields)
    .map(([k, v]) => (/[\s"]/.test(v) ? `${k}=${JSON.stringify(v)}` : `${k}=${v}`))
    .join(" ");
}

let ts = 1_700_000_000_000;
function nextTs(): number {
  ts += 1000;
  return ts;
}

function line(fields: Record<string, string>, tsMs = nextTs()) {
  return { line: logfmt({ level: "INFO", ...fields }), tsMs };
}

describe("aggregateAgentLogs — flag panel", () => {
  it("takes the resolved values from the newest agent.evaluate line", () => {
    const out = aggregateAgentLogs([
      line({
        msg: "agent.evaluate",
        session_id: "game_aaa",
        decision_backend: "rules",
        battery_threshold: "20",
        variance_window: "5",
        max_per_minute: "12",
        game_mode: "JoustFFA",
      }),
      line({
        msg: "agent.evaluate",
        session_id: "game_aaa",
        decision_backend: "claude",
        battery_threshold: "30",
        variance_window: "8",
        max_per_minute: "20",
        game_mode: "Zombies",
      }),
    ]);
    expect(out.flags).not.toBeNull();
    // Newest (second) line wins.
    expect(out.flags?.decisionBackend).toBe("claude");
    expect(out.flags?.maxPerMinute).toBe("20");
    expect(out.flags?.gameMode).toBe("Zombies");
  });

  it("newest wins regardless of input order", () => {
    const newer = line({ msg: "agent.evaluate", decision_backend: "claude" }, 2000);
    const older = line({ msg: "agent.evaluate", decision_backend: "rules" }, 1000);
    // Feed older AFTER newer to prove it's ts-driven, not order-driven.
    expect(aggregateAgentLogs([newer, older]).flags?.decisionBackend).toBe("claude");
  });

  it("agent.evaluate never appears in the decision feed", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.evaluate", decision_backend: "rules" }),
      line({ msg: "agent.evaluate", decision_backend: "rules" }),
    ]);
    expect(out.feed).toHaveLength(0);
    expect(out.flags).not.toBeNull();
  });

  it("is null when no agent.evaluate line is present", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.decision_blocked", intervention: "grant_shield", reason: "rate_limit" }),
    ]);
    expect(out.flags).toBeNull();
  });
});

describe("aggregateAgentLogs — blocked tallies", () => {
  it("folds blocked decisions by action and by reason", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.decision_blocked", intervention: "grant_shield", reason: "rate_limit" }),
      line({ msg: "agent.decision_blocked", intervention: "grant_shield", reason: "battery_threshold" }),
      line({ msg: "agent.decision_blocked", intervention: "raise_demand", reason: "rate_limit" }),
    ]);
    expect(out.overlay.totalBlocked).toBe(3);
    // Most-blocked action first.
    expect(out.overlay.blockedByAction[0]).toEqual({ action: "grant_shield", blocked: 2 });
    expect(out.overlay.blockedByAction).toContainEqual({ action: "raise_demand", blocked: 1 });
    // Reasons descending: rate_limit (2) before battery_threshold (1).
    expect(out.overlay.blockedByReason[0]).toEqual({ reason: "rate_limit", count: 2 });
  });

  it("is order-independent (additive)", () => {
    const a = line({ msg: "agent.decision_blocked", intervention: "x", reason: "r1" }, 1000);
    const b = line({ msg: "agent.decision_blocked", intervention: "x", reason: "r1" }, 2000);
    const fwd = aggregateAgentLogs([a, b]);
    const rev = aggregateAgentLogs([b, a]);
    expect(fwd.overlay.totalBlocked).toBe(rev.overlay.totalBlocked);
    expect(fwd.overlay.blockedByAction).toEqual(rev.overlay.blockedByAction);
  });

  it("rollout lines are NOT counted as blocked", () => {
    const out = aggregateAgentLogs([line({ msg: "agent.rollout_expand", reason: "stable" })]);
    expect(out.overlay.totalBlocked).toBe(0);
    expect(out.feed).toHaveLength(1);
  });
});

describe("aggregateAgentLogs — decision feed", () => {
  it("includes blocked / rollout rows with the right kind", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.decision_blocked", intervention: "grant_shield", reason: "rate_limit", session_id: "game_z" }),
      line({ msg: "agent.rollout_rollback", reason: "regression" }),
    ]);
    const kinds = out.feed.map((r) => r.kind).sort();
    expect(kinds).toEqual(["blocked", "rollout"]);
    const blocked = out.feed.find((r) => r.kind === "blocked");
    expect(blocked?.blocked).toBe(true);
    expect(blocked?.action).toBe("grant_shield");
    expect(blocked?.reason).toBe("rate_limit");
    expect(blocked?.sessionId).toBe("game_z");
  });

  it("is sorted newest-first", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.decision_blocked", intervention: "a", reason: "r" }, 1000),
      line({ msg: "agent.decision_blocked", intervention: "b", reason: "r" }, 3000),
      line({ msg: "agent.decision_blocked", intervention: "c", reason: "r" }, 2000),
    ]);
    expect(out.feed.map((r) => r.tsMs)).toEqual([3000, 2000, 1000]);
  });

  it("ignores unrelated agent.* messages", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.game.summary_written", session_id: "game_a" }),
      line({ msg: "agent.llm.retro_captured" }),
    ]);
    expect(out.feed).toHaveLength(0);
    expect(out.overlay.totalBlocked).toBe(0);
  });
});

describe("parseAppliedCounts", () => {
  it("sums result series across namespaced jobs", () => {
    const body: VmQueryResponse = {
      status: "success",
      data: {
        resultType: "vector",
        result: [
          { metric: { result: "ok", job: "infrastructure/agent-service" }, value: [1700, "7"] as [number, string] },
          { metric: { result: "ok", job: "other/agent" }, value: [1700, "3"] as [number, string] },
          { metric: { result: "error" }, value: [1700, "2"] as [number, string] },
        ],
      },
    };
    expect(parseAppliedCounts(body)).toEqual({ appliedOk: 10, appliedError: 2 });
  });

  it("returns nulls when VM has no samples", () => {
    expect(parseAppliedCounts({ status: "success", data: { resultType: "vector", result: [] } }))
      .toEqual({ appliedOk: null, appliedError: null });
  });

  it("returns nulls on a malformed body", () => {
    expect(parseAppliedCounts({ status: "error" })).toEqual({ appliedOk: null, appliedError: null });
  });

  it("skips non-finite values", () => {
    const body: VmQueryResponse = {
      status: "success",
      data: {
        resultType: "vector",
        result: [{ metric: { result: "ok" }, value: [1700, "NaN"] as [number, string] }],
      },
    };
    // The one ok series had a non-finite value, so ok stays null.
    expect(parseAppliedCounts(body)).toEqual({ appliedOk: null, appliedError: null });
  });
});

describe("UC-040 A21 parseBlockedCounts (blocked tallies from agent_decisions_total)", () => {
  const vm = (series: [Record<string, string>, string][]): VmQueryResponse => ({
    status: "success",
    data: {
      resultType: "vector",
      result: series.map(([metric, v]) => ({ metric, value: [1700, v] as [number, string] })),
    },
  });

  it("counts repeated rate_limit refusals the Loki lines no longer carry", () => {
    const out = parseBlockedCounts(vm([
      [{ action: "grant_shield", block_reason: "rate_limit" }, "48.6"],
      [{ action: "send_controller_effect", block_reason: "rate_limit" }, "12"],
      [{ action: "end_game", block_reason: "not_allowed" }, "1"],
    ]));
    expect(out).toEqual({
      blockedByAction: [
        { action: "grant_shield", blocked: 49 },
        { action: "send_controller_effect", blocked: 12 },
        { action: "end_game", blocked: 1 },
      ],
      blockedByReason: [
        { reason: "rate_limit", count: 61 },
        { reason: "not_allowed", count: 1 },
      ],
      totalBlocked: 62,
    });
  });

  it("drops zero and non-finite series", () => {
    const out = parseBlockedCounts(vm([
      [{ action: "grant_shield", block_reason: "rate_limit" }, "0.2"],
      [{ action: "play_audio_cue", block_reason: "rate_limit" }, "NaN"],
    ]));
    expect(out).toEqual({ blockedByAction: [], blockedByReason: [], totalBlocked: 0 });
  });

  it("returns null without samples so the Loki fold is kept", () => {
    expect(parseBlockedCounts(vm([]))).toBeNull();
    expect(parseBlockedCounts({ status: "error" })).toBeNull();
  });
});
