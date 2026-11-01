#!/usr/bin/env python3
"""JSON helpers and the report of scripts/agent-group-ab.sh (#2467 PR 6, UC-040 A47).

Stdlib only. Subcommands:

  real-games                    stdin: GET /games of the flag service; prints the ids of
                                the games that are not shadow games (unknown kind = real)
  overlay                       stdin: GET /{set}/{flag}; prints the stack overlay as
                                "<spec>\t<writer>\t<expires epoch>" (spec none, variant:<name> or
                                value:<json>; "-" for an empty writer or no expiry)
  admin-variant                 stdin: GET /{set}/{flag}; the persisted admin variant, or "-"
  ttl-left EPOCH                seconds until EPOCH (0 when past)
  verify-game ...               exit 0 when the game has a remote agent.decide span with the profile
  manifest-line ...             one JSON line for the run manifest
  report --manifest M (--jaeger-url U | --traces-file F) [--json]
                                per arm: group and single grants, their timing in the
                                game, and the outcomes

The report keeps a game only when its remote ``agent.decide`` spans carry the arm's
``decide.prompt_profile``: a game without a remote decide, or with another profile, is
listed as excluded and never counted. It describes; it judges nothing (the success
criterion is pre-registered in docs/agent-group-ab-runbook.md).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

GROUP_ACTION = "grant_group_shield"
SINGLE_ACTION = "grant_shield"
ENDGAME_ALIVE = 3  # a group decided with this many players alive or fewer
LATE_FRACTION = 2 / 3  # the last third of the game's observed span
SEARCH_TIME_MIN = "1970-01-01T00:00:00Z"
SEARCH_TIME_MAX = "2100-01-01T00:00:00Z"


def real_games(doc: dict) -> list[str]:
    """Ids of the registered games that are not shadow games (fail closed)."""
    out = []
    for game in doc.get("games") or []:
        if (game.get("game_kind") or "") != "shadow":
            out.append(str(game.get("game_id") or "?"))
    return out


def _epoch(stamp: str | None) -> str:
    """RFC 3339 -> epoch seconds (string); "-" when absent or unparsable."""
    if not stamp:
        return "-"
    try:
        return str(int(datetime.fromisoformat(stamp).timestamp()))
    except ValueError:
        return "-"


def overlay(view: dict) -> str:
    """The stack overlay as spec, writer and expiry (tab separated)."""
    ov = view.get("stack_overlay")
    if not ov:
        return "none\t-\t-"
    spec = (
        "variant:" + str(ov["variant"])
        if ov.get("variant")
        else "value:" + json.dumps(ov.get("value"))
    )
    return f"{spec}\t{ov.get('writer') or '-'}\t{_epoch(ov.get('expires_at'))}"


def admin_variant(view: dict) -> str:
    ov = view.get("admin_value")
    return str(ov.get("variant") or "-") if ov else "-"


def ttl_left(epoch: str) -> int:
    try:
        return max(0, int(epoch) - int(time.time()))
    except ValueError:
        return 0


# ---------------------------------------------------------------- OTLP spans


def _attr_value(v: dict):
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        return int(v["intValue"])
    return None


def flatten_spans(doc: dict) -> list[dict]:
    """The spans of a Jaeger v3 answer as dicts: name, attrs, start_ns, end_ns."""
    result = doc.get("result", doc)
    out = []
    for rs in result.get("resourceSpans") or []:
        for ss in rs.get("scopeSpans") or []:
            for sp in ss.get("spans") or []:
                attrs = {
                    a["key"]: _attr_value(a.get("value") or {})
                    for a in sp.get("attributes") or []
                }
                out.append(
                    {
                        "name": sp.get("name", ""),
                        "attrs": attrs,
                        "start_ns": int(sp.get("startTimeUnixNano") or 0),
                        "end_ns": int(sp.get("endTimeUnixNano") or 0),
                    }
                )
    return out


def fetch_spans(base: str, operation: str, game_id: str) -> list[dict]:
    """Spans of one operation for one game from Jaeger's query API v3."""
    params = {
        "query.serviceName": "agent",
        "query.operationName": operation,
        "query.attributes": json.dumps({"game.id": game_id}),
        "query.searchDepth": "200",
        "query.startTimeMin": SEARCH_TIME_MIN,
        "query.startTimeMax": SEARCH_TIME_MAX,
    }
    url = base.rstrip("/") + "/v3/traces?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return flatten_spans(json.load(resp))
    except urllib.error.HTTPError as err:
        if err.code == 404:  # "No traces found" is an empty answer
            return []
        raise


# ---------------------------------------------------------------- the report


