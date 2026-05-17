#!/usr/bin/env python3
"""Pytest coverage for the LLM-backed reject verifier path."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PARSER_SCRIPT = REPO_ROOT / "scripts" / "review_parse_consolidator.sh"
VERIFIER_SCRIPT = REPO_ROOT / "scripts" / "review_reject_verify.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"


def _seed_repo(workspace_dir: Path) -> Path:
	workspace_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src").mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src" / "module.py").write_text(
		"\n".join([
			"def sample(x):",
			"    if x == None:",
			"        return",
			"    return x",
			"line5",
			"line6",
			"line7",
			"line8",
			"line9",
			"line10",
		])
		+ "\n",
		encoding="utf-8",
	)

	subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace_dir, check=True)
	for key, value in (
		("user.email", "test@local"),
		("user.name", "test"),
		("commit.gpgsign", "false"),
	):
		subprocess.run(["git", "config", key, value], cwd=workspace_dir, check=True)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace_dir, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
		cwd=workspace_dir,
		check=True,
	)

	shutil.copy2(FIXTURES / "reviewer_bundle.txt", runtime_dir / "reviewer_bundle.txt")
	(runtime_dir / "pr_diff.patch").write_text("", encoding="utf-8")
	(runtime_dir / "linked_issue_context.txt").write_text("", encoding="utf-8")
	return runtime_dir


def _issue_block(
	*,
	issue_id: str,
	classification: str = "non-actionable",
	line_spec: str = "2-3",
	rejection_kind: str,
	typed_header: str,
	typed_body: str,
	notes: str = "Conservatively rejected with evidence.",
) -> str:
	lines = [
		f"=== ISSUE {issue_id} ===",
		"FILE: src/module.py",
		f"LINES: {line_spec}",
		"LENS: CORRECTNESS & LOGIC",
		"SEVERITY: med",
		"FLAGGED_BY: reviewer_alpha",
		f"CLASSIFICATION: {classification}",
		f"REJECTION_KIND: {rejection_kind}",
		f"{typed_header}:",
	]
	for raw_line in typed_body.splitlines():
		lines.append(f"  {raw_line}")
	lines.extend([
		"EVIDENCE:",
		'  reviewer_alpha> "saw bug"',
		"CURRENT_CODE:",
		"  if x == None:",
		"    return",
		"SUGGESTED_APPROACH:",
		"  Keep the change minimal.",
		"NOTES:",
		f"  {notes}",
		f"=== END ISSUE {issue_id} ===",
		"",
	])
	return "\n".join(lines)


def _run_parser(workspace_dir: Path, runtime_dir: Path, *, raw_text: str) -> subprocess.CompletedProcess[str]:
	(runtime_dir / "consolidator_raw.txt").write_text(raw_text, encoding="utf-8")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["REVIEW_PARSER_FAILOPEN"] = "1"
	env["CONSOLIDATOR_REJECT_SCHEMA_ENABLED"] = "true"
	return subprocess.run(
		["bash", str(PARSER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)


def _install_mock_timeout(mock_bin_dir: Path) -> Path:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	call_file = mock_bin_dir / "timeout_calls.txt"
	(mock_bin_dir / "timeout").write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"while [ \"$#\" -gt 0 ]; do\n"
		"\tcase \"$1\" in\n"
		"\t\t--signal=*|--kill-after=*) shift ;;\n"
		"\t\t--) shift; break ;;\n"
		"\t\t*) break ;;\n"
		"\tesac\n"
		"done\n"
		"duration=\"${1:-}\"\n"
		"shift || true\n"
		"printf '%s\\n' \"$duration\" >> \"${MOCK_TIMEOUT_CALLS_FILE}\"\n"
		"if [ \"${MOCK_TIMEOUT_MODE:-pass}\" = \"timeout\" ]; then\n"
		"\texit 124\n"
		"fi\n"
		"exec \"$@\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "timeout").chmod(0o755)
	return call_file


def _install_mock_codex(mock_bin_dir: Path, responses: list[dict[str, object]]) -> tuple[Path, Path]:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	responses_dir = mock_bin_dir / "codex_responses"
	calls_dir = mock_bin_dir / "codex_calls"
	responses_dir.mkdir(parents=True, exist_ok=True)
	calls_dir.mkdir(parents=True, exist_ok=True)
	for idx, response in enumerate(responses, start=1):
		(responses_dir / f"call_{idx}.stdout").write_text(str(response.get("stdout", "")), encoding="utf-8")
		(responses_dir / f"call_{idx}.stderr").write_text(str(response.get("stderr", "")), encoding="utf-8")
		(responses_dir / f"call_{idx}.exitcode").write_text(str(response.get("exit_code", 0)), encoding="utf-8")
	(mock_bin_dir / "codex").write_text(
		"#!/usr/bin/env python3\n"
		"from __future__ import annotations\n"
		"import json\n"
		"import os\n"
		"import sys\n"
		"from pathlib import Path\n\n"
		"calls_dir = Path(os.environ['MOCK_CODEX_CALLS_DIR'])\n"
		"responses_dir = Path(os.environ['MOCK_CODEX_RESPONSES_DIR'])\n"
		"counter_file = calls_dir / 'counter.txt'\n"
		"count = int(counter_file.read_text(encoding='utf-8').strip() or '0') + 1 if counter_file.exists() else 1\n"
		"counter_file.write_text(str(count), encoding='utf-8')\n"
		"stdin_text = sys.stdin.read()\n"
		"(calls_dir / f'call_{count}.stdin').write_text(stdin_text, encoding='utf-8')\n"
		"(calls_dir / f'call_{count}.args').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
		"(calls_dir / f'call_{count}.codex_home').write_text(os.environ.get('CODEX_HOME', ''), encoding='utf-8')\n"
		"config_parts = []\n"
		"for rel in ('config.toml', '.codex/config.toml'):\n"
		"\tpath = Path(os.environ.get('CODEX_HOME', '')) / rel\n"
		"\tif path.is_file():\n"
		"\t\tconfig_parts.append(f'=== {rel} ===\\n' + path.read_text(encoding='utf-8'))\n"
		"(calls_dir / f'call_{count}.config').write_text('\\n'.join(config_parts), encoding='utf-8')\n"
		"stdout_path = responses_dir / f'call_{count}.stdout'\n"
		"stderr_path = responses_dir / f'call_{count}.stderr'\n"
		"exit_path = responses_dir / f'call_{count}.exitcode'\n"
		"if stdout_path.exists():\n"
		"\tsys.stdout.write(stdout_path.read_text(encoding='utf-8'))\n"
		"if stderr_path.exists():\n"
		"\tsys.stderr.write(stderr_path.read_text(encoding='utf-8'))\n"
		"exit_code = int(exit_path.read_text(encoding='utf-8').strip() or '0') if exit_path.exists() else 0\n"
		"raise SystemExit(exit_code)\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "codex").chmod(0o755)
	return calls_dir, responses_dir


def _run_verifier(
	workspace_dir: Path,
	runtime_dir: Path,
	*,
	verifier_enabled: str,
	mock_bin_dir: Path | None = None,
	verifier_reasoning: str = "low",
	verifier_batch_max: str = "8",
	support_prompts_dir: Path | None = None,
	timeout_mode: str = "pass",
	extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
	home_dir = workspace_dir / "home"
	(home_dir / ".codex").mkdir(parents=True, exist_ok=True)
	(home_dir / ".codex" / "config.toml").write_text('model_reasoning_effort = "low"\n', encoding="utf-8")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["HOME"] = str(home_dir)
	if mock_bin_dir is not None:
		env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"
		env["MOCK_TIMEOUT_CALLS_FILE"] = str(mock_bin_dir / "timeout_calls.txt")
		env["MOCK_TIMEOUT_MODE"] = timeout_mode
		env["MOCK_CODEX_CALLS_DIR"] = str(mock_bin_dir / "codex_calls")
		env["MOCK_CODEX_RESPONSES_DIR"] = str(mock_bin_dir / "codex_responses")
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
	env["PR_DIFF_FILE"] = str(runtime_dir / "pr_diff.patch")
	env["LINKED_ISSUE_CONTEXT_FILE"] = str(runtime_dir / "linked_issue_context.txt")
	env["PR_NUMBER"] = "4242"
	env["AUTOFIX_ITERATION"] = "1"
	env["CONSOLIDATOR_REJECT_SCHEMA_ENABLED"] = "true"
	env["CONSOLIDATOR_REJECT_VERIFIER_ENABLED"] = verifier_enabled
	env["CONSOLIDATOR_REJECT_VERIFIER_REASONING"] = verifier_reasoning
	env["CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX"] = verifier_batch_max
	if support_prompts_dir is not None:
		env["SUPPORT_PROMPTS_DIR"] = str(support_prompts_dir)
	if extra_env is not None:
		env.update(extra_env)
	return subprocess.run(
		["bash", str(VERIFIER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)


def _artifact_path(workspace_dir: Path) -> Path:
	return workspace_dir / ".ai" / "review_runtime" / "pr-4242" / "round-1" / "verified_rejections.json"


def _load_artifact(workspace_dir: Path) -> dict[str, object]:
	return json.loads(_artifact_path(workspace_dir).read_text(encoding="utf-8"))


def _extract_issue_block(text: str, issue_id: str) -> str:
	start = f"=== ISSUE {issue_id} ==="
	end = f"=== END ISSUE {issue_id} ==="
	start_idx = text.index(start)
	end_idx = text.index(end, start_idx)
	return text[start_idx : end_idx + len(end)]


def _write_pr_diff(runtime_dir: Path, workspace_dir: Path, new_lines: list[str]) -> None:
	(workspace_dir / "src" / "module.py").write_text("\n".join(new_lines) + "\n", encoding="utf-8")
	diff = subprocess.run(
		["git", "diff", "--", "src/module.py"],
		cwd=workspace_dir,
		check=True,
		capture_output=True,
		text=True,
	).stdout
	(runtime_dir / "pr_diff.patch").write_text(diff, encoding="utf-8")


def _codex_call_count(calls_dir: Path) -> int:
	counter = calls_dir / "counter.txt"
	if not counter.exists():
		return 0
	return int(counter.read_text(encoding="utf-8").strip() or "0")


def _read_prompt_payload(calls_dir: Path, call_number: int) -> dict[str, object]:
	stdin_text = (calls_dir / f"call_{call_number}.stdin").read_text(encoding="utf-8")
	_, payload_text = stdin_text.split("INPUT_PAYLOAD\n", 1)
	return json.loads(payload_text)


def test_llm_verifier_flag_off_skips_codex_and_preserves_classification() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		calls_dir, _ = _install_mock_codex(mock_bin, responses=[])
		_install_mock_timeout(mock_bin)
		raw_text = _issue_block(
			issue_id="001",
			rejection_kind="reviewer-wrong",
			typed_header="EVIDENCE_RUNTIME_PATH",
			typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
		)
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, verifier_enabled="false", mock_bin_dir=mock_bin, support_prompts_dir=REPO_ROOT / "prompts")
		assert verify_result.returncode == 0, verify_result.stderr
		assert _codex_call_count(calls_dir) == 0
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		assert "REVERSAL_REASON:" not in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "inconclusive"
		assert artifact["results"][0]["reason"] == "LLM reject verifier is disabled by CONSOLIDATOR_REJECT_VERIFIER_ENABLED=false."


def test_llm_verifier_support_keeps_non_actionable_and_uses_fixed_model() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		calls_dir, _ = _install_mock_codex(
			mock_bin,
			responses=[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "001",
									"rejection_kind": "reviewer-wrong",
									"verdict": "support",
									"reason": "The supplied runtime-path evidence supports keeping this rejection non-actionable.",
								}
							]
						}
					)
				}
			],
		)
		timeout_calls = _install_mock_timeout(mock_bin)
		raw_text = _issue_block(
			issue_id="001",
			rejection_kind="reviewer-wrong",
			typed_header="EVIDENCE_RUNTIME_PATH",
			typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
		)
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(
			workspace,
			runtime,
			verifier_enabled="true",
			mock_bin_dir=mock_bin,
			verifier_reasoning="medium",
			support_prompts_dir=REPO_ROOT / "prompts",
		)
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		assert "REVERSAL_REASON:" not in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "support"
		args = json.loads((calls_dir / "call_1.args").read_text(encoding="utf-8"))
		assert "openai/gpt-5.4-mini" in args
		assert "model_reasoning_effort=medium" in args
		assert "web_search=disabled" in args
		config_snapshot = (calls_dir / "call_1.config").read_text(encoding="utf-8")
		assert 'web_search = "disabled"' in config_snapshot
		assert 'model_reasoning_effort = "medium"' in config_snapshot
		assert 'sandbox_mode = "read-only"' in config_snapshot
		payload = _read_prompt_payload(calls_dir, 1)
		assert payload["items"][0]["rejection_kind"] == "reviewer-wrong"
		assert payload["items"][0]["EVIDENCE_RUNTIME_PATH"].lstrip().startswith("location: process_request:187")
		assert timeout_calls.read_text(encoding="utf-8").strip() == "120"


def test_llm_verifier_does_not_support_reverses_classification() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		long_reason = (
			"The quoted spec passage does not support dismissing the reviewer finding because "
			+ ("x" * 180)
		)
		expected_reason = long_reason[:197] + "..."
		assert len(expected_reason) == 200
		_install_mock_codex(
			mock_bin,
			responses=[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "001",
									"rejection_kind": "spec-doesnt-support",
									"verdict": "does-not-support",
									"reason": long_reason,
								}
							]
						}
					)
				}
			],
		)
		_install_mock_timeout(mock_bin)
		raw_text = _issue_block(
			issue_id="001",
			rejection_kind="spec-doesnt-support",
			typed_header="EVIDENCE_SPEC_QUOTE",
			typed_body='source: docs/spec.md#validation\nquote: "Dry-run requests may reuse duplicate idempotency keys without failing validation."',
		)
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, verifier_enabled="true", mock_bin_dir=mock_bin, support_prompts_dir=REPO_ROOT / "prompts")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: must-fix" in block
		assert f"REVERSAL_REASON: {expected_reason}" in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "does-not-support"
		assert artifact["results"][0]["reason"] == expected_reason
		assert "CONSOLIDATOR_REJECT_REVERSED issue=001 kind=spec-doesnt-support" in verify_result.stdout


def test_llm_verifier_inconclusive_leaves_classification_unchanged() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		_install_mock_codex(
			mock_bin,
			responses=[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "001",
									"rejection_kind": "reviewer-wrong",
									"verdict": "inconclusive",
									"reason": "The supplied excerpt is too limited to reliably overturn the reviewer finding.",
								}
							]
						}
					)
				}
			],
		)
		_install_mock_timeout(mock_bin)
		raw_text = _issue_block(
			issue_id="001",
			rejection_kind="reviewer-wrong",
			typed_header="EVIDENCE_RUNTIME_PATH",
			typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
		)
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, verifier_enabled="true", mock_bin_dir=mock_bin, support_prompts_dir=REPO_ROOT / "prompts")
		assert verify_result.returncode == 0, verify_result.stderr
		block = _extract_issue_block((runtime / "review_issues.txt").read_text(encoding="utf-8"), "001")
		assert "CLASSIFICATION: non-actionable" in block
		assert "REVERSAL_REASON:" not in block
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "inconclusive"
		assert "CONSOLIDATOR_REJECT_VERIFIER_INCONCLUSIVE issue=001 kind=reviewer-wrong" in verify_result.stdout


@pytest.mark.parametrize(
	("failure_mode", "responses", "timeout_mode", "expected_reason"),
	[
		(
			"malformed-json",
			[{"stdout": "not json\n"}],
			"pass",
			"LLM reject verifier returned malformed JSON.",
		),
		(
			"missing-row",
			[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "001",
									"rejection_kind": "reviewer-wrong",
									"verdict": "support",
									"reason": "Only one row returned.",
								}
							]
						}
					)
				}
			],
			"pass",
			"LLM reject verifier returned incomplete or malformed results.",
		),
		(
			"multi-sentence-reason",
			[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "001",
									"rejection_kind": "reviewer-wrong",
									"verdict": "support",
									"reason": "First sentence. Second sentence.",
								},
								{
									"issue_id": "002",
									"rejection_kind": "spec-doesnt-support",
									"verdict": "support",
									"reason": "Single sentence.",
								},
							]
						}
					)
				}
			],
			"pass",
			"LLM reject verifier returned incomplete or malformed results.",
		),
		(
			"timeout",
			[{"stdout": json.dumps({"results": []})}],
			"timeout",
			"LLM reject verifier timed out after 120s.",
		),
	],
)
def test_llm_verifier_fail_open_modes_preserve_classification(
	failure_mode: str,
	responses: list[dict[str, object]],
	timeout_mode: str,
	expected_reason: str,
) -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / f"mock_bin_{failure_mode}"
		_install_mock_codex(mock_bin, responses=responses)
		_install_mock_timeout(mock_bin)
		raw_text = "".join([
			_issue_block(
				issue_id="001",
				rejection_kind="reviewer-wrong",
				typed_header="EVIDENCE_RUNTIME_PATH",
				typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
			),
			_issue_block(
				issue_id="002",
				rejection_kind="spec-doesnt-support",
				typed_header="EVIDENCE_SPEC_QUOTE",
				typed_body='source: docs/spec.md#validation\nquote: "Dry-run requests may reuse duplicate idempotency keys without failing validation."',
			),
		])
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(
			workspace,
			runtime,
			verifier_enabled="true",
			mock_bin_dir=mock_bin,
			support_prompts_dir=REPO_ROOT / "prompts",
			timeout_mode=timeout_mode,
		)
		assert verify_result.returncode == 0, verify_result.stderr
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		assert issues.count("CLASSIFICATION: non-actionable") >= 2
		assert "REVERSAL_REASON:" not in issues
		artifact = _load_artifact(workspace)
		assert [row["verdict"] for row in artifact["results"]] == ["inconclusive", "inconclusive"]
		assert all(row["reason"] == expected_reason for row in artifact["results"])
		assert "CONSOLIDATOR_REJECT_VERIFIER_FAIL" in verify_result.stdout


def test_llm_verifier_splits_batches_at_configured_max() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		calls_dir, _ = _install_mock_codex(
			mock_bin,
			responses=[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": f"{idx:03d}",
									"rejection_kind": "reviewer-wrong",
									"verdict": "support",
									"reason": "The supplied runtime-path evidence supports keeping this rejection non-actionable.",
								}
								for idx in range(1, 9)
							]
						}
					)
				},
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "009",
									"rejection_kind": "reviewer-wrong",
									"verdict": "support",
									"reason": "The supplied runtime-path evidence supports keeping this rejection non-actionable.",
								}
							]
						}
					)
				},
			],
		)
		_install_mock_timeout(mock_bin)
		raw_text = "".join([
			_issue_block(
				issue_id=f"{idx:03d}",
				rejection_kind="reviewer-wrong",
				typed_header="EVIDENCE_RUNTIME_PATH",
				typed_body=f"location: process_request:{180 + idx}\nrationale: Guard returns before the reviewer-described call path.",
			)
			for idx in range(1, 10)
		])
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(
			workspace,
			runtime,
			verifier_enabled="true",
			mock_bin_dir=mock_bin,
			verifier_batch_max="8",
			support_prompts_dir=REPO_ROOT / "prompts",
		)
		assert verify_result.returncode == 0, verify_result.stderr
		assert _codex_call_count(calls_dir) == 2
		assert len(_read_prompt_payload(calls_dir, 1)["items"]) == 8
		assert len(_read_prompt_payload(calls_dir, 2)["items"]) == 1
		assert _read_prompt_payload(calls_dir, 2)["items"][0]["issue_id"] == "009"
		artifact = _load_artifact(workspace)
		assert len(artifact["results"]) == 9
		assert all(row["verdict"] == "support" for row in artifact["results"])


def test_llm_verifier_missing_prompt_reason_is_capped() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		missing_prompt_dir = workspace.joinpath(*(["long-path-segment"] * 16))
		raw_reason = f"LLM reject verifier prompt {missing_prompt_dir / 'consolidator-reject-verifier.txt'} is unavailable."
		expected_reason = raw_reason[:197] + "..."
		assert len(raw_reason) > 200
		assert len(expected_reason) == 200
		raw_text = _issue_block(
			issue_id="001",
			rejection_kind="reviewer-wrong",
			typed_header="EVIDENCE_RUNTIME_PATH",
			typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
		)
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(
			workspace,
			runtime,
			verifier_enabled="true",
			support_prompts_dir=missing_prompt_dir,
		)
		assert verify_result.returncode == 0, verify_result.stderr
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["verdict"] == "inconclusive"
		assert artifact["results"][0]["reason"] == expected_reason
		assert len(artifact["results"][0]["reason"]) == 200
		assert "CONSOLIDATOR_REJECT_VERIFIER_FAIL reason=missing_prompt first_issue=001 batch_size=1" in verify_result.stdout


def test_llm_verifier_tolerates_config_write_failures_and_keeps_script_only_checks() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		calls_dir, _ = _install_mock_codex(
			mock_bin,
			responses=[
				{
					"stdout": json.dumps(
						{
							"results": [
								{
									"issue_id": "001",
									"rejection_kind": "reviewer-wrong",
									"verdict": "support",
									"reason": "The supplied runtime-path evidence supports keeping this rejection non-actionable.",
								}
							]
						}
					)
				}
			],
		)
		_install_mock_timeout(mock_bin)
		source_codex_home = workspace / "source_codex_home"
		source_codex_home.mkdir(parents=True, exist_ok=True)
		(source_codex_home / "config.toml").mkdir()
		runner_temp = workspace / "runner_temp"
		runner_temp.mkdir(parents=True, exist_ok=True)
		_write_pr_diff(
			runtime,
			workspace,
			[
				"def sample(x):",
				"    if x is None:",
				"        return None",
				"    return x",
				"line5",
				"line6",
				"line7",
				"line8",
				"line9",
				"line10",
			],
		)
		raw_text = "".join([
			_issue_block(
				issue_id="001",
				rejection_kind="reviewer-wrong",
				typed_header="EVIDENCE_RUNTIME_PATH",
				typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
			),
			_issue_block(
				issue_id="002",
				rejection_kind="already-fixed",
				typed_header="EVIDENCE_DIFF_HUNK",
				typed_body="file: src/module.py\nlines: 2-3\nexcerpt: if x is None:",
			),
		])
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(
			workspace,
			runtime,
			verifier_enabled="true",
			mock_bin_dir=mock_bin,
			support_prompts_dir=REPO_ROOT / "prompts",
			extra_env={
				"CODEX_HOME": str(source_codex_home),
				"RUNNER_TEMP": str(runner_temp),
			},
		)
		assert verify_result.returncode == 0, verify_result.stderr
		assert _codex_call_count(calls_dir) == 1
		args = json.loads((calls_dir / "call_1.args").read_text(encoding="utf-8"))
		assert "model_reasoning_effort=low" in args
		assert "web_search=disabled" in args
		assert "CONSOLIDATOR_REJECT_VERIFIER_FAIL" not in verify_result.stdout
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=002 kind=already-fixed verdict=support" in verify_result.stdout
		artifact = _load_artifact(workspace)
		assert [row["verdict"] for row in artifact["results"]] == ["support", "support"]
		temp_root = runner_temp / "codex_home_reject_verify"
		if temp_root.exists():
			assert list(temp_root.iterdir()) == []


def test_llm_verifier_internal_errors_fail_open_and_keep_script_only_checks() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		calls_dir, _ = _install_mock_codex(mock_bin, responses=[])
		_install_mock_timeout(mock_bin)
		runner_temp_file = workspace / "runner_temp_file"
		runner_temp_file.write_text("not a directory\n", encoding="utf-8")
		_write_pr_diff(
			runtime,
			workspace,
			[
				"def sample(x):",
				"    if x is None:",
				"        return None",
				"    return x",
				"line5",
				"line6",
				"line7",
				"line8",
				"line9",
				"line10",
			],
		)
		raw_text = "".join([
			_issue_block(
				issue_id="001",
				rejection_kind="reviewer-wrong",
				typed_header="EVIDENCE_RUNTIME_PATH",
				typed_body="location: process_request:187\nrationale: Guard returns before the reviewer-described call path.",
			),
			_issue_block(
				issue_id="002",
				rejection_kind="already-fixed",
				typed_header="EVIDENCE_DIFF_HUNK",
				typed_body="file: src/module.py\nlines: 2-3\nexcerpt: if x is None:",
			),
		])
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(
			workspace,
			runtime,
			verifier_enabled="true",
			mock_bin_dir=mock_bin,
			support_prompts_dir=REPO_ROOT / "prompts",
			extra_env={"RUNNER_TEMP": str(runner_temp_file)},
		)
		assert verify_result.returncode == 0, verify_result.stderr
		assert _codex_call_count(calls_dir) == 0
		assert "CONSOLIDATOR_REJECT_VERIFIER_FAIL reason=unexpected_exception first_issue=001 batch_size=1" in verify_result.stdout
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=002 kind=already-fixed verdict=support" in verify_result.stdout
		artifact = _load_artifact(workspace)
		assert [row["verdict"] for row in artifact["results"]] == ["inconclusive", "support"]
		assert artifact["results"][0]["reason"] == "LLM reject verifier hit an unexpected internal error."


def test_script_only_rejections_bypass_llm_when_feature_flag_is_on() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_repo(workspace)
		mock_bin = workspace / "mock_bin"
		calls_dir, _ = _install_mock_codex(mock_bin, responses=[])
		_install_mock_timeout(mock_bin)
		_write_pr_diff(
			runtime,
			workspace,
			[
				"def sample(x):",
				"    if x is None:",
				"        return None",
				"    return x",
				"line5",
				"line6",
				"line7",
				"line8",
				"line9",
				"line10",
			],
		)
		raw_text = _issue_block(
			issue_id="001",
			rejection_kind="already-fixed",
			typed_header="EVIDENCE_DIFF_HUNK",
			typed_body="file: src/module.py\nlines: 2-3\nexcerpt: if x is None:",
		)
		parse_result = _run_parser(workspace, runtime, raw_text=raw_text)
		assert parse_result.returncode == 0, parse_result.stderr
		verify_result = _run_verifier(workspace, runtime, verifier_enabled="true", mock_bin_dir=mock_bin, support_prompts_dir=REPO_ROOT / "prompts")
		assert verify_result.returncode == 0, verify_result.stderr
		assert _codex_call_count(calls_dir) == 0
		artifact = _load_artifact(workspace)
		assert artifact["results"][0]["rejection_kind"] == "already-fixed"
		assert artifact["results"][0]["verdict"] == "support"
