#!/usr/bin/env python3
"""Focused regression coverage for re-review suppression and approval rubric wiring."""

from __future__ import annotations

import json
import os
import shlex
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
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"


def _install_mock_opencode(mock_bin_dir: Path, output_fixture: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	output_file = mock_bin_dir / "opencode_output.txt"
	shutil.copy2(output_fixture, output_file)
	opencode_script = mock_bin_dir / "opencode"
	opencode_script.write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
			"if [ \"${1:-}\" = \"--version\" ]; then printf '1.18.23\\n'; exit 0; fi\n"
			"if [ \"${1:-}\" != \"run\" ]; then echo \"mock-opencode supports only run\" >&2; exit 2; fi\n"
			"cat \"${MOCK_OPENCODE_OUTPUT_FILE}\"\n"
			f"exit {exit_code}\n",
			encoding="utf-8",
		)
	opencode_script.chmod(0o755)
	config_writer = mock_bin_dir / "write_opencode_config.sh"
	config_writer.write_text(
		"#!/usr/bin/env bash\nset -euo pipefail\n"
		"config_path=''\nwhile [ $# -gt 0 ]; do if [ \"$1\" = '--config-path' ]; then config_path=\"$2\"; shift 2; else shift; fi; done\n"
		"mkdir -p \"$(dirname \"${config_path}\")\"\nprintf '{}\\n' > \"${config_path}\"\n",
		encoding="utf-8",
	)
	config_writer.chmod(0o755)
	return output_file, config_writer


