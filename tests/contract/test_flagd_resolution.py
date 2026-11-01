"""flagd contract tier: real flagd only, no game stack (#1422).

The Go agent's unit tests and the Python services' unit tests evaluate jsonlogic
in-process or mock the provider. What only the real flagd server shows is
checked here, against ONE flagd container (the image docker-compose.yml runs,
v0.18.1) serving a temporary copy of ``services/flagd/ci`` (see flagd_harness):

- typed resolution: every flag the game coordinator reads resolves, through the
  RPC and the in-process resolver, with the SAME typed getter the coordinator
  calls. A wrong getter does not raise: it returns the caller's default with
  TYPE_MISMATCH, so an intervention or calibration flag silently stops working
  (#903, #1127). The getter is derived from the coordinator source, not copied;
- server-side targeting of the baseline: the ``windows`` preset ladder.

flagd here reads the baseline files directly (``file:`` sources), which is what
the flag service hands it unchanged when no runtime value is set. Runtime
writes (per-player ladders, the rollout count, experiment arms, propagation)
go through the flag service and are tested against the real stack in
tests/service (test_intervention_targeting.py, test_rollout_plumbing.py,
test_experiment_shadow_isolation.py, test_flag_propagation.py; epic #1407, #1561)
and tests/integration (test_tc013_shadow_alongside_real.py).

Docker is required: a missing docker fails in CI (``CI`` set) and skips locally.
"""

from __future__ import annotations

import ast
import contextlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import flagd_harness as fh
import infra_config as ic
import pytest


def _require(available: bool, what: str) -> None:
    if available:
        return
    if os.environ.get("CI"):
        pytest.fail(f"{what} is required in CI for the flagd contract tier")
    pytest.skip(f"{what} not available")


try:
    from openfeature.evaluation_context import EvaluationContext
    from openfeature.exception import ErrorCode
    from openfeature.flag_evaluation import Reason

    _HAVE_OPENFEATURE = True
except ImportError:  # pragma: no cover - exercised only without the extra deps
    _HAVE_OPENFEATURE = False


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def flagd(tmp_path_factory):
    """One flagd container for the module, on a temp copy of services/flagd/ci."""
    _require(
        _HAVE_OPENFEATURE, "openfeature-provider-flagd (make test-contract installs it)"
    )
    _require(ic.docker_available(), "docker")

    flag_dir = fh.copy_ci_flag_dir(tmp_path_factory.mktemp("flagd") / "ci")
    container = fh.FlagdContainer(flag_dir)
    container.start()
    try:
        for resolver in ("rpc", "in_process"):
            with container.client("game", resolver=resolver) as client:
                assert fh.poll_until(lambda c=client: _provider_ready(c)), (
                    f"flagd {resolver} resolver never became ready\n{container.logs()}"
                )
        yield container
    finally:
        container.stop()


def _provider_ready(client) -> bool:
    """Connected = an unknown flag is FLAG_NOT_FOUND (not PROVIDER_NOT_READY)."""
    details = client.get_string_details(
        "__contract_no_such_flag__", "", EvaluationContext()
    )
    return details.error_code == ErrorCode.FLAG_NOT_FOUND


@contextlib.contextmanager
def _ready_client(flagd, flag_set: str, resolver: str = "rpc"):
    with flagd.client(flag_set, resolver=resolver) as client:
        assert fh.poll_until(lambda: _provider_ready(client)), (
            f"{resolver} client for {flag_set} never connected"
        )
        yield client


# --------------------------------------------------------------------------- #
# 1. Typed getters (the #903 TYPE_MISMATCH class)
# --------------------------------------------------------------------------- #
COORDINATOR_DIR = ic.REPO_ROOT / "services" / "game_coordinator"
INTERVENTIONS_PY = COORDINATOR_DIR / "interventions.py"

_GETTER_CALL = re.compile(
    r"^get_(boolean|integer|float|string|object)_(value|details)$"
)
# Anything shaped like a lib/feature_flags read helper (read_<x>_flag[...]).
_READ_HELPER_SHAPE = re.compile(r"^read_\w+_flag\w*$")

# lib/feature_flags read helpers -> the OpenFeature getter each one calls.
# read_int_flag reads with the FLOAT getter on purpose (whole-number floats
# still resolve) and coerces afterwards. Every helper takes (domain, flag_key, ...).
_READ_HELPERS = {
    "read_bool_flag": "boolean",
    "read_float_flag": "float",
    "read_int_flag": "float",
    "read_string_flag": "string",
    "read_object_flag": "object",
    "read_object_flag_variant": "object",
}

# Coordinator wrappers that take the flag key as a parameter:
# name -> (flag set, getter, key parameter name).
# games/flag_snapshot.py _read_volume_flag: get_flag_client("user").get_float_value(flag_name).
_WRAPPERS = {"_read_volume_flag": ("user", "float", "flag_name")}

# Getter call sites with a non-literal key that are covered another way, as
# (file, enclosing function, key expression). Exact sites only: the same
# expression anywhere else is reported as uncovered.
_COVERED_NON_LITERAL = {
    ("interventions.py", "_read_grant", "spec.flag_key"): "INTERVENTION_SPECS",
    ("interventions.py", "_read_flag", "spec.flag_key"): "INTERVENTION_SPECS",
    ("interventions.py", "_read_event_list", "spec.flag_key"): "INTERVENTION_SPECS",
    (
        "interventions.py",
        "resolve_player_targets",
        "flag_key",
    ): "targeted_value_kinds()",
    ("flag_snapshot.py", "_read_volume_flag", "flag_name"): "_WRAPPERS",
}

# Per-serial reads: the caller passes value_kind, which must match the spec's.
# name -> (positional index of value_kind or None, parameter name).
_TARGETED_READERS = {
    "resolve_player_targets": (None, "value_kind"),
    "_grant_values": (3, "value_kind"),
}

_KIND_TO_GETTER = {"int": "integer", "float": "float", "string": "string"}


@dataclass(frozen=True)
class FlagRead:
    flag_set: str
    key: str
    getter: str  # boolean | integer | float | string | object
    where: str


def _literal(node) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _call_name(func: ast.AST) -> str | None:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _arg(call: ast.Call, index: int | None, name: str) -> ast.AST | None:
    """The argument bound to ``name`` (positional ``index`` or keyword)."""
    if index is not None and len(call.args) > index:
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _client_domains(tree: ast.AST) -> dict[str, str]:
    """``<receiver source> -> flag set`` from ``X = get_flag_client("set")``."""
    domains = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == "get_flag_client"
            and node.value.args
            and _literal(node.value.args[0])
        ):
            for target in node.targets:
                domains[ast.unparse(target)] = _literal(node.value.args[0])
    return domains


def _loop_keys(func: ast.AST, name: str) -> list[str]:
    """Keys a loop variable takes: ``for name, _ in specs`` over a literal ``specs``."""
    literals = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(
            node.value, (ast.Tuple, ast.List)
        ):
            for target in node.targets:
                literals[ast.unparse(target)] = node.value
    keys = []
    for node in ast.walk(func):
        if not isinstance(node, ast.For) or ast.unparse(node.iter) not in literals:
            continue
        target = node.target
        index = None
        if isinstance(target, ast.Name) and target.id == name:
            index = -1
        elif isinstance(target, ast.Tuple):
            index = next(
                (
                    i
                    for i, e in enumerate(target.elts)
                    if isinstance(e, ast.Name) and e.id == name
                ),
                None,
            )
        if index is None:
            continue
        for element in literals[ast.unparse(node.iter)].elts:
            value = element if index == -1 else element.elts[index]
            if _literal(value):
                keys.append(_literal(value))
    return keys


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """node -> innermost enclosing function (walk order visits outer first)."""
    parents = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                if node is not func:
                    parents[node] = func
    return parents


