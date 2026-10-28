"""scripts/spec_refs.py: the spec reference lint (#1280).

The real-repo smoke test at the end is the CI gate itself; the rest pin the
parser and every finding code on a tiny tmp repo.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import telemetry_inventory as ti

sys.path.insert(0, str(ti.REPO_ROOT / "scripts"))
import spec_refs as sr


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """Point the lint at an empty repo layout under tmp_path."""
    monkeypatch.setattr(sr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sr, "SPECS", tmp_path / "docs/specs")
    monkeypatch.setattr(
        sr, "BASELINE", tmp_path / "docs/specs/notes/spec-refs-baseline.json"
    )
    for d in ("docs/specs/generated", "docs/specs/notes", "svc/pkg", "scripts"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "svc/pkg/state.go").write_text(
        "package pkg\n\ntype Store struct{}\n\nfunc (s *Store) Load() {\n\tx := 1\n}\n\nfunc digest() {}\n"
    )
    (tmp_path / "svc/pkg/helper.py").write_text(
        "class Widget:\n    def spin(self):\n        return 1\n"
    )
    return tmp_path


def spec(repo, text, name="flags.md"):
    (repo / "docs/specs" / name).write_text(text)


def codes(repo):
    return [f.code for f in sr.check_refs(sr.scan_specs())]


def test_valid_line_and_symbol_refs_pass(tmp_repo):
    spec(tmp_repo, "See `svc/pkg/state.go:5` and `svc/pkg/state.go:digest`.\n")
    assert codes(tmp_repo) == []


def test_out_of_range_line_is_flagged_with_a_suggestion(tmp_repo):
    spec(tmp_repo, "x `svc/pkg/state.go:6-40`\n")
    [f] = sr.check_refs(sr.scan_specs())
    assert f.code == "REF_LINE_OOB"
    assert "file has 9 lines" in f.message


def test_oob_message_names_the_enclosing_symbol(tmp_repo):
    spec(tmp_repo, "x `svc/pkg/state.go:6`\n")
    assert sr.suggest_for(sr.scan_specs()[0]) == "try svc/pkg/state.go:Store.Load"


def test_missing_file_and_missing_symbol(tmp_repo):
    spec(tmp_repo, "a `svc/gone.go:3`\nb `svc/pkg/state.go:nothere`\n")
    assert codes(tmp_repo) == ["REF_NO_FILE", "REF_NO_SYMBOL"]


def test_type_method_and_python_class_method_resolve(tmp_repo):
    spec(tmp_repo, "`svc/pkg/state.go:Store.Load` `svc/pkg/helper.py:Widget.spin`\n")
    assert codes(tmp_repo) == []
    spec(tmp_repo, "`svc/pkg/helper.py:Widget.nope`\n")
    assert codes(tmp_repo) == ["REF_NO_SYMBOL"]


def test_basename_only_ref_is_not_a_full_path_even_when_unique(tmp_repo):
    spec(tmp_repo, "`state.go:5` and `pkg/state.go:5`\n")
    assert codes(tmp_repo) == ["REF_NOT_FULL_PATH", "REF_NOT_FULL_PATH"]


def test_fences_urls_and_ignore_marker_are_skipped(tmp_repo):
    spec(
        tmp_repo,
        "```\nsvc/gone.go:3\n```\n"
        "https://example.com/svc/gone.go:3\n"
        "e.g. `svc/gone.go:3` <!-- spec-refs: ignore -->\n",
    )
    assert codes(tmp_repo) == []


def test_generated_and_notes_are_skipped(tmp_repo):
    (tmp_repo / "docs/specs/generated/g.md").write_text("`svc/gone.go:3`\n")
    (tmp_repo / "docs/specs/notes/n.md").write_text("`svc/gone.go:3`\n")
    assert codes(tmp_repo) == []


def test_comma_list_and_pytest_ids_parse(tmp_repo):
    spec(tmp_repo, "`svc/pkg/state.go:3,5-6` and `svc/pkg/helper.py::Widget`\n")
    refs = sr.scan_specs()
    assert [r.lines for r in refs] == ["3,5-6"]
    assert codes(tmp_repo) == []


def _go_const(repo, value):
    (repo / "services/agent").mkdir(parents=True, exist_ok=True)
    (repo / "services/agent/telemetry.go").write_text(
        f'package agent\n\nconst (\n\tAttrThing = "{value}"\n)\n'
    )


def test_attr_must_appear_exactly_and_wildcard_does_not_document(tmp_repo):
    _go_const(tmp_repo, "decision.reason")
    spec(tmp_repo, "`decision.*`: `.reason`, `decision.reasons`\n", "telemetry.md")
    assert [f.code for f in sr.check_attrs()] == ["ATTR_UNDOCUMENTED"]
    spec(tmp_repo, "`decision.reason`\n", "telemetry.md")
    assert sr.check_attrs() == []


def test_prefix_of_a_documented_key_does_not_document_it(tmp_repo):
    _go_const(tmp_repo, "agent.decide")
    spec(tmp_repo, "`agent.decide.backend_misconfigured`\n", "telemetry.md")
    assert [f.code for f in sr.check_attrs()] == ["ATTR_UNDOCUMENTED"]
    spec(tmp_repo, "`agent.decide`, then `agent.decide.x`.\n", "telemetry.md")
    assert sr.check_attrs() == []


def test_baseline_suppresses_attr_but_never_refs(tmp_repo, capsys):
    _go_const(tmp_repo, "a.b")
    spec(tmp_repo, "`svc/gone.go:3`\n", "telemetry.md")
    sr.BASELINE.write_text(sr.render_baseline(sr.check_attrs()))
    assert sr.run_check(None) == 1
    out = capsys.readouterr().out
    assert "REF_NO_FILE" in out and "ATTR_UNDOCUMENTED" not in out


def test_hand_built_ref_fingerprint_in_baseline_never_swallows_the_ref(
    tmp_repo, capsys
):
    spec(tmp_repo, "`svc/gone.go:3`\n", "telemetry.md")
    [ref_finding] = sr.check_refs(sr.scan_specs())
    assert ref_finding.code == "REF_NO_FILE"
    sr.BASELINE.write_text(
        json.dumps(
            {
                "version": 1,
                "findings": [
                    {
                        "code": ref_finding.code,
                        "file": ref_finding.file,
                        "value": ref_finding.value,
                        "fingerprint": ref_finding.fingerprint,
                    }
                ],
            }
        )
    )
    assert sr.run_check(None) == 1
    assert "REF_NO_FILE" in capsys.readouterr().out
    assert json.loads(sr.render_baseline([ref_finding]))["findings"] == []


def test_new_attr_fails_and_stale_baseline_entry_fails(tmp_repo, capsys):
    _go_const(tmp_repo, "a.b")
    spec(tmp_repo, "nothing\n", "telemetry.md")
    assert sr.run_check(None) == 1  # not in the (absent) baseline
    sr.BASELINE.write_text(sr.render_baseline(sr.check_attrs()))
    assert sr.run_check(None) == 0
    spec(tmp_repo, "`a.b`\n", "telemetry.md")  # now documented: entry is stale
    assert sr.run_check(None) == 1
    assert "stale entry" in capsys.readouterr().out


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_base(repo):
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")


def test_line_ref_growth_warns_and_shrinking_does_not(tmp_repo):
    spec(tmp_repo, "`svc/pkg/state.go:3`\n")
    _commit_base(tmp_repo)
    spec(tmp_repo, "`svc/pkg/state.go:3` `svc/pkg/state.go:5`\n")
    warnings, errors = sr.growth_report(sr.scan_specs(), "HEAD")
    assert errors == [] and len(warnings) == 1
    assert "grew by 1" in warnings[0] and "svc/pkg/state.go:Store.Load" in warnings[0]
    spec(tmp_repo, "`svc/pkg/state.go:digest`\n")
    assert sr.growth_report(sr.scan_specs(), "HEAD") == ([], [])


def test_moving_a_ref_between_spec_files_is_not_growth(tmp_repo):
    spec(tmp_repo, "`svc/pkg/state.go:3`\n", "flags.md")
    _commit_base(tmp_repo)
    spec(tmp_repo, "none\n", "flags.md")
    spec(tmp_repo, "`svc/pkg/state.go:3`\n", "services.md")
    assert sr.growth_report(sr.scan_specs(), "HEAD")[0] == []


def test_baseline_growth_needs_the_label(tmp_repo, monkeypatch):
    sr.BASELINE.write_text(json.dumps({"version": 1, "findings": []}))
    _commit_base(tmp_repo)
    _go_const(tmp_repo, "a.b")
    sr.BASELINE.write_text(sr.render_baseline(sr.check_attrs()))
    monkeypatch.setenv("PR_LABELS", "")
    assert len(sr.growth_report([], "HEAD")[1]) == 1
    monkeypatch.setenv("PR_LABELS", "x,spec-baseline-growth")
    assert sr.growth_report([], "HEAD")[1] == []


def test_baseline_growth_is_by_fingerprint_not_count(tmp_repo, monkeypatch):
    _go_const(tmp_repo, "a.b")
    sr.BASELINE.write_text(sr.render_baseline(sr.check_attrs()))
    _commit_base(tmp_repo)
    # one entry swapped for another: same count, a new fingerprint
    _go_const(tmp_repo, "c.d")
    sr.BASELINE.write_text(sr.render_baseline(sr.check_attrs()))
    monkeypatch.setenv("PR_LABELS", "")
    [err] = sr.growth_report([], "HEAD")[1]
    assert "'c.d'" in err
    monkeypatch.setenv("PR_LABELS", "spec-baseline-growth")
    assert sr.growth_report([], "HEAD")[1] == []


def test_baseline_growth_is_skipped_without_pr_context(tmp_repo, monkeypatch):
    """merge_group has neither PR_NUMBER nor PR_LABELS: the PR already passed."""
    sr.BASELINE.write_text(json.dumps({"version": 1, "findings": []}))
    _commit_base(tmp_repo)
    _go_const(tmp_repo, "a.b")
    sr.BASELINE.write_text(sr.render_baseline(sr.check_attrs()))
    monkeypatch.delenv("PR_LABELS", raising=False)
    monkeypatch.setenv("PR_NUMBER", "")
    assert sr.growth_report([], "HEAD")[1] == []


def test_suggest_prints_a_ready_path_symbol(tmp_repo, capsys):
    spec(tmp_repo, "`svc/pkg/state.go:6`\n")
    assert sr.main(["--suggest"]) == 0
    assert "-> svc/pkg/state.go:Store.Load" in capsys.readouterr().out


def test_baseline_is_sorted_and_line_free(tmp_repo):
    _go_const(tmp_repo, "a.b")
    data = json.loads(sr.render_baseline(sr.check_attrs()))
    assert data["findings"][0].keys() == {"code", "file", "value", "fingerprint"}


def test_real_repo_check_passes():
    """The CI gate itself: the committed specs resolve against the committed code."""
    assert sr.main(["--check"]) == 0
