/**
 * The Agent and Experiments pollers run only while their tab is shown and the
 * page is visible (#2059; UC-035 main steps 3 and 4).
 */
import { describe, expect, it } from "vitest";
import { PageGate } from "../pageGate.js";

class FakeTarget {
  hidden = false;
  private readonly listeners = new Map<string, Array<() => void>>();
  addEventListener(type: string, listener: () => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  fire(type: string): void {
    for (const l of this.listeners.get(type) ?? []) l();
  }
}

function fakePoller(gate: () => PageGate, tab: string) {
  const calls: string[] = [];
  return {
    calls,
    stop: () => calls.push("stop"),
    // A real poller polls only when its tab is active (poller.ts).
    poke: () => calls.push(gate().isActive(tab) ? "poll" : "skip"),
  };
}

function setup(initialTab = "agent") {
  const doc = new FakeTarget();
  const win = new FakeTarget();
  let gate: PageGate | null = null;
  const poller = fakePoller(() => gate!, "agent");
  gate = new PageGate(doc, win, initialTab, () => [poller, null]);
  return { doc, win, gate, poller };
}

describe("UC-035: main step 3, pollers run only for the shown tab of a visible page", () => {
  it("UC-035/main step 3 only the shown tab is active", () => {
    const { gate } = setup("agent");
    expect(gate.isActive("agent")).toBe(true);
    expect(gate.isActive("experiments")).toBe(false);
    gate.activeTab = "experiments";
    expect(gate.isActive("agent")).toBe(false);
    expect(gate.isActive("experiments")).toBe(true);
  });

  it("UC-035/main step 3 a hidden page pauses the pollers; visible again resumes them", () => {
    const { doc, gate, poller } = setup();
    doc.hidden = true;
    doc.fire("visibilitychange");
    expect(gate.isActive("agent")).toBe(false);
    expect(poller.calls).toEqual(["stop"]);
    doc.hidden = false;
    doc.fire("visibilitychange");
    expect(poller.calls).toEqual(["stop", "poll"]);
  });

  it("UC-035/main step 3 a page in the back/forward cache polls nothing; pageshow resumes at once", () => {
    const { win, gate, poller } = setup();
    win.fire("pagehide");
    expect(gate.isActive("agent")).toBe(false);
    expect(poller.calls).toEqual(["stop"]);
    win.fire("pageshow");
    // pageShown flips before the resume, so the poke polls.
    expect(poller.calls).toEqual(["stop", "poll"]);
    expect(gate.isActive("agent")).toBe(true);
  });
});