def _coordinator_sources() -> dict[str, str]:
    return {
        str(path.relative_to(ic.REPO_ROOT)): path.read_text()
        for path in sorted(COORDINATOR_DIR.rglob("*.py"))
        if "tests" not in path.parts
    }


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """``alias -> real name`` for ``from ... import read_float_flag as rf``."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def _is_read_name(name: str | None) -> bool:
    """A read helper, wrapper or OpenFeature getter name."""
    return bool(
        name
        and (
            name in _READ_HELPERS
            or name in _WRAPPERS
            or _READ_HELPER_SHAPE.match(name)
            or _GETTER_CALL.match(name)
        )
    )


def _dynamic_read_reasons(
    tree: ast.AST, aliases: dict[str, str]
) -> list[tuple[ast.AST, str]]:
    """Reads the scan cannot follow: a helper or getter referenced without being
    called (``functools.partial(read_float_flag, ...)``, ``f = client.get_float_value``)
    and ``getattr`` with a getter/helper-shaped or computed (f-string) name."""
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    reasons = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _call_name(node.func) == "getattr"
            and len(node.args) >= 2
        ):
            attr = node.args[1]
            if isinstance(attr, ast.JoinedStr):
                prefix = "".join(
                    v.value for v in attr.values if isinstance(v, ast.Constant)
                )
                if prefix.startswith(("get_", "read_")):
                    reasons.append(
                        (node, f"getattr with a computed name {ast.unparse(attr)}")
                    )
            elif _is_read_name(_literal(attr)):
                reasons.append((node, f"getattr({_literal(attr)!r})"))
        elif (
            isinstance(node, (ast.Name, ast.Attribute))
            and isinstance(node.ctx, ast.Load)
            and id(node) not in called
        ):
            name = _call_name(node)
            if _is_read_name(aliases.get(name, name)):
                reasons.append((node, f"{ast.unparse(node)} referenced but not called"))
    return reasons


def scan_flag_reads(sources: dict[str, str]) -> tuple[list[FlagRead], list[str]]:
    """Every flag read in ``sources`` (path -> code), with its getter.

    Returns (reads, uncovered): ``uncovered`` lists read sites the scan cannot
    resolve to a literal (flag set, key) and nothing else covers.
    """
    reads: list[FlagRead] = []
    uncovered: list[str] = []
    for rel, source in sources.items():
        tree = ast.parse(source)
        domains = _client_domains(tree)
        parents = _enclosing_functions(tree)
        aliases = _import_aliases(tree)
        filename = Path(rel).name
        for node, reason in _dynamic_read_reasons(tree, aliases):
            uncovered.append(f"{rel}:{node.lineno} {reason}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = _call_name(func)
            name = aliases.get(name, name)
            where = f"{rel}:{node.lineno}"
            enclosing = getattr(parents.get(node), "name", "<module>")
            if name in _READ_HELPERS:
                domain, key = _arg(node, 0, "domain"), _arg(node, 1, "flag_key")
                if _literal(domain) and _literal(key):
                    reads.append(
                        FlagRead(
                            _literal(domain), _literal(key), _READ_HELPERS[name], where
                        )
                    )
                else:
                    uncovered.append(f"{where} {name}: non-literal flag set or key")
            elif name and _READ_HELPER_SHAPE.match(name):
                uncovered.append(f"{where} {name}: read helper not in _READ_HELPERS")
            elif name in _WRAPPERS:
                flag_set, getter, param = _WRAPPERS[name]
                key = _arg(node, 0, param)
                if _literal(key):
                    reads.append(FlagRead(flag_set, _literal(key), getter, where))
                else:
                    uncovered.append(f"{where} {name}: non-literal key")
            elif name and _GETTER_CALL.match(name):
                getter = _GETTER_CALL.match(name).group(1)
                receiver = func.value if isinstance(func, ast.Attribute) else None
                flag_set = (
                    domains.get(ast.unparse(receiver)) if receiver is not None else None
                )
                key_node = _arg(node, 0, "flag_key")
                key_expr = ast.unparse(key_node) if key_node is not None else "<none>"
                if flag_set and _literal(key_node):
                    reads.append(FlagRead(flag_set, _literal(key_node), getter, where))
                    continue
                keys = (
                    _loop_keys(parents[node], key_node.id)
                    if flag_set and isinstance(key_node, ast.Name) and node in parents
                    else []
                )
                if keys:
                    reads += [FlagRead(flag_set, key, getter, where) for key in keys]
                elif (filename, enclosing, key_expr) not in _COVERED_NON_LITERAL:
                    uncovered.append(
                        f"{where} {ast.unparse(func)}({key_expr}) in {enclosing}"
                    )
    return reads, uncovered


def coordinator_flag_reads() -> tuple[list[FlagRead], list[str]]:
    return scan_flag_reads(_coordinator_sources())


def scan_targeted_value_kinds(
    sources: dict[str, str],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Per-serial reads -> (flag_key, value_kind, where).

    The flag is the one the enclosing handler is registered for
    (``register_handler("<flag>", handler)`` or ``lambda ctx: handler(ctx, ...)``).
    A ``_TARGETED_READERS`` function forwarding its own ``value_kind`` parameter
    is the wrapper itself; its callers are checked instead.
    """
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    handler_flags: dict[str, str] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) == "register_handler"
                and len(node.args) >= 2
            ):
                flag, handler = _literal(node.args[0]), node.args[1]
                if isinstance(handler, ast.Lambda) and isinstance(
                    handler.body, ast.Call
                ):
                    handler = handler.body.func
                if flag and isinstance(handler, (ast.Name, ast.Attribute)):
                    handler_flags[_call_name(handler)] = flag

    kinds: list[tuple[str, str, str]] = []
    uncovered: list[str] = []
    for rel, tree in trees.items():
        parents = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node.func) in _TARGETED_READERS
            ):
                continue
            index, param = _TARGETED_READERS[_call_name(node.func)]
            kind_node = _arg(node, index, param)
            enclosing = parents.get(node)
            where = f"{rel}:{node.lineno}"
            if (
                isinstance(kind_node, ast.Name)
                and getattr(enclosing, "name", None) in _TARGETED_READERS
                and kind_node.id == param
            ):
                continue  # the wrapper forwarding its own parameter
            flag = handler_flags.get(getattr(enclosing, "name", None))
            kind = (
                _literal(kind_node) if kind_node is not None else "float"
            )  # the default
            if flag is None or kind is None:
                uncovered.append(
                    f"{where} {_call_name(node.func)}: no registered handler or non-literal value_kind"
                )
            else:
                kinds.append((flag, kind, where))
    return kinds, uncovered


def intervention_specs() -> dict[str, dict]:
    """INTERVENTION_SPECS rows (flag_key -> {edge_triggered, value_kind}), from source."""
    tree = ast.parse(INTERVENTIONS_PY.read_text())
    specs = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "InterventionSpec"
        ):
            continue
        wanted = ("flag_key", "edge_triggered", "value_kind")
        kwargs = {
            kw.arg: ast.literal_eval(kw.value)
            for kw in node.keywords
            if kw.arg in wanted
        }
        kwargs.setdefault("value_kind", "string")
        kwargs["lineno"] = node.lineno
        specs[kwargs["flag_key"]] = kwargs
    return specs


def intervention_spec_reads() -> list[FlagRead]:
    """INTERVENTION_SPECS rows -> the getter InterventionManager reads them with.

    Edge-triggered (one-shot) flags are per-game event lists read with the object
    getter (``{"epoch","seq","events"}``, FR-137);
    state flags by ``value_kind`` (``_read_flag``; per-serial reads must use the
    same kind, checked by test_targeted_reads_use_the_spec_value_kind). Parsed
    from the source so the bare contract venv needs no service deps.
    """
    return [
        FlagRead(
            "interventions",
            key,
            "object" if spec["edge_triggered"] else _KIND_TO_GETTER[spec["value_kind"]],
            f"{INTERVENTIONS_PY.name}:{spec['lineno']}",
        )
        for key, spec in intervention_specs().items()
    ]


def all_flag_reads() -> list[FlagRead]:
    reads, _ = coordinator_flag_reads()
    return reads + intervention_spec_reads()


_SENTINEL_DEFAULTS = {
    "boolean": False,
    "integer": -987654,
    "float": -987654.5,
    "string": "__contract_unset__",
    "object": {"__contract_unset__": True},
}


def _type_ok(getter: str, value) -> bool:
    if getter == "boolean":
        return isinstance(value, bool)
    if getter == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if getter == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if getter == "string":
        return isinstance(value, str)
    return isinstance(value, (dict, list))


def _same_value(got, want) -> bool:
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        return (
            isinstance(got, (int, float))
            and not isinstance(got, bool)
            and math.isclose(got, want)
        )
    return got == want


def test_coordinator_flag_reads_are_all_resolved_by_the_scan():
    """Guards the scan: a read site whose flag set or key the scan cannot see fails here."""
    reads, uncovered = coordinator_flag_reads()
    assert not uncovered, (
        "game-coordinator flag reads the typed-getter contract cannot resolve; use literal "
        f"arguments or extend _READ_HELPERS / _WRAPPERS / _COVERED_NON_LITERAL: {uncovered}"
    )
    found = {(r.flag_set, r.key) for r in reads}
    # Spot checks across every scan path (helper, client getter, loop, wrapper).
    for expected in (
        ("game", "windows"),
        ("game", "max_concurrent_games"),
        ("system", "game_loop.update_frequency_hz"),
        ("agent", "interventions_allowed"),
        ("agent", "perception.zone_still_max"),
        ("user", "audio_volume.game"),
    ):
        assert expected in found, f"scan lost {expected}; found {sorted(found)}"


