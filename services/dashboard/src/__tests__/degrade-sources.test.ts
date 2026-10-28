/**
 * UC-035 A3 and A4 clauses that depend on which source a panel reads (#1725):
 * Jaeger down vs no data (#2059), VictoriaMetrics down, Grafana down, and the
 * game event stream's silent loss.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchDecideFeed, fetchFlagStates, fetchInterventionCounts, fetchRollouts, RolloutStore } from "../agent.js";
import { fetchExperiments } from "../experiments.js";
import { JaegerUnavailableError } from "../jaeger.js";
import { keepGameEventStreamOpen } from "../stream.js";
import { ShieldStore, shieldBadge } from "../shield.js";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A fetch stub that answers per backend path; a backend in `down` fails. */
function stubBackends(down: { vm?: boolean; grafana?: boolean; jaeger?: "reject" | "503" | "errors" }) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("/grafana")) {
      if (down.grafana) throw new TypeError("Failed to fetch");
      return json(200, {});
    }
    if (url.includes("/victoria-metrics/")) {
      if (down.vm) return json(503, { error: "unreachable" });
      return json(200, { status: "success", data: { result: [] } });
    }
    if (url.includes("/jaeger/")) {
      if (down.jaeger === "reject") throw new TypeError("Failed to fetch");
      if (down.jaeger === "503") return new Response("no healthy upstream", { status: 503 });
      if (down.jaeger === "errors") return json(200, { data: null, errors: [{ code: 500, msg: "badger: closed" }] });
      return json(200, { data: [] });
    }
    if (url.includes("/loki/")) throw new Error(`the dashboard must not read Loki: ${url}`);
    throw new Error(`unexpected URL ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("UC-035: A3 VictoriaMetrics down", () => {
  it("UC-035/A3 with VictoriaMetrics down Applied and Blocked are both n/a", async () => {
    stubBackends({ vm: true });

    const overlay = await fetchInterventionCounts();

    expect(overlay.totalBlocked).toBeNull();
    expect(overlay.blockedByAction).toEqual([]);
    expect(overlay.blockedByReason).toEqual([]);
    expect(overlay.appliedOk).toBeNull();
    expect(overlay.appliedError).toBeNull();
    expect(overlay.shadowAppliedOk).toBeNull();
    expect(overlay.shadowAppliedError).toBeNull();
    expect(overlay.shadowBlocked).toBeNull();
  });
});

describe("UC-040: A21 VictoriaMetrics unreachable", () => {
  it("UC-040/A21 a failed VictoriaMetrics fetch shows the blocked tallies as n/a", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/victoria-metrics/")) throw new TypeError("Failed to fetch");
        throw new Error("no other backend is read for the tallies");
      }),
    );

    const overlay = await fetchInterventionCounts();

    expect(overlay.totalBlocked).toBeNull();
  });
});

describe("UC-035: A3 Jaeger down vs no data", () => {
  for (const mode of ["reject", "503", "errors"] as const) {
    it(`UC-035/A3 Jaeger down (${mode}) fails every agent and experiment search`, async () => {
      stubBackends({ jaeger: mode });
      await expect(fetchDecideFeed(null)).rejects.toBeInstanceOf(JaegerUnavailableError);
      await expect(fetchFlagStates()).rejects.toBeInstanceOf(JaegerUnavailableError);
      await expect(fetchRollouts(new RolloutStore())).rejects.toBeInstanceOf(JaegerUnavailableError);
      await expect(fetchExperiments()).rejects.toBeInstanceOf(JaegerUnavailableError);
    });
  }

  it("UC-035/A3 Jaeger up without spans (agent or collector down, or an old agent image) is data, not an error", async () => {
    stubBackends({});
    await expect(fetchFlagStates()).resolves.toEqual([]);
    await expect(fetchExperiments()).resolves.toEqual([]);
    const store = new RolloutStore();
    await fetchRollouts(store);
    expect(store.list()).toEqual([]);
  });
});

describe("UC-035: A3 Grafana down", () => {
  it("UC-035/A3 only the Metrics tab's embed and its outbound links depend on Grafana", () => {
    const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");
    const grafanaRefs = [...html.matchAll(/(src|href)="(\/grafana[^"]*)"/g)].map((m) => `${m[1]}=${m[2]}`);
    expect(grafanaRefs.length).toBeGreaterThan(0);
    expect(grafanaRefs).toContain("src=/grafana/d/controller-overview/controller-overview?orgId=1&theme=dark&kiosk");
    // The Experiments link opens the spans in Jaeger, no longer Loki in Grafana (#2059).
    expect(grafanaRefs.some((r) => r.startsWith("href=/grafana/explore"))).toBe(false);
    expect(html).toContain('href="/jaeger/search?service=agent&operation=experiment.progress"');
    // Every other Grafana reference is an outbound link, never a source a panel reads.
    for (const ref of grafanaRefs) {
      if (ref.startsWith("src=")) expect(ref).toContain("/grafana/d/controller-overview/");
    }
    expect(html).toMatch(/id="grafana-embed"/);
  });

  it("UC-035/A3 the Agent, Experiments and decide panels still load with Grafana down", async () => {
    const calls = stubBackends({ grafana: true });

    await expect(fetchInterventionCounts()).resolves.toBeDefined();
    await expect(fetchFlagStates()).resolves.toBeDefined();
    await expect(fetchExperiments()).resolves.toBeDefined();
    await expect(fetchDecideFeed(null)).resolves.toBeDefined();

    expect(calls.length).toBeGreaterThan(0);
    expect(calls.filter((u) => u.includes("/grafana"))).toEqual([]);
  });
});

describe("UC-035: A4 the game event stream is lost", () => {
  it("UC-035/A4 a failed game event stream shows no message in the event feed and reopens after 2 s", async () => {
    const feed: string[] = [];
    const delays: number[] = [];
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    async function* failing(): AsyncGenerator<string> {
      yield "game_start";
      throw new Error("connect-proxy restarted");
    }

    await keepGameEventStreamOpen(failing, (e) => feed.push(e), (_fn, ms) => delays.push(ms));

    expect(feed).toEqual(["game_start"]);
    expect(delays).toEqual([2000]);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("UC-035/A4 the shield badges stop updating, without a message, while the stream is lost", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const now = Date.now();
    const store = new ShieldStore();
    type Ev = { eventType: string; data: Record<string, string> };
    async function* failing(): AsyncGenerator<Ev> {
      yield { eventType: "player_shielded", data: { serial: "MOCK0001", kind: "full", seconds: "5.0", until_ms: String(now + 5000) } };
      throw new Error("coordinator restarted");
    }
    let reopen: (() => void) | undefined;

    await keepGameEventStreamOpen(failing, (e) => store.apply(e), (fn) => (reopen = fn));

    // The badge from before the loss keeps counting down on its own; nothing new arrives.
    expect(shieldBadge(store.get("MOCK0001", now + 1000), now + 1000)?.text).toBe("Shield 4.0s");
    expect(store.get("MOCK0002", now + 1000)).toBeNull();
    expect(reopen).toBeTypeOf("function");
  });

  it("UC-035/A4 a game event stream that ends cleanly shows no message either", async () => {
    const feed: string[] = [];
    vi.spyOn(console, "warn").mockImplementation(() => {});
    async function* ended(): AsyncGenerator<string> {
      yield "game_ended";
    }

    await keepGameEventStreamOpen(ended, (e) => feed.push(e), () => {});

    expect(feed).toEqual(["game_ended"]);
  });
});
