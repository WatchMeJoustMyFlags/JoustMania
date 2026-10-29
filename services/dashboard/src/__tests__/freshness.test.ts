// @vitest-environment happy-dom
/**
 * UC-035 (#2388): the "updated Ns ago / stale" hint keeps each tab's own threshold
 * after the shared renderFreshness: Agent 12 s, Experiments 15 s. Just below is
 * fresh, at the threshold is stale; changing one STALE_AFTER_MS alone fails here.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentView } from "../components/AgentView.js";
import { ExperimentsView } from "../components/ExperimentsView.js";
import type { InterventionOverlay } from "../agentOverlay.js";

const T0 = 1_000_000;
let now = T0;

function el(id: string): HTMLElement {
  const e = document.createElement("div");
  e.id = id;
  document.body.appendChild(e);
  return e;
}

beforeEach(() => {
  document.body.innerHTML = "";
  now = T0;
  vi.spyOn(performance, "now").mockImplementation(() => now);
});
afterEach(() => {
  vi.restoreAllMocks();
});

const overlay = {
  blockedByAction: [],
  blockedByReason: [],
  totalBlocked: null,
  shadowBlocked: null,
  appliedOk: null,
  appliedError: null,
  shadowAppliedOk: null,
  shadowAppliedError: null,
} as unknown as InterventionOverlay;

describe("UC-035: tab freshness thresholds (#2388)", () => {
  it("UC-035 AgentView main hint goes stale at 12 s, not before", () => {
    el("feed"); el("flags"); el("overlay");
    const hint = el("fresh");
    const view = new AgentView("feed", "flags", "overlay", "fresh");
    view.render({ feed: [], flags: null, overlay });
    now = T0 + 11_999;
    view.updateFreshness();
    expect(hint.classList.contains("stale")).toBe(false);
    now = T0 + 12_000;
    view.updateFreshness();
    expect(hint.classList.contains("stale")).toBe(true);
    expect(hint.textContent).toContain("stale");
  });

  it("UC-035 AgentView decide hint goes stale at 12 s, not before", () => {
    el("feed"); el("flags"); el("overlay"); el("strip"); el("dfeed");
    const hint = el("dfresh");
    const view = new AgentView("feed", "flags", "overlay", undefined, { strip: "strip", feed: "dfeed", freshness: "dfresh" });
    view.renderDecide({ rows: [], current: null });
    now = T0 + 11_999;
    view.updateDecideFreshness();
    expect(hint.classList.contains("stale")).toBe(false);
    now = T0 + 12_000;
    view.updateDecideFreshness();
    expect(hint.classList.contains("stale")).toBe(true);
  });

  it("UC-035 ExperimentsView hint goes stale at 15 s, not before", () => {
    el("exps");
    const hint = el("efresh");
    const view = new ExperimentsView("exps", "efresh");
    view.render([]);
    now = T0 + 14_999;
    view.updateFreshness();
    expect(hint.classList.contains("stale")).toBe(false);
    now = T0 + 15_000;
    view.updateFreshness();
    expect(hint.classList.contains("stale")).toBe(true);
  });
});
