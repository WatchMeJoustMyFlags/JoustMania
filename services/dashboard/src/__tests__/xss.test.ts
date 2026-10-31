// @vitest-environment happy-dom
/**
 * UC-035 (dashboard views): hostile text from the wire (model rationale, decide answers, flag
 * keys, experiment text, controller names) never becomes an element or breaks out of an
 * attribute, on the real views and a real DOM (#2388 part 3).
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { AgentView, renderDecideRow } from "../components/AgentView.js";
import { ControllerGrid } from "../components/ControllerGrid.js";
import { ExperimentsView } from "../components/ExperimentsView.js";
import { GameStatus } from "../components/GameStatus.js";
import type { DecideRow, DecisionFeedRow } from "../agent.js";
import { parseDecideTraces } from "../agent.js";
import type { ExperimentView } from "../experiments.js";
import actFixture from "./fixtures/jaeger-decide-remote-act.json";

const IMG = "<img src=x onerror=alert(1)>";
const EVIL_ATTR = `" onmouseover="alert(1)' x='`;

/** No element a hostile string could have created, and no injected handler attribute. */
function expectInert(root: ParentNode) {
  expect(root.querySelector("img, script, iframe")).toBeNull();
  for (const el of root.querySelectorAll("*")) {
    for (const attr of el.getAttributeNames()) expect(attr.startsWith("on"), `${el.tagName}[${attr}]`).toBe(false);
  }
}

const baseRow = (): DecideRow => parseDecideTraces(actFixture as never).rows[0];

describe("UC-035: XSS in the agent view (rationale, decide answers)", () => {
  let view: AgentView;
  beforeEach(() => {
    document.body.innerHTML = '<div id="f"></div><div id="g"></div><div id="o"></div><div id="s"></div><div id="d"></div>';
    view = new AgentView("f", "g", "o", undefined, { strip: "s", feed: "d" });
  });

  it("UC-035/main step 3 a hostile rationale is text in the row and intact in its title attribute", () => {
    view.renderDecide({ current: null, rows: [{ ...baseRow(), rationale: IMG + EVIL_ATTR }] } as never);
    const feed = document.getElementById("d")!;
    expectInert(feed);
    expect(feed.querySelector(".decide-why")?.getAttribute("title")).toBe(IMG + EVIL_ATTR);
    expect(feed.querySelector(".decide-why")?.textContent).toContain(IMG);
  });

  it("UC-035/main step 3 hostile decide answers, models and backends stay text (row and strip)", () => {
    const row: DecideRow = {
      ...baseRow(),
      intervention: IMG,
      target: IMG,
      model: IMG,
      backend: EVIL_ATTR,
      requested: IMG,
      fallbackReason: IMG,
      errorType: EVIL_ATTR,
      outcomeReason: IMG,
      decisions: [
        { spanId: "z", tsMs: 0, action: IMG, blocked: false, reason: EVIL_ATTR, blockReason: "", applied: true, notAppliedReason: "", targetSerial: EVIL_ATTR, targetName: IMG },
      ],
    };
    const current = { tsMs: 0, backend: IMG, requested: EVIL_ATTR, model: IMG, threshold: 0.5, thresholdFromOlder: false, gameId: IMG, gameKind: EVIL_ATTR };
    view.renderDecide({ current, rows: [row] } as never);
    expectInert(document.getElementById("d")!);
    expectInert(document.getElementById("s")!);
    expect(document.getElementById("d")!.textContent).toContain(IMG);
  });

  it("UC-035/main step 3 the activity feed, flag panel and overlay escape span-derived text", () => {
    const feedRow: DecisionFeedRow = {
      tsMs: 0, kind: "blocked", msg: IMG, signal: IMG, action: IMG, blocked: true, reason: EVIL_ATTR,
      sessionId: EVIL_ATTR, target: IMG, traceId: "t", spanId: "s",
    };
    view.render({
      feed: [feedRow],
      flags: { gameId: IMG, gameKind: EVIL_ATTR, traceId: EVIL_ATTR, spanId: "s", trigger: IMG, tsMs: 0, enabled: true, interventionsEnabled: true, decisionBackend: IMG, objectives: EVIL_ATTR, actThreshold: null } as never,
      overlay: { appliedOk: 1, appliedError: 0, shadowAppliedOk: 0, shadowAppliedError: 0, shadowBlocked: 0, totalBlocked: 1, blockedByAction: [{ action: IMG, blocked: 1 }], blockedByReason: [{ reason: EVIL_ATTR, count: 1 }] },
    });
    for (const id of ["f", "g", "o"]) expectInert(document.getElementById(id)!);
    expect(document.getElementById("f")!.textContent).toContain(IMG);
  });

  it("UC-035/A3 an error message from the wire is text", () => {
    view.setError(IMG);
    view.setJaegerError(IMG);
    view.setDecideError(IMG);
    for (const id of ["f", "g", "o", "d"]) expectInert(document.getElementById(id)!);
    expect(document.getElementById("d")!.textContent).toBe(IMG);
  });

  it("UC-035/main step 3 the row keeps its confidence bar as markup, not as escaped text", () => {
    const html = renderDecideRow(baseRow());
    expect(html).toContain('<div class="decide-conf ');
    expect(html).not.toContain("&lt;div");
    view.renderDecide({ current: null, rows: [baseRow()] } as never);
    expect(document.querySelector("#d .decide-conf .decide-conf-track")).not.toBeNull();
  });
});

