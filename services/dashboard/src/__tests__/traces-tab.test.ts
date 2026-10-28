/**
 * UC-035 step 5 (#2286): the Traces tab's Jaeger search lists game roots, not
 * the gRPC health checks that dominate game-coordinator-service by volume.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const html = readFileSync(join(__dirname, "..", "index.html"), "utf8");

function tracesTabUrls(): URL[] {
  const panel = html.slice(html.indexOf('id="tab-traces"'));
  const raw = [...panel.matchAll(/(?:src|href)="(\/jaeger\/search\?[^"]+)"/g)].map((m) => m[1]);
  return raw.map((r) => new URL(r.replaceAll("&amp;", "&"), "http://x"));
}

describe("UC-035: dashboard Traces tab search", () => {
  it("UC-035 step 5: searches game roots by the game.kind=primary tag, so Health/Check spans never match", () => {
    const urls = tracesTabUrls();
    expect(urls.length).toBeGreaterThanOrEqual(2); // the link and the iframe
    for (const url of urls) {
      expect(url.searchParams.get("service")).toBe("game-coordinator-service");
      expect(JSON.parse(url.searchParams.get("tags") ?? "{}")).toEqual({ "game.kind": "primary" });
    }
  });
});
