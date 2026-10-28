"""A small fake of the flag service's HTTP API for the script tests (epic #1407).

Serves the stack-overlay part of docs/specs/services.md §7.2 over one flag set
file, with the REAL registry (services/flag-service/registry.yaml) deciding
which flags are mutable, so a script that writes a read-only flag or an
undeclared variant fails here exactly as it would against the service:

    GET    /state                 epoch, one sync stream, served sets
    GET    /{set}                 every flag: effective_default, variants, variant_values, stack_overlay,
                                  admin_value
    GET    /{set}/{flag}
    PUT    /{set}/{flag}          {"variant"|"value", "writer", "persist"}: 422 flag_read_only / out_of_bounds;
                                  "persist": true sets the persisted admin value and drops the stack overlay
    DELETE /{set}/{flag}          clear the stack overlay (?persist=true: the persisted admin value)
    DELETE /{set}?writer=W        clear that writer's stack overlays (admin values stay)

The persisted admin value is a layer under the stack overlay, as in the service.

--fail-put N / --fail-delete N answer the Nth PUT / DELETE with 503 (a refused write).

Every request is appended to --log as "METHOD PATH BODY". Run:

    python3 fake_flag_service.py --flags agent=path/agent.json --registry registry.yaml \
        --port-file port.txt --log requests.log
"""

import argparse
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REGISTRY_ROW = re.compile(
    r"\{set: ([\w.-]+), key: ([\w.-]+), kind: (\w+), scope: \[([^\]]*)\]"
)


def load_registry(path):
    rows = {}
    with open(path) as fh:
        for line in fh:
            m = REGISTRY_ROW.search(line)
            if m:
                rows[(m.group(1), m.group(2))] = {
                    "kind": m.group(3),
                    "persist": "persist: true" in line,
                    "free_string": "free_string: true" in line,
                    "range": "range:" in line,
                }
    return rows


class Fake:
    def __init__(
        self,
        sets,
        registry,
        log_path,
        games=0,
        scope_values=0,
        fail_put=0,
        fail_delete=0,
    ):
        self.sets = sets  # set -> {flag: flag json}
        self.registry = registry
        self.log_path = log_path
        self.overlays = {}  # (set, flag) -> stack overlay {"variant", "value", "writer"}
        self.admin = {}  # (set, flag) -> persisted admin value {"variant", "value", "writer"}
        self.games = games
        self.scope_values = scope_values
        self.fail_put = fail_put  # 1-based index of the PUT answered with 503
        self.puts = 0
        self.fail_delete = fail_delete  # 1-based index of the DELETE answered with 503
        self.deletes = 0
        self.lock = threading.Lock()

    def view(self, flag_set, key):
        flag = self.sets[flag_set][key]
        variants = dict(flag["variants"])
        effective = flag["defaultVariant"]
        layers = {}
        # admin first, so a stack overlay over it wins the effective variant
        for name, ov in (
            ("admin_value", self.admin.get((flag_set, key))),
            ("stack_overlay", self.overlays.get((flag_set, key))),
        ):
            if ov:
                effective = ov["variant"]
                if ov["variant"] not in variants:
                    variants[ov["variant"]] = ov["value"]
                layers[name] = {
                    "variant": ov["variant"],
                    "value": variants[ov["variant"]],
                    "writer": ov["writer"],
                }
        return {
            "flag": key,
            "mutable": (flag_set, key) in self.registry,
            "baseline_default": flag["defaultVariant"],
            "effective_default": effective,
            "variants": list(flag["variants"]),
            "variant_values": flag["variants"],
            "stack_overlay": layers.get("stack_overlay"),
            "admin_value": layers.get("admin_value"),
        }