def analyse_game(entry: dict, spans: list[dict]) -> dict:
    """One game's counts, or an exclusion reason."""
    game_id = entry["game_id"]
    mine = [s for s in spans if s["attrs"].get("game.id") == game_id]
    profiles = {
        s["attrs"].get("decide.prompt_profile")
        for s in mine
        if s["name"] == "agent.decide" and s["attrs"].get("decide.prompt_profile")
    }
    if not profiles:
        return {"excluded": "no_remote_decide"}
    if profiles != {entry["profile"]}:
        return {"excluded": "profile_mismatch:" + ",".join(sorted(profiles))}
    starts = [s["start_ns"] for s in mine if s["start_ns"]]
    ends = [s["end_ns"] for s in mine if s["end_ns"]]
    # The game window is the wall clock around the run when the manifest has it (the game, not
    # the first decide); older manifests fall back to the span window.
    t0 = min(starts) if starts else 0
    span_ns = (max(ends) - t0) if ends and starts else 0
    if entry.get("started_ns") and entry.get("ended_ns", 0) > entry["started_ns"]:
        t0 = int(entry["started_ns"])
        span_ns = int(entry["ended_ns"]) - t0
    grants = []
    for s in mine:
        if s["name"] != "agent.decision":
            continue
        a = s["attrs"]
        action = a.get("decision.action")
        if action not in (GROUP_ACTION, SINGLE_ACTION):
            continue
        frac = ((s["start_ns"] - t0) / span_ns) if span_ns > 0 else 0.0
        grants.append(
            {
                "action": action,
                "blocked": bool(a.get("decision.blocked")),
                "applied": bool(a.get("decision.applied")),
                "reason": a.get("decision.block_reason") or "",
                "alive": a.get("decision.group.alive"),
                "size": a.get("decision.group.size"),
                "fraction": round(frac, 3),
            }
        )
    return {"grants": grants}


def summarise_arm(entries: list[dict], analysed: list[dict]) -> dict:
    kept = [(e, a) for e, a in zip(entries, analysed) if "grants" in a]
    excluded = Counter(a["excluded"] for a in analysed if "excluded" in a)
    groups = [g for _, a in kept for g in a["grants"] if g["action"] == GROUP_ACTION]
    singles = [g for _, a in kept for g in a["grants"] if g["action"] == SINGLE_ACTION]
    g_applied = [g for g in groups if g["applied"]]
    s_applied = [g for g in singles if g["applied"]]
    n = len(kept)
    outcomes = Counter(
        str((e.get("record") or {}).get("outcome", "?")) for e, _ in kept
    )
    winners = Counter(
        str((e.get("record") or {}).get("winner_slot", "?")) for e, _ in kept
    )
    return {
        "games": n,
        "excluded": dict(excluded),
        "games_with_group": sum(
            1
            for _, a in kept
            if any(g["action"] == GROUP_ACTION and g["applied"] for g in a["grants"])
        ),
        "group_decisions": len(groups),
        "group_applied": len(g_applied),
        "group_blocked": dict(
            Counter(g["reason"] or "other" for g in groups if g["blocked"])
        ),
        "group_per_game": round(len(g_applied) / n, 3) if n else 0.0,
        "group_endgame": sum(
            1
            for g in g_applied
            if isinstance(g["alive"], int) and g["alive"] <= ENDGAME_ALIVE
        ),
        "group_late": sum(1 for g in g_applied if g["fraction"] >= LATE_FRACTION),
        "group_fractions": sorted(g["fraction"] for g in g_applied),
        "single_applied": len(s_applied),
        "single_blocked": dict(
            Counter(g["reason"] or "other" for g in singles if g["blocked"])
        ),
        "single_per_game": round(len(s_applied) / n, 3) if n else 0.0,
        "single_late": sum(1 for g in s_applied if g["fraction"] >= LATE_FRACTION),
        "outcomes": dict(outcomes),
        "winner_slots": dict(winners),
    }


def paired_by_seed(entries: list[dict], analysed: dict[str, list[dict]]) -> dict:
    """Applied group grants per seed and arm, and B minus A per seed (games kept in both arms).

    The games are seed-matched, so the per-seed difference uses the design; the unpaired
    totals do not.
    """
    per: dict[str, dict[str, int]] = {}
    for arm, rows in analysed.items():
        for e, a in rows:
            if "grants" not in a:
                continue
            n = sum(
                1 for g in a["grants"] if g["action"] == GROUP_ACTION and g["applied"]
            )
            per.setdefault(str(e["seed"]), {}).setdefault(arm, 0)
            per[str(e["seed"])][arm] += n
    diffs = {
        seed: arms["B"] - arms["A"]
        for seed, arms in per.items()
        if "A" in arms and "B" in arms
    }
    return {"group_applied": per, "b_minus_a": diffs}


