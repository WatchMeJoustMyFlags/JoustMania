/**
 * Unit tests for the pure parse + fold in experiments.ts (issue #1047).
 *
 * These exercise the logfmt parser and aggregateLogLines fold in isolation — no
 * Loki / DOM — covering the cases called out in the #1045 review:
 *   - a quoted reason="..." with embedded =, spaces, and parens
 *   - the promote / discard / aborted / inconclusive verdict matrix
 *   - in-flight = assigned − concluded
 *   - delta requires BOTH named arms to have samples
 *   - terminal-verdict precedence (concluded.outcome beats torn_down.status),
 *     independent of log-line order
 */
import { describe, it, expect } from "vitest";
import { parseLogfmt, aggregateLogLines } from "../experiments.js";

/** Build a synthetic slog TextHandler line from key→value pairs. */
function logfmt(fields: Record<string, string>): string {
  return Object.entries(fields)
    .map(([k, v]) => (/[\s"]/.test(v) ? `${k}=${JSON.stringify(v)}` : `${k}=${v}`))
    .join(" ");
}

let ts = 1_700_000_000_000;
/** Monotonically increasing timestamp so ordering is explicit per call. */
function nextTs(): number {
  ts += 1000;
  return ts;
}

function line(fields: Record<string, string>) {
  return { line: logfmt({ level: "INFO", ...fields }), tsMs: nextTs() };
}

describe("parseLogfmt", () => {
  it("parses bare key=value pairs", () => {
    const out = parseLogfmt(
      "time=2026-06-14T00:00:00Z level=INFO msg=experiment.game_concluded experiment_id=exp_a1b2 arm=control fitness=0.42",
    );
    expect(out["msg"]).toBe("experiment.game_concluded");
    expect(out["experiment_id"]).toBe("exp_a1b2");
    expect(out["arm"]).toBe("control");
    expect(out["fitness"]).toBe("0.42");
  });

  it("parses a quoted reason with embedded =, spaces, and parens", () => {
    const out = parseLogfmt(
      'msg=experiment.torn_down experiment_id=exp_x status=done reason="paired: d_z=0.81 (pairs=6)"',
    );
    expect(out["status"]).toBe("done");
    expect(out["reason"]).toBe("paired: d_z=0.81 (pairs=6)");
  });

  it("unescapes embedded quotes and backslashes in quoted values", () => {
    const out = parseLogfmt('msg=experiment.torn_down experiment_id=e reason="a \\"q\\" b\\\\c"');
    expect(out["reason"]).toBe('a "q" b\\c');
  });

  it("tolerates a container-log prefix before the key=value tokens", () => {
    const out = parseLogfmt(
      "2026-06-14T00:00:00Z stdout F msg=experiment.concluded experiment_id=exp_p outcome=promote",
    );
    expect(out["msg"]).toBe("experiment.concluded");
    expect(out["outcome"]).toBe("promote");
  });
});

describe("aggregateLogLines — counts, means, in-flight, delta", () => {
  it("ignores non-experiment lines and lines missing msg/experiment_id", () => {
    const result = aggregateLogLines([
      { line: "msg=some.other.event foo=bar", tsMs: nextTs() },
      { line: "level=INFO no_msg_here=1", tsMs: nextTs() },
      line({ msg: "experiment.game_assigned" }), // no experiment_id → ignored
    ]);
    expect(result).toHaveLength(0);
  });

  it("folds sample counts and mean fitness per arm", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "control" }),
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "control", fitness: "0.2" }),
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "control", fitness: "0.4" }),
    ]);
    expect(result).toHaveLength(1);
    const control = result[0].arms.find((a) => a.arm === "control")!;
    expect(control.sampleCount).toBe(2);
    expect(control.meanFitness).toBeCloseTo(0.3, 9);
    expect(control.assignedCount).toBe(1);
  });

  it("computes in-flight as assigned − concluded (clamped at 0)", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "experimental" }),
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "experimental" }),
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "experimental" }),
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "experimental", fitness: "0.5" }),
    ]);
    const exp = result[0].arms.find((a) => a.arm === "experimental")!;
    expect(exp.assignedCount).toBe(3);
    expect(exp.sampleCount).toBe(1);
    // The view renders Math.max(0, assigned - sample); the aggregate carries the
    // raw counts. Verify the inputs the view subtracts.
    expect(exp.assignedCount - exp.sampleCount).toBe(2);
  });

  it("ignores non-finite fitness values (no sample recorded)", () => {
    const result = aggregateLogLines([
      // An assignment establishes the arm; a NaN-fitness conclusion must not
      // count as a sample.
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "control" }),
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "control", fitness: "not-a-number" }),
    ]);
    const control = result[0].arms.find((a) => a.arm === "control")!;
    expect(control.assignedCount).toBe(1);
    expect(control.sampleCount).toBe(0);
    expect(control.meanFitness).toBe(0);
  });

  it("computes delta only when BOTH named arms have samples", () => {
    // Only experimental has a sample → delta stays null.
    const onlyOne = aggregateLogLines([
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "experimental", fitness: "0.9" }),
    ]);
    expect(onlyOne[0].delta).toBeNull();

    // Both arms have samples → delta = experimental_mean − control_mean.
    const both = aggregateLogLines([
      line({ msg: "experiment.game_concluded", experiment_id: "e2", arm: "experimental", fitness: "0.8" }),
      line({ msg: "experiment.game_concluded", experiment_id: "e2", arm: "experimental", fitness: "0.6" }), // mean 0.7
      line({ msg: "experiment.game_concluded", experiment_id: "e2", arm: "control", fitness: "0.4" }), // mean 0.4
    ]);
    expect(both[0].delta).toBeCloseTo(0.3, 9);
  });

  it("does not compute delta from a non-canonical arm pair", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "armA", fitness: "0.8" }),
      line({ msg: "experiment.game_concluded", experiment_id: "e1", arm: "armB", fitness: "0.4" }),
    ]);
    expect(result[0].delta).toBeNull();
  });

  it("orders arms experimental, control, then others", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "zzz" }),
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "control" }),
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "experimental" }),
    ]);
    expect(result[0].arms.map((a) => a.arm)).toEqual(["experimental", "control", "zzz"]);
  });
});