# Scanner fixtures: each evasion the guard must catch (or read correctly).
_SCAN_FIXTURES = {
    "helper_non_literal_key": ('read_float_flag("game", key)\n', None),
    "helper_keywords": (
        'read_float_flag(domain="game", flag_key="k", default=1.0)\n',
        ("game", "k", "float"),
    ),
    "helper_non_literal_keyword_domain": (
        'read_float_flag(domain=d, flag_key="k", default=1.0)\n',
        None,
    ),
    "helper_mixed_args": (
        'read_string_flag("game", flag_key="k", default="x")\n',
        ("game", "k", "string"),
    ),
    "unknown_helper": ('read_secret_flag("game", "k", False)\n', None),
    "wrapper_non_literal": ("_read_volume_flag(name, 0.5)\n", None),
    "wrapper_keyword": (
        '_read_volume_flag(flag_name="audio_volume.x", default=0.5)\n',
        ("user", "audio_volume.x", "float"),
    ),
    "getter_unknown_receiver": ('def f(c):\n    c.get_float_value("k", 1.0)\n', None),
    "getter_covered_expr_elsewhere": (
        "def other(spec, client):\n    client.get_float_value(spec.flag_key, 1.0)\n",
        None,
    ),
    "helper_import_alias": (
        'from lib.feature_flags import read_float_flag as rf\nrf("game", "k", 1.0)\n',
        ("game", "k", "float"),
    ),
    "helper_import_alias_non_literal": (
        "from lib.feature_flags import read_float_flag as rf\nrf(domain, key, 1.0)\n",
        None,
    ),
    "helper_partial": (
        'import functools\nf = functools.partial(read_float_flag, "game")\n',
        None,
    ),
    "helper_alias_partial": (
        'from functools import partial\nfrom lib.feature_flags import read_string_flag as rs\np = partial(rs, "game")\n',
        None,
    ),
    "getter_bound_reference": (
        'c = get_flag_client("game")\nf = c.get_float_value\n',
        None,
    ),
    "getattr_computed_getter": (
        'def f(c, kind):\n    getattr(c, f"get_{kind}_value")("k", 0)\n',
        None,
    ),
    "getattr_literal_getter": (
        'def f(c):\n    getattr(c, "get_float_value")("k", 0.0)\n',
        None,
    ),
    "getter_keyword_key": (
        'c = get_flag_client("game")\ndef f():\n    c.get_integer_value(flag_key="k", default_value=1)\n',
        ("game", "k", "integer"),
    ),
}


@pytest.mark.parametrize("case", sorted(_SCAN_FIXTURES))
def test_scanner_catches_evasions(case):
    source, expected = _SCAN_FIXTURES[case]
    # "interventions.py" so the covered-expression case shares the file of a real allow-list entry.
    reads, uncovered = scan_flag_reads(
        {"services/game_coordinator/interventions.py": source}
    )
    if expected is None:
        assert uncovered and not reads, f"{case}: evasion not reported (reads={reads})"
    else:
        assert not uncovered, f"{case}: unexpectedly uncovered {uncovered}"
        assert [(r.flag_set, r.key, r.getter) for r in reads] == [expected]


def test_contract_deps_match_uv_lock():
    """The Makefile's CONTRACT_DEPS pins are the uv.lock versions (one source of truth)."""
    import tomllib

    makefile = (ic.REPO_ROOT / "Makefile").read_text()
    line = next(
        line for line in makefile.splitlines() if line.startswith("CONTRACT_DEPS")
    )
    pins = dict(re.findall(r"'(openfeature-[\w-]+)==([^']+)'", line))
    assert set(pins) == {"openfeature-sdk", "openfeature-provider-flagd"}, (
        f"CONTRACT_DEPS must pin both: {line}"
    )
    lock = tomllib.loads((ic.REPO_ROOT / "uv.lock").read_text())
    locked = {
        pkg["name"]: pkg["version"] for pkg in lock["package"] if pkg["name"] in pins
    }
    assert pins == locked, f"Makefile CONTRACT_DEPS {pins} != uv.lock {locked}"


def test_stale_container_rule_spares_live_and_foreign_runs():
    """remove_stale_containers only removes orphans, never a parallel live run."""
    now = 1_000_000.0
    me = {
        fh.OWNER_HOST_LABEL: "h",
        fh.OWNER_PID_LABEL: str(os.getpid()),
        fh.STARTED_LABEL: str(int(now)),
    }
    assert not fh.is_stale(me, host="h", now=now), "live owner on this host"
    other_host = {**me, fh.OWNER_HOST_LABEL: "elsewhere", fh.OWNER_PID_LABEL: "1"}
    assert not fh.is_stale(other_host, host="h", now=now), (
        "owner on another host, within age"
    )
    dead = {**me, fh.OWNER_PID_LABEL: str(2**22 + 12345)}  # above Linux pid_max default
    assert fh.is_stale(dead, host="h", now=now), "owner process gone"
    old = {
        **other_host,
        fh.STARTED_LABEL: str(int(now - fh.MAX_CONTAINER_AGE_SECONDS - 1)),
    }
    assert fh.is_stale(old, host="h", now=now), "older than the worst-case runtime"
    assert fh.is_stale({}, host="h", now=now), "unlabelled legacy container"


def test_intervention_specs_cover_every_intervention_flag():
    """Every interventions.json flag has an InterventionSpec row and vice versa."""
    shipped = set(
        json.loads((fh.CI_FLAG_DIR / "interventions.json").read_text())["flags"]
    )
    base = set(
        json.loads((fh.CI_FLAG_DIR.parent / "interventions.json").read_text())["flags"]
    )
    specs = set(intervention_specs())
    assert shipped == specs, (
        f"ci/interventions.json vs INTERVENTION_SPECS: {sorted(shipped ^ specs)}"
    )
    assert base == specs, (
        f"interventions.json vs INTERVENTION_SPECS: {sorted(base ^ specs)}"
    )


def test_targeted_reads_use_the_spec_value_kind():
    """Per-serial reads (resolve_player_targets / _grant_values) pass the spec's value_kind.

    A handler that reads a per-player flag with another kind than INTERVENTION_SPECS
    declares uses a different getter than the one checked against flagd above.
    """
    kinds, uncovered = scan_targeted_value_kinds(_coordinator_sources())
    assert not uncovered, f"per-serial reads the scan cannot attribute: {uncovered}"
    assert kinds, "scan found no per-serial reads"
    specs = intervention_specs()
    mismatches = [
        f"{where}: {flag} read with value_kind={kind!r}, spec says {specs[flag]['value_kind']!r}"
        for flag, kind, where in kinds
        if flag not in specs or specs[flag]["value_kind"] != kind
    ]
    assert not mismatches, "\n".join(mismatches)


def test_targeted_value_kind_scan_catches_a_mismatch():
    source = (
        "def handle_x(ctx, manager):\n"
        '    manager.resolve_player_targets(flag_key=ctx.spec.flag_key, default=0, game=None, value_kind="int")\n'
        "def register(manager):\n"
        '    manager.register_handler("shield_seconds", lambda ctx: handle_x(ctx, manager))\n'
    )
    kinds, uncovered = scan_targeted_value_kinds({"h.py": source})
    assert not uncovered
    assert [(flag, kind) for flag, kind, _ in kinds] == [("shield_seconds", "int")]
    assert intervention_specs()["shield_seconds"]["value_kind"] != "int"


@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_every_variant_matches_the_coordinator_getter(flag_dir):
    """Static: EVERY variant value of each read flag has the getter's type.

    The live check resolves with an empty context, so a variant reached only by
    targeting (windows presets, sensitivity, experiment.mock_archetype) or by an
    agent defaultVariant flip would escape it.
    """
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    failures = []
    for read in sorted(
        set(all_flag_reads()), key=lambda r: (r.flag_set, r.key, r.getter)
    ):
        path = root / f"{read.flag_set}.json"
        flag = json.loads(path.read_text())["flags"].get(read.key)
        if flag is None:
            if flag_dir == "ci":
                failures.append(
                    f"{read.flag_set}/{read.key}: not in {flag_dir}/{path.name}"
                )
            continue
        for variant, value in flag["variants"].items():
            if not _type_ok(read.getter, value):
                failures.append(
                    f"{flag_dir}/{read.flag_set}/{read.key} variant {variant!r}={value!r} "
                    f"is not {read.getter} ({read.where})"
                )
    assert not failures, "\n".join(failures)


