#!/usr/bin/env python3
"""Integration tests for the review artifact stage chain."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "review_pipeline"
FLOOR_SCRIPT = REPO_ROOT / "scripts" / "review_floor_rules.sh"
CONSOLIDATE_SCRIPT = REPO_ROOT / "scripts" / "review_consolidate.sh"
PARSER_SCRIPT = REPO_ROOT / "scripts" / "review_parse_consolidator.sh"
LEDGER_SCRIPT = REPO_ROOT / "scripts" / "review_issue_ledger.sh"
JUDGE_INTERIM_SCRIPT = REPO_ROOT / "scripts" / "review_run_judge_interim.sh"
REJECT_VERIFY_SCRIPT = REPO_ROOT / "scripts" / "review_reject_verify.sh"
STICKY_SCRIPT = REPO_ROOT / "scripts" / "review_annotate_sticky.sh"
SYNTH_SCRIPT = REPO_ROOT / "scripts" / "review_synthesise_smoke.sh"


def _seed_workspace_repo(workspace_dir: Path) -> Path:
	workspace_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir = workspace_dir / "runtime"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src").mkdir(parents=True, exist_ok=True)
	(workspace_dir / "src" / "module.py").write_text(
		"line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n",
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
	return runtime_dir


def _install_mock_codex(mock_bin_dir: Path, *, consolidator_fixture: str | None) -> None:
	mock_bin_dir.mkdir(parents=True, exist_ok=True)
	output_file = mock_bin_dir / "codex_output.txt"
	if consolidator_fixture is None:
		output_file.write_text("", encoding="utf-8")
	else:
		shutil.copy2(FIXTURES / consolidator_fixture, output_file)

	codex_script = mock_bin_dir / "codex"
	# Scan every arg for `exec` rather than checking $1, since the
	# canonical Codex CLI v0.114.0+ invocation places `--ask-for-approval`
	# (a top-level flag) before the `exec` subcommand:
	#     codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --model X --sandbox Y
	# Anchoring on $1 would only match the legacy form (broken on
	# v0.114.0+) and silently mis-mock the production layout.
	codex_script.write_text(
		"#!/usr/bin/env bash\n"
		"set -euo pipefail\n\n"
		"case \" $* \" in\n"
		"\t*\" exec \"*) ;;\n"
		"\t*) echo \"mock-codex supports only exec\" >&2; exit 2 ;;\n"
		"esac\n"
		"if [ -n \"${MOCK_CODEX_OUTPUT_FILE:-}\" ] && [ -f \"${MOCK_CODEX_OUTPUT_FILE}\" ]; then\n"
		"\tcat \"${MOCK_CODEX_OUTPUT_FILE}\"\n"
		"fi\n",
		encoding="utf-8",
	)
	codex_script.chmod(0o755)


def _install_mock_codex_responses(mock_bin_dir: Path, *, responses: list[dict[str, object]]) -> Path:
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
		"(calls_dir / f'call_{count}.args').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
		"(calls_dir / f'call_{count}.stdin').write_text(sys.stdin.read(), encoding='utf-8')\n"
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
	return calls_dir


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
		"exec \"$@\"\n",
		encoding="utf-8",
	)
	(mock_bin_dir / "timeout").chmod(0o755)
	return call_file


def _load_test_module(module_name: str, relative_path: str) -> object:
	previous = sys.dont_write_bytecode
	sys.dont_write_bytecode = True
	try:
		module_path = REPO_ROOT / relative_path
		assert module_path.is_file(), f"Missing helper test module: {module_path}"
		spec = importlib.util.spec_from_file_location(module_name, module_path)
		assert spec is not None and spec.loader is not None, f"Unable to load helper test module: {module_path}"
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		sys.dont_write_bytecode = previous


def _commit_autofix_change(workspace_dir: Path) -> str:
	module = workspace_dir / "src" / "module.py"
	module.write_text(
		"def sample(cache, key):\n"
		"\tvalue = cache[key]\n"
		"\tif key in cache:\n"
		"\t\treturn value\n"
		"\treturn None\n",
		encoding="utf-8",
	)
	subprocess.run(["git", "add", "src/module.py"], cwd=workspace_dir, check=True)
	subprocess.run(
		["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "[ai-autofix] add sticky cache lookup"],
		cwd=workspace_dir,
		check=True,
	)
	return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace_dir, text=True).strip()


def _base_review_runtime_env(workspace_dir: Path, runtime_dir: Path, mock_bin_dir: Path) -> dict[str, str]:
	home_dir = workspace_dir / "home"
	(home_dir / ".codex").mkdir(parents=True, exist_ok=True)
	(home_dir / ".codex" / "config.toml").write_text('model_reasoning_effort = "low"\n', encoding="utf-8")
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["HOME"] = str(home_dir)
	env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"
	env["SUPPORT_ROOT_DIR"] = str(REPO_ROOT)
	env["SUPPORT_SCRIPTS_DIR"] = str(REPO_ROOT / "scripts")
	env["SUPPORT_PROMPTS_DIR"] = str(REPO_ROOT / "prompts")
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["PR_NUMBER"] = "4242"
	env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
	env["PR_DIFF_FILE"] = str(runtime_dir / "pr_diff.patch")
	env["LINKED_ISSUE_CONTEXT_FILE"] = str(runtime_dir / "linked_issue_context.txt")
	env["MOCK_CODEX_CALLS_DIR"] = str(mock_bin_dir / "codex_calls")
	env["MOCK_CODEX_RESPONSES_DIR"] = str(mock_bin_dir / "codex_responses")
	env["MOCK_TIMEOUT_CALLS_FILE"] = str(mock_bin_dir / "timeout_calls.txt")
	return env


def _parsed_issue_block(
	*,
	issue_id: str,
	line_spec: str,
	rejection_kind: str,
	typed_header: str,
	typed_body: str,
	evidence_text: str,
	notes: str,
) -> str:
	lines = [
		f"=== ISSUE {issue_id} ===",
		"FILE: src/module.py",
		f"LINES: {line_spec}",
		"LENS: CORRECTNESS & LOGIC",
		"SEVERITY: med",
		"FLAGGED_BY: reviewer_alpha",
		"CLASSIFICATION: non-actionable",
		f"REJECTION_KIND: {rejection_kind}",
		f"{typed_header}:",
	]
	for raw_line in typed_body.splitlines():
		lines.append(f"  {raw_line}")
	lines.extend([
		"EVIDENCE:",
		f"  reviewer_alpha> {evidence_text}",
		"CURRENT_CODE:",
		"  value = cache[key]",
		"SUGGESTED_APPROACH:",
		"  Keep the change minimal.",
		"NOTES:",
		f"  {notes}",
		f"=== END ISSUE {issue_id} ===",
		"",
	])
	return "\n".join(lines)


def _extract_issue_block(text: str, issue_id: str) -> str:
	start = f"=== ISSUE {issue_id} ==="
	end = f"=== END ISSUE {issue_id} ==="
	try:
		start_idx = text.index(start)
		end_idx = text.index(end, start_idx)
	except ValueError as exc:
		raise AssertionError(f"Missing issue block markers for {issue_id}:\n{text}") from exc
	return text[start_idx:end_idx + len(end)]


def _codex_call_count(calls_dir: Path) -> int:
	counter = calls_dir / "counter.txt"
	if not counter.exists():
		return 0
	return int(counter.read_text(encoding="utf-8").strip() or "0")


def _run_stage_chain(
	workspace_dir: Path,
	runtime_dir: Path,
	*,
	mock_bin_dir: Path | None,
	consolidator_enabled: str,
) -> dict[str, subprocess.CompletedProcess[str]]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNTIME_DIR"] = str(runtime_dir)
	env["SUPPORT_PROMPTS_DIR"] = str(REPO_ROOT / "prompts")
	env["PR_NUMBER"] = "4242"
	env["AUTOFIX_ITERATION"] = "1"
	env["REVIEW_CONSOLIDATOR_ENABLED"] = consolidator_enabled
	env["REVIEW_PARSER_FAILOPEN"] = "1"
	env["REVIEW_ISSUES_FILE"] = str(runtime_dir / "review_issues.txt")
	env["PARSER_STATS_FILE"] = str(runtime_dir / "parser_stats.txt")
	env["LEDGER_STATUS_FILE"] = str(runtime_dir / "ledger_status.txt")
	env["FLOOR_TAGS_FILE"] = str(runtime_dir / "floor_tags.txt")
	env["CONSOLIDATOR_RAW_FILE"] = str(runtime_dir / "consolidator_raw.txt")
	env["REVIEW_LEDGER_ENABLED"] = "1"
	env["REVIEW_LEDGER_PATH"] = str(runtime_dir / "review_issue_ledger.txt")
	if mock_bin_dir is not None:
		env["MOCK_CODEX_OUTPUT_FILE"] = str(mock_bin_dir / "codex_output.txt")
		env["PATH"] = f"{mock_bin_dir}:{env.get('PATH', '')}"

	results: dict[str, subprocess.CompletedProcess[str]] = {}
	results["floor"] = subprocess.run(
		[
			"bash",
			str(FLOOR_SCRIPT),
			str(runtime_dir / "reviewer_bundle.txt"),
			str(runtime_dir / "floor_tags.txt"),
		],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	results["consolidate"] = subprocess.run(
		["bash", str(CONSOLIDATE_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	results["parse"] = subprocess.run(
		["bash", str(PARSER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	results["ledger"] = subprocess.run(
		["bash", str(LEDGER_SCRIPT)],
		cwd=workspace_dir,
		env=env,
		capture_output=True,
		text=True,
	)
	return results


def _load_kv_file(path: Path) -> dict[str, str]:
	pairs: dict[str, str] = {}
	for raw in path.read_text(encoding="utf-8").splitlines():
		if "=" not in raw:
			continue
		k, v = raw.split("=", 1)
		pairs[k] = v
	return pairs


def _parse_status_rows(path: Path) -> list[list[str]]:
	rows: list[list[str]] = []
	for raw in path.read_text(encoding="utf-8").splitlines():
		if not raw.strip():
			continue
		rows.append(raw.split("\t"))
	return rows


def _assert_artifacts_present(runtime_dir: Path) -> None:
	for name in ("floor_tags.txt", "review_issues.txt", "parser_stats.txt", "ledger_status.txt"):
		artifact = runtime_dir / name
		assert artifact.exists(), f"missing artifact: {artifact}"


def test_chain_happy_path_with_mocked_consolidator() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		mock_bin = workspace / "mock_bin"
		_install_mock_codex(mock_bin, consolidator_fixture="consolidator_well_formed.txt")

		results = _run_stage_chain(workspace, runtime, mock_bin_dir=mock_bin, consolidator_enabled="1")
		for stage, result in results.items():
			assert result.returncode == 0, f"{stage} failed: {result.stderr}"

		_assert_artifacts_present(runtime)
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		status_rows = _parse_status_rows(runtime / "ledger_status.txt")

		assert stats["parse_failed"] == "0"
		assert stats["parsed_blocks"] == "1"
		assert stats["passthrough_blocks"] == "2"
		assert stats["anchors_total"] == "4"
		assert stats["anchors_covered"] == "2"
		assert "=== ISSUE 001 ===" in issues
		assert "=== ISSUE PASSTHROUGH 002 ===" in issues
		assert len(status_rows) == 3
		assert all(row[1] == "NEW" for row in status_rows)
		assert any(row[4] == "CORRECTNESS & LOGIC" for row in status_rows)


def test_chain_fail_open_when_consolidator_disabled() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		results = _run_stage_chain(workspace, runtime, mock_bin_dir=None, consolidator_enabled="0")
		for stage, result in results.items():
			assert result.returncode == 0, f"{stage} failed: {result.stderr}"

		_assert_artifacts_present(runtime)
		stats = _load_kv_file(runtime / "parser_stats.txt")
		issues = (runtime / "review_issues.txt").read_text(encoding="utf-8")
		status_rows = _parse_status_rows(runtime / "ledger_status.txt")

		assert "disabled=1" in results["consolidate"].stderr
		assert stats["parse_failed"] == "1"
		assert stats["parse_error"] == "no_issue_markers"
		assert stats["parsed_blocks"] == "0"
		assert stats["passthrough_blocks"] == "4"
		assert stats["anchors_total"] == "4"
		assert stats["anchors_covered"] == "0"
		assert "=== ISSUE 001 ===" not in issues
		assert "=== ISSUE PASSTHROUGH 004 ===" in issues
		assert len(status_rows) == 4
		assert all(row[4] == "UNKNOWN_LENS" for row in status_rows)


def test_chain_fail_open_when_consolidator_returns_empty() -> None:
	with tempfile.TemporaryDirectory() as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		mock_bin = workspace / "mock_bin"
		_install_mock_codex(mock_bin, consolidator_fixture=None)

		results = _run_stage_chain(workspace, runtime, mock_bin_dir=mock_bin, consolidator_enabled="1")
		for stage, result in results.items():
			assert result.returncode == 0, f"{stage} failed: {result.stderr}"

		_assert_artifacts_present(runtime)
		stats = _load_kv_file(runtime / "parser_stats.txt")
		status_rows = _parse_status_rows(runtime / "ledger_status.txt")

		assert "failopen=1" in results["consolidate"].stderr
		assert (runtime / "consolidator_raw.txt").read_text(encoding="utf-8") == ""
		assert stats["parse_failed"] == "1"
		assert stats["parse_error"] == "no_issue_markers"
		assert stats["passthrough_blocks"] == "4"
		assert len(status_rows) == 4
		assert all(row[1] == "NEW" for row in status_rows)


def test_multiround_review_runtime_and_spot_fix_reissue_baseline_contract() -> None:
	label_propagation = _load_test_module(
		"test_review_rb_judge_label_propagation_integration",
		"tests/test_review_rb_judge_label_propagation.py",
	)
	assert callable(getattr(label_propagation, "_run_close_and_reissue", None)), (
		"Missing _run_close_and_reissue in tests/test_review_rb_judge_label_propagation.py"
	)
	reissue_baseline = _load_test_module(
		"test_review_rb_judge_reissue_baseline_integration",
		"tests/test_review_rb_judge_reissue_baseline.py",
	)
	assert callable(getattr(reissue_baseline, "_run_baseline_resolver", None)), (
		"Missing _run_baseline_resolver in tests/test_review_rb_judge_reissue_baseline.py"
	)

	with tempfile.TemporaryDirectory(prefix="review_runtime_rollout_") as td:
		workspace = Path(td)
		runtime = _seed_workspace_repo(workspace)
		head_sha = _commit_autofix_change(workspace)
		(runtime / "pr_diff.patch").write_text("", encoding="utf-8")
		(runtime / "linked_issue_context.txt").write_text(
			"Fix the sticky cache guard.\n\n- files_touched:\n  - src/module.py\n",
			encoding="utf-8",
		)

		mock_bin = workspace / "mock_bin"
		codex_calls = _install_mock_codex_responses(
			mock_bin,
			responses=[
				{
					"stdout": json.dumps(
						{
							"round": 1,
							"head_sha": head_sha,
							"remaining_issues": [
								{
									"id": "src/module.py:2:sticky-guard",
									"file": "src/module.py",
									"line_start": 2,
									"line_end": 3,
									"symptom": "Missing nil guard around sticky cache refresh.",
									"evidence_quote": "\tvalue = cache[key]\\n\tif key in cache:",
									"severity": "must-fix",
								}
							],
						}
					)
					+ "\n",
				},
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
							],
						}
					)
					+ "\n",
				},
				{
					"stdout": json.dumps(
						[
							{
								"path": "validation/tests/suggested_sticky_guard.sh",
								"content": "python3 -c \"raise SystemExit(1)\"",
								"expected_to_fail_until_fixed": True,
							}
						]
					)
					+ "\n",
				},
			],
		)
		timeout_calls = _install_mock_timeout(mock_bin)
		base_env = _base_review_runtime_env(workspace, runtime, mock_bin)

		judge_env = dict(base_env)
		judge_env.update({
			"ROUND_NUMBER": "0",
			"JUDGE_INTERIM_REASONING": "low",
			"JUDGE_INTERIM_TIMEOUT_S": "10",
			"TOOL_CALL_BUDGET_JUDGE": "20",
		})
		judge_result = subprocess.run(
			["bash", str(JUDGE_INTERIM_SCRIPT)],
			cwd=workspace,
			env=judge_env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		round_one_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-1"
		judge_artifact = round_one_dir / "judge_interim.json"
		judge_output = judge_result.stdout + judge_result.stderr
		assert judge_result.returncode == 0, judge_output
		assert judge_artifact.exists(), judge_output
		judge_payload = json.loads(judge_artifact.read_text(encoding="utf-8"))
		assert judge_payload["head_sha"] == head_sha
		assert judge_payload["remaining_issues"][0]["symptom"] == "Missing nil guard around sticky cache refresh."
		assert "JUDGE_INTERIM_PASS_OK round=1" in judge_output

		(runtime / "review_issues.txt").write_text(
			_parsed_issue_block(
				issue_id="001",
				line_spec="2-3",
				rejection_kind="reviewer-wrong",
				typed_header="EVIDENCE_RUNTIME_PATH",
				typed_body="location: sample:2\nrationale: Guard returns before the reviewer-described retry path.",
				evidence_text="Missing nil guard around sticky cache refresh.",
				notes="Prior round note explains why the reviewer-reported path is not actually reachable.",
			),
			encoding="utf-8",
		)
		verify_one_env = dict(base_env)
		verify_one_env.update({
			"AUTOFIX_ITERATION": "1",
			"CONSOLIDATOR_REJECT_SCHEMA_ENABLED": "true",
			"CONSOLIDATOR_REJECT_VERIFIER_ENABLED": "true",
			"CONSOLIDATOR_REJECT_VERIFIER_REASONING": "low",
			"CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX": "4",
		})
		verify_one_result = subprocess.run(
			["bash", str(REJECT_VERIFY_SCRIPT)],
			cwd=workspace,
			env=verify_one_env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		verify_one_output = verify_one_result.stdout + verify_one_result.stderr
		assert verify_one_result.returncode == 0, verify_one_output
		round_one_verified = round_one_dir / "verified_rejections.json"
		assert round_one_verified.exists(), verify_one_output
		round_one_verified_payload = json.loads(round_one_verified.read_text(encoding="utf-8"))
		assert round_one_verified_payload["round"] == 1
		assert round_one_verified_payload["results"][0]["verdict"] == "support"
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=001 kind=reviewer-wrong verdict=support" in verify_one_result.stdout
		assert "CLASSIFICATION: non-actionable" in _extract_issue_block(
			(runtime / "review_issues.txt").read_text(encoding="utf-8"),
			"001",
		)
		shutil.copy2(runtime / "review_issues.txt", round_one_dir / "consolidator_parsed.txt")

		synth_env = dict(base_env)
		synth_env.update({
			"ROUND_NUMBER": "0",
			"BEHAVIOURAL_SMOKE_LANG": "python",
		})
		synth_result = subprocess.run(
			["bash", str(SYNTH_SCRIPT)],
			cwd=workspace,
			env=synth_env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		synth_output = synth_result.stdout + synth_result.stderr
		manifest = round_one_dir / "synth" / "synth_round_1_manifest.json"
		assert synth_result.returncode == 0, synth_output
		assert manifest.exists(), synth_output
		manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
		assert manifest_payload["round"] == 1
		assert manifest_payload["head_sha"] == head_sha
		assert manifest_payload["language"] == "python"
		assert len(manifest_payload["files"]) == 1
		wrapper_path = workspace / manifest_payload["files"][0]["cache_relpath"]
		assert wrapper_path.exists(), wrapper_path
		wrapper_text = wrapper_path.read_text(encoding="utf-8")
		assert "BEHAVIOURAL_SMOKE_PRESENT_FAILED" in wrapper_text
		assert "BEHAVIOURAL_SMOKE_SYNTHESISED count=1 round=1 language=python" in synth_output

		(runtime / "reviewer_bundle.txt").write_text(
			"FILE_PATH: /tmp/review_alpha.txt\n"
			"CONTENT_START\n"
			"File: src/module.py\n"
			"Line or code reference: line 5\n"
			"Problem: Missing nil guard around sticky cache refresh.\n"
			"Why it fails at runtime: Retry path still indexes the cache before checking membership.\n"
			"ISSUE_CONFIDENCE: 5\n"
			"CONTENT_END\n",
			encoding="utf-8",
		)
		sticky_env = os.environ.copy()
		sticky_env.update({
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUNTIME_DIR": str(runtime),
			"PR_NUMBER": "4242",
			"AUTOFIX_ITERATION": "2",
			"STICKY_FINDINGS_ENABLED": "true",
		})
		sticky_result = subprocess.run(
			["bash", str(STICKY_SCRIPT)],
			cwd=workspace,
			env=sticky_env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		sticky_output = sticky_result.stdout + sticky_result.stderr
		round_two_dir = workspace / ".ai" / "review_runtime" / "pr-4242" / "round-2"
		sticky_json = round_two_dir / "sticky_findings.json"
		sticky_priors = runtime / "sticky_findings_priors.txt"
		assert sticky_result.returncode == 0, sticky_output
		assert sticky_json.exists(), sticky_output
		assert sticky_priors.exists(), sticky_output
		sticky_payload = json.loads(sticky_json.read_text(encoding="utf-8"))
		assert sticky_payload["current_round"] == 2
		assert sticky_payload["matches"][0]["prior_issue_id"] == "001"
		assert sticky_payload["matches"][0]["prior_rejection_kind"] == "reviewer-wrong"
		assert "STICKY_FINDING_DETECTED issue=001 file=src/module.py" in sticky_output
		assert "prior_issue_id: 001" in sticky_priors.read_text(encoding="utf-8")

		(runtime / "review_issues.txt").write_text(
			_parsed_issue_block(
				issue_id="001",
				line_spec="5",
				rejection_kind="already-rejected-with-evidence",
				typed_header="EVIDENCE_PRIOR_ROUND",
				typed_body="round: 1\nissue_id: 001\nrejection_kind: reviewer-wrong\nsticky: true",
				evidence_text="Missing nil guard around sticky cache refresh.",
				notes="Second round carries forward the prior supported rejection.",
			),
			encoding="utf-8",
		)
		verify_two_env = dict(base_env)
		verify_two_env.update({
			"AUTOFIX_ITERATION": "2",
			"CONSOLIDATOR_REJECT_SCHEMA_ENABLED": "true",
			"CONSOLIDATOR_REJECT_VERIFIER_ENABLED": "true",
			"STICKY_FINDINGS_ENABLED": "true",
		})
		verify_two_result = subprocess.run(
			["bash", str(REJECT_VERIFY_SCRIPT)],
			cwd=workspace,
			env=verify_two_env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		verify_two_output = verify_two_result.stdout + verify_two_result.stderr
		assert verify_two_result.returncode == 0, verify_two_output
		round_two_verified = round_two_dir / "verified_rejections.json"
		assert round_two_verified.exists(), verify_two_output
		round_two_verified_payload = json.loads(round_two_verified.read_text(encoding="utf-8"))
		assert round_two_verified_payload["round"] == 2
		assert round_two_verified_payload["results"][0]["verdict"] == "support"
		assert "CONSOLIDATOR_REJECT_VERIFIED issue=001 kind=already-rejected-with-evidence verdict=support" in verify_two_result.stdout
		assert "CLASSIFICATION: non-actionable" in _extract_issue_block(
			(runtime / "review_issues.txt").read_text(encoding="utf-8"),
			"001",
		)

		assert _codex_call_count(codex_calls) == 3
		assert timeout_calls.read_text(encoding="utf-8").splitlines() == ["10", "120", "120"]

		judge_state = label_propagation._run_close_and_reissue(
			["ai:orchestrator-managed"],
			judge_payload={
				"action": "close_and_reissue",
				"reissue_mode": "spot-fix",
				"justification": "Preserve the baseline diff and patch only the grounded remainder.",
				"remaining_issues": [
					{"file": "src/module.py", "line_start": 2, "line_end": 5, "symptom": "Sticky guard remains unresolved"},
					{"file": "README.md", "line_start": 1, "line_end": 2, "symptom": "Document the sticky rollout flag"},
				],
				"new_issue": {
					"title": "Reissue: preserve sticky baseline",
					"body": "Keep the prior implementation and patch only the remaining grounded gaps.",
				},
			},
			reissue_preserve_baseline_enabled="true",
			repo_files={
				"src/module.py": "print('sticky')\n",
				"README.md": "sticky docs\n",
			},
		)
		creates = judge_state.get("issue_create_args", [])
		assert len(creates) == 1, creates
		body = creates[0][creates[0].index("--body") + 1]
		branch_line = [line for line in body.splitlines() if line.startswith("- prior_pr_baseline_branch: ")]
		assert len(branch_line) == 1, body
		branch = branch_line[0].split(": ", 1)[1].strip()
		resolver_result, resolver_outputs, _ = reissue_baseline._run_baseline_resolver(
			body,
			feature_enabled="true",
			pr_head_oid=str(judge_state["_repo_head_before"]),
		)
		assert resolver_result.returncode == 0, resolver_result.stderr
		assert resolver_outputs == {
			"branch": branch,
			"sha": str(judge_state["_repo_head_before"]),
			"status": "accepted",
		}
		assert "REISSUE_BASELINE_PRESERVED" in judge_state["_stdout"]


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