def load_manifest(path: str) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def report(
    manifest: str, jaeger_url: str | None, traces_file: str | None, as_json: bool
) -> int:
    entries = load_manifest(manifest)
    if not entries:
        print("report: the manifest has no games", file=sys.stderr)
        return 3
    all_spans: list[dict] | None = None
    if traces_file:
        with open(traces_file) as fh:
            all_spans = flatten_spans(json.load(fh))
    arms: dict[str, list[dict]] = {}
    for e in entries:
        arms.setdefault(e["arm"], []).append(e)
    summary = {}
    kept_rows: dict[str, list] = {}
    for arm in sorted(arms):
        analysed = []
        for e in arms[arm]:
            if all_spans is not None:
                spans = all_spans
            else:
                spans = fetch_spans(
                    jaeger_url or "", "agent.decide", e["game_id"]
                ) + fetch_spans(jaeger_url or "", "agent.decision", e["game_id"])
            analysed.append(analyse_game(e, spans))
        s = summarise_arm(arms[arm], analysed)
        s["profile"] = arms[arm][0]["profile"]
        summary[arm] = s
        kept_rows[arm] = list(zip(arms[arm], analysed))
    summary["paired_by_seed"] = paired_by_seed(entries, kept_rows)
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    for arm, s in summary.items():
        if arm == "paired_by_seed":
            continue
        print(
            f"arm {arm}  prompt_profile={s['profile']}  games={s['games']}  excluded={s['excluded'] or 'none'}"
        )
        print(
            f"  group grants: applied {s['group_applied']} in {s['games_with_group']} game(s)"
            f" ({s['group_per_game']}/game), blocked {s['group_blocked'] or 'none'}"
        )
        print(
            f"    timing: {s['group_endgame']} with <= {ENDGAME_ALIVE} alive,"
            f" {s['group_late']} in the last third; at game fractions {s['group_fractions']}"
        )
        print(
            f"  single grants: applied {s['single_applied']} ({s['single_per_game']}/game),"
            f" {s['single_late']} in the last third, blocked {s['single_blocked'] or 'none'}"
        )
        print(f"  outcomes {s['outcomes']}  winner slots {s['winner_slots']}")
    p = summary["paired_by_seed"]
    print(
        f"paired by seed: applied groups {p['group_applied']}, B minus A {p['b_minus_a']}"
    )
    print(
        "note: a repeated refusal emits no span, so blocked counts here are first refusals only (repeats: agent_decisions_total)"
    )
    return 0


def verify_game(
    jaeger_url: str | None, traces_file: str | None, game_id: str, profile: str
) -> int:
    """0 when a remote agent.decide span of the game carries the profile (the agent saw the game)."""
    if traces_file:
        with open(traces_file) as fh:
            spans = flatten_spans(json.load(fh))
    else:
        spans = fetch_spans(jaeger_url or "", "agent.decide", game_id)
    seen = {
        s["attrs"].get("decide.prompt_profile")
        for s in spans
        if s["name"] == "agent.decide" and s["attrs"].get("game.id") == game_id
    }
    return 0 if profile in seen else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("real-games")
    sub.add_parser("overlay")
    sub.add_parser("admin-variant")
    t = sub.add_parser("ttl-left")
    t.add_argument("epoch")
    v = sub.add_parser("verify-game")
    v.add_argument("--game-id", required=True)
    v.add_argument("--profile", required=True)
    v.add_argument("--jaeger-url")
    v.add_argument("--traces-file")
    m = sub.add_parser("manifest-line")
    for opt in ("arm", "profile", "seed", "rep", "archetype", "record-file"):
        m.add_argument("--" + opt, required=True)
    m.add_argument("--started-ns", default="0")
    m.add_argument("--ended-ns", default="0")
    r = sub.add_parser("report")
    r.add_argument("--manifest", required=True)
    r.add_argument("--jaeger-url")
    r.add_argument("--traces-file")
    r.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.cmd == "real-games":
        print("\n".join(real_games(json.load(sys.stdin))))
        return 0
    if args.cmd == "overlay":
        print(overlay(json.load(sys.stdin)))
        return 0
    if args.cmd == "admin-variant":
        print(admin_variant(json.load(sys.stdin)))
        return 0
    if args.cmd == "ttl-left":
        print(ttl_left(args.epoch))
        return 0
    if args.cmd == "verify-game":
        return verify_game(
            args.jaeger_url, args.traces_file, args.game_id, args.profile
        )
    if args.cmd == "manifest-line":
        with open(args.record_file) as fh:
            record = json.load(fh)
        print(
            json.dumps(
                {
                    "arm": args.arm,
                    "profile": args.profile,
                    "seed": args.seed,
                    "rep": int(args.rep),
                    "archetype": args.archetype,
                    "game_id": record.get("game_id", ""),
                    "started_ns": int(args.started_ns),
                    "ended_ns": int(args.ended_ns),
                    "record": record,
                },
                sort_keys=True,
            )
        )
        return 0
    if not (args.jaeger_url or args.traces_file):
        ap.error("report needs --jaeger-url or --traces-file")
    return report(args.manifest, args.jaeger_url, args.traces_file, args.json)


if __name__ == "__main__":
    sys.exit(main())