def make_handler(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _err(self, status, reason, detail):
            self._send(status, {"error": reason, "detail": detail})

        def _parts(self):
            url = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
            return parts, urllib.parse.parse_qs(url.query)

        def _log(self, body=""):
            with open(fake.log_path, "a") as fh:
                fh.write(f"{self.command} {self.path} {body}\n")

        def do_GET(self):
            self._log()
            parts, _ = self._parts()
            with fake.lock:
                if parts == ["state"]:
                    return self._send(
                        200,
                        {
                            "epoch": "fake0001",
                            "sync_streams": 1,
                            "served_sets": sorted(fake.sets),
                            "live_games": fake.games,
                            "scope_values": fake.scope_values,
                            "overlays": len(fake.overlays),
                        },
                    )
                if len(parts) == 1 and parts[0] in fake.sets:
                    flags = [
                        fake.view(parts[0], k) for k in sorted(fake.sets[parts[0]])
                    ]
                    return self._send(
                        200, {"flag_set": parts[0], "served": True, "flags": flags}
                    )
                if (
                    len(parts) == 2
                    and parts[0] in fake.sets
                    and parts[1] in fake.sets[parts[0]]
                ):
                    return self._send(200, fake.view(*parts))
            self._err(404, "unknown_flag", self.path)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            self._log(raw)
            parts, _ = self._parts()
            with fake.lock:
                fake.puts += 1
                if fake.puts == fake.fail_put:
                    return self._err(503, "unavailable", "injected failure")
            if len(parts) != 2 or parts[0] not in fake.sets:
                return self._err(404, "unknown_flag_set", self.path)
            flag_set, key = parts
            if key not in fake.sets[flag_set]:
                return self._err(404, "unknown_flag", key)
            row = fake.registry.get((flag_set, key))
            if row is None:
                return self._err(
                    422, "flag_read_only", f"{flag_set}/{key} is read-only baseline"
                )
            body = json.loads(raw or "{}")
            if ("variant" in body) == ("value" in body):
                return self._err(400, "malformed", "exactly one of variant and value")
            if body.get("persist") and not row["persist"]:
                return self._err(400, "malformed", f"{key} cannot persist")
            variants = fake.sets[flag_set][key]["variants"]
            if "variant" in body:
                if body["variant"] not in variants:
                    return self._err(
                        422,
                        "out_of_bounds",
                        f"{body['variant']!r} is not a declared variant of {key}",
                    )
                variant, value = body["variant"], variants[body["variant"]]
            else:
                value = body["value"]
                match = [n for n, v in variants.items() if v == value]
                if match:
                    variant = match[0]
                elif (
                    row["free_string"]
                    and isinstance(value, str)
                    or row["range"]
                    and isinstance(value, (int, float))
                ):
                    variant = "ofake_1"
                else:
                    return self._err(
                        422, "out_of_bounds", f"{value!r} is not allowed for {key}"
                    )
            entry = {
                "variant": variant,
                "value": value,
                "writer": body.get("writer", ""),
            }
            with fake.lock:
                if body.get("persist"):
                    # a persisted write replaces the stack overlay (state/write.go)
                    fake.admin[(flag_set, key)] = entry
                    fake.overlays.pop((flag_set, key), None)
                else:
                    fake.overlays[(flag_set, key)] = entry
            self._send(
                200,
                {
                    "flag_set": flag_set,
                    "flag": key,
                    "variant": variant,
                    "epoch": "fake0001",
                    "snapshot_version": 1,
                    "persisted": bool(body.get("persist")),
                },
            )

        def do_DELETE(self):
            self._log()
            parts, query = self._parts()
            with fake.lock:
                fake.deletes += 1
                if fake.deletes == fake.fail_delete:
                    return self._err(503, "no_sync_stream", "injected failure")
                if len(parts) == 1 and parts[0] in fake.sets:
                    writer = (query.get("writer") or [""])[0]
                    if not writer:
                        return self._err(400, "malformed", "writer is required")
                    for k in [
                        k
                        for k, ov in fake.overlays.items()
                        if k[0] == parts[0] and ov["writer"] == writer
                    ]:
                        del fake.overlays[k]
                    return self._send(204)
                if len(parts) == 2 and parts[0] in fake.sets:
                    if (query.get("persist") or [""])[0] == "true":
                        fake.admin.pop(tuple(parts), None)
                    else:
                        fake.overlays.pop(tuple(parts), None)
                    return self._send(204)
            self._err(404, "unknown_flag_set", self.path)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--flags", action="append", required=True, help="set=path/to/set.json"
    )
    ap.add_argument("--registry", required=True)
    ap.add_argument("--port-file", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument(
        "--games", type=int, default=0, help="live games reported by GET /state"
    )
    ap.add_argument("--scope-values", type=int, default=0)
    ap.add_argument(
        "--fail-put", type=int, default=0, help="answer the Nth PUT with 503"
    )
    ap.add_argument(
        "--fail-delete", type=int, default=0, help="answer the Nth DELETE with 503"
    )
    args = ap.parse_args()
    sets = {}
    for spec in args.flags:
        name, path = spec.split("=", 1)
        with open(path) as fh:
            sets[name] = json.load(fh)["flags"]
    fake = Fake(
        sets,
        load_registry(args.registry),
        args.log,
        args.games,
        args.scope_values,
        args.fail_put,
        args.fail_delete,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    with open(args.port_file + ".tmp", "w") as fh:
        fh.write(str(server.server_address[1]))
    import os

    os.replace(args.port_file + ".tmp", args.port_file)
    server.serve_forever()


if __name__ == "__main__":
    main()