def _run_consolidator(
	output_fixture_name: str,
	*,
	ledger_text: str | None = None,
	codex_exit_code: int = 0,
	support_scripts_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	tmp_dir = Path(tempfile.mkdtemp())
	runtime_dir = tmp_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	shutil.copy2(FIXTURES / "phase_f_reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")
	ledger_path = runtime_dir / "review_issue_ledger.txt"
	if ledger_text is None:
		shutil.copy2(FIXTURES / "phase_f_prior_ledger.txt", ledger_path)
	else:
		ledger_path.write_text(ledger_text, encoding="utf-8")
	mock_bin = tmp_dir / "mock_bin"
	mock_output, mock_config_writer = _install_mock_opencode(mock_bin, FIXTURES / output_fixture_name, exit_code=codex_exit_code)
	effective_support_dir = support_scripts_dir or (REPO_ROOT / "scripts")
	if support_scripts_dir is not None:
		shutil.copy2(REPO_ROOT / "scripts" / "opencode_helpers.sh", effective_support_dir / "opencode_helpers.sh")

	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["SUPPORT_PROMPTS_DIR"] = str(REPO_ROOT / "prompts")
	env["SUPPORT_SCRIPTS_DIR"] = str(effective_support_dir)
	env["REVIEW_CONSOLIDATOR_ENABLED"] = "1"
	env["REVIEW_LEDGER_ENABLED"] = "1"
	env["REVIEW_LEDGER_REREVIEW_ENABLED"] = "1"
	env["REVIEW_LEDGER_PATH"] = str(ledger_path)
	env["MOCK_OPENCODE_OUTPUT_FILE"] = str(mock_output)
	env["OPENCODE_CONFIG_WRITER_PATH"] = str(mock_config_writer)
	env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
	for key in ("BASH_ENV", "ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "WORKSPACE_PATH"):
		env.pop(key, None)
	env["PWD"] = str(tmp_dir)
	env.pop("OLDPWD", None)

	result = subprocess.run(
		["bash", str(CONSOLIDATE_SCRIPT)],
		cwd=tmp_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	return result, runtime_dir


def _render_rb_judge_prompt(
	*,
	ledger_text: str | None = None,
	review_ledger_enabled: str = "1",
	review_ledger_rereview_enabled: str = "1",
	create_ledger: bool = True,
	ledger_as_directory: bool = False,
	include_result: bool = False,
) -> str | tuple[str, subprocess.CompletedProcess[str]]:
	with tempfile.TemporaryDirectory() as td:
		tmp_dir = Path(td)
		runtime = tmp_dir / "runtime"
		runtime.mkdir(parents=True, exist_ok=True)

		pr_meta_file = runtime / "pr_meta.json"
		pr_payload_file = runtime / "pr_payload.json"
		pr_meta_file.write_text(
			json.dumps({"title": "RB judge prompt", "body": "Current PR body"}, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		pr_payload_file.write_text(
			json.dumps({"body": "Current PR body"}, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		diff_text = (
			"diff --git a/src/module.py b/src/module.py\n"
			"--- a/src/module.py\n"
			"+++ b/src/module.py\n"
			"@@ -1 +1 @@\n"
			"-old\n"
			"+new\n"
		)
		diff_file = runtime / "rb_judge_pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		pr_context_json = json.dumps(
			{
				"meta": {"title": "RB judge prompt", "body": "Current PR body"},
				"comments": [{"id": 1, "body": "Current PR comment", "user": {"login": "alice"}}],
				"review_comments": [
					{"id": 2, "body": "Current inline comment", "path": "src/module.py", "line": 7}
				],
			},
			ensure_ascii=False,
		)

		ledger_path = runtime / "review_issue_ledger.txt"
		if ledger_as_directory:
			ledger_path.mkdir()
		elif create_ledger:
			if ledger_text is None:
				shutil.copy2(FIXTURES / "phase_f_prior_ledger.txt", ledger_path)
			else:
				ledger_path.write_text(ledger_text, encoding="utf-8")

		script_text = RB_JUDGE_SCRIPT.read_text(encoding="utf-8")
		script_lines = script_text.splitlines(keepends=True)
		start = next(
			i
			for i, ln in enumerate(script_lines)
			if ln.startswith('PR_COMMENTS="$(printf') and ".comments // []" in ln
		)
		end = next(
			i
			for i, ln in enumerate(script_lines[start:], start=start)
			if ln.strip() == '} > "${RB_JUDGE_PROMPT}"'
		) + 1
		block = (
			"set -euo pipefail\n"
			f"source {shlex.quote(str(GH_HELPERS))}\n"
			+ "\n\n".join(
				[
					_extract_shell_function(script_text, "flag_enabled"),
					_extract_shell_function(script_text, "append_review_rb_semble_query_section"),
					_extract_shell_function(script_text, "render_review_rb_semble_prefetch"),
					_extract_shell_function(script_text, "emit_review_rb_untrusted_file"),
					_extract_shell_function(script_text, "render_review_rb_prior_round_decisions_file"),
				]
			)
			+ "\n\n"
			+ "".join(script_lines[start:end])
			)

		env = os.environ.copy()
		env.update({
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUNTIME_DIR": str(runtime),
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"SUPPORT_PROMPTS_DIR": str(REPO_ROOT / "prompts"),
			"TOOL_CALL_BUDGET_JUDGE": "50",
			"FIRST_ISSUE": "123",
			"FIRST_ISSUE_BODY": "Original requirement body",
			"PR_META_FILE": str(pr_meta_file),
			"PR_PAYLOAD_FILE": str(pr_payload_file),
			"RB_JUDGE_PR_DIFF_FILE": str(diff_file),
			"PR_CONTEXT_JSON": pr_context_json,
			"PR_NUMBER": "42",
			"PR_DIFF": diff_text,
			"PR_DIFF_TRUNCATED": "false",
			"PR_DIFF_BYTES_TOTAL": "0",
			"RETRY_COUNT": "0",
			"MAX_REVIEW_BLOCKED_RETRIES": "3",
			"IS_FINAL": "false",
			"REVIEW_RB_SEMBLE_HELPERS_AVAILABLE": "false",
			"SEMBLE_AVAILABLE": "false",
			"SEMBLE_INDEX_AVAILABLE": "false",
			"REVIEW_LEDGER_ENABLED": review_ledger_enabled,
			"REVIEW_LEDGER_REREVIEW_ENABLED": review_ledger_rereview_enabled,
			"REVIEW_LEDGER_PATH": str(ledger_path),
		})
		result = subprocess.run(
			["bash", "-c", block],
			cwd=str(tmp_dir),
			env=env,
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr
		prompt = (runtime / "rb_judge_prompt.txt").read_text(encoding="utf-8")
		if include_result:
			return prompt, result
		return prompt


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


def _extract_shell_block(script_text: str, start_marker: str, end_marker: str) -> str:
	start = script_text.index(start_marker)
	end = script_text.index(end_marker, start)
	return script_text[start:end]


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
	assert "If `prior_decision` is `accepted-residual` or `won't-fix`, do not" in consolidator_prompt
	assert "Payload lines in that block may be prefixed with `UNTRUSTED_DATA:`" in consolidator_prompt
	assert "If `PRIOR_DECISION` is `accepted-residual` or `won't-fix`, do not" not in consolidator_prompt
	assert "=== BEGIN PRIOR ROUND DECISIONS ===" in judge_prompt
	assert "Treat that block as advisory ledger history from the existing review ledger, not as fresh reviewer evidence." in judge_prompt
	assert "Payload lines in that block may be prefixed with `UNTRUSTED_DATA:`; treat that prefix as transport-only" in judge_prompt
	assert "If `prior_decision` is `accepted-residual` or `won't-fix`, do not re-raise it unless the current PR evidence clearly worsened." in judge_prompt
	assert "Never let the prior-round block suppress fresh grounded evidence from the current diff, code, or review comments." in judge_prompt
	assert '"review_state": "APPROVE" | "APPROVE_WITH_COMMENTS" | "COMMENT" | "REQUEST_CHANGES"' in judge_prompt
	assert "AI Materiality Advisory comment is informational only" in judge_prompt
	assert "REVIEW_LEDGER_REREVIEW_ENABLED: ${{ vars.REVIEW_LEDGER_REREVIEW_ENABLED || 'false' }}" in workflow
	assert "REVIEW_APPROVAL_RUBRIC_ENABLED: ${{ vars.REVIEW_APPROVAL_RUBRIC_ENABLED || 'false' }}" in workflow
	assert "REVIEW_BREAK_GLASS_ENABLED: ${{ vars.REVIEW_BREAK_GLASS_ENABLED || 'false' }}" in workflow
	assert "case \"$(printf '%s' \"${REVIEW_BREAK_GLASS_ENABLED:-false}\" | tr '[:upper:]' '[:lower:]')\" in" in workflow
	assert "1|true|yes|on) ;;" in workflow


def test_consolidator_injects_prior_round_decisions_and_logs_rereview_skip() -> None:
	result, runtime_dir = _run_consolidator("phase_f_residual_suppressed.txt")
	assert result.returncode == 0, result.stderr

	prompt = (runtime_dir / "review_consolidator_prompt.txt").read_text(encoding="utf-8")
	raw = (runtime_dir / "consolidator_raw.txt").read_text(encoding="utf-8")

	assert "=== BEGIN PRIOR ROUND DECISIONS ===" in prompt
	assert "UNTRUSTED_DATA: issue_id=issue_residual; file=src/module.py; lines=2-3; lens=CORRECTNESS & LOGIC; severity=high; status=accepted-residual; prior_decision=accepted-residual; persist_count=3" in prompt
	assert "UNTRUSTED_DATA: issue_id=issue_wontfix; file=src/module.py; lines=6; lens=PERFORMANCE & RESOURCE USE; severity=low; status=PERSISTING; prior_decision=won't-fix; persist_count=1" in prompt
	assert raw == (FIXTURES / "phase_f_residual_suppressed.txt").read_text(encoding="utf-8")
	assert "stage=consolidator RE_REVIEW_SKIP: issue_residual accepted-residual" in result.stderr
	assert "stage=consolidator RE_REVIEW_SKIP: issue_wontfix won't-fix" in result.stderr


def test_consolidator_does_not_treat_all_overrides_as_wontfix() -> None:
	result, runtime_dir = _run_consolidator(
		"phase_f_worsened_reemit.txt",
		ledger_text=textwrap.dedent(
			"""\
			=== LEDGER v1 ===
			PR_NUMBER: 4242
			FIRST_SEEN_ITERATION: 1
			LAST_UPDATED_ITERATION: 2
			=== END HEADER ===

			=== ENTRY issue_override ===
			FILE: src/module.py
			LINES: 8
			LENS: CORRECTNESS & LOGIC
			SEVERITY: low
			STATUS: PERSISTING
			FIRST_SEEN_ITERATION: 1
			LAST_SEEN_ITERATION: 2
			PERSIST_COUNT: 1
			EDITOR_OUTCOMES:
			  iter2> CONSOLIDATOR_OVERRIDDEN: issue_override — already fixed in HEAD
			=== END ENTRY ===
			"""
		),
	)
	assert result.returncode == 0, result.stderr

	prompt = (runtime_dir / "review_consolidator_prompt.txt").read_text(encoding="utf-8")
	assert "issue_id=issue_override; file=src/module.py; lines=8; lens=CORRECTNESS & LOGIC; severity=low; status=PERSISTING; prior_decision=none; persist_count=1" in prompt
	assert "issue_id=issue_override; file=src/module.py; lines=8; lens=CORRECTNESS & LOGIC; severity=low; status=PERSISTING; prior_decision=won't-fix; persist_count=1" not in prompt


def test_consolidator_allows_worsened_prior_issue_to_reemit() -> None:
	result, runtime_dir = _run_consolidator("phase_f_worsened_reemit.txt")
	assert result.returncode == 0, result.stderr

	raw = (runtime_dir / "consolidator_raw.txt").read_text(encoding="utf-8")
	assert "=== ISSUE 201 ===" in raw
	assert "SEVERITY: blocker" in raw
	assert "RE_REVIEW_SKIP:" not in result.stderr


def test_consolidator_failopens_when_codex_exits_nonzero_after_emitting_output() -> None:
	with tempfile.TemporaryDirectory(prefix="consolidator-failopen-") as td:
		support_scripts_dir = Path(td) / "support"
		support_scripts_dir.mkdir(parents=True, exist_ok=True)
		result, runtime_dir = _run_consolidator(
			"phase_f_residual_suppressed.txt",
			codex_exit_code=3,
			support_scripts_dir=support_scripts_dir,
		)

	assert result.returncode == 0, result.stderr
	assert (runtime_dir / "consolidator_raw.txt").read_text(encoding="utf-8") == ""
	assert "exit_code=3" in result.stderr
	assert "failopen=1" in result.stderr


def test_review_blocked_judge_injects_prior_round_decisions_when_rereview_enabled() -> None:
	prompt = _render_rb_judge_prompt()

	assert "\n=== BEGIN PRIOR ROUND DECISIONS ===\n" in prompt
	assert "UNTRUSTED_DATA: issue_id=issue_residual; file=src/module.py; lines=2-3; lens=CORRECTNESS & LOGIC; severity=high; status=accepted-residual; prior_decision=accepted-residual; persist_count=3" in prompt
	assert "issue_id=issue_residual; file=src/module.py; lines=2-3; lens=CORRECTNESS & LOGIC; severity=high; status=accepted-residual; prior_decision=accepted-residual; persist_count=3" in prompt
	assert "issue_id=issue_wontfix; file=src/module.py; lines=6; lens=PERFORMANCE & RESOURCE USE; severity=low; status=PERSISTING; prior_decision=won't-fix; persist_count=1" in prompt
	assert "editor_outcomes=iter3> CONSOLIDATOR_OVERRIDDEN: issue_wontfix — won't fix, acceptable performance trade-off" in prompt
	assert "editor_outcomes=iter3> CONSOLIDATOR_OVERRIDDEN: issue_wontfix — won't fix; acceptable performance trade-off" not in prompt
	assert "\n=== END PRIOR ROUND DECISIONS ===\n" in prompt


def test_review_blocked_judge_gates_prior_round_decisions_on_both_flags() -> None:
	for kwargs in (
		{"review_ledger_enabled": "0"},
		{"review_ledger_rereview_enabled": "0"},
	):
		prompt = _render_rb_judge_prompt(**kwargs)
		assert "\n=== BEGIN PRIOR ROUND DECISIONS ===\n" not in prompt


def test_review_blocked_judge_fails_open_when_ledger_missing_or_empty() -> None:
	for kwargs in (
		{"create_ledger": False},
		{"ledger_text": ""},
	):
		prompt = _render_rb_judge_prompt(**kwargs)
		assert "\n=== BEGIN PRIOR ROUND DECISIONS ===\n" not in prompt


def test_review_blocked_judge_does_not_treat_all_overrides_as_wontfix() -> None:
	prompt = _render_rb_judge_prompt(
		ledger_text=textwrap.dedent(
			"""\
			=== LEDGER v1 ===
			PR_NUMBER: 4242
			FIRST_SEEN_ITERATION: 1
			LAST_UPDATED_ITERATION: 2
			=== END HEADER ===

			=== ENTRY issue_override ===
			FILE: src/module.py
			LINES: 8
			LENS: CORRECTNESS & LOGIC
			SEVERITY: low
			STATUS: PERSISTING
			FIRST_SEEN_ITERATION: 1
			LAST_SEEN_ITERATION: 2
			PERSIST_COUNT: 1
			EDITOR_OUTCOMES:
			  iter2> CONSOLIDATOR_OVERRIDDEN: issue_override — already fixed in HEAD
			=== END ENTRY ===
			"""
		),
	)

	assert "issue_id=issue_override; file=src/module.py; lines=8; lens=CORRECTNESS & LOGIC; severity=low; status=PERSISTING; prior_decision=none; persist_count=1" in prompt
	assert "issue_id=issue_override; file=src/module.py; lines=8; lens=CORRECTNESS & LOGIC; severity=low; status=PERSISTING; prior_decision=won't-fix; persist_count=1" not in prompt


def test_review_blocked_judge_warns_when_prior_round_decision_parse_fails() -> None:
	prompt, result = _render_rb_judge_prompt(
		create_ledger=False,
		ledger_as_directory=True,
		include_result=True,
	)

	assert "\n=== BEGIN PRIOR ROUND DECISIONS ===\n" not in prompt
	assert "::warning::review_blocked_judge prior_round_decisions_skipped=1 reason=ledger_parse_failed" in result.stderr


def test_review_blocked_judge_hides_review_state_when_rubric_disabled() -> None:
	script_text = RB_JUDGE_SCRIPT.read_text(encoding="utf-8")
	state_block = _extract_shell_block(
		script_text,
		'RB_ACTION="$(printf',
		'# -----------------------------------------------------------\n# Merged-PR action guard',
	)
	comment_block = _extract_shell_block(
		script_text,
		'JUDGE_COMMENT="## Review-Blocked Judge Decision"',
		'\n\npost_review_blocked_assessment',
	)

	with tempfile.TemporaryDirectory(prefix="rb-judge-review-state-") as td:
		tmp = Path(td)
		github_output = tmp / "github_output.txt"
		env = os.environ.copy()
		env.update({
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_OUTPUT": str(github_output),
			"JUDGE_JSON": json.dumps({
				"action": "fix",
				"justification": "needs edits",
				"remaining_issues_summary": "still failing",
				"review_state": "REQUEST_CHANGES",
			}),
			"REVIEW_APPROVAL_RUBRIC_ENABLED": "false",
			"REVIEW_BREAK_GLASS_ENABLED": "false",
			"REVIEW_BREAK_GLASS": "false",
			"RUNTIME_DIR": str(tmp),
			"RETRY_COUNT": "0",
			"MAX_REVIEW_BLOCKED_RETRIES": "3",
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				+ "\n\n".join([
					_extract_shell_function(script_text, "flag_enabled"),
					_extract_shell_function(script_text, "normalize_review_state"),
					_extract_shell_function(script_text, "resolve_review_state_for_post"),
				])
				+ "\n\n"
				+ state_block
				+ "\n"
				+ comment_block,
			],
			env=env,
			capture_output=True,
			text=True,
			check=True,
		)

		comment_body = (tmp / "rb_judge_comment.md").read_text(encoding="utf-8")
		output_text = github_output.read_text(encoding="utf-8") if github_output.exists() else ""

	assert "Logical review state:" not in result.stdout
	assert "judge_review_state_logical=" not in output_text
	assert "judge_review_state_outbound=" not in output_text
	assert "**Logical review state:**" not in comment_body
	assert "**Posted review state:**" not in comment_body


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
		assert review_payloads[0]["commit_id"] == "deadbeef"
		assert review_payloads[1]["body"] == approve_body
		assert review_payloads[1]["commit_id"] == "deadbeef"


def main() -> int:
	test_funcs = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
