"""flagd contract tier: real flagd only, no game stack (#1422).

The Go agent's unit tests and the Python services' unit tests evaluate jsonlogic
in-process or mock the provider. What only the real flagd server shows is
checked here, against ONE flagd container (the image docker-compose.yml runs,
v0.16.0) serving a temporary copy of ``services/flagd/ci`` (see flagd_harness):

- typed resolution: every flag the game coordinator reads resolves, through the
  RPC and the in-process resolver, with the SAME typed getter the coordinator
  calls. A wrong getter does not raise: it returns the caller's default with
  TYPE_MISMATCH, so an intervention or calibration flag silently stops working
  (#903, #1127). The getter is derived from the coordinator source, not copied;
- server-side targeting: the per-player ladder the agent's Writer writes, the
  ``windows`` preset ladder, the rollout ladder the RolloutWriter walks, and
  the shadow-only experiment rule (a REAL game never sees an experiment);
- NFR-014: how long an in-place flag-file edit takes to reach live readers.

These tests moved here from tests/integration (they never needed the game
services): test_intervention_flow.py, test_f6_flow.py, test_rollout_plumbing.py,
test_experiment_shadow_isolation.py and test_flag_propagation.py.

Docker is required: a missing docker fails in CI (``CI`` set) and skips locally.
"""

from __future__ import annotations

import ast
import contextlib
import json
import math
import os
import random
import re
import statistics
import time
import uuid
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


@pytest.fixture
def flag_files(flagd):
    """Snapshot every served flag file; restore the changed ones after the test.

    A restored file also gets a fresh RESTORE_PROBE_FLAG nonce, and the fixture
    waits until both resolvers serve it: flagd reloads a file as a whole, so the
    restored content is then live before the next test runs. The restored file
    therefore KEEPS the extra ``contract_restore_epoch`` flag (it is not
    byte-identical to services/flagd/ci); the typed-getter tests, which compare
    the served flag sets to the shipped files, run before any mutating test.
    """
    originals = {p: p.read_text() for p in flagd.flag_dir.glob("*.json")}
    yield
    for path, text in originals.items():
        if path.read_text() == text:
            continue
        doc = json.loads(text)
        nonce = uuid.uuid4().hex
        doc["flags"][fh.RESTORE_PROBE_FLAG] = {
            "state": "ENABLED",
            "variants": {"epoch": nonce},
            "defaultVariant": "epoch",
        }

        def _restore(path=path, doc=doc):
            fh.dump_in_place(path, doc)

        _restore()
        flag_set = doc.get("metadata", {}).get("flagSetId", path.stem)
        assert flagd.wait_served(
            flag_set, fh.RESTORE_PROBE_FLAG, nonce, rewrite=_restore
        ), f"flagd did not serve the restored {path.name}"


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
    "read_float_flag": "float",
    "read_int_flag": "float",
    "read_string_flag": "string",
    "read_object_flag": "object",
    "read_object_flag_variant": "object",
}

# Coordinator wrappers that take the flag key as a parameter:
# name -> (flag set, getter, key parameter name).
# games/base.py _read_volume_flag: get_flag_client("user").get_float_value(flag_name).
_WRAPPERS = {"_read_volume_flag": ("user", "float", "flag_name")}

