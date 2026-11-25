#!/usr/bin/env python3
"""Issue #10057: review findings the autofix editor cannot reach.

Covers the ledger classifier (scripts/review_issue_ledger.sh), the follow-up
issue sync (review_unreachable_followup_sync in scripts/review_apply_fixes.sh),
the judge ledger reader (scripts/review_rb_judge.sh), the editor-reachable
subcommand (scripts/review_untrusted_workspace.py) and the no_result split in
scripts/collect_pr_check_runs_context.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
LEDGER_SCRIPT = SCRIPTS / "review_issue_ledger.sh"
APPLY_FIXES = SCRIPTS / "review_apply_fixes.sh"
RB_JUDGE = SCRIPTS / "review_rb_judge.sh"
COMMIT_CHANGES = SCRIPTS / "review_commit_changes.sh"
WORKSPACE_PY = SCRIPTS / "review_untrusted_workspace.py"
COLLECT_PY = SCRIPTS / "collect_pr_check_runs_context.py"

PR = "4242"
LEDGER_REL = ".ai/review_issue_ledger/pr-4242.txt"
AUTO_CLOSE_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s*:?\s+#\d+", re.IGNORECASE)


def _skip_unless_gawk() -> None:
	if shutil.which("gawk") is None:
		raise unittest.SkipTest("gawk is required by the review ledger scripts")


def _issue_block(block_id: str, file_path: str, lens: str, evidence: str = "Missing guard on this step.") -> str:
	return textwrap.dedent(
		f"""\
		=== ISSUE {block_id} ===
		FILE: {file_path}
		LINES: 3
		LENS: {lens}
		SEVERITY: high
		CLASSIFICATION: must-fix
		EVIDENCE:
		\x20\x20reviewer_1> "{evidence}"
		CURRENT_CODE:
		\x20\x20run: echo hi
		SUGGESTED_APPROACH:
		\x20\x20Add the guard.
		=== END ISSUE {block_id} ===
		"""
	)


def _seed_repo(root: Path) -> None:
	files = {
		".github/workflows/ci.yml": "name: ci\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n",
		"src/module.py": "def add(a, b):\n\ttotal = a + b\n\treturn total\n",
		"scripts/x.sh": "#!/usr/bin/env bash\necho one\necho two\n",
	}
	for rel, text in files.items():
		path = root / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")
	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
	for key, value in (("user.email", "test@local"), ("user.name", "test"), ("commit.gpgsign", "false")):
		subprocess.run(["git", "config", key, value], cwd=root, check=True)
	subprocess.run(["git", "add", "-A"], cwd=root, check=True)
	subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def _env(root: Path, **overrides: str) -> dict[str, str]:
	env = os.environ.copy()
	for key in ("GITHUB_WORKSPACE", "WRITE_GUARDS_ENABLED", "SUPPORT_SCRIPTS_DIR", "PR_CHECK_RUNS_CONTEXT_FILE", "REVIEW_UNREACHABLE_FOLLOWUP_ENABLED"):
		env.pop(key, None)
	runtime = root / "runtime"
	env.update({
		"PYTHONDONTWRITEBYTECODE": "1",
		"RUNTIME_DIR": str(runtime),
		"PR_NUMBER": PR,
		"REVIEW_LEDGER_PATH": LEDGER_REL,
		"REVIEW_LEDGER_ENABLED": "1",
		"REVIEW_LEDGER_PERSIST_LIMIT": "2",
		"REVIEW_ISSUES_FILE": str(runtime / "review_issues.txt"),
		"LEDGER_STATUS_FILE": str(runtime / "ledger_status.txt"),
		"FLOOR_TAGS_FILE": str(runtime / "floor_tags.txt"),
		"ALLOW_WORKFLOW_EDITS": "false",
		"IS_WORKFLOW_SOURCE_REPO": "true",
		"GITHUB_REPOSITORY": "octo/repo",
	})
	env.update(overrides)
	return env


def _run_ledger(root: Path, issues_text: str, iteration: int, **overrides: str) -> subprocess.CompletedProcess[str]:
	_skip_unless_gawk()
	runtime = root / "runtime"
	runtime.mkdir(parents=True, exist_ok=True)
	(runtime / "review_issues.txt").write_text(issues_text, encoding="utf-8")
	env = _env(root, AUTOFIX_ITERATION=str(iteration), **overrides)
	return subprocess.run(["bash", str(LEDGER_SCRIPT)], cwd=root, env=env, capture_output=True, text=True, check=True)


def _extract_shell_function(script: Path, name: str) -> str:
	text = script.read_text(encoding="utf-8")
	start = text.index(f"{name}() {{\n")
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def _fake_gh(root: Path) -> Path:
	bin_dir = root / "fakebin"
	bin_dir.mkdir(exist_ok=True)
	log = root / "gh_calls.log"
	gh = bin_dir / "gh"
	gh.write_text(
		textwrap.dedent(
			f"""\
			#!/usr/bin/env bash
			printf '%s\\n' "$*" >> "{log}"
			prev=""
			for arg in "$@"; do
			\x20\x20if [ "$prev" = "--body-file" ]; then cat "$arg" >> "{root}/gh_bodies.log"; fi
			\x20\x20prev="$arg"
			done
			if [ "$1 $2" = "issue create" ]; then echo "https://github.com/octo/repo/issues/777"; fi
			if [ "$1 $2" = "issue comment" ]; then echo "https://github.com/octo/repo/issues/777#issuecomment-1"; fi
			exit 0
			"""
		),
		encoding="utf-8",
	)
	gh.chmod(0o755)
	return bin_dir


def _run_sync(root: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
	bin_dir = _fake_gh(root)
	driver = root / "sync_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		"gh_retry() { \"$@\"; }\n"
		"ensure_label_exists() { :; }\n"
		+ _extract_shell_function(APPLY_FIXES, "review_unreachable_followup_sync")
		+ "review_unreachable_followup_sync\n",
		encoding="utf-8",
	)
	env = _env(root, AUTOFIX_ITERATION="1", **overrides)
	env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
	return subprocess.run(["bash", str(driver)], cwd=root, env=env, capture_output=True, text=True, check=True)


def _gh_calls(root: Path) -> list[str]:
	log = root / "gh_calls.log"
	return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _ledger_text(root: Path) -> str:
	return (root / LEDGER_REL).read_text(encoding="utf-8")


def _entry_fields(root: Path) -> dict[str, str]:
	fields: dict[str, str] = {}
	for line in _ledger_text(root).splitlines():
		if ":" in line and not line.startswith(("=== ", "  ")):
			key, value = line.split(":", 1)
			fields.setdefault(key.strip(), value.strip())
	return fields


def _issues(root: Path) -> str:
	return (root / "runtime" / "review_issues.txt").read_text(encoding="utf-8")


def test_workflow_file_finding_recorded_once_and_followup_created(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	result = _run_ledger(tmp_path, _issue_block("001", ".github/workflows/ci.yml", "CORRECTNESS & LOGIC"), 1)
	assert "unreachable:1" in result.stderr
	fields = _entry_fields(tmp_path)
	assert fields["STATUS"] == "unreachable"
	assert fields["UNREACHABLE_REASON"] == "write_guard"
	assert fields["UNREACHABLE_SECURITY"] == "false"
	assert fields["UNREACHABLE_FOLLOWUP"] == "pending"
	issues = _issues(tmp_path)
	assert "=== UNREACHABLE ISSUE 001 ===" in issues
	assert "=== ISSUE 001 ===" not in issues
	assert "FOLLOWUP: pending" in issues

	sync = _run_sync(tmp_path)
	calls = _gh_calls(tmp_path)
	assert len(calls) == 1
	assert calls[0].startswith("issue create")
	assert "--label ai:clarification" in calls[0]
	assert "REVIEW_UNREACHABLE pr=4242 round=1 findings=1 followup=777 outcome=recorded" in sync.stdout
	body = (tmp_path / "gh_bodies.log").read_text(encoding="utf-8")
	assert "Refs #4242" in body
	assert "<!-- AI_REVIEW_UNREACHABLE_FOLLOWUP pr=4242 -->" in body
	assert ".github/workflows/ci.yml" in body
	assert not AUTO_CLOSE_RE.search(body)
	ledger = _ledger_text(tmp_path)
	assert "UNREACHABLE_FOLLOWUP_ISSUE: 777" in ledger
	assert "UNREACHABLE_FOLLOWUP: #777" in ledger
	assert "FOLLOWUP: #777" in _issues(tmp_path)
	summary = (tmp_path / "runtime" / "review_unreachable_summary.md").read_text(encoding="utf-8")
	assert summary.startswith("### Unreachable findings (tracked in #777)")


def test_next_round_same_finding_no_second_issue_no_fix_round(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	block = _issue_block("001", ".github/workflows/ci.yml", "CORRECTNESS & LOGIC")
	_run_ledger(tmp_path, block, 1)
	_run_sync(tmp_path)
	assert len(_gh_calls(tmp_path)) == 1
	first_persist = _entry_fields(tmp_path)["PERSIST_COUNT"]

	_run_ledger(tmp_path, block, 2)
	fields = _entry_fields(tmp_path)
	# Sticky: not promoted to accepted-residual by the persist limit and no
	# persist-count growth.
	assert fields["STATUS"] == "unreachable"
	assert fields["PERSIST_COUNT"] == first_persist
	assert fields["UNREACHABLE_FOLLOWUP"] == "#777"
	assert "UNREACHABLE_FOLLOWUP_ISSUE: 777" in _ledger_text(tmp_path)
	issues = _issues(tmp_path)
	assert "=== ISSUE 001 ===" not in issues
	assert "FOLLOWUP: #777" in issues
	sync = _run_sync(tmp_path)
	assert len(_gh_calls(tmp_path)) == 1
	assert "findings=0 followup=777 outcome=skipped" in sync.stdout


def test_new_finding_appends_to_existing_followup(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	first = _issue_block("001", ".github/workflows/ci.yml", "CORRECTNESS & LOGIC")
	_run_ledger(tmp_path, first, 1)
	_run_sync(tmp_path)
	second = first + _issue_block("002", ".github/ai/write_guards.v1.json", "CORRECTNESS & LOGIC", "Config gap.")
	_run_ledger(tmp_path, second, 2)
	sync = _run_sync(tmp_path)
	calls = _gh_calls(tmp_path)
	assert len(calls) == 2
	assert calls[1].startswith("issue comment 777")
	assert "findings=1 followup=777 outcome=recorded" in sync.stdout
	assert "dot_directory" in _ledger_text(tmp_path)


def test_security_finding_on_unreachable_file_stays_blocking(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	_run_ledger(tmp_path, _issue_block("001", ".github/workflows/ci.yml", "SECURITY & INPUT VALIDATION"), 1)
	fields = _entry_fields(tmp_path)
	assert fields["STATUS"] == "unreachable"
	assert fields["UNREACHABLE_SECURITY"] == "true"
	issues = _issues(tmp_path)
	assert "=== ISSUE 001 ===\nUNREACHABLE: security-hold\n" in issues
	assert "UNREACHABLE ISSUE" not in issues

	sync = _run_sync(tmp_path)
	assert "outcome=recorded" in sync.stdout
	assert _gh_calls(tmp_path)[0].startswith("issue create")
	body = (tmp_path / "gh_bodies.log").read_text(encoding="utf-8")
	assert "keeps the PR blocked" in body

	text = RB_JUDGE.read_text(encoding="utf-8")
	start = text.index("render_review_rb_prior_round_decisions_file() {")
	body_start = text.index("<<'PY'\n", start) + len("<<'PY'\n")
	parser = text[body_start:text.index("\nPY\n", body_start)]
	out = subprocess.run(
		[sys.executable, "-", str(tmp_path / LEDGER_REL)],
		input=parser, capture_output=True, text=True, check=True,
	).stdout
	assert "prior_decision=unreachable-followup" in out
	assert "followup=#777" in out
	assert "security_hold=true" in out


def test_reachable_finding_unchanged(tmp_path: Path) -> None:
	block = _issue_block("001", "scripts/x.sh", "CORRECTNESS & LOGIC")
	outputs = []
	for switch in ("true", "false"):
		root = tmp_path / switch
		_seed_repo(root)
		_run_ledger(root, block, 1, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED=switch)
		outputs.append((_issues(root), _ledger_text(root), (root / "runtime" / "ledger_status.txt").read_text(encoding="utf-8")))
	assert outputs[0] == outputs[1]
	assert outputs[0][0] == block
	sync = _run_sync(tmp_path / "true")
	assert _gh_calls(tmp_path / "true") == []
	assert "findings=0 followup=none outcome=skipped" in sync.stdout
	assert not (tmp_path / "true" / "runtime" / "review_unreachable_summary.md").exists()


def test_failing_check_named_in_finding_stays_reachable(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	context = tmp_path / "runtime" / "pr_check_runs_context.txt"
	context.parent.mkdir(parents=True, exist_ok=True)
	context.write_text(
		"PR_CHECK_RUNS_CONTEXT\nfailed_count: 1\n\nfailed[0].name: unit-tests\nfailed[0].conclusion: failure\n",
		encoding="utf-8",
	)
	block = _issue_block("001", ".github/workflows/ci.yml", "CORRECTNESS & LOGIC", "The unit-tests check fails on the head.")
	_run_ledger(tmp_path, block, 1, PR_CHECK_RUNS_CONTEXT_FILE=str(context))
	assert _entry_fields(tmp_path)["STATUS"] == "NEW"
	assert _issues(tmp_path) == block


def test_cancelled_ci_job_not_a_finding(monkeypatch: pytest.MonkeyPatch) -> None:
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_10057", COLLECT_PY)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	raw = json.dumps({"check_runs": [
		{"name": "orchestrate-poll shard 3", "status": "completed", "conclusion": "cancelled", "html_url": "https://x/1", "output": {"summary": "cancelled"}},
		{"name": "lint", "status": "completed", "conclusion": "failure", "html_url": "https://x/2", "output": {"summary": "E501"}},
	]})
	monkeypatch.delenv("CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT", raising=False)
	monkeypatch.setenv("REVIEW_UNREACHABLE_FOLLOWUP_ENABLED", "true")
	text = module._build_context_text(raw_text=raw, head_sha="a" * 40, final_status="ok")
	assert "failed_count: 1\n" in text
	assert "no_result_count: 1\n" in text
	assert "no_result[0].name: orchestrate-poll shard 3" in text
	assert "no_result[0].conclusion: cancelled" in text
	assert "failed[0].name: lint" in text
	assert "failed[1]" not in text
	assert "orchestrate-poll" not in text.split("no_result[0]")[0]

	# The clean-result merge probe keeps a cancelled check as a failure.
	monkeypatch.setenv("CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT", "true")
	probe = module._build_context_text(raw_text=raw, head_sha="a" * 40, final_status="ok")
	assert "failed_count: 2\n" in probe
	assert "no_result" not in probe

	# Callers that do not opt in (check_failure_triage.sh) are unchanged.
	monkeypatch.delenv("CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT", raising=False)
	monkeypatch.delenv("REVIEW_UNREACHABLE_FOLLOWUP_ENABLED", raising=False)
	plain = module._build_context_text(raw_text=raw, head_sha="a" * 40, final_status="ok")
	assert "failed_count: 2\n" in plain
	assert "no_result" not in plain


def test_switch_off_output_identical(tmp_path: Path) -> None:
	fixture = (REPO_ROOT / "tests" / "fixtures" / "review_pipeline" / "review_issues_two_distinct.txt").read_text(encoding="utf-8")
	outputs = []
	for switch in ("true", "false"):
		root = tmp_path / f"fixture-{switch}"
		_seed_repo(root)
		_run_ledger(root, fixture, 1, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED=switch)
		_run_ledger(root, fixture, 2, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED=switch)
		outputs.append((_issues(root), _ledger_text(root), (root / "runtime" / "ledger_status.txt").read_text(encoding="utf-8")))
	assert outputs[0] == outputs[1]

	# Switched off, a workflow-file finding is handled exactly like any other.
	root = tmp_path / "off"
	_seed_repo(root)
	block = _issue_block("001", ".github/workflows/ci.yml", "CORRECTNESS & LOGIC")
	result = _run_ledger(root, block, 1, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="false")
	assert _issues(root) == block
	assert "UNREACHABLE" not in _ledger_text(root)
	assert _entry_fields(root)["STATUS"] == "NEW"
	assert "unreachable:" not in result.stderr
	assert "accepted-residual:0 accepted_residual_added=0" in result.stderr
	assert not (root / "runtime" / "review_unreachable_new.tsv").exists()
	sync = _run_sync(root, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="false")
	assert _gh_calls(root) == []
	assert "outcome=skipped" in sync.stdout


def test_protected_prefix_rule_matches_commit_guard() -> None:
	ledger = LEDGER_SCRIPT.read_text(encoding="utf-8")
	match = re.search(r"^REVIEW_UNREACHABLE_PROTECTED_PREFIX_RE='([^']+)'$", ledger, re.MULTILINE)
	assert match is not None
	commit = COMMIT_CHANGES.read_text(encoding="utf-8")
	assert f"grep -Eq '{match.group(1)}'" in commit


def test_consumer_protected_path_is_unreachable(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	_run_ledger(tmp_path, _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC"), 1, IS_WORKFLOW_SOURCE_REPO="false")
	fields = _entry_fields(tmp_path)
	assert fields["STATUS"] == "unreachable"
	assert fields["UNREACHABLE_REASON"] == "consumer_protected_path"


def test_editor_reachable_subcommand(tmp_path: Path) -> None:
	paths = tmp_path / "paths.txt"
	report = tmp_path / "report.tsv"
	paths.write_text(
		"scripts/a.sh\n.claude/commands/foo.md\n.github/ai/x.json\nbad name\n../escape.py\n",
		encoding="utf-8",
	)
	env = os.environ.copy()
	env.pop("GITHUB_WORKSPACE", None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(
		[sys.executable, "-I", "-B", str(WORKSPACE_PY), "editor-reachable", str(tmp_path), str(paths), str(report)],
		env=env, capture_output=True, text=True, check=True,
	)
	assert report.read_text(encoding="utf-8").splitlines() == [
		"reachable\tscripts/a.sh",
		"unreachable\tnot_admitted_command\t.claude/commands/foo.md",
		"unreachable\tdot_directory\t.github/ai/x.json",
	]
	assert "REVIEW_EDITOR_REACHABLE checked=3 unreachable=2" in result.stderr
	assert "foo.md" not in result.stderr


def _run_rb_judge_render(root: Path, ledger: Path, **overrides: str) -> str:
	driver = root / "rb_render_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		+ _extract_shell_function(RB_JUDGE, "flag_enabled")
		+ _extract_shell_function(RB_JUDGE, "render_review_rb_prior_round_decisions_file")
		+ 'render_review_rb_prior_round_decisions_file "$1" "$2"\n',
		encoding="utf-8",
	)
	out = root / "rb_prior.txt"
	env = os.environ.copy()
	for key in ("REVIEW_LEDGER_ENABLED", "REVIEW_LEDGER_REREVIEW_ENABLED", "REVIEW_UNREACHABLE_FOLLOWUP_ENABLED"):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(overrides)
	subprocess.run(["bash", str(driver), str(ledger), str(out)], cwd=root, env=env, capture_output=True, text=True, check=True)
	return out.read_text(encoding="utf-8")


def test_judge_sees_unreachable_rows_with_rereview_off(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	reachable = _issue_block("001", "scripts/x.sh", "CORRECTNESS & LOGIC")
	# Consumer protected path: decided without the write-guard config.
	held = _issue_block("002", "prompts/mode-x.txt", "SECURITY & INPUT VALIDATION")
	_run_ledger(tmp_path, reachable + held, 1, IS_WORKFLOW_SOURCE_REPO="false")
	ledger = tmp_path / LEDGER_REL
	# Rereview off (the workflow default): only the unreachable row is rendered.
	text = _run_rb_judge_render(tmp_path, ledger, REVIEW_LEDGER_REREVIEW_ENABLED="false")
	rows = [line for line in text.splitlines() if line]
	assert len(rows) == 1
	assert "prior_decision=unreachable-followup" in rows[0]
	assert "security_hold=true" in rows[0]
	assert "file=prompts/mode-x.txt" in rows[0]
	# Switch off: the earlier empty output.
	assert _run_rb_judge_render(tmp_path, ledger, REVIEW_LEDGER_REREVIEW_ENABLED="false", REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="false") == ""
	# Rereview on: every entry, as before.
	full = _run_rb_judge_render(tmp_path, ledger, REVIEW_LEDGER_REREVIEW_ENABLED="true")
	assert len([line for line in full.splitlines() if line]) == 2


def test_security_hold_blocks_clean_marker() -> None:
	text = (SCRIPTS / "review_autofix_step_post_editor_summary.sh").read_text(encoding="utf-8")
	branch = text.index('clean_marker_reason="unreachable_security_hold"')
	assert "review_unreachable_all.tsv" in text[branch - 400:branch]
	assert "$5 == \"true\"" in text[branch - 400:branch]
	# The hold is checked before the branch that writes the marker.
	assert branch < text.index("clean_marker_suffix=$'\\n\\n'\"<!-- review-autofix-clean:v1")


def test_editor_prompt_restates_unreachable_paths(tmp_path: Path) -> None:
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	(runtime / "review_unreachable_all.tsv").write_text(
		"iss_a\t.github/workflows/ci.yml\t3\tX\tfalse\twrite_guard\t#5\niss_b\tbad path\t3\tX\tfalse\twrite_guard\tpending\n",
		encoding="utf-8",
	)
	driver = tmp_path / "note_driver.sh"
	driver.write_text(
		_extract_shell_function(APPLY_FIXES, "_review_unreachable_editor_scope_note")
		+ 'printf "A%s\\nB\\n" "$(_review_unreachable_editor_scope_note)"\n',
		encoding="utf-8",
	)
	env = os.environ.copy()
	env["RUNTIME_DIR"] = str(runtime)
	env.pop("REVIEW_UNREACHABLE_FOLLOWUP_ENABLED", None)
	on = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, check=True).stdout
	assert "- .github/workflows/ci.yml" in on
	assert "bad path" not in on
	env["REVIEW_UNREACHABLE_FOLLOWUP_ENABLED"] = "false"
	off = subprocess.run(["bash", str(driver)], env=env, capture_output=True, text=True, check=True).stdout
	assert off == "A\nB\n"
	assert "authoritative findings source.$(_review_unreachable_editor_scope_note)\n" in APPLY_FIXES.read_text(encoding="utf-8")


def test_editor_reachable_leaves_non_file_names_reachable(tmp_path: Path) -> None:
	(tmp_path / "scripts").mkdir()
	(tmp_path / "blob.bin").write_bytes(b"\0")
	paths = tmp_path / "paths.txt"
	report = tmp_path / "report.tsv"
	paths.write_text("scripts\nN-A\nblob.bin\n", encoding="utf-8")
	env = os.environ.copy()
	env.pop("GITHUB_WORKSPACE", None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	subprocess.run(
		[sys.executable, "-I", "-B", str(WORKSPACE_PY), "editor-reachable", str(tmp_path), str(paths), str(report)],
		env=env, capture_output=True, text=True, check=True,
	)
	assert report.read_text(encoding="utf-8").splitlines() == ["unreachable\tunsupported_type\tblob.bin"]


@pytest.mark.parametrize("value, split", [("true", True), ("1", True), ("yes", True), ("On", True), ("false", False), ("0", False), ("off", False), ("", False)])
def test_no_result_switch_parsing_matches_ledger(monkeypatch: pytest.MonkeyPatch, value: str, split: bool) -> None:
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_10057b", COLLECT_PY)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	monkeypatch.setenv("REVIEW_UNREACHABLE_FOLLOWUP_ENABLED", value)
	assert module._no_result_split_enabled() is split


ENABLE_AUTO_MERGE = SCRIPTS / "review_enable_auto_merge.sh"


def _extract_brace_newline_function(script: Path, name: str) -> str:
	text = script.read_text(encoding="utf-8")
	start = text.index(f"{name}()\n{{\n")
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def test_resume_rebuilds_unreachable_tsv_from_ledger(tmp_path: Path) -> None:
	# A same-head partial resume skips the ledger stage and the partial
	# snapshot does not carry the TSVs; they are rebuilt from the ledger.
	_seed_repo(tmp_path)
	held = _issue_block("002", "prompts/mode-x.txt", "SECURITY & INPUT VALIDATION")
	_run_ledger(tmp_path, held, 1, IS_WORKFLOW_SOURCE_REPO="false")
	runtime = tmp_path / "runtime"
	expected = (runtime / "review_unreachable_all.tsv").read_text(encoding="utf-8")
	assert "\tprompts/mode-x.txt\t" in expected
	for name in ("review_unreachable_all.tsv", "review_unreachable_new.tsv"):
		(runtime / name).unlink()
	driver = tmp_path / "rebuild_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		+ _extract_shell_function(APPLY_FIXES, "_review_unreachable_rebuild_from_ledger")
		+ "_review_unreachable_rebuild_from_ledger\n",
		encoding="utf-8",
	)
	result = subprocess.run(["bash", str(driver)], cwd=tmp_path, env=_env(tmp_path), capture_output=True, text=True, check=True)
	assert "rebuilt review_unreachable_all.tsv from the ledger (1 row(s))" in result.stdout
	assert (runtime / "review_unreachable_all.tsv").read_text(encoding="utf-8") == expected
	new_rows = (runtime / "review_unreachable_new.tsv").read_text(encoding="utf-8").splitlines()
	assert len(new_rows) == 1 and new_rows[0].split("\t")[4:] == ["true", "consumer_protected_path", "-"]
	# Switched off: nothing is rebuilt.
	for name in ("review_unreachable_all.tsv", "review_unreachable_new.tsv"):
		(runtime / name).unlink()
	subprocess.run(["bash", str(driver)], cwd=tmp_path, env=_env(tmp_path, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="false"), capture_output=True, text=True, check=True)
	assert not (runtime / "review_unreachable_all.tsv").exists()


def _hold_active(root: Path, **overrides: str) -> bool:
	driver = root / "hold_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		+ _extract_brace_newline_function(ENABLE_AUTO_MERGE, "review_unreachable_security_hold_active")
		+ "if review_unreachable_security_hold_active; then echo HOLD; else echo NONE; fi\n",
		encoding="utf-8",
	)
	env = _env(root, **overrides)
	env.pop("RUNTIME_DIR", None)
	out = subprocess.run(["bash", str(driver)], cwd=root, env=env, capture_output=True, text=True, check=True).stdout
	return out.strip() == "HOLD"


def test_unreachable_security_hold_refuses_auto_merge(tmp_path: Path) -> None:
	# The hold is read from the durable ledger (no runtime TSV needed).
	held_root = tmp_path / "held"
	_seed_repo(held_root)
	_run_ledger(held_root, _issue_block("002", "prompts/mode-x.txt", "SECURITY & INPUT VALIDATION"), 1, IS_WORKFLOW_SOURCE_REPO="false")
	assert _hold_active(held_root)
	assert not _hold_active(held_root, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="false")
	plain_root = tmp_path / "plain"
	_seed_repo(plain_root)
	_run_ledger(plain_root, _issue_block("002", "prompts/mode-x.txt", "CORRECTNESS & LOGIC"), 1, IS_WORKFLOW_SOURCE_REPO="false")
	assert not _hold_active(plain_root)
	assert not _hold_active(tmp_path)
	text = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
	gate = text.index("if review_unreachable_security_hold_active; then")
	# Checked before the first head-gate success / merge-label path.
	assert gate < text.index('if [ "${ENABLE_AUTO_MERGE}" != "true" ]; then')
	assert "action=refuse reason=unreachable_security_hold" in text[gate:gate + 400]


def test_judge_merge_gate_holds_on_unreachable_security(tmp_path: Path) -> None:
	# Every judge merge path (merge, merge_with_followup, final-attempt fix,
	# no-change fix) calls rb_security_merge_gate; the wrapper holds them all.
	text = RB_JUDGE.read_text(encoding="utf-8")
	start = text.index("rb_unreachable_security_hold_active() {\n")
	end = text.index("  _rb_unreachable_inner_merge_gate\n}\n", start) + len("  _rb_unreachable_inner_merge_gate\n}\n")
	driver = tmp_path / "judge_gate_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		"rb_security_merge_gate() { echo INNER; return 0; }\n"
		+ text[start:end]
		+ 'if rb_security_merge_gate; then echo ALLOW; else echo "HOLD=${RB_SECURITY_HOLD_REASON}"; fi\n',
		encoding="utf-8",
	)
	held_root = tmp_path / "held"
	_seed_repo(held_root)
	_run_ledger(held_root, _issue_block("002", "prompts/mode-x.txt", "SECURITY & INPUT VALIDATION"), 1, IS_WORKFLOW_SOURCE_REPO="false")
	held = subprocess.run(["bash", str(driver)], cwd=held_root, env=_env(held_root), capture_output=True, text=True, check=True).stdout
	assert "HOLD=unreachable_security_hold" in held
	assert "action=refuse reason=unreachable_security_hold" in held
	assert "INNER" not in held
	off = subprocess.run(["bash", str(driver)], cwd=held_root, env=_env(held_root, REVIEW_UNREACHABLE_FOLLOWUP_ENABLED="false"), capture_output=True, text=True, check=True).stdout
	assert "INNER" in off and "ALLOW" in off
	plain = subprocess.run(["bash", str(driver)], cwd=tmp_path, env=_env(tmp_path), capture_output=True, text=True, check=True).stdout
	assert "INNER" in plain and "ALLOW" in plain


def test_orphan_create_recovers_number_without_second_issue(tmp_path: Path) -> None:
	_seed_repo(tmp_path)
	_run_ledger(tmp_path, _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC"), 1, IS_WORKFLOW_SOURCE_REPO="false")
	bin_dir = _fake_gh(tmp_path)
	gh = bin_dir / "gh"
	gh.write_text(
		gh.read_text(encoding="utf-8").replace(
			'echo "https://github.com/octo/repo/issues/777"; fi',
			'echo "created"; fi\nif [ "$1 $2" = "issue list" ]; then echo 778; fi',
		),
		encoding="utf-8",
	)
	driver = tmp_path / "orphan_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		"gh_retry() { \"$@\"; }\n"
		"ensure_label_exists() { :; }\n"
		+ _extract_shell_function(APPLY_FIXES, "review_unreachable_followup_sync")
		+ "review_unreachable_followup_sync\n",
		encoding="utf-8",
	)
	env = _env(tmp_path, AUTOFIX_ITERATION="1")
	env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
	sync = subprocess.run(["bash", str(driver)], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
	assert "followup=778 outcome=recorded" in sync.stdout
	assert "UNREACHABLE_FOLLOWUP_ISSUE: 778" in _ledger_text(tmp_path)
	calls = _gh_calls(tmp_path)
	assert [call.split()[:2] for call in calls] == [["issue", "create"], ["issue", "list"]]


def test_resumed_rows_keep_reviewer_text(tmp_path: Path) -> None:
	# Rows rebuilt from the ledger carry block "-"; the follow-up body finds
	# the reviewer text in the restored pre-rewrite copy by FILE and LINES.
	_seed_repo(tmp_path)
	_run_ledger(tmp_path, _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC", "Distinct reviewer evidence."), 1, IS_WORKFLOW_SOURCE_REPO="false")
	runtime = tmp_path / "runtime"
	rows = (runtime / "review_unreachable_new.tsv").read_text(encoding="utf-8").splitlines()
	(runtime / "review_unreachable_new.tsv").write_text(
		"".join("\t".join(row.split("\t")[:6] + ["-"]) + "\n" for row in rows), encoding="utf-8",
	)
	_run_sync(tmp_path)
	body = (tmp_path / "gh_bodies.log").read_text(encoding="utf-8")
	assert "Distinct reviewer evidence." in body
	finalize = (SCRIPTS / "review_autofix_step_partial_finalize.sh").read_text(encoding="utf-8")
	assert '"review_issues.pre_unreachable.txt",' in finalize


def test_security_hold_withdraws_existing_auto_merge() -> None:
	text = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
	gate = text.index("if review_unreachable_security_hold_active; then")
	branch = text[gate:text.index("\nfi\n", gate)]
	assert 'review_head_gate_withdraw_auto_merge "${GITHUB_REPOSITORY}" "${PR_NUMBER}" ""' in branch
	assert "--disable-auto" in branch
	assert branch.rstrip().endswith("exit 0")


def _write_gh(root: Path, create_out: str, list_body: str) -> Path:
	# Fake gh whose create output and list behaviour the test chooses.
	bin_dir = _fake_gh(root)
	gh = bin_dir / "gh"
	gh.write_text(
		gh.read_text(encoding="utf-8").replace(
			'echo "https://github.com/octo/repo/issues/777"; fi',
			f'echo "{create_out}"; fi\nif [ "$1 $2" = "issue list" ]; then {list_body}; fi',
		),
		encoding="utf-8",
	)
	return bin_dir


def _run_sync_with(root: Path, bin_dir: Path, iteration: int) -> subprocess.CompletedProcess[str]:
	driver = root / "sync_custom_driver.sh"
	driver.write_text(
		"set -euo pipefail\n"
		"gh_retry() { \"$@\"; }\n"
		"ensure_label_exists() { :; }\n"
		+ _extract_shell_function(APPLY_FIXES, "review_unreachable_followup_sync")
		+ "review_unreachable_followup_sync\n",
		encoding="utf-8",
	)
	env = _env(root, AUTOFIX_ITERATION=str(iteration))
	env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
	return subprocess.run(["bash", str(driver)], cwd=root, env=env, capture_output=True, text=True, check=True)


def test_double_orphan_never_opens_a_second_issue(tmp_path: Path) -> None:
	# Create succeeds without a URL and the recovery list fails: the ledger
	# keeps an "orphan" sentinel, and later rounds look the issue up before
	# any create instead of opening a second one.
	_seed_repo(tmp_path)
	block = _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC")
	_run_ledger(tmp_path, block, 1, IS_WORKFLOW_SOURCE_REPO="false")
	sync = _run_sync_with(tmp_path, _write_gh(tmp_path, "created", "exit 1"), 1)
	assert "followup=none outcome=orphan" in sync.stdout
	assert "UNREACHABLE_FOLLOWUP_ISSUE: orphan" in _ledger_text(tmp_path)
	assert "UNREACHABLE_FOLLOWUP: pending" in _ledger_text(tmp_path)

	# Round 2: the ledger stage keeps the sentinel; the lookup fails again,
	# so the round makes no create at all.
	_run_ledger(tmp_path, block, 2, IS_WORKFLOW_SOURCE_REPO="false")
	assert "UNREACHABLE_FOLLOWUP_ISSUE: orphan" in _ledger_text(tmp_path)
	(tmp_path / "gh_calls.log").unlink()
	sync = _run_sync_with(tmp_path, _write_gh(tmp_path, "created", "exit 1"), 2)
	assert "outcome=orphan" in sync.stdout
	assert [call.split()[:2] for call in _gh_calls(tmp_path)] == [["issue", "list"]]

	# Round 3: the lookup finds the issue; the round comments on it.
	_run_ledger(tmp_path, block, 3, IS_WORKFLOW_SOURCE_REPO="false")
	(tmp_path / "gh_calls.log").unlink()
	sync = _run_sync_with(tmp_path, _write_gh(tmp_path, "created", "echo 900"), 3)
	assert "followup=900 outcome=recorded" in sync.stdout
	calls = _gh_calls(tmp_path)
	assert [call.split()[:3] for call in calls] == [["issue", "list", "--repo"], ["issue", "comment", "900"]]
	ledger = _ledger_text(tmp_path)
	assert "UNREACHABLE_FOLLOWUP_ISSUE: 900" in ledger
	assert "orphan" not in ledger
	assert "UNREACHABLE_FOLLOWUP: #900" in ledger


def test_shared_location_fallback_omits_ambiguous_text(tmp_path: Path) -> None:
	# Two blocks share FILE and LINES; a rebuilt row (block "-") cannot tell
	# them apart, so no reviewer text is attached rather than the wrong one.
	_seed_repo(tmp_path)
	issues = _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC", "First evidence.") + _issue_block(
		"002", "prompts/mode-x.txt", "ERROR PATHS & EDGE CASES", "Second evidence."
	)
	_run_ledger(tmp_path, issues, 1, IS_WORKFLOW_SOURCE_REPO="false")
	runtime = tmp_path / "runtime"
	rows = (runtime / "review_unreachable_new.tsv").read_text(encoding="utf-8").splitlines()
	assert len(rows) == 2
	(runtime / "review_unreachable_new.tsv").write_text(
		"".join("\t".join(row.split("\t")[:6] + ["-"]) + "\n" for row in rows), encoding="utf-8",
	)
	_run_sync(tmp_path)
	body = (tmp_path / "gh_bodies.log").read_text(encoding="utf-8")
	assert "First evidence." not in body
	assert "Second evidence." not in body
	assert body.count("### `prompts/mode-x.txt`") == 2


def test_failed_create_writes_orphan_sentinel_and_lookup_ignores_phase_label(tmp_path: Path) -> None:
	# A create that exits non-zero may still have opened the issue: the
	# ledger gets the "orphan" sentinel, and the next round's lookup does not
	# filter on the ai:clarification phase label (the pipeline swaps it).
	_seed_repo(tmp_path)
	block = _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC")
	_run_ledger(tmp_path, block, 1, IS_WORKFLOW_SOURCE_REPO="false")
	bin_dir = _write_gh(tmp_path, "created", "exit 1")
	gh = bin_dir / "gh"
	gh.write_text(
		gh.read_text(encoding="utf-8").replace('echo "created"; fi', 'exit 1; fi'),
		encoding="utf-8",
	)
	sync = _run_sync_with(tmp_path, bin_dir, 1)
	assert "outcome=orphan" in sync.stdout
	assert "create_failed=1" in sync.stdout
	assert "UNREACHABLE_FOLLOWUP_ISSUE: orphan" in _ledger_text(tmp_path)
	assert "UNREACHABLE_FOLLOWUP: pending" in _ledger_text(tmp_path)

	_run_ledger(tmp_path, block, 2, IS_WORKFLOW_SOURCE_REPO="false")
	(tmp_path / "gh_calls.log").unlink()
	sync = _run_sync_with(tmp_path, _write_gh(tmp_path, "created", "echo 901"), 2)
	assert "followup=901 outcome=recorded" in sync.stdout
	calls = _gh_calls(tmp_path)
	assert [call.split()[:2] for call in calls] == [["issue", "list"], ["issue", "comment"]]
	assert "ai:clarification" not in calls[0]


def test_unavailable_ci_context_marks_no_new_finding_unreachable(tmp_path: Path) -> None:
	# Without a collected check-runs context the CI exemption cannot decide,
	# so the round marks nothing new unreachable (fail toward reachable).
	_seed_repo(tmp_path)
	context = tmp_path / "runtime" / "pr_check_runs_context.txt"
	context.parent.mkdir(parents=True, exist_ok=True)
	block = _issue_block("001", "prompts/mode-x.txt", "CORRECTNESS & LOGIC")
	for text in ("", "PR_CHECK_RUNS_CONTEXT\ncollection_status: api_error\n"):
		context.write_text(text, encoding="utf-8")
		(tmp_path / LEDGER_REL).unlink(missing_ok=True)
		result = _run_ledger(tmp_path, block, 1, IS_WORKFLOW_SOURCE_REPO="false", PR_CHECK_RUNS_CONTEXT_FILE=str(context))
		assert "unreachable_check=skipped reason=ci_context_unavailable" in result.stderr
		assert _entry_fields(tmp_path)["STATUS"] == "NEW"
		assert _issues(tmp_path) == block
	# A collected context keeps the classification.
	context.write_text("PR_CHECK_RUNS_CONTEXT\ncollection_status: ready\nfailed_count: 0\n", encoding="utf-8")
	(tmp_path / LEDGER_REL).unlink()
	_run_ledger(tmp_path, block, 1, IS_WORKFLOW_SOURCE_REPO="false", PR_CHECK_RUNS_CONTEXT_FILE=str(context))
	assert _entry_fields(tmp_path)["STATUS"] == "unreachable"
	# A sticky entry stays unreachable when the context becomes unavailable.
	context.write_text("", encoding="utf-8")
	_run_ledger(tmp_path, block, 2, IS_WORKFLOW_SOURCE_REPO="false", PR_CHECK_RUNS_CONTEXT_FILE=str(context))
	assert _entry_fields(tmp_path)["STATUS"] == "unreachable"
