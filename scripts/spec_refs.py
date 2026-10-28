"""Lint the code references and telemetry names of docs/specs (#1280).

    make spec-refs-check             # CI: fail on a broken reference
    make spec-refs                   # rewrite the ATTR baseline (needs a label to grow)
    scripts/spec_refs.py --suggest   # print path:Symbol for every path:line ref
    scripts/spec_refs.py --report    # markdown drift report (weekly workflow, #1918); exit 0

Checks (docs/specs/*.md; generated/ and notes/ are skipped):
  REF_NOT_FULL_PATH  a `path:ref` whose path is not a repo-relative file path
  REF_NO_FILE        the file does not exist
  REF_LINE_OOB       a line number beyond the end of the file
  REF_NO_SYMBOL      a `path:Symbol` whose symbol (each dotted part) is not a word in the file
                     (a word search, not a declaration check)
  ATTR_UNDOCUMENTED  a Go Span*/Attr* constant value that no spec mentions
                     (exact string; `ns.*` never documents a key)
REF_* findings are never baselined. ATTR_UNDOCUMENTED findings may sit in
docs/specs/notes/spec-refs-baseline.json; a new baseline fingerprint needs the
`spec-baseline-growth` label (PR context only), and stale entries fail ("prune me").
Line refs may not grow versus --base (warning, with suggestions).

Ignore an example with `<!-- spec-refs: ignore -->` on its line; fenced code
blocks are skipped. A ref fix is not a spec change (docs/CONTRIBUTING.md).

Runs with the standard library only:
    python3 scripts/spec_refs.py [--check|--update-baseline|--suggest|--report] [--base REV]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPECS = REPO_ROOT / "docs" / "specs"
BASELINE = SPECS / "notes" / "spec-refs-baseline.json"
GROWTH_LABEL = "spec-baseline-growth"

sys.path.insert(0, str(REPO_ROOT / "tests" / "contract"))
import telemetry_inventory as ti

IGNORE_MARK = "spec-refs: ignore"
EXTS = "go|py|rs|ts|tsx|sh|yaml|yml|json|proto|toml|md"
REF_RE = re.compile(
    rf"(?<![\w/.-])(?P<path>[\w./-]+\.(?:{EXTS})):"
    r"(?:(?P<lines>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)|(?P<sym>[A-Za-z_][\w.]*))"
)
ATTR_RE = re.compile(r'^\s*((?:Span|Attr)\w*)\s*(?:\w+\s*)?=\s*"([^"]+)"', re.MULTILINE)
_SKIP_WALK = {".git", ".worktrees", "node_modules", "target", ".venv", "__pycache__"}


class Finding:
    def __init__(self, code, file, value, line=0, message=""):
        self.code, self.file, self.value = code, file, value
        self.line, self.message = line, message

    @property
    def fingerprint(self) -> str:
        key = f"{self.code}|{self.file}|{self.value}"
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def render(self) -> str:
        return f"{self.file}:{self.line}: {self.code} {self.message}"


def spec_files(root: Path | None = None) -> list[Path]:
    """Hand-written specs: docs/specs/*.md (no generated/, no notes/)."""
    return sorted((SPECS if root is None else root).glob("*.md"))


# --------------------------------------------------------------------------- #
# Reference scanning
# --------------------------------------------------------------------------- #
class Ref:
    def __init__(self, spec, lineno, path, lines, sym):
        self.spec, self.lineno, self.path = spec, lineno, path
        self.lines, self.sym = lines, sym

    @property
    def text(self) -> str:
        return f"{self.path}:{self.lines or self.sym}"

    @property
    def first_line(self) -> int:
        return int(re.split(r"[-,]", self.lines)[0])

    @property
    def max_line(self) -> int:
        return max(int(n) for n in re.split(r"[-,]", self.lines))


def scan_text(spec: str, text: str) -> list[Ref]:
    refs, in_fence = [], False
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or IGNORE_MARK in line:
            continue
        for m in REF_RE.finditer(line):
            start = m.start()
            if "://" in line[max(0, start - 12) : start + len(m.group(0))]:
                continue
            refs.append(Ref(spec, lineno, m["path"], m["lines"], m["sym"]))
    return refs


def scan_specs() -> list[Ref]:
    out = []
    for path in spec_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        out += scan_text(rel, path.read_text())
    return out


def _basename_exists(name: str) -> bool:
    for _dir, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_WALK]
        if name in files:
            return True
    return False


def _read(path: Path) -> str:
    return path.read_text(errors="replace")


def _symbol_found(text: str, sym: str) -> bool:
    return all(
        re.search(rf"\b{re.escape(part)}\b", text) for part in sym.split(".") if part
    )


def check_refs(refs: list[Ref]) -> list[Finding]:
    findings = []
    cache: dict[str, str] = {}
    for r in refs:
        target = REPO_ROOT / r.path
        if not target.is_file():
            # A file with that basename elsewhere means the author wrote a suffix.
            suffix_hit = _basename_exists(r.path.rsplit("/", 1)[-1])
            code = "REF_NOT_FULL_PATH" if suffix_hit else "REF_NO_FILE"
            msg = (
                f"{r.text}: not a full repo path; write the path from the repo root"
                if suffix_hit
                else f"{r.text}: file does not exist"
            )
            findings.append(Finding(code, r.spec, r.text, r.lineno, msg))
            continue
        text = cache.setdefault(r.path, _read(target))
        if r.lines:
            n = len(text.splitlines())
            if r.max_line > n:
                hint = suggest_for(r)
                findings.append(
                    Finding(
                        "REF_LINE_OOB",
                        r.spec,
                        r.text,
                        r.lineno,
                        f"{r.text} (file has {n} lines)"
                        + (f"; {hint}" if hint else ""),
                    )
                )
        elif not _symbol_found(text, r.sym):
            findings.append(
                Finding(
                    "REF_NO_SYMBOL",
                    r.spec,
                    r.text,
                    r.lineno,
                    f"{r.text}: symbol not found in the file",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# --suggest: enclosing symbol of a line ref
# --------------------------------------------------------------------------- #
_GO_FUNC = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?(\w+)(?:\[[^\]]*\])?\)\s*)?(\w+)")
_GO_DECL = re.compile(r"^(?:type|var|const)\s+(\w+)")
_PY_DEF = re.compile(r"^(\s*)(?:async\s+def|def|class)\s+(\w+)")
_PY_CLASS = re.compile(r"^(\s*)class\s+(\w+)")
_RS_FN = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)")
_SH_FN = re.compile(r"^(?:function\s+)?(\w+)\s*\(\)\s*\{")
_YAML_KEY = re.compile(r"^([\w.-]+):")


def enclosing_symbol(path: Path, line: int) -> str | None:
    lines = _read(path).splitlines()[:line]
    ext = path.suffix
    for idx in range(len(lines) - 1, -1, -1):
        text = lines[idx]
        if ext == ".go":
            if m := _GO_FUNC.match(text):
                return f"{m[1]}.{m[2]}" if m[1] else m[2]
            if m := _GO_DECL.match(text):
                return m[1]
        elif ext == ".py":
            if m := _PY_DEF.match(text):
                if m[1]:
                    for back in range(idx - 1, -1, -1):
                        c = _PY_CLASS.match(lines[back])
                        if c and len(c[1]) < len(m[1]):
                            return f"{c[2]}.{m[2]}"
                return m[2]
        elif ext == ".rs":
            if m := _RS_FN.match(text):
                return m[1]
        elif ext == ".sh":
            if m := _SH_FN.match(text):
                return m[1]
        elif ext in (".yaml", ".yml"):
            if m := _YAML_KEY.match(text):
                return m[1]
    return None


def suggest_for(ref: Ref) -> str | None:
    target = REPO_ROOT / ref.path
    if not ref.lines or not target.is_file():
        return None
    n = len(_read(target).splitlines())
    sym = enclosing_symbol(target, min(ref.first_line, n))
    return f"try {ref.path}:{sym}" if sym else None


# --------------------------------------------------------------------------- #
# Span/Attr constants
# --------------------------------------------------------------------------- #
def go_telemetry_consts() -> list[tuple[str, str]]:
    out = []
    for path in ti._source_files(REPO_ROOT / "services", ".go", REPO_ROOT):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for m in ATTR_RE.finditer(_read(path)):
            out.append((rel, m.group(2)))
    return sorted(set(out))


def spec_corpus() -> str:
    parts = []
    for name in ("telemetry.md", "decide-contract.md"):
        p = SPECS / name
        if p.exists():
            parts.append(_read(p))
    gen = SPECS / "generated"
    if gen.is_dir():
        parts += [_read(p) for p in sorted(gen.glob("*.md"))]
    return "\n".join(parts)


def check_attrs() -> list[Finding]:
    corpus = spec_corpus()
    findings = []
    for rel, value in go_telemetry_consts():
        if not re.search(rf"(?<![\w.-]){re.escape(value)}(?![\w-]|\.\w)", corpus):
            findings.append(
                Finding(
                    "ATTR_UNDOCUMENTED",
                    rel,
                    value,
                    0,
                    f"{value!r} is not in docs/specs/telemetry.md (exact key)",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #
def load_baseline_text(text: str | None) -> dict[str, dict]:
    if not text:
        return {}
    return {f["fingerprint"]: f for f in json.loads(text).get("findings", [])}


def render_baseline(findings: list[Finding]) -> str:
    rows = sorted(
        (
            {
                "code": f.code,
                "file": f.file,
                "value": f.value,
                "fingerprint": f.fingerprint,
            }
            for f in findings
            if f.code == "ATTR_UNDOCUMENTED"
        ),
        key=lambda r: (r["file"], r["value"]),
    )
    return json.dumps({"version": 1, "findings": rows}, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# Base comparison (line-ref growth, baseline growth)
# --------------------------------------------------------------------------- #
def _git(*args: str) -> str | None:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout if res.returncode == 0 else None


def base_line_refs(base: str) -> Counter | None:
    listing = _git("ls-tree", "--name-only", base, "docs/specs/")
    if listing is None:
        return None
    counts: Counter = Counter()
    for name in listing.split():
        if name.endswith(".md") and name.count("/") == 2:
            text = _git("show", f"{base}:{name}") or ""
            for r in scan_text(name, text):
                if r.lines:
                    counts[(name, r.text)] += 1
    return counts


def labels() -> set[str]:
    env = os.environ.get("PR_LABELS")
    if env is not None:
        return {s for s in env.split(",") if s}
    number = os.environ.get("PR_NUMBER")
    if not number:
        return set()
    res = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            number,
            "--json",
            "labels",
            "--jq",
            '[.labels[].name] | join(",")',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return {s for s in res.stdout.strip().split(",") if s}


def growth_report(refs: list[Ref], base: str) -> tuple[list[str], list[str]]:
    """(warnings about new line refs, errors about baseline growth)."""
    warnings, errors = [], []
    before = base_line_refs(base)
    if before is not None:
        now: Counter = Counter((r.spec, r.text) for r in refs if r.lines)
        grown = sum(now.values()) - sum(before.values())
        if grown > 0:
            new = now - before
            lines = []
            for r in refs:
                if r.lines and new.get((r.spec, r.text)):
                    new[(r.spec, r.text)] -= 1
                    lines.append(
                        f"  {r.spec}:{r.lineno}: {r.text} -> "
                        + (suggest_for(r) or "no enclosing symbol")
                    )
            warnings.append(
                f"::warning title=Spec line refs grew::path:line refs grew by {grown} "
                f"versus {base}; prefer path:Symbol:\n" + "\n".join(lines)
            )
    # Baseline growth is a PR-only check: the merge queue has no PR number or
    # labels (the PR passed it with its label already).
    if not (os.environ.get("PR_NUMBER") or "PR_LABELS" in os.environ):
        return warnings, errors
    rel = BASELINE.relative_to(REPO_ROOT).as_posix()
    old = load_baseline_text(_git("show", f"{base}:{rel}"))
    if old or _git("cat-file", "-e", f"{base}:{rel}") is not None:
        head = load_baseline_text(_read(BASELINE) if BASELINE.exists() else None)
        added = sorted(head.keys() - old.keys())
        if added and GROWTH_LABEL not in labels():
            names = ", ".join(repr(head[fp]["value"]) for fp in added)
            errors.append(
                f"::error title=Spec refs baseline grew::{BASELINE.name} gained "
                f"{len(added)} entr{'y' if len(added) == 1 else 'ies'} ({names}). "
                f"Document the new names in docs/specs/telemetry.md, or add the "
                f"'{GROWTH_LABEL}' label."
            )
    return warnings, errors


# --------------------------------------------------------------------------- #
def run_check(base: str | None) -> int:
    refs = scan_specs()
    findings = check_refs(refs)
    attrs = check_attrs()
    baseline = load_baseline_text(_read(BASELINE) if BASELINE.exists() else None)
    status = 0
    for f in findings:
        print(f.render())
        status = 1
    current = {f.fingerprint for f in attrs}
    for f in attrs:
        if f.fingerprint not in baseline:
            print(f.render())
            status = 1
    for fp, entry in sorted(baseline.items(), key=lambda kv: kv[1]["value"]):
        if fp not in current:
            print(
                f"{BASELINE.name}: stale entry {entry['file']} {entry['value']!r} is "
                "documented or gone; prune it (make spec-refs)"
            )
            status = 1
    if base:
        warnings, errors = growth_report(refs, base)
        for w in warnings:
            print(w)
        for e in errors:
            print(e)
            status = 1
    if status:
        print(
            "\n::error title=Spec references::broken spec references or undocumented "
            "telemetry names above. Fix the spec (REF_*: path:Symbol survives edits; "
            "`scripts/spec_refs.py --suggest` prints them). See docs/CONTRIBUTING.md#spec-references",
            file=sys.stderr,
        )
        return 1
    n_line = sum(1 for r in refs if r.lines)
    print(
        f"spec refs OK: {len(refs)} refs ({n_line} line, {len(refs) - n_line} symbol), "
        f"{len(attrs)} baselined undocumented attr(s)."
    )
    return 0


def run_suggest() -> int:
    for r in scan_specs():
        if r.lines and (hint := suggest_for(r)):
            print(f"{r.spec}:{r.lineno}: {r.text} -> {hint.removeprefix('try ')}")
    return 0


# --------------------------------------------------------------------------- #
# Drift report (#1918)
# --------------------------------------------------------------------------- #
REPORT_MARK = "spec-refs-drift"


def _spec_line_commit(spec: str, lineno: int) -> str | None:
    out = _git("blame", "-L", f"{lineno},{lineno}", "--porcelain", "--", spec)
    return out.split(None, 1)[0] if out else None


def _code_changed_since(commit: str, ref: Ref) -> bool | None:
    """True when the cited line range changed after the spec line was last edited."""
    out = _git(
        "log", f"{commit}..HEAD", f"-L{ref.first_line},{ref.max_line}:{ref.path}",
        "--format=%h", "--no-patch",
    )  # fmt: skip
    return None if out is None else bool(out.strip())


def drift_findings(refs: list[Ref]) -> list[str]:
    """Line refs whose cited lines changed since the spec line was last edited."""
    out, seen = [], set()
    for r in refs:
        key = (r.spec, r.lineno, r.text)
        target = REPO_ROOT / r.path
        if not r.lines or key in seen or not target.is_file():
            continue
        seen.add(key)
        if r.max_line > len(_read(target).splitlines()):
            continue  # reported as REF_LINE_OOB
        commit = _spec_line_commit(r.spec, r.lineno)
        if commit and _code_changed_since(commit, r):
            hint = suggest_for(r)
            out.append(
                f"- `{r.spec}:{r.lineno}` cites `{r.text}`, changed since the spec line "
                f"was edited ({commit[:8]})" + (f"; {hint}" if hint else "")
            )
    return out


def render_report(broken: list[str], drift: list[str]) -> str:
    total = len(broken) + len(drift)
    lines = [f"<!-- {REPORT_MARK}: {total} -->", "# Spec reference drift", ""]
    if not total:
        return "\n".join(lines + ["No broken or drifted references."]) + "\n"
    lines.append(
        "Weekly report of `scripts/spec_refs.py --report` (#1918). "
        "It never gates a PR; fix the spec (prefer `path:Symbol`) and the next run updates this issue."
    )
    if broken:
        lines += ["", f"## Broken references ({len(broken)})", ""] + broken
    if drift:
        lines += ["", f"## Possibly drifted line references ({len(drift)})", ""] + drift
    return "\n".join(lines) + "\n"


def run_report() -> int:
    refs = scan_specs()
    broken = [f"- `{f.render()}`" for f in check_refs(refs)]
    print(render_report(broken, drift_findings(refs)), end="")
    return 0


def run_update() -> int:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(render_baseline(check_attrs()))
    print(f"wrote {BASELINE.relative_to(REPO_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update-baseline", action="store_true")
    mode.add_argument("--suggest", action="store_true")
    mode.add_argument("--report", action="store_true")
    ap.add_argument("--base", help="git rev to compare line refs and baseline with")
    args = ap.parse_args(argv)
    if args.suggest:
        return run_suggest()
    if args.report:
        return run_report()
    if args.update_baseline:
        return run_update()
    return run_check(args.base)


if __name__ == "__main__":
    sys.exit(main())