# Getter call sites with a non-literal key that are covered another way, as
# (file, enclosing function, key expression). Exact sites only: the same
# expression anywhere else is reported as uncovered.
_COVERED_NON_LITERAL = {
    ("interventions.py", "_read_grant", "spec.flag_key"): "INTERVENTION_SPECS",
    ("interventions.py", "_read_flag", "spec.flag_key"): "INTERVENTION_SPECS",
    ("interventions.py", "_read_edge_value", "spec.flag_key"): "INTERVENTION_SPECS",
    (
        "interventions.py",
        "resolve_player_targets",
        "flag_key",
    ): "targeted_value_kinds()",
    ("base.py", "_read_volume_flag", "flag_name"): "_WRAPPERS",
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
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if (
                _call_name(node.value.func) == "get_flag_client"
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
        filename = Path(rel).name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = _call_name(func)
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
            elif isinstance(func, ast.Attribute) and _GETTER_CALL.match(func.attr):
                getter = _GETTER_CALL.match(func.attr).group(1)
                flag_set = domains.get(ast.unparse(func.value))
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

    Edge-triggered flags are read with the string getter (``"<nonce>:<payload>"``);
    state flags by ``value_kind`` (``_read_flag``; per-serial reads must use the
    same kind, checked by test_targeted_reads_use_the_spec_value_kind). Parsed
    from the source so the bare contract venv needs no service deps.
    """
    return [
        FlagRead(
            "interventions",
            key,
            "string" if spec["edge_triggered"] else _KIND_TO_GETTER[spec["value_kind"]],
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
    "unknown_helper": ('read_bool_flag("game", "k", False)\n', None),
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
# 2. Per-player targeting ladder (from test_intervention_flow.py)
# --------------------------------------------------------------------------- #
def write_targeted(path: Path, flag_key: str, neutral: str, serial: str, value) -> None:
    """Per-player state write with a flagd targeting if-ladder.

    Mirrors Writer.setTargeted (services/agent/actions/writer.go): a per-serial
    ``agent_<serial>`` variant plus a nested ``if(=== targetingKey serial,
    agent_<serial>, ... neutral)`` ladder, defaultVariant flipped to the neutral
    variant. Merges with existing agent-driven serials.
    """
    doc = fh.load(path)
    flag = doc["flags"][flag_key]
    flag["variants"][f"agent_{serial}"] = value
    agent_serials = sorted(
        name[len("agent_") :] for name in flag["variants"] if name.startswith("agent_")
    )
    expr: object = neutral
    for s in reversed(agent_serials):
        expr = {"if": [{"===": [{"var": "targetingKey"}, s]}, f"agent_{s}", expr]}
    flag["targeting"] = expr
    flag["defaultVariant"] = neutral
    fh.dump_in_place(path, doc)


def test_per_player_targeting_resolves_against_live_flagd(flagd, flag_files):
    """The written per-serial if-ladder resolves through flagd's server-side
    evaluation (RPC) for distinct serials, and an unknown serial falls through
    to the neutral default. Catches schema/targeting-rule rejections the Go unit
    tests (in-process jsonlogic) cannot.
    """
    path = flagd.file("interventions")
    serial_a, serial_b, unknown = "SER_A", "SER_B", "SER_UNKNOWN"

    def _write_branches():
        write_targeted(path, "player_sensitivity_factor", "default", serial_a, 1.5)
        write_targeted(path, "player_sensitivity_factor", "default", serial_b, 0.7)

    _write_branches()
    # resolver="rpc" deliberately: in-process would evaluate the jsonlogic
    # client-side and mask a server-side rule rejection.
    with _ready_client(flagd, "interventions") as client:

        def _eval(serial: str) -> float:
            return client.get_float_value(
                "player_sensitivity_factor",
                1.0,
                EvaluationContext(targeting_key=serial),
            )

        assert fh.poll_until(
            lambda: (
                abs(_eval(serial_a) - 1.5) <= 0.01
                and abs(_eval(serial_b) - 0.7) <= 0.01
            ),
            rewrite=_write_branches,
        ), (
            f"ladder did not resolve: serial_a={_eval(serial_a)} (want 1.5), serial_b={_eval(serial_b)} (want 0.7)"
        )
        assert abs(_eval(unknown) - 1.0) <= 0.01, (
            f"unknown resolved to {_eval(unknown)}"
        )


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
# 4. Rollout ladder (from test_rollout_plumbing.py)
# --------------------------------------------------------------------------- #
def _set_default_variant(path: Path, flag_key: str, variant: str) -> None:
    """Flip a rollout flag's defaultVariant, as RolloutWriter does
    (services/agent/actions/rollout_writer.go). The agent never invents
    variants, so a missing variant is an error here too."""
    doc = fh.load(path)
    flag = doc["flags"][flag_key]
    assert variant in flag["variants"], (
        f"variant {variant!r} not in {flag_key}.variants ({sorted(flag['variants'])}) - agent never invents variants"
    )
    flag["defaultVariant"] = variant
    fh.dump_in_place(path, doc)


def test_rollout_write_shape_resolves_via_live_flagd(flagd, flag_files):
    """The agent's rollout write shape (strategy=progressive, target_backend flip,
    current_controller_count along the none -> one -> three -> six -> all
    ladder, rollback to none) resolves to the expected values through the flagd
    RPC the controller-manager reads.
    """
    path = flagd.file("rollout")
    _set_default_variant(path, "strategy", "progressive")
    _set_default_variant(path, "target_backend", "rust")

    with _ready_client(flagd, "rollout") as client:
        empty = EvaluationContext()
        # Both polled: separate file versions, so serving one flip does not
        # prove flagd loaded the other.
        assert fh.poll_until(
            lambda: client.get_string_value("strategy", "off", empty) == "progressive",
            rewrite=lambda: _set_default_variant(path, "strategy", "progressive"),
        ), f"strategy resolved to {client.get_string_value('strategy', 'off', empty)!r}"
        assert fh.poll_until(
            lambda: (
                client.get_string_value("target_backend", "python", empty) == "rust"
            ),
            rewrite=lambda: _set_default_variant(path, "target_backend", "rust"),
        ), "target_backend did not resolve to the written rust"

        for variant, expected in [
            ("none", 0),
            ("one", 1),
            ("three", 3),
            ("six", 6),
            ("all", 99),
        ]:
            _set_default_variant(path, "current_controller_count", variant)
            assert fh.poll_until(
                lambda e=expected: (
                    client.get_integer_value("current_controller_count", -1, empty) == e
                ),
                rewrite=lambda v=variant: _set_default_variant(
                    path, "current_controller_count", v
                ),
            ), (
                f"current_controller_count={variant!r} resolved to "
                f"{client.get_integer_value('current_controller_count', -1, empty)}, want {expected}; "
                f"file defaultVariant={fh.load(path)['flags']['current_controller_count']['defaultVariant']!r}"
            )

        # Rollback shape: the loop resets to "none" (0) on an allowed rollback.
        _set_default_variant(path, "current_controller_count", "none")
        assert fh.poll_until(
            lambda: (
                client.get_integer_value("current_controller_count", -1, empty) == 0
            ),
            rewrite=lambda: _set_default_variant(
                path, "current_controller_count", "none"
            ),
        ), "rollback-to-none did not resolve to 0"


# --------------------------------------------------------------------------- #
# 5. Shadow-only experiments (from test_experiment_shadow_isolation.py)
# --------------------------------------------------------------------------- #
# The game_kind contract the #954 wiring sets on the eval context
# (lib/feature_flags GAME_KIND_*) and the experiment writer scopes targeting on
# (services/agent/experiment/targeting.go). Defined here because the contract
# venv does not install lib.feature_flags' deps; lib/tests/
# test_shipped_safety_defaults.py pins these against both sources.
GAME_KIND_VAR = "game_kind"
GAME_KIND_REAL = "real"
GAME_KIND_SHADOW = "shadow"
# Reserved variant the experiment Writer adds (targeting.go ExperimentVariant).
EXPERIMENT_VARIANT = "agent_experiment"


def _build_shadow_targeting(existing_targeting):
    """Reproduce targeting.go buildShadowTargeting: ``if(game_kind != real,
    agent_experiment, <pre-existing targeting or null>)``. A real context makes
    the condition false, so flagd evaluates the pre-write targeting."""
    return {
        "if": [
            {"!=": [{"var": GAME_KIND_VAR}, GAME_KIND_REAL]},
            EXPERIMENT_VARIANT,
            existing_targeting,
        ]
    }


def write_experiment(path: Path, flag_key: str, experimental_value) -> None:
    """Apply a shadow-scoped experiment as Writer.Apply does: ADD the reserved
    variant (never mutating others or defaultVariant) and wrap any pre-existing
    targeting. Idempotent, so it doubles as the poll's rewrite."""
    doc = fh.load(path)
    flag = doc["flags"][flag_key]
    flag.setdefault("variants", {})[EXPERIMENT_VARIANT] = experimental_value
    existing = flag.get("targeting")
    if (
        isinstance(existing, dict)
        and existing.get("if", [None, None])[1] == EXPERIMENT_VARIANT
    ):
        existing = existing["if"][2]  # already wrapped by an earlier (re)write
    flag["targeting"] = _build_shadow_targeting(existing)
    fh.dump_in_place(path, doc)


@pytest.mark.uc("UC-043", flow="main", br=["BR-001"], steps=[1])
def test_experiment_reaches_shadow_never_real(flagd, flag_files):
    """A written shadow-scoped experiment resolves the EXPERIMENTAL value for a
    shadow context and leaves every REAL context at its pre-experiment baseline,
    on the real flagd (RPC: server-side evaluation of the ``!= real`` rule).

    Two flags cover both branches of buildShadowTargeting:
    - ``invincibility_seconds``: no pre-existing targeting -> else is null;
    - ``sensitivity``: pre-existing targeting (game_mode==Werewolf) -> wrapped
      as the else-branch, so a real Werewolf game keeps its prior value.
    """
    path = flagd.file("game")
    shadow_ctx = EvaluationContext(attributes={GAME_KIND_VAR: GAME_KIND_SHADOW})
    real_ctx = EvaluationContext(attributes={GAME_KIND_VAR: GAME_KIND_REAL})
    real_werewolf_ctx = EvaluationContext(
        attributes={GAME_KIND_VAR: GAME_KIND_REAL, "game_mode": "Werewolf"}
    )
    # No game_kind: raw flagd resolves EXPERIMENTAL (missing var is not "real").
    # The real-by-default fail-safe lives in the APPLICATION context
    # (lib.feature_flags defaults game_kind="real"), not in flagd.
    empty_ctx = EvaluationContext()

    with _ready_client(flagd, "game") as client:

        def _inv(ctx) -> float:
            return client.get_float_value("invincibility_seconds", float("nan"), ctx)

        def _sens(ctx) -> int:
            return int(client.get_integer_value("sensitivity", -1, ctx))

        assert fh.poll_until(
            lambda: not math.isnan(_inv(real_ctx)) and _sens(real_ctx) != -1
        ), "flagd RPC provider did not serve a baseline in time"

        # Baselines from the live flagd BEFORE mutating; they must not move.
        inv_real_baseline = _inv(real_ctx)
        sens_real_baseline = _sens(real_ctx)
        sens_real_werewolf_baseline = _sens(real_werewolf_ctx)

        inv_experiment = 8.0 if inv_real_baseline < 8.0 else 2.0
        sens_experiment = 0 if sens_real_baseline != 0 else 4
        assert abs(inv_experiment - inv_real_baseline) > 0.01, (
            f"invincibility experiment value must differ from the real baseline ({inv_real_baseline})"
        )
        assert sens_experiment != sens_real_baseline, (
            f"sensitivity experiment value must differ from the real baseline ({sens_real_baseline})"
        )

        def _write():
            write_experiment(path, "invincibility_seconds", inv_experiment)
            write_experiment(path, "sensitivity", sens_experiment)

        _write()

        def _served() -> bool:
            return (
                abs(_inv(shadow_ctx) - inv_experiment) <= 0.01
                and abs(_inv(real_ctx) - inv_real_baseline) <= 0.01
                and _sens(shadow_ctx) == sens_experiment
            )

        assert fh.poll_until(_served, rewrite=_write), (
            "experiment did not resolve as expected on live flagd: "
            f"shadow invincibility={_inv(shadow_ctx)} (want {inv_experiment}), "
            f"real invincibility={_inv(real_ctx)} (want baseline {inv_real_baseline})"
        )

        # invincibility_seconds (no pre-existing targeting, else=null)
        assert abs(_inv(shadow_ctx) - inv_experiment) <= 0.01, (
            f"shadow did NOT get the experiment: {_inv(shadow_ctx)}"
        )
        assert abs(_inv(real_ctx) - inv_real_baseline) <= 0.01, (
            f"REAL leaked the experiment: invincibility={_inv(real_ctx)} "
            f"(MUST stay pre-experiment baseline {inv_real_baseline})"
        )

        # sensitivity (pre-existing Werewolf targeting wrapped as else)
        assert _sens(shadow_ctx) == sens_experiment, (
            f"shadow sensitivity did NOT get the experiment: {_sens(shadow_ctx)}"
        )
        assert _sens(real_ctx) == sens_real_baseline, (
            f"REAL sensitivity leaked: {_sens(real_ctx)} (MUST stay pre-experiment baseline {sens_real_baseline})"
        )
        assert _sens(real_werewolf_ctx) == sens_real_werewolf_baseline, (
            f"REAL Werewolf targeting was clobbered: {_sens(real_werewolf_ctx)} "
            f"(MUST stay pre-experiment baseline {sens_real_werewolf_baseline})"
        )

        # Where the fail-safe lives: raw flagd vs application context.
        assert abs(_inv(empty_ctx) - inv_experiment) <= 0.01, (
            "raw flagd should resolve EXPERIMENTAL for a context with no "
            f"game_kind (rule is != real): got {_inv(empty_ctx)}. The real-by-"
            "default fail-safe is enforced by the application context, not flagd."
        )


# --------------------------------------------------------------------------- #
# 6. NFR-014 flag propagation (from test_flag_propagation.py)
# --------------------------------------------------------------------------- #
# How long an in-place edit of a flag file takes to reach a reader. Two readers
# are polled side by side: the in-process resolver (sync stream, port 8015, the
# path every Python service reads flags through) and the RPC evaluation API
# (port 8013, cache off). A probe flag no service reads is written into
# observability.json; each sample writes a new nonce IN PLACE and polls both
# readers every 5 ms. flagd v0.16 in docker stat-polls each file about once a
# second, so latency is roughly uniform over [0, 1 s] plus reload and stream
# time. Writes are spaced by a random 1.1-2.1 s so they land at different
# phases of that poll; equal spacing would phase-lock.
#
# The bounds are deliberately loose (shared CI runners); the printed
# distribution, every raw sample included, is the measurement.
PROBE_FLAG = "nfr014_propagation_probe"
PROPAGATION_FLAG_SET = "observability"
SAMPLES = 12
POLL_INTERVAL_SECONDS = 0.005
SAMPLE_TIMEOUT_SECONDS = 10.0
MEDIAN_BOUND_SECONDS = 1.5
MAX_BOUND_SECONDS = 3.0


def _with_probe(base_doc: dict, nonce: str) -> str:
    doc = json.loads(json.dumps(base_doc))
    doc["flags"][PROBE_FLAG] = {
        "state": "ENABLED",
        "variants": {"current": nonce},
        "defaultVariant": "current",
    }
    return json.dumps(doc, indent=2) + "\n"


def _percentile(ordered: list[float], pct: float) -> float:
    return ordered[min(len(ordered) - 1, max(0, round(pct * len(ordered)) - 1))]


def measure_flag_propagation(
    flagd, samples: int = SAMPLES
) -> dict[str, list[float | None]]:
    """Per-reader latencies (seconds) for ``samples`` in-place flag edits.

    ``None`` = the reader did not serve the edit within SAMPLE_TIMEOUT_SECONDS
    (flagd's file watcher missed that write, not a slow reload).
    """
    path = flagd.file(PROPAGATION_FLAG_SET)
    original_text = path.read_text()
    base_doc = json.loads(original_text)
    latencies: dict[str, list[float | None]] = {"in_process": [], "rpc": []}
    try:
        with (
            _ready_client(flagd, PROPAGATION_FLAG_SET, "in_process") as in_process,
            _ready_client(flagd, PROPAGATION_FLAG_SET, "rpc") as rpc,
        ):
            readers = {"in_process": in_process, "rpc": rpc}
            # Prime: the probe must be served before timing starts, so the first
            # sample measures an edit, not a new-flag cold path.
            prime = uuid.uuid4().hex
            fh.write_in_place(path, _with_probe(base_doc, prime))
            deadline = time.monotonic() + 30.0
            while any(
                r.get_string_value(PROBE_FLAG, "", None) != prime
                for r in readers.values()
            ):
                assert time.monotonic() < deadline, (
                    "flagd never served the primed probe flag"
                )
                time.sleep(0.1)

            for _ in range(samples):
                time.sleep(random.uniform(1.1, 2.1))
                nonce = uuid.uuid4().hex
                written_at = fh.write_in_place(path, _with_probe(base_doc, nonce))
                pending = dict(readers)
                deadline = written_at + SAMPLE_TIMEOUT_SECONDS
                while pending and time.monotonic() < deadline:
                    for name, reader in list(pending.items()):
                        if reader.get_string_value(PROBE_FLAG, "", None) == nonce:
                            latencies[name].append(time.monotonic() - written_at)
                            del pending[name]
                    time.sleep(POLL_INTERVAL_SECONDS)
                for name in pending:
                    latencies[name].append(None)
    finally:
        fh.write_in_place(path, original_text)
    return latencies


def format_distribution(latencies: dict[str, list[float | None]]) -> str:
    """Summary line plus every raw sample per reader, for the CI log."""
    samples = max((len(v) for v in latencies.values()), default=0)
    lines = [
        f"--- NFR-014 flag propagation, {samples} in-place edits of {PROPAGATION_FLAG_SET}.json ---"
    ]
    for name, values in latencies.items():
        seen = sorted(v for v in values if v is not None)
        raw = " ".join("MISS" if v is None else f"{v * 1000:.0f}" for v in values)
        if seen:
            lines.append(
                f"  {name:<10} n={len(seen)} misses={len(values) - len(seen)}  min {seen[0] * 1000:.0f} ms"
                f"  median {statistics.median(seen) * 1000:.0f} ms  p90 {_percentile(seen, 0.9) * 1000:.0f} ms"
                f"  max {seen[-1] * 1000:.0f} ms"
            )
        else:
            lines.append(f"  {name:<10} n=0 misses={len(values)}")
        lines.append(f"  {name:<10} raw ms: {raw}")
    return "\n".join(lines)


@pytest.mark.nfr("NFR-014")
def test_flag_file_edit_reaches_readers_within_poll_bound(flagd, flag_files, capsys):
    latencies = measure_flag_propagation(flagd)
    report = format_distribution(latencies)
    # Past pytest's capture so a passing CI run's log carries the measurement too.
    with capsys.disabled():
        print("\n" + report)

    for name, values in latencies.items():
        missed = [i for i, v in enumerate(values) if v is None]
        assert not missed, (
            f"{name}: no reload observed for sample(s) {missed} within {SAMPLE_TIMEOUT_SECONDS}s "
            f"(missed reload, not a slow one)\n{report}"
        )
        seen = [v for v in values if v is not None]
        assert statistics.median(seen) < MEDIAN_BOUND_SECONDS, (
            f"{name}: slow propagation, median {statistics.median(seen):.3f}s >= {MEDIAN_BOUND_SECONDS}s\n{report}"
        )
        assert max(seen) < MAX_BOUND_SECONDS, (
            f"{name}: slow propagation, max {max(seen):.3f}s >= {MAX_BOUND_SECONDS}s\n{report}"
        )
