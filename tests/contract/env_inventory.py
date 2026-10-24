"""Static inventory of the env vars the services read, and of what compose sets (#1526).

Python reads are found with the AST: ``os.getenv``, ``os.environ.get /
setdefault / pop``, ``os.environ[...]`` and ``"X" in os.environ``, including
the ``from os import environ, getenv`` and ``import os as _o`` spellings. A
non-literal key (``os.getenv(name)``) cannot be attributed to a var and is
skipped; its literal call sites through a wrapper are not tracked, so read
env vars directly with a literal key. Go reads with regexes over ``os.Getenv`` / ``os.LookupEnv``
and the per-package env helpers (``getEnv(key, def)``, ``secondsEnv``, ...), which
are discovered from their bodies so a new helper is picked up without a list.
Go comments are stripped before matching.

A read "has a code default" when the code supplies a non-empty fallback: a
second argument, ``getenv(...) or X``, or a Go helper's fallback parameter. An
empty fallback (``""``) counts as no default: it usually means "feature off",
which is exactly the silent gap this check exists to catch.

Compose is merged the way ``docker compose -f a -f b`` merges ``environment``:
key by key, except ``!override`` (replace) and ``!reset`` (clear). The final
stage of a service's Dockerfile also counts, since compose runs that image.

Compose loader semantics (what "set" means): only the ``environment:`` key is
read. ``env_file:`` and ``extends:`` are NOT followed (no compose file uses
them; add support before introducing one). A bare ``KEY`` (host passthrough)
and ``KEY: ${VAR}`` / ``${VAR}`` interpolation without a default count as set
even when the host provides nothing, so a passthrough can still leave the
container without a value (the AGENT_EXPERIMENT_PROPOSER_ENABLED class of
gap is therefore only caught when the passthrough is missing entirely).
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES = REPO_ROOT / "services"
LIB = REPO_ROOT / "lib"

COMPOSE_BASE = REPO_ROOT / "docker-compose.yml"
COMPOSE_CI = REPO_ROOT / "docker-compose.ci.yml"
COMPOSE_LITE = (
    REPO_ROOT / "docker-compose.lite.yml"
)  # standalone Pi stack, not an overlay
COMPOSE_DRY_RUN = REPO_ROOT / "docker-compose.dry-run.yml"

# compose service -> (language, code roots). Python services also load lib/.
CODE_ROOTS: dict[str, tuple[str, tuple[Path, ...]]] = {
    "controller-manager": ("python", (SERVICES / "controller_manager", LIB)),
    "pairing-daemon": ("python", (SERVICES / "pairing_daemon", LIB)),
    "python-hid": ("python", (SERVICES / "python_hid", LIB)),
    "game-coordinator": ("python", (SERVICES / "game_coordinator", LIB)),
    "menu": ("python", (SERVICES / "menu", LIB)),
    "audio": ("python", (SERVICES / "audio", LIB)),
    # #2050: profile-gated (operator-pad); INPUT_GID is a compose-only interpolation
    # (group_add), never read by the code, so it is not an env read here (flags.md §15.2)
    "operator-pad": ("python", (SERVICES / "operator_pad", LIB)),
    "agent": ("go", (SERVICES / "agent",)),
    "flag-service": ("go", (SERVICES / "flag-service",)),
    "connect-proxy": ("go", (SERVICES / "connect-proxy",)),
}

_TEST_DIRS = {"tests", "test", "testdata", "__pycache__", "gen", "generated"}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class EnvRead:
    var: str
    file: Path
    line: int
    has_default: bool

    def where(self) -> str:
        return f"{self.file.relative_to(REPO_ROOT)}:{self.line}"


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #
def _python_files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).parts
        if any(p in _TEST_DIRS for p in rel[:-1]):
            continue
        name = path.name
        if (
            name.startswith("test_")
            or name == "conftest.py"
            or name.endswith("_pb2.py")
            or name.endswith("_pb2_grpc.py")
        ):
            continue
        out.append(path)
    return out


@dataclass(frozen=True)
class _OsNames:
    """Names a module binds to ``os``, ``os.environ`` and ``os.getenv``."""

    os_modules: frozenset[str]
    environ: frozenset[str]
    getenv: frozenset[str]


def _os_names(tree: ast.AST) -> _OsNames:
    os_modules, environ, getenv = {"os"}, set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_modules.update(a.asname or a.name for a in node.names if a.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for a in node.names:
                if a.name == "environ":
                    environ.add(a.asname or a.name)
                elif a.name == "getenv":
                    getenv.add(a.asname or a.name)
    return _OsNames(frozenset(os_modules), frozenset(environ), frozenset(getenv))


def _is_environ(node: ast.AST, names: _OsNames) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names.environ
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id in names.os_modules
    )


def _literal_key(node: ast.expr) -> str | None:
    """The string literal, or None for a non-literal key (skipped, see module doc)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _env_call_key(
    node: ast.AST, names: _OsNames
) -> tuple[str | None, list[ast.expr]] | None:
    """``getenv(K, ...)`` / ``environ.get|setdefault|pop(K, ...)`` -> (K or None, rest)."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    is_getenv = (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id in names.os_modules
    ) or (isinstance(func, ast.Name) and func.id in names.getenv)
    is_environ_method = (
        isinstance(func, ast.Attribute)
        and func.attr in ("get", "setdefault", "pop")
        and _is_environ(func.value, names)
    )
    if not (is_getenv or is_environ_method):
        return None
    return _literal_key(node.args[0]), node.args[1:]


def _non_empty_default(args: list[ast.expr]) -> bool:
    if not args:
        return False
    default = args[0]
    if isinstance(default, ast.Constant):
        return default.value not in ("", None)
    return True  # a name or expression: a real fallback value


def python_reads(path: Path) -> list[EnvRead]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = _os_names(tree)
    reads: list[EnvRead] = []
    or_defaulted: set[int] = (
        set()
    )  # id() of env calls followed by a fallback: `getenv(K) or default`
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            or_defaulted.update(id(value) for value in node.values[:-1])
    for node in ast.walk(tree):
        found = _env_call_key(node, names)
        if found:
            var, rest = found
            if var is not None:
                reads.append(
                    EnvRead(
                        var,
                        path,
                        node.lineno,
                        _non_empty_default(rest) or id(node) in or_defaulted,
                    )
                )
        elif (
            isinstance(node, ast.Subscript)
            and _is_environ(node.value, names)
            and isinstance(node.ctx, ast.Load)
        ):
            var = _literal_key(node.slice)
            if var is not None:
                reads.append(EnvRead(var, path, node.lineno, False))
        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and _is_environ(node.comparators[0], names)
        ):
            var = _literal_key(node.left)
            if var is not None:
                reads.append(EnvRead(var, path, node.lineno, False))
    return reads


# --------------------------------------------------------------------------- #
# Go
# --------------------------------------------------------------------------- #
_GO_FUNC = re.compile(r"^func (\w+)\(([^)]*)\)", re.M)
_GO_CONST = re.compile(r"\b(\w+)\s*(?:string\s*)?=\s*\"([A-Z][A-Z0-9_]*)\"")


_GO_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _go_source(path: Path) -> str:
    """File text with comments blanked (newlines kept so line numbers hold).

    Naive about ``//`` inside string literals (e.g. URLs); that only blanks the
    rest of such a line, and no env read shares a line with one.
    """
    return _GO_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), path.read_text())


def _go_files(root: Path) -> list[Path]:
    return [
        p
        for p in sorted(root.rglob("*.go"))
        if not p.name.endswith("_test.go")
        and not (set(p.relative_to(root).parts[:-1]) & _TEST_DIRS)
    ]


def _go_param_names(params: str) -> list[str]:
    """``key, def string`` / ``key string, fallback int`` -> [key, def]."""
    return [piece.split()[0] for piece in params.split(",") if piece.strip()]


def _go_helpers(files: list[Path]) -> dict[str, tuple[int, bool]]:
    """Env helper name -> (index of its key parameter, takes another argument).

    A helper is a func with a parameter passed straight to os.Getenv /
    os.LookupEnv; that parameter may sit at any position (``ceilingEnv(logger,
    key, warnZero)``). Any other parameter counts as a fallback: the helper
    decides what an unset var means, so a call site has a code default."""
    helpers: dict[str, tuple[int, bool]] = {}
    for path in files:
        text = _go_source(path)
        matches = list(_GO_FUNC.finditer(text))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end() : end]
            params = _go_param_names(m.group(2))
            for index, param in enumerate(params):
                if re.search(rf"os\.(?:Getenv|LookupEnv)\({param}\)", body):
                    name, shape = m.group(1), (index, len(params) > 1)
                    if helpers.get(name, shape) != shape:
                        raise ValueError(
                            f"env helper {name} has inconsistent arity across packages"
                        )
                    helpers[name] = shape
                    break
    return helpers


def go_reads(root: Path) -> list[EnvRead]:
    files = _go_files(root)
    helpers = _go_helpers(files)
    # Key constants per package directory; a qualified name (pkg.Const) or a
    # name from another package resolves only when its value is unambiguous.
    local: dict[Path, dict[str, str]] = {}
    for path in files:
        local.setdefault(path.parent, {}).update(
            dict(_GO_CONST.findall(_go_source(path)))
        )
    shared: dict[str, set[str]] = {}
    for consts in local.values():
        for name, value in consts.items():
            shared.setdefault(name, set()).add(value)

    def resolve(key: str, path: Path) -> str | None:
        if key.startswith('"'):
            return key.strip('"')
        name = key.rsplit(".", 1)[-1]
        if "." not in key and name in local.get(path.parent, {}):
            return local[path.parent][name]
        values = shared.get(name, set())
        return next(iter(values)) if len(values) == 1 else None

    # Only a helper's own key parameter may stay unresolved (its inner
    # os.Getenv(key)); any other unresolvable key is a loud error. A call whose
    # earlier argument contains parentheses or commas is not matched by the
    # per-index pattern below; that fails safe (test_agent_compose_env_is_read).
    helper_params = {
        _go_param_names(m.group(2))[helpers[m.group(1)][0]]
        for p in files
        for m in _GO_FUNC.finditer(_go_source(p))
        if m.group(1) in helpers
    }
    # One call pattern per key position: the key is the (index+1)-th argument.
    by_index: dict[int, list[str]] = {0: ["os\\.Getenv", "os\\.LookupEnv"]}
    for h, (index, _) in helpers.items():
        by_index.setdefault(index, []).append(rf"\b{re.escape(h)}")
    calls = [
        re.compile(
            r"("
            + "|".join(names)
            + r")\(\s*"
            + r"[^,()]+,\s*" * index
            + r"(\"[A-Z0-9_]+\"|[A-Za-z_][\w.]*)\s*(\)|,\s*(\"\"\s*\))?)"
        )
        for index, names in by_index.items()
    ]
    reads: list[EnvRead] = []
    for path in files:
        text = _go_source(path)
        for call in calls:
            for m in call.finditer(text):
                fn, key, tail, empty_fallback = m.groups()
                line = text.count("\n", 0, m.start()) + 1
                var = resolve(key, path)
                if var is None:
                    if key in helper_params:
                        continue  # the helper's own os.Getenv(key)
                    raise ValueError(
                        f"{path if not path.is_relative_to(REPO_ROOT) else path.relative_to(REPO_ROOT)}"
                        f":{line}: cannot resolve env key {key!r}"
                    )
                has_default = (
                    fn in helpers
                    and helpers[fn][1]
                    and tail.startswith(",")
                    and not empty_fallback
                )
                reads.append(EnvRead(var, path, line, has_default))
    return reads


# --------------------------------------------------------------------------- #
# Inventory per compose service
# --------------------------------------------------------------------------- #
@functools.cache
def service_reads(service: str) -> tuple[EnvRead, ...]:
    lang, roots = CODE_ROOTS[service]
    reads: list[EnvRead] = []
    for root in roots:
        if lang == "python":
            for path in _python_files(root):
                reads.extend(python_reads(path))
        else:
            reads.extend(go_reads(root))
    return tuple(reads)


def required_vars(service: str) -> dict[str, list[EnvRead]]:
    """Vars the service reads with no code default at ANY read site.

    A var read once with a default and once without still needs a value: the
    no-default site gets nothing.
    """
    out: dict[str, list[EnvRead]] = {}
    for read in service_reads(service):
        if not read.has_default:
            out.setdefault(read.var, []).append(read)
    return out


# --------------------------------------------------------------------------- #
# Compose
# --------------------------------------------------------------------------- #
class _Override(list):
    pass


class _OverrideMap(dict):
    pass


class _Reset:
    pass


class _ComposeLoader(yaml.SafeLoader):
    pass


def _construct_override(loader: yaml.SafeLoader, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return _Override(loader.construct_sequence(node, deep=True))
    if isinstance(node, yaml.MappingNode):
        return _OverrideMap(loader.construct_mapping(node, deep=True))
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_override)
_ComposeLoader.add_constructor("!reset", lambda loader, node: _Reset())


@functools.cache
def _load_compose(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_ComposeLoader)["services"]  # noqa: S506 (SafeLoader subclass)


def _env_dict(env) -> dict[str, str | None]:
    if env is None or isinstance(env, _Reset):
        return {}
    if isinstance(env, dict):
        return {k: (None if v is None else str(v)) for k, v in env.items()}
    out: dict[str, str | None] = {}
    for item in env:
        key, sep, value = str(item).partition("=")
        out[key] = value if sep else None  # bare "KEY" = passthrough from the host
    return out


def compose_environment(
    service: str, overlays: tuple[Path, ...] = (), base_file: Path = COMPOSE_BASE
) -> dict[str, str | None] | None:
    """The merged ``environment`` of ``service`` in ``base_file`` plus ``overlays``;
    None when the base file has no such service."""
    base = _load_compose(base_file)
    if service not in base:
        return None
    env = _env_dict(base[service].get("environment"))
    for overlay in overlays:
        svc = (_load_compose(overlay) or {}).get(service) or {}
        if "environment" not in svc:
            continue
        layer = svc["environment"]
        if isinstance(layer, (_Override, _OverrideMap, _Reset)):
            env = _env_dict(layer)
        else:
            env.update(_env_dict(layer))
    return env


@functools.cache
def dockerfile_env(service: str) -> frozenset[str]:
    """ENV keys of the final stage of the service's Dockerfile."""
    build = _load_compose(COMPOSE_BASE)[service].get("build") or {}
    dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
    if not dockerfile:
        return frozenset()
    lines = (REPO_ROOT / dockerfile).read_text().splitlines()
    last_from = max(
        (i for i, ln in enumerate(lines) if ln.strip().upper().startswith("FROM ")),
        default=0,
    )
    keys: set[str] = set()
    for ln in lines[last_from:]:
        stripped = ln.strip()
        if stripped.upper().startswith("ENV "):
            rest = stripped[4:].strip()
            if "=" in rest.split()[0]:
                keys.update(tok.split("=", 1)[0] for tok in rest.split() if "=" in tok)
            else:
                keys.add(rest.split()[0])
    return frozenset(keys)


def is_env_name(name: str) -> bool:
    return bool(_ENV_NAME.match(name))