describe("UC-035: XSS in the experiments view", () => {
  it("UC-035/BR-013 experiment ids, flag keys, thesis, reasons and arms stay text", () => {
    document.body.innerHTML = '<div id="x"></div>';
    const exp = {
      experimentId: IMG + EVIL_ATTR, verdict: IMG, live: false, status: IMG,
      arms: [{ arm: IMG, sampleCount: 1, meanFitness: 0.5, inFlight: 1 }],
      delta: 0.1, flagKey: IMG, experimentalValue: EVIL_ATTR, objective: IMG, thesis: IMG + EVIL_ATTR,
      reason: IMG + EVIL_ATTR, recommendation: { flag: IMG, value: EVIL_ATTR, owner: IMG }, traceId: EVIL_ATTR,
    } as unknown as ExperimentView;
    const view = new ExperimentsView("x");
    view.render([exp]);
    expectInert(document.body);
    view.setError(IMG);
    expectInert(document.body);
  });
});

describe("UC-004: XSS in the controller grid (names) and game status", () => {
  it("UC-004/BR-007 a hostile controller name and a hostile error are text", () => {
    document.body.innerHTML = '<div id="grid"></div>';
    const grid = new ControllerGrid("grid");
    grid.render([{ serial: "AA", name: IMG } as never]);
    expectInert(document.body);
    expect(document.querySelector(".controller-name")?.textContent).toBe(IMG);
    grid.setError(IMG);
    expectInert(document.body);
  });

  it("UC-035/main step 2 a hostile game state is text", () => {
    document.body.innerHTML = '<div id="game-state"></div><div id="player-count"></div><div id="alive-count"></div><div id="event-log"></div>';
    new GameStatus().updateState(IMG);
    expectInert(document.body);
  });
});

describe("UC-035: markup reaches the DOM only through the html tag", () => {
  const walk = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return name === "__tests__" || name === "gen" ? [] : walk(path);
      return path.endsWith(".ts") ? [path] : [];
    });

  it("UC-035/BR-013 no source file assigns innerHTML except setHtml in html.ts", () => {
    const offenders = walk(join(import.meta.dirname, ".."))
      .filter((p) => !p.endsWith("html.ts"))
      .filter((p) => /\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML/.test(readFileSync(p, "utf8")));
    expect(offenders).toEqual([]);
  });
});
