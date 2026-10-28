/**
 * Background load of the dashboard (UC-035 BR-012, #2290): lazy iframes and a
 * gameplay stream that pauses on a hidden page.
 */
import { describe, expect, it } from "vitest";
import { keepControllerStreamOpen } from "../controllerStream.js";
import { EmbedGate, VisibilityGate } from "../visibilityGate.js";

class FakeDoc {
  hidden = false;
  private listeners: Array<() => void> = [];
  addEventListener(_t: "visibilitychange", l: () => void) {
    this.listeners.push(l);
  }
  setHidden(hidden: boolean) {
    this.hidden = hidden;
    for (const l of this.listeners) l();
  }
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("UC-035: BR-012 iframes load on first use of their tab", () => {
  function setup() {
    const writes: string[] = [];
    const frame = (name: string) => ({
      set src(v: string) {
        writes.push(`${name}=${v}`);
      },
    });
    const gate = new EmbedGate([
      { frame: frame("g"), tab: "metrics", src: "/grafana/x", unloadWhenAway: true },
      { frame: frame("j"), tab: "traces", src: "/jaeger/y", unloadWhenAway: false },
    ]);
    return { gate, writes };
  }

  it("UC-035/BR-012 sets no src at page load on the Controllers tab", () => {
    const { gate, writes } = setup();
    gate.sync("controllers", false);
    expect(writes).toEqual([]);
  });

  it("UC-035/BR-012 loads Grafana once on open, blanks it on leave and when the page hides", () => {
    const { gate, writes } = setup();
    gate.sync("metrics", false);
    gate.sync("metrics", false);
    expect(writes).toEqual(["g=/grafana/x"]);
    gate.sync("controllers", false);
    expect(writes).toEqual(["g=/grafana/x", "g=about:blank"]);
    gate.sync("metrics", false);
    gate.sync("metrics", true);
    expect(writes.slice(-2)).toEqual(["g=/grafana/x", "g=about:blank"]);
  });

  it("UC-035/BR-012 loads the Jaeger search once and keeps it", () => {
    const { gate, writes } = setup();
    gate.sync("traces", false);
    gate.sync("controllers", false);
    gate.sync("traces", false);
    expect(writes).toEqual(["j=/jaeger/y"]);
  });
});

describe("UC-035: BR-012 the gameplay stream pauses on a hidden page", () => {
  it("UC-035/BR-012 aborts the stream when hidden, shows no loss, and reopens on show", async () => {
    const doc = new FakeDoc();
    const gate = new VisibilityGate(doc);
    const opened: AbortSignal[] = [];
    const errors: string[] = [];
    const updates: number[] = [];
    void keepControllerStreamOpen<number>({
      gate,
      open: async function* (signal) {
        opened.push(signal!);
        yield 1;
        await new Promise((_, reject) =>
          signal!.addEventListener("abort", () => reject(new Error("aborted"))),
        );
      },
      onUpdate: (u) => updates.push(u),
      grid: { setLoading: () => {}, setError: (m) => errors.push(m) },
      setStreaming: () => {},
    });
    await tick();
    expect(opened).toHaveLength(1);
    expect(updates).toEqual([1]);

    doc.setHidden(true);
    await tick();
    expect(opened[0].aborted).toBe(true);
    expect(errors).toEqual([]);
    expect(opened).toHaveLength(1); // nothing reopens while hidden

    doc.setHidden(false);
    await tick();
    expect(opened).toHaveLength(2);
    expect(opened[1].aborted).toBe(false);
    expect(updates).toEqual([1, 1]);
  });

  it("UC-035/BR-012 does not open at all when the page starts hidden", async () => {
    const doc = new FakeDoc();
    doc.hidden = true;
    const gate = new VisibilityGate(doc);
    let opens = 0;
    void keepControllerStreamOpen<number>({
      gate,
      open: async function* () {
        opens++;
        yield 1;
      },
      onUpdate: () => {},
      grid: { setLoading: () => {}, setError: () => {} },
      setStreaming: () => {},
    });
    await tick();
    expect(opens).toBe(0);
    doc.setHidden(false);
    await tick();
    expect(opens).toBe(1);
  });
});
