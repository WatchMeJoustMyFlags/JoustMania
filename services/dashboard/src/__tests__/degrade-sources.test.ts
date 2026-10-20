/**
 * UC-035 A3 and A4 clauses that depend on which source a panel reads (#1725):
 * VictoriaMetrics down, Grafana down, and the game event stream's silent loss.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchAgentView, fetchDecideFeed } from "../agent.js";
import { fetchExperiments } from "../experiments.js";
import { keepGameEventStreamOpen } from "../stream.js";
import { ShieldStore, shieldBadge } from "../shield.js";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const NOW_NS = BigInt(Date.now()) * 1_000_000n;

function lokiBody(lines: string[]) {
  return {
    status: "success",
    data: { result: [{ stream: { service_name: "agent" }, values: lines.map((l, i) => [String(NOW_NS - BigInt(i) * 1_000_000_000n), l]) }] },
  };
}

/** A fetch stub that answers per backend path; a backend in `down` fails. */
function stubBackends(down: { vm?: boolean; grafana?: boolean }, lokiLines: string[] = []) {
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
    if (url.includes("/loki/")) return json(200, lokiBody(lokiLines));
    if (url.includes("/jaeger/")) return json(200, { data: [] });
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
  it("UC-035/A3 with VictoriaMetrics down the blocked counts come from Loki and Applied is n/a", async () => {
    stubBackends({ vm: true }, [
      'level=INFO msg=agent.decision_blocked intervention=grant_shield reason=not_allowed',
      'level=INFO msg=agent.decision_blocked intervention=grant_shield reason=rate_limit',
      'level=INFO msg=agent.decision_blocked intervention=end_game reason=not_allowed',
    ]);

    const view = await fetchAgentView();

    expect(view.overlay.totalBlocked).toBe(3);
    expect(view.overlay.blockedByAction[0]).toEqual({ action: "grant_shield", blocked: 2 });
    expect(view.overlay.blockedByReason[0]).toEqual({ reason: "not_allowed", count: 2 });
    expect(view.overlay.appliedOk).toBeNull();
    expect(view.overlay.appliedError).toBeNull();
  });
});

describe("UC-040: A21 VictoriaMetrics unreachable", () => {
  it("UC-040/A21 a failed VictoriaMetrics fetch falls back to the Loki warning-line count", async () => {
    const lines = [
      "level=WARN msg=agent.decision_blocked intervention=grant_shield reason=rate_limit",
      "level=WARN msg=agent.decision_blocked intervention=grant_shield reason=rate_limit",
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/victoria-metrics/")) throw new TypeError("Failed to fetch");
        return json(200, lokiBody(lines));
      }),
    );

    const view = await fetchAgentView();

    expect(view.overlay.totalBlocked).toBe(2);
    expect(view.overlay.blockedByReason).toEqual([{ reason: "rate_limit", count: 2 }]);
  });
});

describe("UC-035: A3 Grafana down", () => {
  it("UC-035/A3 only the Metrics tab's embed and the log links depend on Grafana", () => {
    const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");
    const grafanaRefs = [...html.matchAll(/(src|href)="(\/grafana[^"]*)"/g)].map((m) => `${m[1]}=${m[2]}`);
    expect(grafanaRefs.length).toBeGreaterThan(0);
    expect(grafanaRefs).toContain("src=/grafana/d/controller-overview/controller-overview?orgId=1&theme=dark&kiosk");
    expect(grafanaRefs.some((r) => r.startsWith("href=/grafana/explore"))).toBe(true);
    // Every other Grafana reference is an outbound link, never a source a panel reads.
    for (const ref of grafanaRefs) {
      if (ref.startsWith("src=")) expect(ref).toContain("/grafana/d/controller-overview/");
    }
    expect(html).toMatch(/id="grafana-embed"/);
  });

  it("UC-035/A3 the Agent, Experiments and decide panels still load with Grafana down", async () => {
    const calls = stubBackends({ grafana: true }, ["level=INFO msg=agent.evaluate enabled=true"]);

    await expect(fetchAgentView()).resolves.toBeDefined();
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