@pytest.mark.uc("UC-040", flow="main", steps=[13])
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_every_coordinator_flag_resolves_with_its_typed_getter(flagd, resolver):
    """Each flag the coordinator reads resolves with the coordinator's getter, no error.

    Covers every interventions.json flag (by InterventionSpec) and every game,
    system, agent, controller and user flag the coordinator reads. A flag that
    resolves with an error, the wrong type, or (when it has no targeting) a value
    other than its defaultVariant's is a silent fallback in production.
    """
    reads = all_flag_reads()
    unique: dict[tuple[str, str, str], list[str]] = {}
    for r in reads:
        unique.setdefault((r.flag_set, r.key, r.getter), []).append(r.where)

    failures = []
    with contextlib.ExitStack() as stack:
        clients = {}
        for (flag_set, key, getter), where in sorted(unique.items()):
            if flag_set not in clients:
                clients[flag_set] = stack.enter_context(
                    _ready_client(flagd, flag_set, resolver)
                )
            flag = json.loads(flagd.file(flag_set).read_text())["flags"].get(key)
            label = f"{flag_set}/{key} via get_{getter} ({', '.join(where)})"
            if flag is None:
                failures.append(f"{label}: not in ci/{flag_set}.json")
                continue
            details = getattr(clients[flag_set], f"get_{getter}_details")(
                key, _SENTINEL_DEFAULTS[getter], EvaluationContext()
            )
            if details.error_code is not None or details.reason == Reason.ERROR:
                failures.append(
                    f"{label}: {details.error_code} {details.error_message}"
                )
            elif not _type_ok(getter, details.value):
                failures.append(
                    f"{label}: wrong type {type(details.value).__name__} {details.value!r}"
                )
            elif "targeting" not in flag and not _same_value(
                details.value, flag["variants"][flag["defaultVariant"]]
            ):
                failures.append(
                    f"{label}: got {details.value!r}, defaultVariant is "
                    f"{flag['variants'][flag['defaultVariant']]!r}"
                )
    assert not failures, (
        f"{len(failures)} of {len(unique)} flag reads fail on {resolver}:\n"
        + "\n".join(failures)
    )


# --------------------------------------------------------------------------- #
# 2a. The agent reads the shield flags like the coordinator (#1837, UC-040 A31)
# --------------------------------------------------------------------------- #
# The agent's charge ledger reads shield.activation and shield.charge_expiry_seconds
# from the game set (services/agent/flags/game.go:ShieldCharge) with a SUBSET of the
# coordinator's evaluation context on the grant path: gameId, the game's game_kind and,
# for a shadow game of an experiment, experiment_id and arm (#2096, UC-043 A17). The coordinator reads both flags inside grant_shield, which the
# InterventionManager dispatches through GameLoopDispatcher.run in the game's flag
# transaction context (interventions.py run_in_game_flag_context, #1970): the API-level
# attributes, the game's context (targetingKey = game_mode, controller_count,
# sensitivity, game_kind) and gameId (pinned by
# services/game_coordinator/tests/test_shield_charge_flag_context.py). flags.md §4
# limits targeting on these two flags to gameId and game_kind (plus experiment_id and
# arm on shield.activation, which is in the experiment catalog since FR-162), so both
# resolve the same value.
SHIELD_LEDGER_FLAGS = {
    "shield.activation": "string",
    "shield.charge_expiry_seconds": "float",
}
SHIELD_TARGETING_VARS = {
    "shield.activation": {"gameId", "game_kind", "experiment_id", "arm"},
    "shield.charge_expiry_seconds": {"gameId", "game_kind"},
}
# The eight hold keys of FR-162 (UC-043 A17): the only shield keys in the experiment
# catalog. The agent's hold gate reads their effective default as what real games and
# shadow games outside an arm resolve, which holds only while they have no baseline
# targeting.
HOLD_KEYS = {
    "shield.activation",
    "shield.energy_max_seconds",
    "shield.energy_budget_seconds",
    "shield.energy_initial_seconds",
    "shield.recharge_per_second",
    "shield.min_burst_seconds",
    "shield.hold_rumble_intensity",
    "shield.empty_pulse_ms",
}
_CONTRACT_GAME_ID = "g-contract-1837"


def _coordinator_context() -> EvaluationContext:
    """lib/feature_flags on the grant path of a real FFA game: API-level attributes, the
    game's transaction context and gameId (see the comment above)."""
    return EvaluationContext(
        targeting_key="FFA",
        attributes={
            "game_mode": "FFA",
            "controller_count": 2,
            "sensitivity": 2,
            "service_name": "game-coordinator",
            "service_namespace": "core",
            "language": "python",
            "environment": "production",
            "hostname": "joustmania",
            "game_kind": "real",
            "gameId": _CONTRACT_GAME_ID,
        },
    )


def _agent_context(
    game_kind: str = "real", experiment_id: str | None = None, arm: str | None = None
) -> EvaluationContext:
    """Keep in sync with shieldChargeContext in services/agent/flags/game.go (its unit
    tests TestShieldCharge_CoordinatorContext and TestShieldCharge_ExperimentArmContext
    pin the Go side)."""
    attributes = {"game_kind": game_kind, "gameId": _CONTRACT_GAME_ID}
    if experiment_id:
        attributes["experiment_id"] = experiment_id
    if arm:
        attributes["arm"] = arm
    return EvaluationContext(attributes=attributes)


