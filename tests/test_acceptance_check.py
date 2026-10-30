#!/usr/bin/env python3
"""Tests for scripts/acceptance_check.py and its wiring (issue #8427, phase P2
of docs/plans/persistent-unattended-workflows-plan.md)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "acceptance_check.py"
PROMPT = REPO_ROOT / "prompts" / "mode-plan-acceptance.txt"
TEMPLATE = REPO_ROOT / "prompts" / "_templates" / "mode-plan-acceptance.txt"
RUN_PLAN = REPO_ROOT / "scripts" / "run_plan_codex.sh"
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
IMPLEMENT_WF = REPO_ROOT / ".github" / "workflows" / "implement.yml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import acceptance_check as ac  # noqa: E402

CI_YML = """name: CI
on:
  pull_request:
jobs:
  lint:
    name: Lint code
    runs-on: ubuntu-latest
    steps:
      - name: Not a job
        run: true
  unit:
    runs-on: ubuntu-latest
  caller:
    name: "Release gate"
    uses: ./.github/workflows/reuse.yml
  dyn:
    name: Build ${{ matrix.os }}
"""
DEPLOY_YML = """name: Deploy
on: push
jobs:
  deploy:
    name: Deploy prod
"""


@pytest.fixture()
def repo(tmp_path):
	root = tmp_path / "repo"
	(root / ".github" / "workflows").mkdir(parents=True)
	(root / ".github" / "workflows" / "ci.yml").write_text(CI_YML, encoding="utf-8")
	(root / ".github" / "workflows" / "deploy.yml").write_text(DEPLOY_YML, encoding="utf-8")
	(root / "tests").mkdir()
	(root / "tests" / "test_a.py").write_text("def test_x():\n\tpass\n", encoding="utf-8")
	return root


def _plan(*lines: str, files: tuple[str, ...] = ("scripts/a.py",), pre: str = "") -> str:
	body = "\n".join(lines)
	listed = "\n".join(f"- `{f}`" for f in files)
	return f"Implementation Plan\n{pre}\n## Files likely to change\n{listed}\n\n## Acceptance\n{body}\n\nSTATUS: CLEAR\n"


def _valid_plan() -> str:
	return _plan(
		"ACCEPTANCE: test tests/test_a.py::test_x red-on-base",
		"ACCEPTANCE: check Lint code",
		"ACCEPTANCE: post-merge workflow .github/workflows/deploy.yml job Deploy prod",
	)


# --- validate ------------------------------------------------------------------


def test_valid_done_check(repo):
	result = ac.validate(_valid_plan(), repo)
	assert result["state"] == "valid", result["errors"]
	assert result["tests"] == [{"path": "tests/test_a.py", "name": "test_x", "red_on_base": True}]
	assert result["checks"] == [{"name": "Lint code"}]
	assert result["post_merge"] == {"workflow": ".github/workflows/deploy.yml", "job": "Deploy prod"}
	assert result["source"] == "plan" and result["errors"] == []


def test_post_merge_none_form_and_job_id(repo):
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge none: library only"), repo)
	assert result["state"] == "valid", result["errors"]
	assert result["post_merge"] is None and result["post_merge_none"] == "library only"
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge workflow .github/workflows/deploy.yml job deploy"), repo)
	assert result["state"] == "valid", result["errors"]


@pytest.mark.parametrize("check, ok", [
	("Lint code", True),
	("lint", False),  # an id with a name set is not the check-run name
	("unit", True),
	("Release gate", True),
	("Release gate / build", True),
	("Build ${{ matrix.os }}", False),
	("Nope", False),
	("Not a job", False),
])
def test_check_name_matching(repo, check, ok):
	result = ac.validate(_plan(f"ACCEPTANCE: check {check}", "ACCEPTANCE: post-merge none: n/a"), repo)
	assert (result["state"] == "valid") is ok, result["errors"]
	if not ok:
		assert any(e.startswith(("unknown_check:", "unknown_form:")) for e in result["errors"])


def test_yaml_absent_fallback_matches(repo, monkeypatch):
	monkeypatch.setattr(ac, "yaml", None)
	jobs = ac._workflow_jobs(repo)
	assert jobs[".github/workflows/ci.yml"]["names"] == {"Lint code", "unit", "Release gate"}
	assert jobs[".github/workflows/ci.yml"]["ids"] == {"lint", "unit", "caller", "dyn"}


def test_test_path_listed_only_in_files_section(repo):
	plan = _plan("ACCEPTANCE: test tests/test_new.py", "ACCEPTANCE: post-merge none: n/a", files=("tests/test_new.py",))
	assert ac.validate(plan, repo)["state"] == "valid"
	plan = _plan("ACCEPTANCE: test tests/test_new.py", "ACCEPTANCE: post-merge none: n/a")
	result = ac.validate(plan, repo)
	assert result["state"] == "invalid" and "missing_test_path:tests/test_new.py" in result["errors"]


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "tests/../x.py", "tests\\a.py", "tests//a.py"])
def test_unsafe_test_paths_rejected(repo, path):
	result = ac.validate(_plan(f"ACCEPTANCE: test {path}", "ACCEPTANCE: post-merge none: n/a"), repo)
	assert result["state"] == "invalid"


def test_symlinked_test_path_rejected(repo, tmp_path):
	outside = tmp_path / "outside.py"
	outside.write_text("x", encoding="utf-8")
	(repo / "tests" / "test_link.py").symlink_to(outside)
	result = ac.validate(_plan("ACCEPTANCE: test tests/test_link.py", "ACCEPTANCE: post-merge none: n/a"), repo)
	assert result["state"] == "invalid" and "missing_test_path:tests/test_link.py" in result["errors"]


def test_missing_workflow_and_unknown_job(repo):
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge workflow .github/workflows/none.yml"), repo)
	assert "missing_workflow:.github/workflows/none.yml" in result["errors"]
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge workflow .github/workflows/deploy.yml job ghost"), repo)
	assert "unknown_job:.github/workflows/deploy.yml:ghost" in result["errors"]


def test_counts_and_required_lines(repo):
	tests = [f"ACCEPTANCE: test tests/test_a.py::t{i}" for i in range(11)]
	result = ac.validate(_plan(*tests, "ACCEPTANCE: post-merge none: n/a"), repo)
	assert "too_many_tests" in result["errors"]
	checks = ["ACCEPTANCE: check unit"] * 6
	result = ac.validate(_plan(*checks, "ACCEPTANCE: post-merge none: n/a"), repo)
	assert "too_many_checks" in result["errors"]
	result = ac.validate(_plan("ACCEPTANCE: post-merge none: n/a"), repo)
	assert result["errors"] == ["no_test_or_check"]
	result = ac.validate(_plan("ACCEPTANCE: check unit"), repo)
	assert result["errors"] == ["post_merge_count:0"]
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge none: a", "ACCEPTANCE: post-merge none: b"), repo)
	assert result["errors"] == ["post_merge_count:2"]


def test_unknown_forms_and_bullets(repo):
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge none: n/a", "ACCEPTANCE: deploy it", "ACCEPTANCE: check `x`"), repo)
	assert result["state"] == "invalid" and result["errors"] == ["unknown_form:3", "unknown_form:4"]
	# A bullet is not an ACCEPTANCE line at all.
	assert ac.validate(_plan("- ACCEPTANCE: check unit"), repo)["state"] == "none"


def test_fenced_lines_and_lines_before_the_files_heading_are_ignored(repo):
	fenced = _plan("```", "ACCEPTANCE: check unit", "ACCEPTANCE: post-merge none: n/a", "```")
	assert ac.validate(fenced, repo)["state"] == "none"
	before = _plan(pre="ACCEPTANCE: check unit\nACCEPTANCE: post-merge none: n/a")
	assert ac.validate(before, repo)["state"] == "none"
	assert ac.validate("Implementation Plan\nno heading\nACCEPTANCE: check unit\n", repo)["state"] == "none"


def test_no_lines_gives_none(repo):
	result = ac.validate(_plan("nothing here"), repo)
	assert result == ac._empty()


def test_heal_reproducible_derivation(repo):
	plan = _plan("## Counterfactual check", "HEAL_COUNTERFACTUAL: REPRODUCIBLE: tests/test_a.py::test_x, tests/test_a.py")
	result = ac.validate(plan, repo)
	assert result["state"] == "valid" and result["source"] == "heal", result["errors"]
	assert [t["red_on_base"] for t in result["tests"]] == [True, True]
	assert result["post_merge"] is None and result["post_merge_none"] == ""
	# Today's grammar (PASS) derives nothing.
	assert ac.validate(_plan("HEAL_COUNTERFACTUAL: PASS"), repo)["state"] == "none"
	# A heal plan with explicit lines follows the normal contract.
	plan = _plan("HEAL_COUNTERFACTUAL: REPRODUCIBLE: tests/test_a.py", "ACCEPTANCE: check unit")
	result = ac.validate(plan, repo)
	assert result["source"] == "plan" and result["errors"] == ["post_merge_count:0"]


def test_marker_too_large_turns_invalid(repo, monkeypatch):
	monkeypatch.setattr(ac, "MAX_MARKER_BYTES", 50)
	result = ac.validate(_valid_plan(), repo)
	assert result["state"] == "invalid" and result["errors"] == ["marker_too_large"]


# --- marker / parse ------------------------------------------------------------


def test_marker_exact_bytes_and_escaping(repo):
	result = ac.validate(_valid_plan(), repo)
	line = ac.marker_line(result)
	assert line == (
		'<!-- AI_ACCEPTANCE_V1 {"checks":[{"name":"Lint code"}],"errors":[],'
		'"post_merge":{"job":"Deploy prod","workflow":".github/workflows/deploy.yml"},"post_merge_none":"",'
		'"schema":"acceptance.v1","source":"plan","state":"valid",'
		'"tests":[{"name":"test_x","path":"tests/test_a.py","red_on_base":true}]} -->'
	)
	obj = dict(result, post_merge=None, post_merge_none="a <b> & --> c")
	escaped = ac.marker_line(obj)
	inner = escaped[len("<!-- AI_ACCEPTANCE_V1 "):-len(" -->")]
	assert "<" not in inner and ">" not in inner and "&" not in inner
	assert json.loads(inner)["post_merge_none"] == "a <b> & --> c"


def _comment(plan: str, marker: str) -> str:
	return f"Implementation Plan\n\n{plan}\nTo proceed reply:\n\n/approved\n\n/reclarify\n\n{marker}\n<!-- ai:plan-proposal:v1 -->\n"


def test_parse_reads_only_the_trailer_position(repo):
	result = ac.validate(_valid_plan(), repo)
	marker = ac.marker_line(result)
	assert ac.parse_marker(_comment(_valid_plan(), marker)) == result
	# A marker the model wrote into the plan body is ignored.
	forged = _comment(_valid_plan() + "\n" + marker + "\n", "")
	assert ac.parse_marker(forged)["state"] == "none"
	assert ac.parse_marker(_comment("x", "<!-- AI_ACCEPTANCE_V1 {not json} -->"))["state"] == "none"
	bad = dict(result, checks=[{"name": "a|b"}])
	assert ac.parse_marker(_comment("x", ac.marker_line(bad)))["state"] == "none"
	invalid = dict(result, state="invalid")
	assert ac.parse_marker(_comment("x", ac.marker_line(invalid)))["state"] == "none"
	assert ac.parse_marker("no trailer")["state"] == "none"


# --- render ---------------------------------------------------------------------


def test_render_not_run_table_and_statuses(repo):
	result = ac.validate(_valid_plan(), repo)
	text = ac.render(result)
	assert text.startswith("## Acceptance\n")
	assert "| test (red-on-base) | tests/test_a.py::test_x | not run |" in text
	assert "| check | Lint code | not run |" in text
	assert "Post-merge: workflow .github/workflows/deploy.yml job Deploy prod" in text
	gate = {"acceptance": {"items": [
		{"kind": "test", "target": "tests/test_a.py::test_x", "status": "unmet (passes on base)"},
		{"kind": "check", "target": "Lint code", "status": "bogus"},
	]}}
	text = ac.render(result, gate)
	assert "| unmet (passes on base) |" in text and "| check | Lint code | not run |" in text
	assert ac.render(ac._empty()) == ""


def test_render_neutralises_mentions_and_refs(repo):
	result = ac.validate(_plan("ACCEPTANCE: check unit", "ACCEPTANCE: post-merge none: Fixes #8427 for @someone"), repo)
	text = ac.render(result)
	assert "#8427" not in text and "@someone" not in text and "`" not in text


# --- CLI ------------------------------------------------------------------------


def _cli(*args):
	env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
	return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)


def test_cli_round_trip(repo, tmp_path):
	plan_file = tmp_path / "plan.txt"
	plan_file.write_text(_valid_plan(), encoding="utf-8")
	out = tmp_path / "acceptance.json"
	proc = _cli("validate", "--plan-file", str(plan_file), "--repo-root", str(repo), "--out", str(out))
	assert proc.returncode == 0 and "PERSIST acceptance outcome=valid errors=0" in proc.stderr
	proc = _cli("marker", "--in", str(out))
	marker = proc.stdout.strip()
	assert marker.startswith("<!-- AI_ACCEPTANCE_V1 {")
	comment = tmp_path / "comment.txt"
	comment.write_text(_comment(_valid_plan(), marker), encoding="utf-8")
	parsed = tmp_path / "parsed.json"
	assert _cli("parse", "--plan-comment-file", str(comment), "--out", str(parsed)).returncode == 0
	proc = _cli("render", "--in", str(parsed))
	assert proc.stdout.startswith("## Acceptance\n")


def test_cli_invalid_prints_no_marker(repo, tmp_path):
	plan_file = tmp_path / "plan.txt"
	plan_file.write_text(_plan("ACCEPTANCE: check Nope"), encoding="utf-8")
	out = tmp_path / "acceptance.json"
	proc = _cli("validate", "--plan-file", str(plan_file), "--repo-root", str(repo), "--out", str(out))
	assert proc.returncode == 0 and "outcome=invalid errors=2" in proc.stderr
	proc = _cli("marker", "--in", str(out))
	assert proc.returncode == 0 and proc.stdout == ""
	proc = _cli("render", "--in", str(out))
	assert proc.returncode == 0 and proc.stdout == ""
	assert _cli("validate", "--plan-file", str(tmp_path / "missing"), "--repo-root", str(repo), "--out", str(out)).returncode == 2


def test_helper_is_ascii_and_executable():
	text = SCRIPT.read_text(encoding="utf-8")
	assert text.isascii() and os.access(SCRIPT, os.X_OK)


# --- wiring -----------------------------------------------------------------------


def _step(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	end = text.find("\n      - name: ", start + 1)
	return text[start:end if end != -1 else len(text)]


def test_prompt_template_matches_rendered_copy():
	assert PROMPT.read_bytes() == TEMPLATE.read_bytes()
	text = PROMPT.read_text(encoding="utf-8")
	assert text.startswith("# tier: DEFAULT\n") and text.isascii()
	assert "ACCEPTANCE: test <repo-path>[::<name>] [red-on-base]" in text


def test_run_plan_codex_append_is_switch_gated_and_outside_the_heal_block():
	text = RUN_PLAN.read_text(encoding="utf-8")
	block_start = text.index('if [ "${PERSIST_ACCEPTANCE_ENABLED:-false}" = "true" ]; then')
	heal_start = text.index('if [ "${HEAL_ROUTE:-false}" = true ]; then')
	heal_end = text.index("\nfi\n", heal_start)
	assert block_start > heal_end
	assert text.index("prompts/mode-plan-acceptance.txt") > block_start
	assert "PERSIST acceptance outcome=skipped reason=prompt_absent" in text


def test_workflows_default_the_switch_off():
	for path in (PLAN_WF, IMPLEMENT_WF):
		text = path.read_text(encoding="utf-8")
		assert text.count("PERSIST_ACCEPTANCE_ENABLED: ${{ vars.PERSIST_ACCEPTANCE_ENABLED || 'false' }}") == 1
		assert " attempt_journal.py acceptance_check.py " in text


def test_plan_workflow_wiring():
	text = PLAN_WF.read_text(encoding="utf-8")
	assert "acceptance_check.py targeted_file_context.py gh_api_retry.py" in text
	assert "install -m 0644 \"${src}\" prompts/mode-plan-acceptance.txt" in text
	validate = _step(text, "Validate done-check (acceptance)")
	assert "if: env.PERSIST_ACCEPTANCE_ENABLED == 'true' && env.SKIP_PLAN != 'true'" in validate
	assert "continue-on-error: true" in validate
	assert text.index("- name: Parse planning output") < text.index("- name: Validate done-check (acceptance)") < text.index("- name: Post implementation plan")
	post = _step(text, "Post implementation plan")
	gate = 'if [ "${PERSIST_ACCEPTANCE_ENABLED:-false}" = true ] && [ -s "${RUNTIME_DIR}/acceptance_marker.txt" ]; then cat'
	assert post.index(gate) < post.index('echo "<!-- ai:plan-proposal:v1 -->"')
	journal = _step(text, "Record attempt journal entry (plan)")
	assert '--acceptance-file "${RUNTIME_DIR}/acceptance.json"' in journal
	assert 'if [ "${PERSIST_ACCEPTANCE_ENABLED:-false}" = true ]' in journal


def test_implement_pr_body_is_switch_gated():
	text = IMPLEMENT_WF.read_text(encoding="utf-8")
	start = text.index("# Done-check (issue #8427, P2): the trailer marker")
	block = text[start:text.index("printf '%s\\n\\n' \"${PR_TITLE}\"", start)]
	assert 'if [ "${PERSIST_ACCEPTANCE_ENABLED:-false}" = true ] && [ -s "${PLAN_FILE:-}" ]; then' in block
	assert "parse --plan-comment-file" in block and "render --in" in block
	assert text.index('cat "${GATE_SUMMARY}"') < start < text.index("lint_pr_body_auto_close.py\" \\\n            --pr-body-file")


def _post_brace_group() -> str:
	post = _step(PLAN_WF.read_text(encoding="utf-8"), "Post implementation plan")
	match = re.search(r"\n( +)\{\n.*?\n\1\} > \"\$\{PLAN_COMMENT_FILE\}\"\n", post, re.S)
	assert match
	return textwrap.dedent(match.group(0))


@pytest.mark.parametrize("switch", [None, "false", "true"])
def test_post_comment_is_byte_identical_with_the_switch_off(tmp_path, switch):
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	(runtime / "acceptance_marker.txt").write_text("<!-- AI_ACCEPTANCE_V1 {} -->\n", encoding="utf-8")
	codex = tmp_path / "codex_output.txt"
	codex.write_text("PLAN BODY\n", encoding="utf-8")
	comment = tmp_path / "comment.txt"
	env = {k: v for k, v in os.environ.items() if k != "PERSIST_ACCEPTANCE_ENABLED"}
	env.update({"RUNTIME_DIR": str(runtime), "CODEX_OUTPUT_FILE": str(codex), "PLAN_COMMENT_FILE": str(comment)})
	if switch is not None:
		env["PERSIST_ACCEPTANCE_ENABLED"] = switch
	proc = subprocess.run(["bash", "-euo", "pipefail", "-c", _post_brace_group()], env=env, capture_output=True, text=True, timeout=30)
	assert proc.returncode == 0, proc.stderr
	expected = (
		"Implementation Plan\n\nPLAN BODY\n\nTo proceed reply:\n\n/approved\n\n"
		"To restart clarification reply:\n\n/reclarify\n\n"
	)
	if switch == "true":
		expected += "<!-- AI_ACCEPTANCE_V1 {} -->\n"
	expected += "<!-- ai:plan-proposal:v1 -->\n"
	assert comment.read_text(encoding="utf-8") == expected