describe("aggregateLogLines — verdict matrix", () => {
  const cases: { outcome?: string; status?: string; expected: string }[] = [
    { outcome: "promote", expected: "promote" },
    { outcome: "discard", expected: "discard" },
    { outcome: "inconclusive", expected: "inconclusive" },
    { status: "done", expected: "done" },
    { status: "discarded", expected: "discarded" },
    { status: "aborted", expected: "aborted" },
  ];

  for (const c of cases) {
    const label = c.outcome ? `concluded outcome=${c.outcome}` : `torn_down status=${c.status}`;
    it(`maps ${label} → verdict "${c.expected}", live=false`, () => {
      const lines = [
        line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "control" }),
      ];
      if (c.outcome) {
        lines.push(line({ msg: "experiment.concluded", experiment_id: "e1", outcome: c.outcome }));
      }
      if (c.status) {
        lines.push(line({ msg: "experiment.torn_down", experiment_id: "e1", status: c.status }));
      }
      const result = aggregateLogLines(lines);
      expect(result[0].verdict).toBe(c.expected);
      expect(result[0].live).toBe(false);
    });
  }

  it('a still-running experiment has verdict "running" and live=true', () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.game_assigned", experiment_id: "e1", arm: "control" }),
    ]);
    expect(result[0].verdict).toBe("running");
    expect(result[0].live).toBe(true);
  });

  it("carries the teardown reason through", () => {
    const result = aggregateLogLines([
      line({
        msg: "experiment.torn_down",
        experiment_id: "e1",
        status: "done",
        reason: "paired: d_z=0.81 (pairs=6)",
      }),
    ]);
    expect(result[0].reason).toBe("paired: d_z=0.81 (pairs=6)");
  });
});

describe("aggregateLogLines — terminal verdict precedence (#1047 nit 1)", () => {
  // Both terminal lines fire; concluded.outcome must win regardless of ORDER.
  it("prefers concluded.outcome over torn_down.status when concluded comes FIRST", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.concluded", experiment_id: "e1", outcome: "promote" }),
      line({ msg: "experiment.torn_down", experiment_id: "e1", status: "done", reason: "cleanup" }),
    ]);
    expect(result[0].verdict).toBe("promote");
    expect(result[0].reason).toBe("cleanup");
    expect(result[0].live).toBe(false);
  });

  it("prefers concluded.outcome over torn_down.status when torn_down comes FIRST", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.torn_down", experiment_id: "e1", status: "done", reason: "cleanup" }),
      line({ msg: "experiment.concluded", experiment_id: "e1", outcome: "discard" }),
    ]);
    expect(result[0].verdict).toBe("discard");
  });

  it("falls back to torn_down.status when there is NO conclusion (aborted)", () => {
    const result = aggregateLogLines([
      line({ msg: "experiment.torn_down", experiment_id: "e1", status: "aborted", reason: "agent restart" }),
    ]);
    expect(result[0].verdict).toBe("aborted");
    expect(result[0].live).toBe(false);
  });
});
