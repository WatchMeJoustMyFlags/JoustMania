#!/usr/bin/env python3
"""Fake VictoriaMetrics query API + Jaeger v3 for scripts/tests/test-agent-shadow-realism.sh (#1017).

Answers the fixed query templates of scripts/agent_shadow_realism.py from a scenario file (JSON,
re-read on every request) and logs every request as "<METHOD> <path>?<query>". Any method other
than GET is answered 405 and logged, so the test can prove the tool only reads.

Scenario: {"games": [{"id", "kind" (primary|shadow), "mode", "start", "dur", "threshold",
"serials": [...], "accel": {serial: g}, "variance": {serial: v}, "skill": {serial: v},
"elim": {serial: seconds after start}, "near_deaths": n, "jaeger_near_deaths": {serial: n} | null,
"linger": seconds the duration gauge outlives the game}], "deaths": [{"game_kind", "mode",
"instance", "before", "max"}]}
"""

from __future__ import annotations

import argparse
import json
import math
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None


def scenario() -> dict:
    with open(ARGS.scenario, encoding="utf-8") as fh:
        return json.load(fh)


def vec(metric: dict, value) -> dict:
    return {"metric": metric, "value": [0, str(value)]}


def accel_at(g, s, t):
    return g["accel"][s] + 0.05 * math.sin(t - g["start"])


def instant(q: str) -> list:
    sc = scenario()
    games = sc["games"]
    out = []
    if q.startswith("last_over_time(game_duration_seconds"):
        return [
            vec({"game_id": g["id"], "game_kind": g["kind"]}, g["dur"]) for g in games
        ]
    if q.startswith("tfirst_over_time(game_duration_seconds"):
        return [
            vec({"game_id": g["id"], "game_kind": g["kind"]}, g["start"] + 2)
            for g in games
        ]
    if q.startswith("first_over_time(game_duration_seconds"):
        return [vec({"game_id": g["id"], "game_kind": g["kind"]}, 2) for g in games]
    if q.startswith(
        "tlast_over_time(game_duration_seconds"
    ):  # a lingering series: not the game end
        return [
            vec({"game_id": g["id"]}, g["start"] + g["dur"] + g.get("linger", 0))
            for g in games
        ]
    if q.startswith("last_over_time(game_current_mode"):
        return [
            vec({"game_id": g["id"], "mode": g["mode"], "game_kind": g["kind"]}, 1)
            for g in games
        ]
    if q.startswith("last_over_time(game_effective_death_threshold"):
        return [
            vec({"game_id": g["id"], "game_kind": g["kind"]}, g["threshold"])
            for g in games
        ]
    if q.startswith("tfirst_over_time(game_player_elimination_order"):
        for g in games:
            out += [
                vec({"game_id": g["id"], "serial": s}, g["start"] + dt)
                for s, dt in g["elim"].items()
            ]
        return out
    for metric, key in (
        ("game_player_movement_variance", "variance"),
        ("game_player_skill_level", "skill"),
    ):
        if q.startswith(f"last_over_time({metric}"):
            for g in games:
                out += [
                    vec({"game_id": g["id"], "serial": s}, v) for s, v in g[key].items()
                ]
            return out
    m = re.match(r'sum\(increase\(game_near_death_events_total\{serial=~"([^"]*)"\}', q)
    if m:
        serials = set(m.group(1).split("|"))
        for g in games:
            if serials == set(g["serials"]):
                return [vec({}, g["near_deaths"])]
        return []
    if "game_player_deaths_total" in q:
        mode = re.search(r'mode="([^"]*)"', q)
        field = "before" if " offset " in q else "max"
        for d in sc.get("deaths", []):
            if mode and d["mode"] != mode.group(1):
                continue
            if d.get(field) is None:
                continue
            out.append(
                vec(
                    {
                        "__name__": "game_player_deaths_total",
                        "game_kind": d["game_kind"],
                        "mode": d["mode"],
                        "instance": d["instance"],
                    },
                    d[field],
                )
            )
        return out
    raise KeyError(q)


def range_query(q: str, start: float, end: float, step: float) -> list:
    gid = re.search(r'game_id="([^"]*)"', q).group(1)
    g = next(x for x in scenario()["games"] if x["id"] == gid)
    out = []
    for s in g["serials"]:
        values = []
        t = start
        while t <= end + 1e-9:
            if g["start"] <= t <= g["start"] + g["dur"]:
                if q.startswith("game_player_accel_magnitude"):
                    values.append([t, str(accel_at(g, s, t))])
                elif q.startswith("game_player_alive"):
                    dead = s in g["elim"] and t >= g["start"] + g["elim"][s]
                    values.append([t, "0" if dead else "1"])
            t += step
        out.append({"metric": {"game_id": gid, "serial": s}, "values": values})
    return out


def traces(params: dict):
    gid = json.loads(params["query.attributes"][0])["game.id"]
    g = next((x for x in scenario()["games"] if x["id"] == gid), None)
    if g is None or g.get("jaeger_near_deaths") is None:
        return None
    events = [
        {
            "name": "player_analytics",
            "attributes": [
                {"key": "game_id", "value": {"stringValue": gid}},
                {"key": "serial", "value": {"stringValue": s}},
                {"key": "near_death_count", "value": {"stringValue": str(n)}},
            ],
        }
        for s, n in g["jaeger_near_deaths"].items()
    ]
    # The same summaries on two spans: counted once per player.
    spans = [
        {"name": "teardown_phase", "events": events},
        {"name": "game_cycle", "events": events},
    ]
    return {"result": {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _log(self):
        with open(ARGS.log, "a", encoding="utf-8") as fh:
            fh.write(f"{self.command} {self.path}\n")

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._log()
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)
        try:
            if url.path.endswith("/api/v1/query"):
                return self._send(
                    200,
                    {
                        "status": "success",
                        "data": {
                            "resultType": "vector",
                            "result": instant(params["query"][0]),
                        },
                    },
                )
            if url.path.endswith("/api/v1/query_range"):
                res = range_query(
                    params["query"][0],
                    float(params["start"][0]),
                    float(params["end"][0]),
                    float(params["step"][0]),
                )
                return self._send(
                    200,
                    {
                        "status": "success",
                        "data": {"resultType": "matrix", "result": res},
                    },
                )
            if url.path.endswith("/v3/traces"):
                doc = traces(params)
                return (
                    self._send(404, {"error": "not found"})
                    if doc is None
                    else self._send(200, doc)
                )
        except KeyError as err:
            return self._send(400, {"status": "error", "error": f"unknown query {err}"})
        return self._send(404, {})

    def _refuse(self):
        self._log()
        self._send(405, {})

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--port-file", required=True)
    ARGS = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(ARGS.port_file, "w", encoding="utf-8") as fh:
        fh.write(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