def _json_vars(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "var":
                name = value[0] if isinstance(value, list) and value else value
                if isinstance(name, str):
                    found.add(name)
            else:
                found |= _json_vars(value)
    elif isinstance(node, list):
        for item in node:
            found |= _json_vars(item)
    return found


@pytest.mark.uc("UC-040", flow="A31", steps=[1])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_shield_flag_targeting_uses_only_the_agents_context(flag_dir):
    """Static: targeting on the two shield flags may reference only the variables the
    agent's context carries (or flagd's own $flagd.* values), so the agent's narrower
    context resolves what the coordinator's does (flags.md §4)."""
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    flags = json.loads((root / "game.json").read_text())["flags"]
    failures = []
    for key in SHIELD_LEDGER_FLAGS:
        if key not in flags:
            failures.append(f"{flag_dir}/game.json has no {key}")
            continue
        extra = {
            v
            for v in _json_vars(flags[key].get("targeting", {}))
            if v not in SHIELD_TARGETING_VARS[key] and not v.startswith("$flagd.")
        }
        if extra:
            failures.append(
                f"{flag_dir}/game.json {key} targeting references {sorted(extra)}"
            )
    assert not failures, "\n".join(failures)


# The shield limits (#2044, UC-040 BR-022, flags.md §3): the coordinator reads them in
# the game's context, the agent with an empty one, so any targeting (even on the API-level
# game_kind the handler registry allows) could give the two sides different limits.
SHIELD_LIMIT_FLAGS = (
    "policy.max_shields_per_player_per_game",
    "policy.shield_cooldown_seconds",
)


@pytest.mark.uc("UC-040", flow="A34", br=["BR-022"])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_shield_limit_flags_carry_no_targeting(flag_dir):
    """Static: the two shield-limit flags exist in agent.json with no targeting at all."""
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    flags = json.loads((root / "agent.json").read_text())["flags"]
    failures = []
    for key in SHIELD_LIMIT_FLAGS:
        if key not in flags:
            failures.append(f"{flag_dir}/agent.json has no {key}")
        elif flags[key].get("targeting"):
            failures.append(f"{flag_dir}/agent.json {key} has targeting")
    assert not failures, "\n".join(failures)


# The group-grant policy flags (#2467, UC-040 BR-033, flags.md §3): the agent reads them
# with an empty context and the coordinator in the game's, so, like the shield limits,
# any targeting could give the two sides different values.
GROUP_GRANT_FLAGS = (
    "policy.group_grant.enabled",
    "policy.group_grant.real_games",
    "policy.group_grant.max_per_game",
    "policy.group_grant.max_targets",
)


@pytest.mark.uc("UC-040", br=["BR-033"])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_group_grant_flags_carry_no_targeting(flag_dir):
    """Static: the four group-grant flags exist in agent.json with no targeting; the switches default off or 0."""
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    flags = json.loads((root / "agent.json").read_text())["flags"]
    failures = []
    for key in GROUP_GRANT_FLAGS:
        flag = flags.get(key)
        if flag is None:
            failures.append(f"{flag_dir}/agent.json has no {key}")
            continue
        if flag.get("targeting"):
            failures.append(f"{flag_dir}/agent.json {key} has targeting")
        default = flag["variants"].get(flag["defaultVariant"])
        if key == "policy.group_grant.max_targets":
            if default != 4:
                failures.append(
                    f"{flag_dir}/agent.json {key} defaults to {default!r}, not 4"
                )
        elif default not in (False, 0):
            failures.append(
                f"{flag_dir}/agent.json {key} defaults to {default!r}, not off or 0"
            )
    assert not failures, "\n".join(failures)


# decision.prompt_profile (#2467 PR 5, UC-040 BR-036, flags.md §3): only the agent reads it,
# per remote request in the game's context (gameId, game_kind, experiment_id, arm). Its
# targeting may use only the game attributes an experiment needs, so a rule can never
# pick a wording by player, mode or anything the agent's context lacks; it ships with none.
PROMPT_PROFILE_FLAG = "decision.prompt_profile"
PROMPT_PROFILE_TARGETING_VARS = {"game_kind", "experiment_id", "arm"}


def _prompt_profile_targeting_violations(flag: dict) -> set[str]:
    """Variables the flag's targeting references beyond the allowed game attributes."""
    return {
        v
        for v in _json_vars(flag.get("targeting", {}))
        if v not in PROMPT_PROFILE_TARGETING_VARS and not v.startswith("$flagd.")
    }


@pytest.mark.uc("UC-040", br=["BR-036"])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_prompt_profile_targeting_only_game_attributes(flag_dir):
    """Static: decision.prompt_profile exists with v1/neutral, defaults to v1 and ships without targeting."""
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    flags = json.loads((root / "agent.json").read_text())["flags"]
    flag = flags.get(PROMPT_PROFILE_FLAG)
    assert flag is not None, f"{flag_dir}/agent.json has no {PROMPT_PROFILE_FLAG}"
    assert flag["variants"] == {"v1": "v1", "neutral": "neutral"}, flag["variants"]
    assert flag["defaultVariant"] == "v1"
    assert not _prompt_profile_targeting_violations(flag), (
        f"{flag_dir}/agent.json {PROMPT_PROFILE_FLAG} targeting references "
        f"{sorted(_prompt_profile_targeting_violations(flag))}"
    )
    assert not flag.get("targeting"), (
        f"{flag_dir}/agent.json {PROMPT_PROFILE_FLAG} must ship without targeting"
    )


@pytest.mark.uc("UC-040", br=["BR-036"])
def test_prompt_profile_targeting_guard_rejects_other_attributes():
    """The guard accepts game_kind / experiment_id / arm rules and rejects any other variable."""
    allowed = {
        "targeting": {
            "if": [
                {
                    "and": [
                        {"==": [{"var": "game_kind"}, "shadow"]},
                        {"==": [{"var": "experiment_id"}, "exp"]},
                        {"==": [{"var": "arm"}, "b"]},
                    ]
                },
                "neutral",
                "v1",
            ]
        }
    }
    assert not _prompt_profile_targeting_violations(allowed)
    for var in ("gameId", "targetingKey", "game_mode", "serial"):
        bad = {"targeting": {"if": [{"==": [{"var": var}, "x"]}, "neutral", "v1"]}}
        assert _prompt_profile_targeting_violations(bad) == {var}


INTERVENTION_SPEC_SOURCE = COORDINATOR_DIR / "interventions.py"


def _coordinator_type_ids() -> set[str]:
    """Every ``InterventionSpec(type_id=...)`` literal in the coordinator, parsed with ast."""
    tree = ast.parse(INTERVENTION_SPEC_SOURCE.read_text())
    type_ids = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "InterventionSpec"
        ):
            for kw in node.keywords:
                if kw.arg == "type_id" and isinstance(kw.value, ast.Constant):
                    type_ids.add(kw.value.value)
    return type_ids


@pytest.mark.uc("UC-040", br=["BR-033"])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_allow_list_variants_only_name_coordinator_types(flag_dir):
    """Static: no ``interventions_allowed`` variant names a type the coordinator has no spec for.

    The coordinator refuses such a type as ``not_allowed`` at the earliest, but the agent would
    still offer and decide it. A lever enters a variant only with its coordinator handler.
    """
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    variants = json.loads((root / "agent.json").read_text())["flags"][
        "interventions_allowed"
    ]["variants"]
    known = _coordinator_type_ids() | {
        "noop"
    }  # noop is the decide contract's "do nothing", never dispatched
    assert "grant_shield" in known and "end_game" in known, (
        "type_id parse found no specs"
    )
    failures = [
        f"{flag_dir}/agent.json interventions_allowed.{name} names {type_id!r}, which has no InterventionSpec"
        for name, ids in variants.items()
        for type_id in (t.strip() for t in ids.split(","))
        if type_id and type_id not in known
    ]
    assert not failures, "\n".join(failures)


GROUP_VARIANTS = {"standard_group", "shadow_group"}


@pytest.mark.uc("UC-040", flow="A47", br=["BR-033"])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_only_the_group_variants_name_grant_group_shield(flag_dir):
    """The group lever is opt-in (#2467 PR 4): only ``standard_group`` and ``shadow_group`` name it.

    ``standard_group`` is ``standard`` plus the lever and ``shadow_group`` is
    ``shadow_experimental`` plus the lever, so choosing a group variant changes nothing
    else; every other variant (the talk's ``standard`` included) stays without it.
    """
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    variants = json.loads((root / "agent.json").read_text())["flags"][
        "interventions_allowed"
    ]["variants"]

    def ids(name):
        return [t.strip() for t in variants[name].split(",") if t.strip()]

    named = {name for name in variants if "grant_group_shield" in ids(name)}
    assert named == GROUP_VARIANTS, (
        f"{flag_dir}/agent.json: grant_group_shield is named by {sorted(named)}, want {sorted(GROUP_VARIANTS)}"
    )
    assert ids("standard_group") == ids("standard") + ["grant_group_shield"]
    assert ids("shadow_group") == ids("shadow_experimental") + ["grant_group_shield"]


# Every flag an intervention handler reads (#1872, flags.md section 1.2). Since #1970 the
# handler runs in the game's flag transaction context (targetingKey = game_mode, game_mode,
# controller_count, sensitivity, game_kind, experiment_id, arm), with the read's own keys
# winning. The registry names the targeting variables each flag may use (narrower where
# another reader, the agent, resolves the flag with less);
# services/game_coordinator/tests/test_handler_flag_reads.py keeps it complete.
HANDLER_FLAG_READS_FILE = COORDINATOR_DIR / "handler_flag_reads.py"
FLAG_SERVICE_REGISTRY = ic.REPO_ROOT / "services" / "flag-service" / "registry.yaml"
# Always present in a handler read: the API-level attributes (lib/feature_flags.py).
HANDLER_API_VARS = {
    "service_name",
    "service_namespace",
    "language",
    "environment",
    "hostname",
    "game_kind",
}


