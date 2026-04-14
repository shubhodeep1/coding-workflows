#!/usr/bin/env python3
"""Regression tests for implement-phase post-Codex diagnose/fix-up recovery.

These tests intentionally execute extracted `run:` blocks from
`.github/workflows/implement.yml` with mocked `gh`/`codex` binaries so the
contract is validated against workflow behavior, not reimplemented logic.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import tempfile


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _step_block(step_name: str) -> list[str]:
	lines = _workflow_text().splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return lines[idx:end]
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _extract_run_script(step_name: str) -> str:
	block = _step_block(step_name)
	run_idx = -1
	run_indent = 0
	for i, line in enumerate(block):
		if line.strip() == "run: |":
			run_idx = i
			run_indent = len(line) - len(line.lstrip(" "))
			break
	if run_idx == -1:
		raise AssertionError(f"Step has no run block: {step_name}")

	script_lines: list[str] = []
	for line in block[run_idx + 1 :]:
		if line.strip() == "":
			script_lines.append("")
			continue
		indent = len(line) - len(line.lstrip(" "))
		if indent <= run_indent:
			break
		prefix = " " * (run_indent + 2)
		if line.startswith(prefix):
			script_lines.append(line[len(prefix) :])
		else:
			script_lines.append(line.lstrip())
	return "\n".join(script_lines).rstrip() + "\n"


def _render_github_expressions(script: str, overrides: dict[str, str] | None = None) -> str:
	values = {
		"github.repository": "owner/repo",
		"github.repository_owner": "owner",
		"github.run_id": "777",
		"github.server_url": "https://github.com",
		"github.event.repository.default_branch": "main",
		"github.sha": "deadbeef",
		"job.status": "failure",
	}
	if overrides:
		values.update(overrides)

	def _replace(match: re.Match[str]) -> str:
		key = " ".join(match.group(1).split())
		return values.get(key, "")

	rendered = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", _replace, script)
	assert "${{" not in rendered, "Found unrendered GitHub expression(s)"
	return rendered


def _run_shell_script(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	script_path = cwd / "__workflow_step_under_test.sh"
	script_path.write_text(script, encoding="utf-8")
	script_path.chmod(0o755)
	return subprocess.run(
		["bash", str(script_path)],
		cwd=str(cwd),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def _git(cmd: list[str], *, cwd: Path) -> None:
	subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bootstrap_git_repo(repo_dir: Path) -> None:
	repo_dir.mkdir(parents=True, exist_ok=True)
	_git(["git", "init"], cwd=repo_dir)
	_git(["git", "config", "user.name", "tests"], cwd=repo_dir)
	_git(["git", "config", "user.email", "tests@example.com"], cwd=repo_dir)
	(repo_dir / "README.md").write_text("test\n", encoding="utf-8")
	_git(["git", "add", "README.md"], cwd=repo_dir)
	_git(["git", "commit", "-m", "init"], cwd=repo_dir)


def _install_mock_gh(bin_dir: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag: str) -> str:
	for i, arg in enumerate(args):
		if arg == flag and i + 1 < len(args):
			return args[i + 1]
	return ""


def collect_values(flag: str) -> list[str]:
	out = []
	for i, arg in enumerate(args):
		if arg == flag and i + 1 < len(args):
			out.append(args[i + 1])
	return out


def first_non_flag(values: list[str]) -> str:
	i = 0
	while i < len(values):
		arg = values[i]
		if arg in ("--jq", "-f", "-H", "-X", "--method", "--input", "--repo", "--title", "--body", "--add-label", "--remove-label"):
			i += 2
			continue
		if arg.startswith("-"):
			i += 1
			continue
		return arg
	return ""


def parse_fields() -> dict[str, str]:
	parsed = {}
	for item in collect_values("-f"):
		if "=" in item:
			k, v = item.split("=", 1)
			parsed[k] = v
	return parsed


def issue_from_path(path: str) -> str:
	m = re.search(r"/issues/(\d+)", path)
	return m.group(1) if m else str(state.get("issue_number", 948))


state.setdefault("calls", []).append(args)

if not args:
	save()
	sys.exit(0)

if args[0] == "label" and len(args) >= 3 and args[1] == "create":
	state.setdefault("label_creates", []).append({
		"name": args[2],
		"repo": first_value("--repo"),
	})
	save()
	sys.exit(0)

if args[0] == "issue" and len(args) >= 3 and args[1] == "edit":
	issue_num = args[2]
	adds = collect_values("--add-label")
	removes = collect_values("--remove-label")
	state.setdefault("issue_edits", []).append({
		"issue": issue_num,
		"add": adds,
		"remove": removes,
		"repo": first_value("--repo"),
	})
	labels = list(state.get("issue_labels", []))
	for label in adds:
		if label not in labels:
			labels.append(label)
	for label in removes:
		labels = [x for x in labels if x != label]
	state["issue_labels"] = labels
	save()
	sys.exit(0)

if args[0] == "issue" and len(args) >= 3 and args[1] == "create":
	next_num = int(state.get("next_issue_number", 900))
	state["next_issue_number"] = next_num + 1
	repo = first_value("--repo")
	title = first_value("--title")
	body = first_value("--body")
	state.setdefault("created_issues", []).append({
		"number": next_num,
		"repo": repo,
		"title": title,
		"body": body,
		"args": args,
	})
	save()
	print(f"https://github.com/{repo}/issues/{next_num}")
	sys.exit(0)

if args[0] == "issue" and len(args) >= 3 and args[1] == "comment":
	state.setdefault("issue_comments", []).append({
		"issue": args[2],
		"repo": first_value("--repo"),
		"body": first_value("--body"),
	})
	save()
	sys.exit(0)

if args[0] == "api":
	path = first_non_flag(args[1:])
	jq = first_value("--jq")
	fields = parse_fields()

	if "/actions/runs/" in path and "/jobs" in path:
		failed_step = state.get("failed_step_name", "")
		payload = {"jobs": [{"steps": [{"name": failed_step, "conclusion": "failure"}]}]}
		if jq:
			if "conclusion == \"failure\"" in jq:
				print(failed_step)
			elif "conclusion == \"cancelled\"" in jq:
				print(state.get("cancelled_step_name", ""))
			else:
				print("")
		else:
			print(json.dumps(payload))
		save()
		sys.exit(0)

	if re.search(r"/issues/\d+/comments$", path) and "body" in fields:
		state.setdefault("api_comments", []).append({
			"issue": issue_from_path(path),
			"body": fields["body"],
			"path": path,
		})
		save()
		print("{}")
		sys.exit(0)

	if re.search(r"/issues/\d+$", path):
		labels = [{"name": x} for x in state.get("issue_labels", [])]
		body = state.get("issue_body", "")
		if jq:
			if "labels" in jq:
				print(json.dumps([x["name"] for x in labels]))
			elif ".body" in jq:
				print(body)
			else:
				print("")
		else:
			print(json.dumps({"labels": labels, "body": body}))
		save()
		sys.exit(0)

	# Asset fetch fallback path (unused in these tests, but keep stable behavior).
	if "/contents/" in path:
		print("not found", file=sys.stderr)
		save()
		sys.exit(1)

	save()
	print("{}")
	sys.exit(0)

print("Unsupported gh call: " + " ".join(args), file=sys.stderr)
save()
sys.exit(1)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)


def _install_mock_codex(bin_dir: Path) -> None:
	codex_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
stdin_text = sys.stdin.read()

calls_file = os.environ.get("MOCK_CODEX_CALLS_FILE")
if calls_file:
	with open(calls_file, "a", encoding="utf-8") as handle:
		handle.write(json.dumps(args))
		handle.write("\n")

stdin_file = os.environ.get("MOCK_CODEX_STDIN_FILE")
if stdin_file:
	Path(stdin_file).write_text(stdin_text, encoding="utf-8")

mode = os.environ.get("MOCK_CODEX_MODE", "success")
if mode == "fail":
	print("mock codex failure", file=sys.stderr)
	sys.exit(1)
if mode == "invalid":
	print("this is not json")
	sys.exit(0)

payload = os.environ.get("MOCK_CODEX_OUTPUT", "{}")
print(payload)
sys.exit(0)
'''
	mock_path = bin_dir / "codex"
	mock_path.write_text(codex_script, encoding="utf-8")
	mock_path.chmod(0o755)


def _read_gh_state(state_file: Path) -> dict:
	return json.loads(state_file.read_text(encoding="utf-8"))


def _copy_diagnose_assets(repo_dir: Path) -> None:
	for rel in (
		"scripts/gh_helpers.sh",
		"scripts/render_prompt.sh",
		"prompts/mode-implement-diagnose.txt",
		"prompts/serena-efficiency-block.txt",
	):
		src = REPO_ROOT / rel
		dst = repo_dir / rel
		dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dst)
		if dst.suffix == ".sh":
			dst.chmod(0o755)


def _prepare_diagnose_repo(tmp_path: Path) -> Path:
	repo_dir = tmp_path / "diag-repo"
	_bootstrap_git_repo(repo_dir)
	_copy_diagnose_assets(repo_dir)

	tracked = repo_dir / "tracked.txt"
	tracked.write_text("before\n", encoding="utf-8")
	_git(["git", "add", "tracked.txt"], cwd=repo_dir)
	_git(["git", "commit", "-m", "tracked"], cwd=repo_dir)
	tracked.write_text("after\n", encoding="utf-8")

	return repo_dir


def _run_diagnose_step(
	tmp_path: Path,
	*,
	issue_labels: list[str],
	capture_contents: str | None,
	codex_mode: str,
	codex_output: dict | None,
	failed_step_name: str,
	issue_body: str,
) -> tuple[subprocess.CompletedProcess[str], dict, Path, dict[str, str]]:
	repo_dir = _prepare_diagnose_repo(tmp_path)
	runtime_dir = tmp_path / "runtime"
	bin_dir = tmp_path / "bin"
	runtime_dir.mkdir(parents=True, exist_ok=True)
	bin_dir.mkdir(parents=True, exist_ok=True)

	_install_mock_gh(bin_dir)
	_install_mock_codex(bin_dir)

	if capture_contents is not None:
		(runtime_dir / "post_codex_validation_errors.txt").write_text(capture_contents, encoding="utf-8")

	issue_body_file = runtime_dir / "issue_body.txt"
	issue_body_file.write_text(issue_body, encoding="utf-8")

	gh_state_file = runtime_dir / "gh_state.json"
	gh_state_file.write_text(
		json.dumps(
			{
				"issue_number": 948,
				"issue_labels": issue_labels,
				"issue_body": issue_body,
				"failed_step_name": failed_step_name,
				"next_issue_number": 1001,
			}
		),
		encoding="utf-8",
	)

	github_output = runtime_dir / "github_output.txt"
	result_file = runtime_dir / "implement_diagnose_result.json"
	prompt_file = runtime_dir / "implement_diagnose_prompt.txt"
	output_file = runtime_dir / "implement_diagnose_output.txt"
	log_file = runtime_dir / "implement_diagnose_log.txt"
	calls_file = runtime_dir / "codex_calls.log"
	stdin_file = runtime_dir / "codex_stdin.txt"

	script = _render_github_expressions(_extract_run_script("Diagnose post-Codex failure and create fix-up issues"))
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_TOKEN": "test-token",
			"OPENROUTER_API_KEY": "test-openrouter",
			"ISSUE_NUMBER": "948",
			"RUNTIME_DIR": str(runtime_dir),
			"GITHUB_OUTPUT": str(github_output),
			"MODEL_EDITOR": "openai/gpt-5.3-codex",
			"PR_BASE_BRANCH": "orchestrator/project-829",
			"ISSUE_BODY_FILE": str(issue_body_file),
			"IMPLEMENT_DIAGNOSE_PROMPT_FILE": str(prompt_file),
			"IMPLEMENT_DIAGNOSE_OUTPUT_FILE": str(output_file),
			"IMPLEMENT_DIAGNOSE_LOG_FILE": str(log_file),
			"IMPLEMENT_DIAGNOSE_RESULT_FILE": str(result_file),
			"CODEX_OUTPUT_FILE": str(runtime_dir / "codex_output.txt"),
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"MOCK_CODEX_MODE": codex_mode,
			"MOCK_CODEX_OUTPUT": json.dumps(codex_output or {}),
			"MOCK_CODEX_CALLS_FILE": str(calls_file),
			"MOCK_CODEX_STDIN_FILE": str(stdin_file),
			"TMPDIR": str(runtime_dir),
		}
	)

	proc = _run_shell_script(script, cwd=repo_dir, env=env)
	state = _read_gh_state(gh_state_file)
	paths = {
		"github_output": str(github_output),
		"result_file": str(result_file),
		"prompt_file": str(prompt_file),
		"output_file": str(output_file),
		"log_file": str(log_file),
		"calls_file": str(calls_file),
		"stdin_file": str(stdin_file),
	}
	return proc, state, runtime_dir, paths


def _read_file(path: str) -> str:
	p = Path(path)
	if not p.exists():
		return ""
	return p.read_text(encoding="utf-8")

def _step_block_text(step_name: str) -> str:
	return "\n".join(_step_block(step_name))


def test_noop_failure_labeling_is_gated_on_non_destructive_failures() -> None:
	wf = _workflow_text()
	assert (
		"if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'false' "
		"&& steps.commit_changes.outputs.destructive_commit_blocked == ''"
	) in wf, (
		"Handle no-op implementation must be skipped when destructive_commit_blocked is set "
		"so destructive-guard failures cannot transition the issue into ai:implementation-failed"
	)


def test_failure_comment_step_skips_destructive_blocked_runs() -> None:
	wf = _workflow_text()
	assert (
		"if: (failure() || cancelled()) && steps.commit_changes.outputs.destructive_commit_blocked == ''"
	) in wf, (
		"Generic failure comment flow must be disabled for destructive-blocked runs to avoid "
		"re-adding ai:awaiting-approval"
	)


def test_telegram_failure_step_skips_destructive_blocked_runs() -> None:
	telegram_block = _step_block_text("Telegram failure notification")
	assert "if: (failure() || cancelled()) && steps.commit_changes.outputs.destructive_commit_blocked == ''" in telegram_block, (
		"Post-failure Telegram flow must be skipped for destructive-blocked runs; only the dedicated "
		"destructive-guard CRITICAL alert should fire"
	)


def test_destructive_guard_path_does_not_set_implementation_failed_or_fixup_flow() -> None:
	destructive_block = _step_block_text("Destructive-commit guard — label + alert on rejection")
	lowered = destructive_block.lower()
	assert "--add-label 'ai:destructive-blocked'" in destructive_block, (
		"Destructive guard must preserve ai:destructive-blocked human-halt signaling"
	)
	assert "ai:implementation-failed" not in destructive_block, (
		"Destructive guard block must not apply ai:implementation-failed"
	)
	assert "fix-up" not in lowered and "fixup" not in lowered, (
		"Destructive guard block must not trigger fix-up issue generation"
	)



def test_diagnose_prompt_contract_round_trip_and_fixup_metadata():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		capture = "===== broken.yml (python3 yaml.safe_load) =====\nscanner error on line 3\n"
		issue_body = "Issue context\n\nTracking issue: #829\n"
		failed_step = "Validate syntax of changed files"
		codex_payload = {
			"status": "needs_fixes",
			"diagnosis": "Validator failed after Codex edits.",
			"fix_issues": [
				{
					"id": "implement-fix-1",
					"title": "Repair syntax capture and parsing",
					"body": "Fix parser output and re-run implementation.",
					"priority": 1,
					"depends_on": [],
				}
			],
			"harness_fixes": "",
		}

		proc, state, _runtime_dir, paths = _run_diagnose_step(
			tmp_path,
			issue_labels=["ai:implementing", "ai:awaiting-approval"],
			capture_contents=capture,
			codex_mode="success",
			codex_output=codex_payload,
			failed_step_name=failed_step,
			issue_body=issue_body,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"

		result = json.loads(_read_file(paths["result_file"]))
		assert isinstance(result, dict)
		assert result.get("status") in {"needs_fixes", "harness_error", "infeasible"}
		assert isinstance(result.get("diagnosis"), str)
		assert isinstance(result.get("fix_issues"), list)
		assert isinstance(result.get("harness_fixes"), str)

		stdin_prompt = _read_file(paths["stdin_file"])
		assert "=== SOURCE ISSUE BODY ===" in stdin_prompt
		assert issue_body.strip() in stdin_prompt
		assert "=== FAILED STEP NAME ===" in stdin_prompt
		assert failed_step in stdin_prompt
		assert "=== WORKING TREE CHANGES (git diff HEAD) ===" in stdin_prompt
		assert "tracked.txt" in stdin_prompt
		assert "=== CAPTURED POST-CODEX VALIDATION ERRORS (FULL) ===" in stdin_prompt
		assert "scanner error on line 3" in stdin_prompt

		created_issues = state.get("created_issues", [])
		assert len(created_issues) == 1
		created = created_issues[0]
		assert created["repo"] == "owner/repo"
		assert created["title"] == "Repair syntax capture and parsing"
		body = created["body"]
		assert "Type: implement-fix-up (post-codex-validation)" in body
		assert "Source issue: #948" in body
		assert f"Failed step: {failed_step}" in body

		labels = state.get("issue_labels", [])
		assert "ai:implementation-failed" in labels
		assert "ai:awaiting-approval" not in labels


def test_validator_capture_aggregates_multiple_files_before_nonzero_exit():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "validate-repo"
		_bootstrap_git_repo(repo_dir)

		(runtime_py := repo_dir / "broken.py").write_text("x = 1\n", encoding="utf-8")
		(runtime_yaml := repo_dir / "broken.yml").write_text("a: 1\n", encoding="utf-8")
		_git(["git", "add", "broken.py", "broken.yml"], cwd=repo_dir)
		_git(["git", "commit", "-m", "add files"], cwd=repo_dir)

		runtime_py.write_text("def nope(:\n\tpass\n", encoding="utf-8")
		runtime_yaml.write_text("key: [1, 2\n", encoding="utf-8")

		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		script = _extract_run_script("Validate syntax of changed files")
		env = os.environ.copy()
		env.update(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"PATH": env.get("PATH", ""),
			}
		)

		proc = _run_shell_script(script, cwd=repo_dir, env=env)
		assert proc.returncode != 0, "expected non-zero exit when syntax errors exist"

		capture_file = runtime_dir / "post_codex_validation_errors.txt"
		assert capture_file.exists(), "expected capture file to be written"
		capture = capture_file.read_text(encoding="utf-8")
		assert "broken.py" in capture
		assert "python3 -m py_compile" in capture
		assert "broken.yml" in capture
		assert "python3 yaml.safe_load" in capture


def test_needs_fixes_labels_source_issue_and_generic_failure_step_is_bypassed():
	wf = _workflow_text()
	assert "--add-label 'ai:implementation-failed'" in wf
	assert "--remove-label 'ai:awaiting-approval'" in wf
	assert "if: (failure() || cancelled()) && steps.commit_changes.outputs.destructive_commit_blocked == '' && steps.diagnose_post_codex_failure.outputs.handled != 'true'" in wf


def test_idempotency_skips_diagnose_and_issue_creation_when_already_failed_label():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		proc, state, _runtime_dir, paths = _run_diagnose_step(
			tmp_path,
			issue_labels=["ai:implementation-failed", "ai:implementing"],
			capture_contents="===== x =====\nerror\n",
			codex_mode="success",
			codex_output={
				"status": "needs_fixes",
				"diagnosis": "should not run",
				"fix_issues": [],
				"harness_fixes": "",
			},
			failed_step_name="Validate syntax of changed files",
			issue_body="Tracking issue: #829\n",
		)

		assert proc.returncode == 0
		assert "handled=true" in _read_file(paths["github_output"])
		assert _read_file(paths["calls_file"]).strip() == ""
		assert state.get("created_issues", []) == []


def test_fallback_creates_deterministic_fixup_issue_when_diagnose_output_invalid():
	for codex_mode in ("invalid", "fail"):
		with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
			tmp_path = Path(td)
			raw_diag = "yaml parse failed on alpha.yml\npython compile failed on beta.py\n"
			proc, state, _runtime_dir, paths = _run_diagnose_step(
				tmp_path,
				issue_labels=["ai:implementing"],
				capture_contents=raw_diag,
				codex_mode=codex_mode,
				codex_output=None,
				failed_step_name="Validate syntax of changed files",
				issue_body="Tracking issue: #829\n",
			)

			assert proc.returncode == 0, f"mode={codex_mode}\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"

			result = json.loads(_read_file(paths["result_file"]))
			assert result.get("status") == "needs_fixes"
			assert "Fallback fix-up issue created" in result.get("diagnosis", "")

			created_issues = state.get("created_issues", [])
			assert len(created_issues) == 1
			created = created_issues[0]
			assert created["title"] == "Implement phase post-Codex validation failure fallback"
			assert "The diagnose step could not produce a valid JSON contract" in created["body"]
			assert "yaml parse failed on alpha.yml" in created["body"]
			assert "Type: implement-fix-up (post-codex-validation)" in created["body"]


def test_out_of_scope_noop_when_capture_file_missing():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		proc, state, _runtime_dir, paths = _run_diagnose_step(
			tmp_path,
			issue_labels=["ai:implementing", "ai:awaiting-approval"],
			capture_contents=None,
			codex_mode="success",
			codex_output={
				"status": "needs_fixes",
				"diagnosis": "should not run",
				"fix_issues": [],
				"harness_fixes": "",
			},
			failed_step_name="Validate syntax of changed files",
			issue_body="Tracking issue: #829\n",
		)

		assert proc.returncode == 0
		assert "handled=false" in _read_file(paths["github_output"])
		assert _read_file(paths["calls_file"]).strip() == ""
		assert state.get("created_issues", []) == []

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
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0

if __name__ == "__main__":
	raise SystemExit(main())
