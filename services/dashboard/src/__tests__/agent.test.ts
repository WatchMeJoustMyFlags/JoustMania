/**
 * Unit tests for the pure parse + fold in agent.ts (issue #792).
 *
 * Exercise aggregateAgentLogs (decision feed + flag panel + Loki-derived blocked
 * tallies) and parseAppliedCounts (VictoriaMetrics applied count) in isolation —
 * no Loki / VM / DOM. Covers the agent.* log shapes the agent really emits
 * (services/agent/decision/decision.go, async_apply.go):
 *   - agent.evaluate feeds the flag panel (newest wins) and never floods the feed
 *   - agent.decision_blocked folds into blocked-by-action and blocked-by-reason
 *   - agent.llm.discarded and agent.rollout_* appear in the feed but don't count
 *     as blocked
 *   - the feed is newest-first
 *   - VM applied-count summing across namespaced jobs, and the empty/unavailable
 *     path returning nulls
 */
import { describe, it, expect } from "vitest";
import { aggregateAgentLogs, parseAppliedCounts } from "../agent.js";
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
        mode: "rules",
        model: "none",
        prompt_variant: "v1",
        battery_threshold: "20",
        variance_window: "5",
        max_per_minute: "12",
        game_mode: "JoustFFA",
      }),
      line({
        msg: "agent.evaluate",
        session_id: "game_aaa",
        mode: "llm",
        model: "claude",
        prompt_variant: "v2",
        battery_threshold: "30",
        variance_window: "8",
        max_per_minute: "20",
        game_mode: "Zombies",
      }),
    ]);
    expect(out.flags).not.toBeNull();
    // Newest (second) line wins.
    expect(out.flags?.mode).toBe("llm");
    expect(out.flags?.model).toBe("claude");
    expect(out.flags?.maxPerMinute).toBe("20");
    expect(out.flags?.gameMode).toBe("Zombies");
  });

  it("newest wins regardless of input order", () => {
    const newer = line({ msg: "agent.evaluate", mode: "llm" }, 2000);
    const older = line({ msg: "agent.evaluate", mode: "rules" }, 1000);
    // Feed older AFTER newer to prove it's ts-driven, not order-driven.
    expect(aggregateAgentLogs([newer, older]).flags?.mode).toBe("llm");
  });

  it("agent.evaluate never appears in the decision feed", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.evaluate", mode: "rules" }),
      line({ msg: "agent.evaluate", mode: "rules" }),
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

  it("discarded and rollout lines are NOT counted as blocked", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.llm.discarded", session_id: "game_a", reason: "timeout" }),
      line({ msg: "agent.rollout_expand", reason: "stable" }),
    ]);
    expect(out.overlay.totalBlocked).toBe(0);
    expect(out.feed).toHaveLength(2);
  });
});

describe("aggregateAgentLogs — decision feed", () => {
  it("includes blocked / discarded / rollout rows with the right kind", () => {
    const out = aggregateAgentLogs([
      line({ msg: "agent.decision_blocked", intervention: "grant_shield", reason: "rate_limit", session_id: "game_z" }),
      line({ msg: "agent.llm.discarded", reason: "stale_context" }),
      line({ msg: "agent.rollout_rollback", reason: "regression" }),
    ]);
    const kinds = out.feed.map((r) => r.kind).sort();
    expect(kinds).toEqual(["blocked", "discarded", "rollout"]);
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
      line({ msg: "agent.llm.prompt_captured" }),
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