def handler_flag_reads() -> dict[tuple[str, str], dict]:
    """The registry, read without importing the coordinator (this venv has none of its deps)."""
    tree = ast.parse(HANDLER_FLAG_READS_FILE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "HANDLER_FLAG_READS"
            for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{HANDLER_FLAG_READS_FILE} has no HANDLER_FLAG_READS")


def handler_read_targeting_failures(
    registry: dict[tuple[str, str], dict], flags_by_domain: dict[str, dict], where: str
) -> list[str]:
    """Targeting variables a handler-read flag uses beyond its declared ones."""
    failures = []
    for (domain, key), entry in sorted(registry.items()):
        flags = flags_by_domain.get(domain, {})
        if key not in flags:
            failures.append(f"{where}/{domain}.json has no {key}")
            continue
        allowed = set(entry["targeting"]) | HANDLER_API_VARS
        extra = {
            v
            for v in _json_vars(flags[key].get("targeting", {}))
            if v not in allowed and not v.startswith("$flagd.")
        }
        if extra:
            failures.append(
                f"{where}/{domain}.json {key} targeting references {sorted(extra)}, "
                f"a handler read has only {sorted(allowed)}"
            )
    return failures


def handler_read_scope_failures(
    registry: dict[tuple[str, str], dict], writer_flags: list[dict]
) -> list[str]:
    """Flag-service writer scopes that would put a rung in front of a handler read."""
    scopes = {(f["set"], f["key"]): set(f.get("scope", [])) for f in writer_flags}
    failures = []
    for (domain, key), entry in sorted(registry.items()):
        scope = scopes.get((domain, key), set())
        targeting = set(entry["targeting"])
        applies = entry.get("experiment_rung") == "applies"
        if "experiment+arm" in scope and not applies:
            failures.append(
                f"{domain}.{key} has the experiment+arm scope but is not declared experiment_rung applies"
            )
        if "experiment+arm" in scope and not {"experiment_id", "arm"} <= targeting:
            failures.append(
                f"{domain}.{key} has the experiment+arm scope but its targeting does not declare experiment_id and arm"
            )
        if applies and "experiment+arm" not in scope:
            failures.append(
                f"{domain}.{key} is declared experiment_rung applies but has no experiment+arm scope"
            )
        if scope & {"game", "game+serial"} and "gameId" not in targeting:
            failures.append(
                f"{domain}.{key} has a game rung but its handler read does not pass gameId"
            )
        if "game+serial" in scope and "targetingKey" not in targeting:
            failures.append(
                f"{domain}.{key} has a game+serial rung but its handler read passes no targetingKey"
            )
    return failures


def _flag_files(flag_dir: str) -> dict[str, dict]:
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    return {
        path.stem: json.loads(path.read_text())["flags"]
        for path in sorted(root.glob("*.json"))
    }


@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_handler_read_flag_targeting_uses_only_the_handler_context(flag_dir):
    """Static: targeting on a flag an intervention handler reads may use only the variables
    the registry declares (plus the API-level attributes and $flagd.*)."""
    registry = handler_flag_reads()
    assert registry, "the handler flag registry is empty"
    failures = handler_read_targeting_failures(
        registry, _flag_files(flag_dir), flag_dir
    )
    assert not failures, "\n".join(failures)


@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
def test_handler_read_flags_writer_scope():
    """Static: the flag service never renders a rung a handler read cannot match."""
    registry = handler_flag_reads()
    writer_flags = ic.load_yaml(FLAG_SERVICE_REGISTRY)["flags"]
    failures = handler_read_scope_failures(registry, writer_flags)
    assert not failures, "\n".join(failures)
    # The scope check sees the flags it is meant to guard.
    assert any(f["set"] == "game" and f["key"] == "windows" for f in writer_flags)


@pytest.mark.uc("UC-040", flow="A31", br=["BR-018"])
def test_handler_read_checks_report_a_violation():
    """Negative fixture: a game_mode rung, a missing flag and an undeclared scope are reported."""
    registry = {
        ("game", "shield.activation"): {"targeting": ["gameId"]},
        ("game", "shield.charge_rumble_intensity"): {"targeting": []},
        ("game", "windows"): {
            "targeting": ["targetingKey", "experiment_id", "arm"],
            "experiment_rung": "applies",
        },
        ("game", "ghost"): {"targeting": []},
    }
    flags = {
        "game": {
            "shield.activation": {
                "targeting": {
                    "if": [{"==": [{"var": "game_mode"}, "FFA"]}, "trigger", "instant"]
                }
            },
            "shield.charge_rumble_intensity": {},
            "windows": {
                "targeting": {
                    "if": [{"==": [{"var": "targetingKey"}, "calm"]}, "calm", "default"]
                }
            },
        }
    }
    failures = handler_read_targeting_failures(registry, flags, "fixture")
    assert len(failures) == 2
    assert "ghost" in failures[0]
    assert "shield.activation" in failures[1] and "game_mode" in failures[1]

    scope_failures = handler_read_scope_failures(
        registry,
        [
            {"set": "game", "key": "shield.activation", "scope": ["stack"]},
            {
                "set": "game",
                "key": "shield.charge_rumble_intensity",
                "scope": ["experiment+arm"],
            },
            {"set": "game", "key": "windows", "scope": ["stack"]},
            {"set": "game", "key": "ghost", "scope": ["game"]},
        ],
    )
    assert len(scope_failures) == 4
    assert "ghost" in scope_failures[0] and "does not pass gameId" in scope_failures[0]
    assert (
        "shield.charge_rumble_intensity" in scope_failures[1]
        and "not declared experiment_rung applies" in scope_failures[1]
    )
    assert (
        "shield.charge_rumble_intensity" in scope_failures[2]
        and "does not declare experiment_id and arm" in scope_failures[2]
    )
    assert (
        "windows" in scope_failures[3]
        and "no experiment+arm scope" in scope_failures[3]
    )


@pytest.mark.uc("UC-043", flow="A17", steps=[1])
@pytest.mark.uc("UC-040", br=["BR-019", "BR-027"])
def test_hold_keys_are_the_only_shield_keys_in_the_experiment_catalog():
    """Static (FR-162, C-034): exactly the eight hold keys have the experiment+arm and
    real scopes, each is declared experiment_rung applies with experiment_id and arm in
    its handler targeting; charge_expiry and the #2094 cue flags stay stack-only with no
    rung."""
    registry = handler_flag_reads()
    writer_flags = ic.load_yaml(FLAG_SERVICE_REGISTRY)["flags"]
    shield_rows = {
        f["key"]: set(f.get("scope", []))
        for f in writer_flags
        if f["set"] == "game" and f["key"].startswith("shield.")
    }
    widened = {k for k, scope in shield_rows.items() if "experiment+arm" in scope}
    assert widened == HOLD_KEYS
    for key in HOLD_KEYS:
        assert shield_rows[key] == {"stack", "experiment+arm", "real"}, key
        entry = registry[("game", key)]
        assert entry.get("experiment_rung") == "applies", key
        assert {"gameId", "experiment_id", "arm"} <= set(entry["targeting"]), key
    for key in set(shield_rows) - HOLD_KEYS:
        assert shield_rows[key] == {"stack"}, key
        entry = registry.get(("game", key))
        if entry is not None:
            assert "experiment_rung" not in entry, key


@pytest.mark.uc("UC-043", flow="A17", steps=[1])
@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_hold_keys_have_no_baseline_targeting(flag_dir):
    """Static: the agent's hold gate reads a hold key's effective default as the value
    real games and shadow games outside an arm resolve (UC-043 A17 step 1); that is
    true only while the key has no baseline targeting."""
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    flags = json.loads((root / "game.json").read_text())["flags"]
    with_targeting = sorted(
        k for k in HOLD_KEYS if k in flags and flags[k].get("targeting")
    )
    assert not with_targeting, f"{flag_dir}/game.json: {with_targeting}"
    assert HOLD_KEYS <= set(flags), (
        f"{flag_dir}/game.json misses {HOLD_KEYS - set(flags)}"
    )


@pytest.mark.uc("UC-040", flow="A31", steps=[1])
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_agent_reads_the_shield_flags_like_the_coordinator(flagd, resolver):
    """Live: each shield flag resolves, with the coordinator's getter, to the same value
    under the coordinator's context and the agent's context (amendment A1 of #1837 PR-4)."""
    failures = []
    with _ready_client(flagd, "game", resolver) as client:
        for key, getter in SHIELD_LEDGER_FLAGS.items():
            read = getattr(client, f"get_{getter}_details")
            coordinator = read(key, _SENTINEL_DEFAULTS[getter], _coordinator_context())
            agent = read(key, _SENTINEL_DEFAULTS[getter], _agent_context())
            for who, details in (("coordinator", coordinator), ("agent", agent)):
                if details.error_code is not None or details.reason == Reason.ERROR:
                    failures.append(
                        f"{key} ({who}): {details.error_code} {details.error_message}"
                    )
            if (
                not _same_value(coordinator.value, agent.value)
                or coordinator.variant != agent.variant
            ):
                failures.append(
                    f"{key}: coordinator {coordinator.value!r} ({coordinator.variant}) != "
                    f"agent {agent.value!r} ({agent.variant})"
                )
    assert not failures, f"on {resolver}:\n" + "\n".join(failures)


# --------------------------------------------------------------------------- #
# 2b. Agent tuning flags migrated from env (#1788, flags.md §15)
# --------------------------------------------------------------------------- #
AGENT_TUNING_GO = ic.REPO_ROOT / "services" / "agent" / "flags" / "tuning.go"
_GO_KEY_CONST = re.compile(r'^\s*(key\w+)\s*=\s*"([^"]+)"', re.MULTILINE)
# The flags.Flags helper each getter calls -> the typed OpenFeature getter behind it
# (secondsFlag -> intFlag -> IntValue; durationFlag -> floatFlag -> FloatValue).
_GO_HELPERS = {
    "boolFlag": "boolean",
    "stringFlag": "string",
    "intFlag": "integer",
    "secondsFlag": "integer",
    "floatFlag": "float",
    "durationFlag": "float",
}
_GO_HELPER_CALL = re.compile(r"f\.(\w+Flag)\(ctx, (key\w+)")


def agent_tuning_reads() -> list[tuple[str, str]]:
    """(key, getter) of every tuning flag the agent reads, derived from tuning.go."""
    source = AGENT_TUNING_GO.read_text()
    consts = dict(_GO_KEY_CONST.findall(source))
    reads = set()
    for helper, const in _GO_HELPER_CALL.findall(source):
        if helper in _GO_HELPERS and const in consts:
            reads.add((consts[const], _GO_HELPERS[helper]))
    return sorted(reads)


def test_agent_tuning_scan_sees_every_key():
    """Guards the scan: every key constant in tuning.go is read through a known helper."""
    source = AGENT_TUNING_GO.read_text()
    keys = set(dict(_GO_KEY_CONST.findall(source)).values())
    assert len(keys) >= 7, keys
    assert {k for k, _ in agent_tuning_reads()} == keys


@pytest.mark.parametrize("flag_dir", ["ci", "base"])
def test_agent_tuning_variants_match_the_getter(flag_dir):
    """Static: each tuning flag exists in agent.json and every variant has the getter's type."""
    root = fh.CI_FLAG_DIR if flag_dir == "ci" else fh.CI_FLAG_DIR.parent
    flags = json.loads((root / "agent.json").read_text())["flags"]
    failures = []
    for key, getter in agent_tuning_reads():
        flag = flags.get(key)
        if flag is None:
            failures.append(f"agent/{key}: not in {flag_dir}/agent.json")
            continue
        for variant, value in flag["variants"].items():
            if not _type_ok(getter, value):
                failures.append(
                    f"{flag_dir}/agent/{key} variant {variant!r}={value!r} is not {getter}"
                )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_agent_tuning_flags_resolve_with_their_typed_getter(flagd, resolver):
    """Each tuning flag resolves on real flagd with the agent's getter, to its defaultVariant."""
    failures = []
    with _ready_client(flagd, "agent", resolver) as client:
        for key, getter in agent_tuning_reads():
            flag = json.loads(flagd.file("agent").read_text())["flags"].get(key)
            if flag is None:
                failures.append(f"agent/{key}: not in ci/agent.json")
                continue
            details = getattr(client, f"get_{getter}_details")(
                key, _SENTINEL_DEFAULTS[getter], EvaluationContext()
            )
            want = flag["variants"][flag["defaultVariant"]]
            if details.error_code is not None or details.reason == Reason.ERROR:
                failures.append(
                    f"agent/{key} via get_{getter}: {details.error_code} {details.error_message}"
                )
            elif not _type_ok(getter, details.value) or not _same_value(
                details.value, want
            ):
                failures.append(
                    f"agent/{key} via get_{getter}: got {details.value!r}, defaultVariant is {want!r}"
                )
    assert not failures, "\n".join(failures)


# --------------------------------------------------------------------------- #
# 3. windows preset ladder (from test_f6_flow.py)
# --------------------------------------------------------------------------- #
def test_windows_preset_ladder_resolves_against_live_flagd(flagd):
    """The F6 ``windows`` targeting if-ladder (game.json) through flagd RPC: a
    known preset key (``calm`` / ``frantic``) resolves its preset window object;
    an unknown key falls through to the ``default`` variant. The coordinator's
    pacing_profile handler resolves the same flag the same way
    (read_object_flag_variant("game", "windows", profile, {})).
    """
    with _ready_client(flagd, "game") as client:

        def _resolve(preset: str) -> dict:
            return client.get_object_value(
                "windows", {}, EvaluationContext(targeting_key=preset)
            )

        calm = _resolve("calm")
        frantic = _resolve("frantic")
        default = _resolve("default")
        unknown = _resolve("SER_NOT_A_PRESET")

        assert calm.get("min_music_slow_time") == 13, f"calm resolved wrong: {calm}"
        assert frantic.get("min_music_fast_time") == 6, (
            f"frantic resolved wrong: {frantic}"
        )
        assert calm != frantic, "calm and frantic resolved to the same object"
        assert unknown == default, (
            f"unknown preset did not fall back to default: {unknown} != {default}"
        )
        assert unknown.get("min_music_fast_time") == 4, (
            f"default variant has unexpected values: {unknown}"
        )


# --------------------------------------------------------------------------- #
# 3a. Handler reads in the game's context (#1970, UC-040 BR-018, UC-043 BR-005)
# --------------------------------------------------------------------------- #
_EXPERIMENT_ID = "exp_contract_1970"
# The flag service inlines an experiment value under a generated variant name (write.go).
_EXPERIMENT_VARIANT = "r1970"
_EXPERIMENT_WINDOWS = {
    "min_music_fast_time": 5,
    "max_music_fast_time": 9,
    "min_music_slow_time": 17,
    "max_music_slow_time": 40,
    "end_min_music_fast_time": 6,
    "end_max_music_fast_time": 10,
    "end_min_music_slow_time": 8,
    "end_max_music_slow_time": 12,
}
# The API-level attributes every coordinator read carries (lib/feature_flags.py).
_API_CONTEXT = {
    "service_name": "game-coordinator",
    "service_namespace": "core",
    "language": "python",
    "environment": "production",
    "hostname": "joustmania",
    "game_kind": "real",
}


def _experiment_rung(experiment_id: str, arm: str) -> dict:
    """Same shape as the experiment rung in services/flag-service/state/render.go."""
    return {
        "and": [
            {"!=": [{"var": "game_kind"}, "real"]},
            _eq("experiment_id", experiment_id),
            _eq("arm", arm),
        ]
    }


def _game_context(
    game_kind: str, experiment_id: str | None = None, arm: str | None = None
):
    """API level merged with set_game_transaction_context (lib/feature_flags.py) for FFA."""
    attributes = {
        "game_mode": "FFA",
        "controller_count": 2,
        "sensitivity": 2,
        "game_kind": game_kind,
    }
    if experiment_id:
        attributes["experiment_id"] = experiment_id
    if arm:
        attributes["arm"] = arm
    return EvaluationContext(attributes=_API_CONTEXT).merge(
        EvaluationContext(targeting_key="FFA", attributes=attributes)
    )


def _handler_read(game_ctx, invocation):
    """The SDK merge order: API, transaction, then the read's invocation context (wins)."""
    return game_ctx.merge(invocation)


_GAMES = {
    "real": _game_context("real"),
    "shadow-no-experiment": _game_context("shadow"),
    "control-arm": _game_context("shadow", _EXPERIMENT_ID, "control"),
    "experimental-arm": _game_context("shadow", _EXPERIMENT_ID, "experimental"),
}


@pytest.mark.uc("UC-043", br=["BR-005"])
@pytest.mark.uc("UC-040", br=["BR-018"])
@pytest.mark.parametrize("flag_source", ["ci", "base"])
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_windows_experiment_rung_reaches_only_the_experimental_arm(
    flagd, resolver, flag_source
):
    """A render.go-shaped experiment rung (generated variant, inline value) in front of the
    windows preset ladder, read as handle_pacing_profile does (game context + targetingKey =
    profile): only the experimental arm resolves the experiment's value; the real game, a
    non-experiment shadow game and the control arm resolve the preset; the baseline read
    (invocation game_kind=real) resolves the preset in the experimental arm too."""
    root = fh.CI_FLAG_DIR if flag_source == "ci" else fh.CI_FLAG_DIR.parent
    source = json.loads((root / "game.json").read_text())["flags"]["windows"]
    path = flagd.file("game")
    original = fh.load(path)
    doc = json.loads(json.dumps(original))
    windows = json.loads(json.dumps(source))
    windows["variants"][_EXPERIMENT_VARIANT] = _EXPERIMENT_WINDOWS
    windows["targeting"] = {
        "if": [
            _experiment_rung(_EXPERIMENT_ID, "experimental"),
            _EXPERIMENT_VARIANT,
            source["targeting"],
        ]
    }
    doc["flags"]["windows"] = windows
    fh.dump_in_place(path, doc)
    calm = source["variants"]["calm"]

    try:
        with _ready_client(flagd, "game", resolver=resolver) as client:

            def read(game: str, profile: str = "calm", **extra) -> dict:
                invocation = EvaluationContext(targeting_key=profile, attributes=extra)
                details = client.get_object_details(
                    "windows", {}, _handler_read(_GAMES[game], invocation)
                )
                assert details.error_code is None, (
                    f"{resolver} {game}: {details.error_code}"
                )
                return details.value

            assert fh.poll_until(
                lambda: read("experimental-arm") == _EXPERIMENT_WINDOWS
            ), (
                f"{resolver}: the experiment rung never served: {read('experimental-arm')}"
            )
            for game in ("real", "shadow-no-experiment", "control-arm"):
                assert read(game) == calm, f"{resolver} {game}: {read(game)}"
            assert read(game="experimental-arm", game_kind="real") == calm
            assert read("experimental-arm", profile="frantic") == _EXPERIMENT_WINDOWS
    finally:
        fh.dump_in_place(path, original)


@pytest.mark.uc("UC-043", flow="A17", steps=[1])
@pytest.mark.uc("UC-040", flow="A31", steps=[1])
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_agent_reads_the_activation_arm_like_the_coordinator(flagd, resolver):
    """Live (#2096): with a render.go-shaped experiment rung for shield.activation=hold,
    the coordinator's grant read in a shadow game (game context + gameId) and the agent's
    context for that game (game_kind, experiment_id, arm, gameId) resolve the same value
    in each arm: hold in the experimental arm, the effective default in the control arm
    and in a real game."""
    path = flagd.file("game")
    original = fh.load(path)
    doc = json.loads(json.dumps(original))
    activation = doc["flags"]["shield.activation"]
    baseline = activation["defaultVariant"]
    activation["targeting"] = {
        "if": [_experiment_rung(_EXPERIMENT_ID, "experimental"), "hold", None]
    }
    fh.dump_in_place(path, doc)
    gid = EvaluationContext(attributes={"gameId": _CONTRACT_GAME_ID})
    try:
        with _ready_client(flagd, "game", resolver=resolver) as client:

            def read(ctx) -> str:
                details = client.get_string_details("shield.activation", "", ctx)
                assert details.error_code is None, f"{resolver}: {details.error_code}"
                return details.value

            def coordinator(game: str) -> str:
                return read(_handler_read(_GAMES[game], gid))

            assert fh.poll_until(lambda: coordinator("experimental-arm") == "hold"), (
                f"{resolver}: the experiment rung never served"
            )
            arms = {
                "experimental-arm": ("shadow", _EXPERIMENT_ID, "experimental"),
                "control-arm": ("shadow", _EXPERIMENT_ID, "control"),
                "real": ("real", None, None),
            }
            for game, (kind, exp, arm) in arms.items():
                agent = read(_agent_context(kind, exp, arm))
                assert agent == coordinator(game), f"{resolver} {game}: agent {agent!r}"
            want = activation["variants"][baseline]
            assert coordinator("control-arm") == want
            assert coordinator("real") == want
    finally:
        fh.dump_in_place(path, original)


def _getter_for(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "float"
    return "object"


@pytest.mark.uc("UC-040", br=["BR-018"])
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_handler_read_flags_resolve_the_same_with_the_game_context(flagd, resolver):
    """#1970 changes nothing for a real or non-experiment shadow game: every handler-read
    flag resolves the same value and variant with the game context as with the API-level
    context alone (the pre-#1970 handler context), for the read's own invocation keys."""
    registry = handler_flag_reads()
    files = _flag_files("ci")
    failures = []
    for (domain, key), _entry in sorted(registry.items()):
        flag = files[domain][key]
        getter = _getter_for(flag["variants"][flag["defaultVariant"]])
        default = _SENTINEL_DEFAULTS[getter]
        # What each handler passes itself (handler_flag_reads.py "readers").
        targeting_key = "calm" if key == "windows" else None
        invocation = EvaluationContext(
            targeting_key=targeting_key, attributes={"gameId": _CONTRACT_GAME_ID}
        )
        with _ready_client(flagd, domain, resolver=resolver) as client:
            read = getattr(client, f"get_{getter}_details")
            before = read(
                key,
                default,
                EvaluationContext(attributes=_API_CONTEXT).merge(invocation),
            )
            for game in ("real", "shadow-no-experiment"):
                after = read(key, default, _handler_read(_GAMES[game], invocation))
                if (
                    after.error_code is not None
                    or not _same_value(after.value, before.value)
                    or after.variant != before.variant
                ):
                    failures.append(
                        f"{domain}.{key} ({game}): {after.value!r} ({after.variant}) "
                        f"!= {before.value!r} ({before.variant})"
                    )
    assert not failures, f"on {resolver}:\n" + "\n".join(failures)


# --------------------------------------------------------------------------- #
# 4. Snapshot stamp reaches the in-process resolver (UC-053 A9, #1552)
# --------------------------------------------------------------------------- #
# The keys the flag service writes (services/flag-service/state/render.go) and
# the game coordinator reads (interventions.py SNAPSHOT_*_KEY).
_STAMPED = (
    ("interventions", "end_game", "object"),
    ("agent", "interventions_allowed", "string"),
)


@pytest.mark.uc("UC-053", flow="A9", tc="TC-019")
def test_snapshot_stamp_reaches_the_in_process_resolver(flagd):
    """The coordinator's cross-set gate compares the snapshot stamp in the flag
    metadata of ``interventions/end_game`` and ``agent/interventions_allowed``.
    It only works if real flagd passes flag metadata, with an integer version,
    through its sync stream to the in-process resolver."""
    for flag_set, key, _ in _STAMPED:
        path = flagd.file(flag_set)
        doc = fh.load(path)
        doc["flags"][key].setdefault("metadata", {}).update(
            {"snapshotEpoch": "contract", "snapshotVersion": 4242}
        )
        fh.dump_in_place(path, doc)

    for flag_set, key, kind in _STAMPED:
        with _ready_client(flagd, flag_set, resolver="in_process") as client:
            read = getattr(client, f"get_{kind}_details")
            default = {} if kind == "object" else ""

            def stamp(read=read, key=key, default=default):
                meta = read(key, default, EvaluationContext()).flag_metadata or {}
                return meta.get("snapshotEpoch"), meta.get("snapshotVersion")

            assert fh.poll_until(lambda: stamp() == ("contract", 4242)), (
                f"{flag_set}/{key} stamp through flagd: {stamp()}"
            )


# --------------------------------------------------------------------------- #
# 5. gameId / serial intervention targeting on both resolvers (#1479)
# --------------------------------------------------------------------------- #
def _eq(var: str, value) -> dict:
    """Same shape as ``eq`` in services/flag-service/state/render.go."""
    return {"==": [{"var": var}, value]}


@pytest.mark.uc("UC-053", flow="main", br=["BR-003"])
@pytest.mark.parametrize("resolver", ["rpc", "in_process"])
def test_gameid_and_serial_scoped_targeting_resolves_on_both_resolvers(flagd, resolver):
    """The rungs the flag service renders (render.go: ``and`` of gameId and
    targetingKey per player, bare gameId per game) resolve on real flagd through
    BOTH the RPC and the in-process resolver (FR-136). Flag-service render tests
    evaluate with Go jsonlogic, not flagd. A context that matches no rung
    (other serial, other game, no gameId) resolves the default with no error.
    """
    path = flagd.file("interventions")
    original = fh.load(path)
    doc = json.loads(json.dumps(original))
    doc["flags"]["player_sensitivity_factor"]["targeting"] = {
        "if": [
            {"and": [_eq("gameId", "game_a"), _eq("targetingKey", "SER_A")]},
            "handicap",
            {"and": [_eq("gameId", "game_a"), _eq("targetingKey", "SER_B")]},
            "protect",
            None,
        ]
    }
    doc["flags"]["global_sensitivity_override"]["targeting"] = {
        "if": [_eq("gameId", "game_a"), "fast", None]
    }
    fh.dump_in_place(path, doc)

    def ctx(serial: str | None, game_id: str | None) -> EvaluationContext:
        attributes = {"gameId": game_id} if game_id is not None else {}
        return EvaluationContext(targeting_key=serial, attributes=attributes)

    try:
        with _ready_client(flagd, "interventions", resolver=resolver) as client:

            def player(serial, game_id):
                return client.get_float_details(
                    "player_sensitivity_factor", 1.0, ctx(serial, game_id)
                )

            def game(game_id):
                return client.get_integer_details(
                    "global_sensitivity_override", -1, ctx(None, game_id)
                )

            assert fh.poll_until(
                lambda: (
                    player("SER_A", "game_a").value == 1.5
                    and player("SER_B", "game_a").value == 0.7
                    and game("game_a").value == 3
                )
            ), (
                f"{resolver}: targeting never served: "
                f"{player('SER_A', 'game_a')} {player('SER_B', 'game_a')}"
            )

            # (serial, gameId) pairs that match no rung: default, no error.
            for serial, game_id in (
                ("SER_UNKNOWN", "game_a"),  # unknown serial
                ("SER_A", "game_b"),  # same serial, other game
                ("SER_A", None),  # no gameId in the context
                (None, None),  # empty context
            ):
                details = player(serial, game_id)
                assert details.error_code is None, (
                    f"{resolver} {serial}/{game_id}: {details.error_code} "
                    f"{details.error_message}"
                )
                assert details.value == 1.0, (
                    f"{resolver} {serial}/{game_id}: want default 1.0, got {details.value}"
                )

            for game_id in ("game_b", None):
                details = game(game_id)
                assert details.error_code is None, (
                    f"{resolver} game {game_id}: {details.error_code}"
                )
                assert details.value == -1, (
                    f"{resolver} game {game_id}: want default -1, got {details.value}"
                )
    finally:
        fh.dump_in_place(path, original)
