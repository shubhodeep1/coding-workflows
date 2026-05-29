#!/usr/bin/env python3
"""Focused regression coverage for re-review suppression and approval rubric wiring."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"
CONSOLIDATE_SCRIPT = REPO_ROOT / "scripts" / "review_consolidate.sh"
POST_REVIEW_COMMENT_SCRIPT = REPO_ROOT / "scripts" / "post_review_comment.sh"
RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEW_CONSOLIDATOR_PROMPT = REPO_ROOT / "prompts" / "review-consolidator.txt"
REVIEW_BLOCKED_PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"


def _install_mock_codex(mock_bin_dir: Path, output_fixture: Path) -> Path:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	output_file = mock_bin_dir / "codex_output.txt"
	shutil.copy2(output_fixture, output_file)
	codex_script = mock_bin_dir / "codex"
	codex_script.write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"case \" $* \" in\n"
		"\t*\" exec \"*) ;;\n"
		"\t*) echo \"mock-codex supports only exec\" >&2; exit 2 ;;\n"
		"esac\n"
		"cat \"${MOCK_CODEX_OUTPUT_FILE}\"\n",
		encoding="utf-8",
	)
	codex_script.chmod(0o755)
	return output_file


def _run_consolidator(output_fixture_name: str) -> tuple[subprocess.CompletedProcess[str], Path]:
	tmp_dir = Path(tempfile.mkdtemp())
	runtime_dir = tmp_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(FIXTURES / "phase_f_reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")
	shutil.copy2(FIXTURES / "phase_f_prior_ledger.txt", runtime_dir / "review_issue_ledger.txt")
	mock_bin = tmp_dir / "mock_bin"
	mock_output = _install_mock_codex(mock_bin, FIXTURES / output_fixture_name)

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["SUPPORT_PROMPTS_DIR"] = str(REPO_ROOT / "prompts")
	env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
	env["REVIEW_CONSOLIDATOR_ENABLED"] = "1"
	env["REVIEW_LEDGER_ENABLED"] = "1"
	env["REVIEW_LEDGER_REREVIEW_ENABLED"] = "1"
	env["REVIEW_LEDGER_PATH"] = str(runtime_dir / "review_issue_ledger.txt")
	env["MOCK_CODEX_OUTPUT_FILE"] = str(mock_output)
	env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"

	result = subprocess.run(
		["bash", str(CONSOLIDATE_SCRIPT)],
		cwd=tmp_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	return result, runtime_dir


def _extract_shell_function(script_text: str, name: str) -> str:
	lines = script_text.splitlines()
	start = None
	for idx, line in enumerate(lines):
		if line.startswith(f"{name}() {{"):
			start = idx
			break
	if start is None:
		raise AssertionError(f"could not locate function {name}() in review_rb_judge.sh")
	for end in range(start + 1, len(lines)):
		if lines[end] == "}":
			return "\n".join(lines[start : end + 1])
	raise AssertionError(f"could not locate closing brace for function {name}()")


def _extract_break_glass_python_snippet() -> str:
	lines = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8").splitlines()
	step_start = None
	for idx, line in enumerate(lines):
		if line.strip() == "- name: Detect review-blocked break-glass override":
			step_start = idx
			break
	if step_start is None:
		raise AssertionError("missing break-glass workflow step")

	step_end = len(lines)
	for idx in range(step_start + 1, len(lines)):
		if lines[idx].startswith("      - name: "):
			step_end = idx
			break

	python_start = None
	python_end = None
	for idx in range(step_start, step_end):
		if "python3 - <<'PY' >> \"$GITHUB_ENV\"" in lines[idx]:
			python_start = idx + 1
			break
	if python_start is None:
		raise AssertionError("missing break-glass python heredoc")
	for idx in range(python_start, step_end):
		if lines[idx].strip() == "PY":
			python_end = idx
			break
	if python_end is None:
		raise AssertionError("missing break-glass heredoc terminator")
	return textwrap.dedent("\n".join(lines[python_start:python_end]))


def _install_mock_gh(mock_bin_dir: Path, state_file: Path) -> None:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	gh_script = mock_bin_dir / "gh"
	gh_script.write_text(
		"#!/usr/bin/env python3\n"
		"import json\n"
		"import os\n"
		"import sys\n"
		"from pathlib import Path\n"
		"\n"
		"state_path = Path(os.environ['MOCK_GH_STATE_FILE'])\n"
		"if state_path.exists():\n"
		"\tstate = json.loads(state_path.read_text(encoding='utf-8'))\n"
		"else:\n"
		"\tstate = {}\n"
		"args = sys.argv[1:]\n"
		"state.setdefault('api_calls', [])\n"
		"payload = ''\n"
		"if '--input' in args:\n"
		"\tinput_path = Path(args[args.index('--input') + 1])\n"
		"\tpayload = input_path.read_text(encoding='utf-8')\n"
		"state['api_calls'].append({'args': args, 'input': payload})\n"
		"state_path.write_text(json.dumps(state), encoding='utf-8')\n"
		"print('{}')\n",
		encoding="utf-8",
	)
	gh_script.chmod(0o755)


def test_prompts_and_workflow_wire_rereview_and_review_state_contract() -> None:
	consolidator_prompt = REVIEW_CONSOLIDATOR_PROMPT.read_text(encoding="utf-8")
	judge_prompt = REVIEW_BLOCKED_PROMPT.read_text(encoding="utf-8")
	workflow = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")

	assert "RE_REVIEW_SKIP:" in consolidator_prompt
	assert "Files absent from the bundle are intentionally invisible here" in consolidator_prompt
	assert '"review_state": "APPROVE" | "APPROVE_WITH_COMMENTS" | "COMMENT" | "REQUEST_CHANGES"' in judge_prompt
	assert "AI Materiality Advisory comment is informational only" in judge_prompt
	assert "REVIEW_LEDGER_REREVIEW_ENABLED: ${{ vars.REVIEW_LEDGER_REREVIEW_ENABLED || 'false' }}" in workflow
	assert "REVIEW_APPROVAL_RUBRIC_ENABLED: ${{ vars.REVIEW_APPROVAL_RUBRIC_ENABLED || 'false' }}" in workflow
	assert "REVIEW_BREAK_GLASS_ENABLED: ${{ vars.REVIEW_BREAK_GLASS_ENABLED || 'false' }}" in workflow


def test_consolidator_injects_prior_round_decisions_and_logs_rereview_skip() -> None:
	result, runtime_dir = _run_consolidator("phase_f_residual_suppressed.txt")
	assert result.returncode == 0, result.stderr

	prompt = (runtime_dir / "review_consolidator_prompt.txt").read_text(encoding="utf-8")
	raw = (runtime_dir / "consolidator_raw.txt").read_text(encoding="utf-8")

	assert "=== BEGIN PRIOR ROUND DECISIONS ===" in prompt
	assert "issue_id=issue_residual; file=src/module.py; lines=2-3; lens=CORRECTNESS & LOGIC; severity=high; status=accepted-residual; prior_decision=accepted-residual; persist_count=3" in prompt
	assert "issue_id=issue_wontfix; file=src/module.py; lines=6; lens=PERFORMANCE & RESOURCE USE; severity=low; status=PERSISTING; prior_decision=won't-fix; persist_count=1" in prompt
	assert raw == (FIXTURES / "phase_f_residual_suppressed.txt").read_text(encoding="utf-8")
	assert "stage=consolidator RE_REVIEW_SKIP: issue_residual accepted-residual" in result.stderr
	assert "stage=consolidator RE_REVIEW_SKIP: issue_wontfix won't-fix" in result.stderr


def test_consolidator_allows_worsened_prior_issue_to_reemit() -> None:
	result, runtime_dir = _run_consolidator("phase_f_worsened_reemit.txt")
	assert result.returncode == 0, result.stderr

	raw = (runtime_dir / "consolidator_raw.txt").read_text(encoding="utf-8")
	assert "=== ISSUE 201 ===" in raw
	assert "SEVERITY: blocker" in raw
	assert "RE_REVIEW_SKIP:" not in result.stderr


def test_break_glass_scan_prefers_latest_human_anchored_override() -> None:
	snippet = _extract_break_glass_python_snippet()
	env = os.environ.copy()
	env["PR_ISSUE_COMMENTS_FILE"] = str(FIXTURES / "phase_j_issue_comments_break_glass.json")
	env["PR_REVIEWS_FILE"] = str(FIXTURES / "phase_j_reviews_break_glass.json")

	result = subprocess.run(
		["python3", "-c", snippet],
		env=env,
		capture_output=True,
		text=True,
		check=True,
	)

	assert result.stdout.strip().splitlines() == [
		"REVIEW_BREAK_GLASS=true",
		"REVIEW_BREAK_GLASS_COMMENTER=bob",
	]


def test_review_state_mapping_and_break_glass_preserve_review_body() -> None:
	judge_text = RB_JUDGE_SCRIPT.read_text(encoding="utf-8")
	helper_script = "\n\n".join(
		[
			_extract_shell_function(judge_text, "flag_enabled"),
			_extract_shell_function(judge_text, "normalize_review_state"),
			_extract_shell_function(judge_text, "resolve_review_state_for_post"),
		]
	)
	break_glass_probe = subprocess.run(
		["bash"],
		input=(
			helper_script
			+ "\n"
			+ "REVIEW_APPROVAL_RUBRIC_ENABLED=true\n"
			+ "REVIEW_BREAK_GLASS_ENABLED=true\n"
			+ "REVIEW_BREAK_GLASS=true\n"
			+ "REVIEW_BREAK_GLASS_COMMENTER=bob\n"
			+ "PR_NUMBER=42\n"
			+ "resolve_review_state_for_post REQUEST_CHANGES\n"
		),
		capture_output=True,
		text=True,
		check=True,
	)
	assert break_glass_probe.stdout.strip() == "COMMENT"
	assert "BREAK_GLASS: pr=42 commenter=bob" in break_glass_probe.stderr

	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		mock_bin = workspace / "mock_bin"
		state_file = workspace / "gh_state.json"
		state_file.write_text("{}", encoding="utf-8")
		_install_mock_gh(mock_bin, state_file)
		(workspace / "support").mkdir(parents=True, exist_ok=True)

		comment_body = "## Review-Blocked Judge Decision\n\nKeep this review body intact.\n"
		comment_body_file = workspace / "comment_body.md"
		comment_body_file.write_text(comment_body, encoding="utf-8")
		approve_body = "Approved with comments body\n"
		approve_body_file = workspace / "approve_body.md"
		approve_body_file.write_text(approve_body, encoding="utf-8")

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["MOCK_GH_STATE_FILE"] = str(state_file)
		env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
		env["SUPPORT_SCRIPTS_DIR"] = str(workspace / "support")
		env["REPOSITORY"] = "owner/repo"
		env["PR_NUMBER"] = "42"
		env["HEAD_SHA"] = "deadbeef"
		env["HEAD_REF"] = "ai/test-branch"
		env["GH_TOKEN"] = "test-token"

		comment_post = subprocess.run(
			[
				"bash",
				str(POST_REVIEW_COMMENT_SCRIPT),
				"--review-state",
				break_glass_probe.stdout.strip(),
				"--body-file",
				str(comment_body_file),
			],
			env=env,
			capture_output=True,
			text=True,
			check=True,
		)
		assert "posted PR review state=COMMENT event=COMMENT" in comment_post.stdout

		approve_post = subprocess.run(
			[
				"bash",
				str(POST_REVIEW_COMMENT_SCRIPT),
				"--review-state",
				"APPROVE_WITH_COMMENTS",
				"--body-file",
				str(approve_body_file),
			],
			env=env,
			capture_output=True,
			text=True,
			check=True,
		)
		assert "posted PR review state=APPROVE_WITH_COMMENTS event=APPROVE" in approve_post.stdout

		state = json.loads(state_file.read_text(encoding="utf-8"))
		review_payloads = [
			json.loads(call["input"])
			for call in state.get("api_calls", [])
			if any("repos/owner/repo/pulls/42/reviews" == arg for arg in call.get("args", []))
		]
		assert [payload["event"] for payload in review_payloads] == ["COMMENT", "APPROVE"]
		assert review_payloads[0]["body"] == comment_body
		assert review_payloads[1]["body"] == approve_body
