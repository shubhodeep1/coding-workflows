#!/usr/bin/env python3
"""Regression tests for implement-phase post-Codex diagnose/fix-up recovery.

These tests intentionally execute extracted `run:` blocks from
`.github/workflows/implement.yml` with mocked `gh`/`codex` binaries so the
contract is validated against workflow behavior, not reimplemented logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import tempfile
import textwrap


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
IMPLEMENT_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "implement_commit_changes.sh"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _implement_commit_script_text() -> str:
	return IMPLEMENT_COMMIT_SCRIPT.read_text(encoding="utf-8")


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


def _isolated_test_env(extra_env: dict[str, str] | None = None, *, cwd: Path | None = None) -> dict[str, str]:
	baseline_env = os.environ.copy()
	env = baseline_env.copy()
	if extra_env:
		env.update(extra_env)
	# Scratch-repo tests must ignore the runner's workspace-shell hook and the
	# main-checkout git-location overrides, or bash/git subprocesses will operate
	# on the outer workspace instead of the temp repo under test. When callers
	# pass os.environ.copy(), drop inherited runner values after the merge while
	# preserving explicit test overrides that intentionally differ.
	for key in ("BASH_ENV", "ENV"):
		env.pop(key, None)
	for key in ("WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
		if env.get(key) == baseline_env.get(key):
			env.pop(key, None)
	if cwd is not None:
		env["PWD"] = str(cwd)
		env.pop("OLDPWD", None)
	return env


def _run_shell_script(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	env = _isolated_test_env(env, cwd=cwd)
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
	env = _isolated_test_env(cwd=cwd)
	subprocess.run(cmd, cwd=str(cwd), env=env, check=True, capture_output=True, text=True)


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
		state.setdefault("issue_queries", []).append(path)
		failures_remaining = int(state.get("issue_api_failures_remaining", 0) or 0)
		if failures_remaining > 0:
			state["issue_api_failures_remaining"] = failures_remaining - 1
			save()
			print("gh: simulated transient failure", file=sys.stderr)
			sys.exit(1)
		raw_issue_response = state.get("issue_api_raw_response")
		if raw_issue_response is not None:
			save()
			print(raw_issue_response)
			sys.exit(0)
		issue_num = int(state.get("issue_number", issue_from_path(path)))
		labels = [{"name": x} for x in state.get("issue_labels", [])]
		body = state.get("issue_body", "")
		title = state.get("issue_title", "Test issue")
		html_url = state.get("issue_url", f"https://github.com/owner/repo/issues/{issue_num}")
		issue_state = state.get("issue_state", "open")
		payload = {
			"number": issue_num,
			"title": title,
			"body": body,
			"html_url": html_url,
			"state": issue_state,
			"labels": labels,
		}
		if jq:
			if "labels" in jq and "state" in jq:
				print(json.dumps({"state": issue_state, "labels": [x["name"] for x in labels]}))
			elif "labels" in jq:
				print(json.dumps([x["name"] for x in labels]))
			elif ".body" in jq:
				print(body)
			else:
				print("")
		else:
			print(json.dumps(payload))
		save()
		sys.exit(0)

	if "/git/ref/heads/" in path:
		ref = path.split("/git/ref/heads/", 1)[1]
		from urllib.parse import unquote

		decoded_ref = unquote(ref)
		state.setdefault("branch_ref_queries", []).append(decoded_ref)
		exists = bool((state.get("branch_exists", {}) or {}).get(decoded_ref, False))
		save()
		if exists:
			print(json.dumps({"ref": f"refs/heads/{decoded_ref}"}))
			sys.exit(0)
		print("gh: Not Found (HTTP 404)", file=sys.stderr)
		sys.exit(1)

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
	(bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
	(bin_dir / "sleep").chmod(0o755)


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
		"scripts/implement_diagnose_post_codex_failure.sh",
		"scripts/render_prompt.sh",
		"scripts/validate_changed_files_syntax.sh",
		"prompts/mode-implement-diagnose.txt",
		"prompts/mode-implement-repair-syntax.txt",
	):
		src = REPO_ROOT / rel
		dst = repo_dir / rel
		dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(src, dst)
		if dst.suffix == ".sh":
			dst.chmod(0o755)


def _copy_write_guard_assets(repo_dir: Path) -> None:
	for rel in (
		"scripts/write_guard.sh",
		".github/ai/write_guards.v1.json",
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
	issue_meta_payload: object | None = None,
	write_issue_body_file: bool = True,
	issue_api_failures_remaining: int = 0,
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
	if write_issue_body_file:
		issue_body_file.write_text(issue_body, encoding="utf-8")

	issue_meta_file = runtime_dir / "issue_meta.json"
	if issue_meta_payload is not None:
		if isinstance(issue_meta_payload, str):
			issue_meta_file.write_text(issue_meta_payload, encoding="utf-8")
		else:
			issue_meta_file.write_text(json.dumps(issue_meta_payload), encoding="utf-8")

	gh_state_file = runtime_dir / "gh_state.json"
	gh_state_file.write_text(
		json.dumps(
			{
				"issue_number": 948,
				"issue_labels": issue_labels,
				"issue_body": issue_body,
				"failed_step_name": failed_step_name,
				"next_issue_number": 1001,
				"issue_api_failures_remaining": issue_api_failures_remaining,
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
			"GITHUB_REPOSITORY": "owner/repo",
			"GITHUB_RUN_ID": "777",
			"GITHUB_SERVER_URL": "https://github.com",
			"JOB_STATUS": "failure",
			"DEFAULT_BRANCH": "main",
			"MODEL_EDITOR": "openai/gpt-5.4",
			"PR_BASE_BRANCH": "orchestrator/project-829",
			"SERENA_AVAILABLE": "true",
			"ISSUE_BODY_FILE": str(issue_body_file),
			"ISSUE_META_FILE": str(issue_meta_file),
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


def _parse_github_output(path: Path) -> dict[str, str]:
	if not path.exists():
		return {}
	parsed: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		if "=" not in line:
			continue
		key, value = line.split("=", 1)
		parsed[key] = value
	return parsed


def _run_resolve_checkout_ref_step(
	tmp_path: Path,
	*,
	issue_body: str,
	default_checkout_ref: str,
	branch_exists: dict[str, bool] | None = None,
	issue_meta_payload: object | None = None,
	issue_api_failures_remaining: int = 0,
	issue_api_raw_response: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict, dict[str, str], dict[str, str]]:
	repo_dir = tmp_path / "checkout-ref-repo"
	_bootstrap_git_repo(repo_dir)
	bin_dir = tmp_path / "bin"
	runtime_dir = tmp_path / "runtime"
	bin_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir.mkdir(parents=True, exist_ok=True)

	_install_mock_gh(bin_dir)

	gh_state_file = runtime_dir / "checkout_ref_gh_state.json"
	gh_state_file.write_text(
		json.dumps(
			{
				"issue_number": 948,
				"issue_body": issue_body,
				"branch_exists": branch_exists or {},
				"issue_api_failures_remaining": issue_api_failures_remaining,
				"issue_api_raw_response": issue_api_raw_response,
			}
		),
		encoding="utf-8",
	)

	github_output = runtime_dir / "checkout_ref_github_output.txt"
	issue_meta_file = runtime_dir / "issue_meta.json"
	issue_body_file = runtime_dir / "issue_body.txt"
	if issue_meta_payload is not None:
		if isinstance(issue_meta_payload, str):
			issue_meta_file.write_text(issue_meta_payload, encoding="utf-8")
		else:
			issue_meta_file.write_text(json.dumps(issue_meta_payload), encoding="utf-8")
	script = _render_github_expressions(_extract_run_script("Resolve checkout ref"))
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_TOKEN": "test-token",
			"GITHUB_OUTPUT": str(github_output),
			"ISSUE_NUMBER": "948",
			"ISSUE_META_FILE": str(issue_meta_file),
			"ISSUE_BODY_FILE": str(issue_body_file),
			"DEFAULT_CHECKOUT_REF": default_checkout_ref,
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"TMPDIR": str(runtime_dir),
		}
	)

	proc = _run_shell_script(script, cwd=repo_dir, env=env)
	state = _read_gh_state(gh_state_file)
	outputs = _parse_github_output(github_output)
	files = {
		"issue_meta": _read_file(str(issue_meta_file)),
		"issue_body": _read_file(str(issue_body_file)),
	}
	return proc, state, outputs, files


def _run_fetch_issue_metadata_step(
	tmp_path: Path,
	*,
	issue_body: str,
	issue_meta_payload: object | None = None,
	issue_title: str = "Test issue",
	issue_url: str = "https://github.com/owner/repo/issues/948",
) -> tuple[subprocess.CompletedProcess[str], dict, str, dict[str, str]]:
	repo_dir = tmp_path / "fetch-issue-repo"
	_bootstrap_git_repo(repo_dir)
	bin_dir = tmp_path / "bin"
	runtime_dir = tmp_path / "runtime"
	bin_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir.mkdir(parents=True, exist_ok=True)

	_install_mock_gh(bin_dir)

	gh_state_file = runtime_dir / "fetch_issue_gh_state.json"
	gh_state_file.write_text(
		json.dumps(
			{
				"issue_number": 948,
				"issue_body": issue_body,
				"issue_title": issue_title,
				"issue_url": issue_url,
			}
		),
		encoding="utf-8",
	)

	github_env = runtime_dir / "github_env.txt"
	issue_meta_file = runtime_dir / "issue_meta.json"
	issue_body_file = runtime_dir / "issue_body.txt"
	if issue_meta_payload is not None:
		if isinstance(issue_meta_payload, str):
			issue_meta_file.write_text(issue_meta_payload, encoding="utf-8")
		else:
			issue_meta_file.write_text(json.dumps(issue_meta_payload), encoding="utf-8")

	script = _render_github_expressions(
		_extract_run_script("Fetch issue metadata"),
		overrides={"steps.refctx.outputs.ref || github.event.repository.default_branch": "orchestrator/project-829"},
	)
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_TOKEN": "test-token",
			"GITHUB_ENV": str(github_env),
			"ISSUE_NUMBER": "948",
			"ISSUE_META_FILE": str(issue_meta_file),
			"ISSUE_BODY_FILE": str(issue_body_file),
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"TMPDIR": str(runtime_dir),
		}
	)

	proc = _run_shell_script(script, cwd=repo_dir, env=env)
	state = _read_gh_state(gh_state_file)
	files = {
		"issue_meta": _read_file(str(issue_meta_file)),
		"issue_body": _read_file(str(issue_body_file)),
	}
	return proc, state, _read_file(str(github_env)), files


def _install_capture_step_mock_gh(bin_dir: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_CAPTURE_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_non_flag(values: list[str]) -> str:
	i = 0
	while i < len(values):
		arg = values[i]
		if arg in ("--jq", "-q", "-f", "--field", "-H", "--header", "-X", "--method", "--input", "--template", "-t", "--preview"):
			i += 2
			continue
		if arg.startswith("-"):
			i += 1
			continue
		return arg
	return ""


state.setdefault("calls", []).append(args)

if args and args[0] == "api":
	path = first_non_flag(args[1:])
	if "/actions/runs/" in path and "/jobs" in path:
		state["jobs_api_calls"] = int(state.get("jobs_api_calls", 0)) + 1
		mode = state.get("mode", "ok")
		if mode == "api_error":
			save()
			print("mock jobs api error", file=sys.stderr)
			sys.exit(1)
		if mode == "invalid_json":
			save()
			print("{invalid-json")
			sys.exit(0)
		payload = state.get("jobs_payload", {"jobs": []})
		save()
		print(json.dumps(payload))
		sys.exit(0)

save()
print("{}")
sys.exit(0)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)


def _run_capture_step(
	tmp_path: Path,
	*,
	jobs_payload: dict | None,
	mode: str,
	job_status: str,
) -> tuple[subprocess.CompletedProcess[str], dict, dict[str, str]]:
	repo_dir = tmp_path / "capture-repo"
	_bootstrap_git_repo(repo_dir)
	bin_dir = tmp_path / "bin"
	runtime_dir = tmp_path / "runtime"
	bin_dir.mkdir(parents=True, exist_ok=True)
	runtime_dir.mkdir(parents=True, exist_ok=True)

	_install_capture_step_mock_gh(bin_dir)

	gh_state_file = runtime_dir / "capture_gh_state.json"
	gh_state_file.write_text(
		json.dumps(
			{
				"mode": mode,
				"jobs_payload": jobs_payload if jobs_payload is not None else {"jobs": []},
				"jobs_api_calls": 0,
			}
		),
		encoding="utf-8",
	)

	github_output = runtime_dir / "capture_github_output.txt"
	script = _render_github_expressions(
		_extract_run_script("Capture post-Codex validation errors"),
		overrides={"job.status": job_status},
	)
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_TOKEN": "test-token",
			"GITHUB_OUTPUT": str(github_output),
			"RUNTIME_DIR": str(runtime_dir),
			"ISSUE_NUMBER": "948",
			"MOCK_CAPTURE_GH_STATE_FILE": str(gh_state_file),
			"TMPDIR": str(runtime_dir),
		}
	)

	proc = _run_shell_script(script, cwd=repo_dir, env=env)
	state = _read_gh_state(gh_state_file)
	outputs = _parse_github_output(github_output)
	return proc, state, outputs


def test_capture_step_reuses_single_jobs_fetch_for_failure_selector() -> None:
	with tempfile.TemporaryDirectory(prefix="test_capture_") as td:
		tmp_path = Path(td)
		payload = {
			"jobs": [
				{
					"steps": [
						{"name": "Checkout", "conclusion": "success", "status": "completed"},
						{
							"name": "Validate syntax of changed files",
							"conclusion": "failure",
							"status": "completed",
						},
					]
				}
			]
		}
		proc, state, outputs = _run_capture_step(
			tmp_path,
			jobs_payload=payload,
			mode="ok",
			job_status="failure",
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert state.get("jobs_api_calls") == 1
		assert outputs.get("failed_step_name") == "Validate syntax of changed files"


def test_capture_step_reuses_single_jobs_fetch_for_cancelled_fallback_selector() -> None:
	with tempfile.TemporaryDirectory(prefix="test_capture_") as td:
		tmp_path = Path(td)
		payload = {
			"jobs": [
				{
					"steps": [
						{"name": "Checkout", "conclusion": "success", "status": "completed"},
						{
							"name": "Run Codex implementation",
							"conclusion": "",
							"status": "in_progress",
						},
					]
				}
			]
		}
		proc, state, outputs = _run_capture_step(
			tmp_path,
			jobs_payload=payload,
			mode="ok",
			job_status="cancelled",
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert state.get("jobs_api_calls") == 1
		assert outputs.get("failed_step_name") == "Run Codex implementation"


def test_capture_step_fails_open_on_invalid_jobs_payload() -> None:
	with tempfile.TemporaryDirectory(prefix="test_capture_") as td:
		tmp_path = Path(td)
		proc, state, outputs = _run_capture_step(
			tmp_path,
			jobs_payload=None,
			mode="invalid_json",
			job_status="cancelled",
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert state.get("jobs_api_calls") == 1
		assert outputs.get("failed_step_name", "") == ""


def test_resolve_checkout_ref_ignores_prior_pr_baseline_branch_when_present() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_") as td:
		tmp_path = Path(td)
		baseline_branch = "ai/reissue-baseline/pr-42-abcdef123456-777-1"
		proc, state, outputs, files = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body=textwrap.dedent(
				f"""\
				Plan body

				---
				**Review-blocked reissue metadata**
				- Replaces: #41 (PR #42 closed — approach rework)
				- Type: review-blocked-reissue
				- prior_pr_baseline_branch: {baseline_branch}
				- files_touched:
				  - src/app.py
				"""
			),
			default_checkout_ref="orchestrator/project-829",
			branch_exists={baseline_branch: True},
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert len(state.get("issue_queries", [])) == 1
		assert state.get("branch_ref_queries", []) == [], (
			"Resolve checkout ref must not resolve prior_pr_baseline_branch; trusted baseline checkout happens only in baseline_refctx"
		)
		assert f"prior_pr_baseline_branch: {baseline_branch}" in files["issue_body"]


def test_resolve_checkout_ref_falls_back_when_hint_missing() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_") as td:
		tmp_path = Path(td)
		proc, state, outputs, _ = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body="No baseline hint here.\n",
			default_checkout_ref="orchestrator/project-829",
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert state.get("branch_ref_queries", []) == [], (
			"Resolve checkout ref should not hit /git/ref/heads when prior_pr_baseline_branch is absent"
		)


def test_resolve_checkout_ref_reuses_matching_cached_issue_metadata() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_cache_hit_") as td:
		tmp_path = Path(td)
		issue_body = "Plan body with cached metadata.\n"
		proc, state, outputs, files = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body=issue_body,
			default_checkout_ref="orchestrator/project-829",
			issue_meta_payload={
				"number": 948,
				"body": issue_body,
				"labels": [],
			},
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert state.get("issue_queries", []) == [], (
			"Matching cached issue metadata should satisfy Resolve checkout ref without re-fetching /issues/{n}"
		)
		assert files["issue_body"] == issue_body


def test_resolve_checkout_ref_refetches_invalid_or_mismatched_cached_issue_metadata() -> None:
	for case_name, issue_meta_payload in (
		(
			"mismatched",
			{
				"number": 999,
				"body": "stale body\n",
				"labels": [],
			},
		),
		("invalid", '{"number": 948, "body": '),
	):
		with tempfile.TemporaryDirectory(prefix=f"test_checkout_ref_cache_miss_{case_name}_") as td:
			tmp_path = Path(td)
			issue_body = "Fresh body from API\n"
			proc, state, outputs, files = _run_resolve_checkout_ref_step(
				tmp_path,
				issue_body=issue_body,
				default_checkout_ref="orchestrator/project-829",
				issue_meta_payload=issue_meta_payload,
				issue_api_failures_remaining=2,
			)

			assert proc.returncode == 0, f"case={case_name}\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			assert outputs.get("ref") == "orchestrator/project-829"
			assert outputs.get("source") == "integration/default"
			assert len(state.get("issue_queries", [])) == 3, f"case={case_name}"
			assert files["issue_body"] == issue_body, f"case={case_name}"
			assert json.loads(files["issue_meta"])["number"] == 948, f"case={case_name}"


def test_resolve_checkout_ref_retries_issue_metadata_fetch() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_") as td:
		tmp_path = Path(td)
		proc, state, outputs, files = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body="Plan body with canonical footer metadata.\n",
			default_checkout_ref="orchestrator/project-829",
			issue_api_failures_remaining=2,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert len(state.get("issue_queries", [])) == 3, (
			"Resolve checkout ref must retry transient issue metadata fetch failures before returning the integration/default ref"
		)
		assert state.get("branch_ref_queries", []) == [], (
			"Resolve checkout ref must not perform baseline ref lookups after a successful retry"
		)
		assert files["issue_meta"], "successful retry path must still cache valid issue metadata"


def test_resolve_checkout_ref_surfaces_final_issue_metadata_fetch_stderr_on_fallback() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_") as td:
		tmp_path = Path(td)
		proc, state, outputs, files = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body="prior_pr_baseline_branch: ai/reissue-baseline/pr-42-failed\n",
			default_checkout_ref="orchestrator/project-829",
			issue_api_failures_remaining=3,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert len(state.get("issue_queries", [])) == 3, (
			"Resolve checkout ref must still retry three times before failing open to the default ref"
		)
		assert files["issue_meta"] == "", "failed issue metadata fetch must not cache partial data"
		assert files["issue_body"] == "", "failed issue metadata fetch must not cache partial body data"
		assert "gh: simulated transient failure" in proc.stderr, (
			"Final failed gh api stderr must remain visible so the fail-open fallback is debuggable"
		)


def test_resolve_checkout_ref_fails_open_on_invalid_issue_metadata_without_caching_it() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_") as td:
		tmp_path = Path(td)
		proc, state, outputs, files = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body="prior_pr_baseline_branch: ai/reissue-baseline/pr-42-invalid\n",
			default_checkout_ref="orchestrator/project-829",
			issue_api_raw_response="<!doctype html>not-json",
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert state.get("branch_ref_queries", []) == [], (
			"Invalid issue metadata must fail open before any prior_pr_baseline_branch lookup"
		)
		assert files["issue_meta"] == "", "invalid issue metadata must not poison ISSUE_META_FILE"
		assert files["issue_body"] == "", "invalid issue metadata must not populate ISSUE_BODY_FILE"


def test_resolve_checkout_ref_ignores_untrusted_baseline_hints_without_api_lookup() -> None:
	with tempfile.TemporaryDirectory(prefix="test_checkout_ref_") as td:
		tmp_path = Path(td)
		proc, state, outputs, _ = _run_resolve_checkout_ref_step(
			tmp_path,
			issue_body="prior_pr_baseline_branch: ../escape\n",
			default_checkout_ref="orchestrator/project-829",
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outputs.get("ref") == "orchestrator/project-829"
		assert outputs.get("source") == "integration/default"
		assert state.get("branch_ref_queries", []) == [], (
			"Resolve checkout ref must ignore untrusted prior_pr_baseline_branch hints instead of resolving them"
		)


def test_resolve_checkout_ref_no_longer_performs_baseline_branch_resolution() -> None:
	checkout_ref_block = _extract_run_script("Resolve checkout ref")
	assert "/git/ref/heads/" not in checkout_ref_block, (
		"Resolve checkout ref must not resolve prior_pr_baseline_branch via /git/ref/heads; only baseline_refctx may honor preserved baselines"
	)
	assert "source=prior_pr_baseline_branch" not in checkout_ref_block, (
		"Resolve checkout ref must no longer emit prior_pr_baseline_branch as a checkout source"
	)
	assert "Using prior_pr_baseline_branch checkout override" not in checkout_ref_block, (
		"Resolve checkout ref must not announce an untrusted baseline checkout override"
	)


def test_checkout_repository_fallback_uses_checkout_ref_output_chain() -> None:
	fallback_checkout_step = _step_block_text("Checkout repository")
	log_step = _step_block_text("Log checkout ref")

	assert (
		"ref: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
		in fallback_checkout_step
	)
	assert (
		"Resolved fallback ref: ${{ steps.checkout_ref.outputs.ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
		in log_step
	)
	assert "steps.checkout_ref.outputs.source" not in log_step, (
		"Resolved checkout source logging must not read the dead checkout_ref.outputs.source output"
	)
	assert 'resolved_checkout_source="${{ steps.baseline_refctx.outputs.branch }}"' in log_step


def test_fetch_issue_metadata_keeps_pr_base_branch_on_refctx_default_chain() -> None:
	fetch_block = _extract_run_script("Fetch issue metadata")
	assert 'PR_BASE_BRANCH="${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}"' in fetch_block, (
		"Fetch issue metadata must keep PR_BASE_BRANCH anchored to the integration/default ref, not the baseline checkout override"
	)
	assert "steps.checkout_ref.outputs.ref" not in fetch_block, (
		"PR_BASE_BRANCH must not follow the optional prior_pr_baseline_branch checkout override"
	)


def test_fetch_issue_metadata_reuses_matching_cache_without_api_call() -> None:
	with tempfile.TemporaryDirectory(prefix="test_fetch_issue_meta_cache_hit_") as td:
		tmp_path = Path(td)
		issue_body = "Cached body\n"
		issue_title = "Cached title"
		issue_url = "https://github.com/owner/repo/issues/948"
		proc, state, github_env_text, files = _run_fetch_issue_metadata_step(
			tmp_path,
			issue_body=issue_body,
			issue_title=issue_title,
			issue_url=issue_url,
			issue_meta_payload={
				"number": 948,
				"title": issue_title,
				"body": issue_body,
				"html_url": issue_url,
			},
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert state.get("issue_queries", []) == [], (
			"Matching ISSUE_META_FILE should satisfy Fetch issue metadata without hitting /issues/{n}"
		)
		assert files["issue_body"] == issue_body
		assert "ISSUE_NUMBER=948" in github_env_text
		assert "PR_BASE_BRANCH=orchestrator/project-829" in github_env_text


def test_fetch_issue_metadata_refetches_invalid_or_mismatched_cache() -> None:
	for case_name, issue_meta_payload in (
		(
			"mismatched",
			{
				"number": 999,
				"title": "wrong",
				"body": "stale body\n",
				"html_url": "https://github.com/owner/repo/issues/999",
			},
		),
		("invalid", '{"number": 948, "title": '),
	):
		with tempfile.TemporaryDirectory(prefix=f"test_fetch_issue_meta_cache_miss_{case_name}_") as td:
			tmp_path = Path(td)
			issue_body = "Fresh fetched body\n"
			issue_title = "Fresh fetched title"
			issue_url = "https://github.com/owner/repo/issues/948"
			proc, state, github_env_text, files = _run_fetch_issue_metadata_step(
				tmp_path,
				issue_body=issue_body,
				issue_title=issue_title,
				issue_url=issue_url,
				issue_meta_payload=issue_meta_payload,
			)

			assert proc.returncode == 0, f"case={case_name}\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			assert state.get("issue_queries", []) == ["repos/owner/repo/issues/948"], f"case={case_name}"
			assert files["issue_body"] == issue_body, f"case={case_name}"
			assert json.loads(files["issue_meta"])["number"] == 948, f"case={case_name}"
			assert f"ISSUE_URL={issue_url}" in github_env_text, f"case={case_name}"


def test_noop_failure_labeling_is_gated_on_non_destructive_failures() -> None:
	wf = _workflow_text()
	assert (
		"if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'false' "
		"&& steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' "
		"&& steps.commit_changes.outputs.destructive_commit_blocked == ''"
	) in wf, (
		"Handle no-op implementation must be skipped when destructive_commit_blocked is set "
		"so destructive-guard failures cannot transition the issue into ai:implementation-failed"
	)


def test_failure_comment_step_skips_destructive_blocked_runs() -> None:
	wf = _workflow_text()
	assert (
		"if: (failure() || cancelled()) && steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' "
		"&& steps.commit_changes.outputs.destructive_commit_blocked == ''"
	) in wf, (
		"Generic failure comment flow must be disabled for destructive-blocked runs to avoid "
		"re-adding ai:awaiting-approval"
	)


def test_preflight_destructive_guard_runs_before_validation_with_temp_index_contract() -> None:
	wf = _workflow_text()
	assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true" in wf
	assert -1 < wf.find("- name: Create implementation branch") < wf.find("- name: Preflight destructive-commit guard") < wf.find("- name: Validate syntax of changed files"), (
		"Destructive-delete preflight must run after branch creation and before syntax-validation tail work"
	)

	preflight_block = _step_block_text("Preflight destructive-commit guard")
	assert 'git read-tree HEAD' in preflight_block, (
		"Preflight destructive guard must project the would-be staged set from a temporary HEAD-seeded index"
	)
	assert 'git add -u -- "${add_u_excludes[@]}"' in preflight_block
	assert 'if [ "${ALLOW_WORKFLOW_EDITS:-false}" != "true" ] && git cat-file -e HEAD:.github/workflows >/dev/null 2>&1; then' in preflight_block
	assert 'git reset -q HEAD -- .github/workflows' in preflight_block
	assert 'git diff --cached --diff-filter=D --name-only' in preflight_block
	assert 'destructive_commit_blocked=canonical-source' in preflight_block
	assert 'destructive_commit_blocked=bulk-delete' in preflight_block


def test_self_repo_guards_use_exact_canonical_repo_match() -> None:
	stage_block = _step_block_text("Stage workflow support files")
	preflight_block = _step_block_text("Preflight destructive-commit guard")
	commit_step = _step_block_text("Commit changes")
	commit_helper = _implement_commit_script_text()

	for block in (stage_block, preflight_block):
		assert 'wf_source="shubhodeep1/coding-workflows"' in block
		assert 'if [ "${{ github.repository }}" = "${wf_source}" ]; then' in block
		assert 'if [[ "${{ github.repository }}" == *"/coding-workflows" ]]; then' not in block

	assert "bash scripts/implement_commit_changes.sh" in commit_step
	assert 'wf_source="shubhodeep1/coding-workflows"' in commit_helper
	assert 'if [ "${GITHUB_REPOSITORY:-}" = "${wf_source}" ]; then' in commit_helper
	assert 'if [[ "${GITHUB_REPOSITORY:-}" == *"/coding-workflows" ]]; then' not in commit_helper

	assert '`*/coding-workflows`' not in preflight_block
	assert '`*/coding-workflows`' not in commit_helper


def test_preflight_destructive_guard_fails_without_touching_the_real_index() -> None:
	with tempfile.TemporaryDirectory(prefix="test_preflight_destructive_guard_") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)
		(repo_dir / "agents.md").write_text("canonical\n", encoding="utf-8")
		_git(["git", "add", "agents.md"], cwd=repo_dir)
		_git(["git", "commit", "-m", "add canonical source"], cwd=repo_dir)
		(repo_dir / "agents.md").unlink()

		script = _render_github_expressions(_extract_run_script("Preflight destructive-commit guard"))
		github_output = repo_dir / "github_output.txt"
		fetched_manifest = repo_dir / "fetched_manifest.txt"
		fetched_manifest.write_text("", encoding="utf-8")
		env = os.environ.copy()
		env.update(
			{
				"GITHUB_OUTPUT": str(github_output),
				"GITHUB_REPOSITORY": "owner/repo",
				"GITHUB_RUN_ID": "777",
				"FETCHED_MANIFEST": str(fetched_manifest),
				"ALLOW_WORKFLOW_EDITS": "false",
				"ALLOW_BULK_DELETE": "false",
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": "",
			}
		)

		proc = _run_shell_script(script, cwd=repo_dir, env=env)
		assert proc.returncode != 0, "canonical-source deletions must fail fast in the preflight guard"
		output_text = github_output.read_text(encoding="utf-8")
		assert "destructive_commit_blocked=canonical-source" in output_text
		assert "destructive_commit_count=1" in output_text
		assert "agents.md" in output_text

		cached = subprocess.run(
			["git", "diff", "--cached", "--name-only"],
			cwd=str(repo_dir),
			check=True,
			capture_output=True,
			text=True,
		).stdout.strip()
		assert cached == "", "preflight guard must not dirty the real git index when it rejects"


def test_commit_helper_fails_closed_on_unsafe_fetched_manifest_paths() -> None:
	with tempfile.TemporaryDirectory(prefix="test_commit_manifest_guard_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "repo"
		outside_path = tmp_path / "outside.txt"
		_bootstrap_git_repo(repo_dir)
		(repo_dir / "scripts").mkdir()
		shutil.copy2(IMPLEMENT_COMMIT_SCRIPT, repo_dir / "scripts" / "implement_commit_changes.sh")
		outside_path.write_text("keep\n", encoding="utf-8")
		github_output = repo_dir / "github_output.txt"
		github_output.write_text("", encoding="utf-8")
		runtime_dir = repo_dir / "runtime"
		runtime_dir.mkdir()
		fetched_manifest = repo_dir / "fetched_manifest.txt"
		fetched_manifest.write_text("../outside.txt\n", encoding="utf-8")
		env = os.environ.copy()
		env.update(
			{
				"FETCHED_MANIFEST": str(fetched_manifest),
				"GITHUB_OUTPUT": str(github_output),
				"GITHUB_REPOSITORY": "owner/repo",
				"ISSUE_NUMBER": "948",
				"RUNTIME_DIR": str(runtime_dir),
				"SERENA_PROJECT_BOOTSTRAP_HASH": "",
				"SERENA_PROJECT_PREEXISTED": "false",
				"TMPDIR": str(runtime_dir),
			}
		)

		proc = _run_shell_script("bash scripts/implement_commit_changes.sh\n", cwd=repo_dir, env=env)
		assert proc.returncode != 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert outside_path.read_text(encoding="utf-8") == "keep\n"
		output_text = github_output.read_text(encoding="utf-8")
		assert "destructive_commit_blocked=unsafe-fetched-manifest" in output_text
		assert "destructive_commit_count=1" in output_text
		assert "../outside.txt" in output_text
		assert "unsafe cleanup path" in (proc.stdout + proc.stderr)


def test_commit_helper_treats_unset_fetched_manifest_as_empty() -> None:
	with tempfile.TemporaryDirectory(prefix="test_commit_manifest_unset_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "repo"
		_bootstrap_git_repo(repo_dir)
		(repo_dir / "scripts").mkdir()
		shutil.copy2(IMPLEMENT_COMMIT_SCRIPT, repo_dir / "scripts" / "implement_commit_changes.sh")
		_git(["git", "add", "scripts/implement_commit_changes.sh"], cwd=repo_dir)
		_git(["git", "commit", "-m", "add helper"], cwd=repo_dir)
		github_output = tmp_path / "github_output.txt"
		github_output.write_text("", encoding="utf-8")
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir()
		env = _isolated_test_env(
			{
				"GITHUB_OUTPUT": str(github_output),
				"GITHUB_REPOSITORY": "owner/repo",
				"ISSUE_NUMBER": "948",
				"RUNTIME_DIR": str(runtime_dir),
				"SERENA_PROJECT_BOOTSTRAP_HASH": "",
				"SERENA_PROJECT_PREEXISTED": "false",
				"TMPDIR": str(runtime_dir),
			},
			cwd=repo_dir,
		)

		proc = subprocess.run(
			["bash", "scripts/implement_commit_changes.sh"],
			cwd=str(repo_dir),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert "No repository changes were produced by Codex implementation" in proc.stdout
		assert "did_commit=false" in github_output.read_text(encoding="utf-8")


def test_validate_step_uses_reusable_validator_with_continue_on_error() -> None:
	validate_block = _step_block_text("Validate syntax of changed files")
	assert "continue-on-error: true" in validate_block
	assert "bash scripts/validate_changed_files_syntax.sh" in validate_block


def test_post_codex_syntax_repair_step_contract() -> None:
	wf = _workflow_text()
	assert "MAX_POST_CODEX_REPAIR_ATTEMPTS: ${{ vars.MAX_POST_CODEX_REPAIR_ATTEMPTS || '3' }}" in wf

	repair_block = _step_block_text("Attempt post-Codex syntax repair")
	assert "steps.validate_syntax_changed_files.outcome == 'failure'" in repair_block
	assert "prompts/mode-implement-repair.txt" in repair_block
	assert "scripts/validate_changed_files_syntax.sh" in repair_block
	assert "MAX_POST_CODEX_REPAIR_ATTEMPTS" in repair_block
	assert "[ \"${max_attempts_raw}\" -lt 0 ]" in repair_block
	assert "if [ \"${max_attempts}\" -eq 0 ]; then" in repair_block
	assert "BASELINE_COMMIT=\"$(git stash create" in repair_block
	assert "PRE_UNTRACKED_FILE=\"${RUNTIME_DIR}/post_codex_pre_untracked_attempt_" in repair_block
	assert "Required repair artifacts are missing from repair-prompt-and-validator-split dependency." in repair_block
	assert 'SERENA_TOOL_HINTS="${REPAIR_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh "${REPAIR_PROMPT_TEMPLATE}"' in repair_block
	assert 'Failed to render repair prompt template ${REPAIR_PROMPT_TEMPLATE}; using raw prompt.' in repair_block
	assert "Keep apply_patch as the primary write path for repository edits" in repair_block


def test_syntax_failure_requires_successful_repair_before_commit_path() -> None:
	enforce_block = _step_block_text("Enforce syntax validation outcome")
	assert "steps.validate_syntax_changed_files.outcome" in enforce_block
	assert "steps.post_codex_syntax_repair.outputs.repaired" in enforce_block
	assert "Syntax validation failed and post-Codex repair did not recover." in enforce_block


def test_telegram_failure_step_skips_destructive_blocked_runs() -> None:
	telegram_block = _step_block_text("Telegram failure notification")
	assert "if: (failure() || cancelled()) && steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' && steps.commit_changes.outputs.destructive_commit_blocked == ''" in telegram_block, (
		"Post-failure Telegram flow must be skipped for destructive-blocked runs; only the dedicated "
		"destructive-guard CRITICAL alert should fire"
	)


def test_destructive_guard_path_does_not_set_implementation_failed_or_fixup_flow() -> None:
	destructive_block = _step_block_text("Destructive-commit guard — label + alert on rejection")
	lowered = destructive_block.lower()
	assert "steps.preflight_destructive_guard.outputs.destructive_commit_blocked != '' || steps.commit_changes.outputs.destructive_commit_blocked != ''" in destructive_block, (
		"Dedicated destructive guard handler must trigger for both early preflight and late commit rejections"
	)
	assert "steps.preflight_destructive_guard.outputs.destructive_commit_blocked || steps.commit_changes.outputs.destructive_commit_blocked" in destructive_block, (
		"Dedicated destructive guard handler must source its reason from either the preflight or commit guard output"
	)
	assert "--add-label 'ai:destructive-blocked'" in destructive_block, (
		"Destructive guard must preserve ai:destructive-blocked human-halt signaling"
	)
	assert "ai:implementation-failed" not in destructive_block, (
		"Destructive guard block must not apply ai:implementation-failed"
	)
	assert "fix-up" not in lowered and "fixup" not in lowered, (
		"Destructive guard block must not trigger fix-up issue generation"
	)

	capture_block = _step_block_text("Capture post-Codex validation errors")
	assert "if: (failure() || cancelled()) && steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' && steps.commit_changes.outputs.destructive_commit_blocked == ''" in capture_block, (
		"Captured validation diagnostics must not run for destructive-blocked failures"
	)

	diagnose_block = _step_block_text("Diagnose post-Codex failure and create fix-up issues")
	assert "if: (failure() || cancelled()) && steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' && steps.commit_changes.outputs.destructive_commit_blocked == ''" in diagnose_block, (
		"Diagnose/fix-up automation must be skipped for destructive-blocked runs"
	)

	diagnose_script = (REPO_ROOT / "scripts" / "implement_diagnose_post_codex_failure.sh").read_text(encoding="utf-8")
	assert 'FIX_COUNT="$(jq -r \'(.fix_issues // []) | if type == "array" then length else 0 end\' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"' in diagnose_script, (
		"needs_fixes handling must tolerate non-array fix_issues without aborting"
	)

def test_destructive_guard_knobs_wired_from_repo_variables() -> None:
	# RC-1 regression guard: both destructive-guard shells read these knobs
	# as bare ${VAR:-default}, so they only take effect when bound at the
	# workflow env level (vars.* resolving to the caller/consumer repo).
	# Mirrors the MAX_POST_CODEX_REPAIR_ATTEMPTS wiring assertion above.
	wf = _workflow_text()
	assert "ALLOW_BULK_DELETE: ${{ vars.ALLOW_BULK_DELETE || 'false' }}" in wf
	assert "BULK_DELETE_THRESHOLD: ${{ vars.BULK_DELETE_THRESHOLD || '3' }}" in wf
	assert "BULK_DELETE_THRESHOLD_MD: ${{ vars.BULK_DELETE_THRESHOLD_MD || '100' }}" in wf


def test_destructive_guard_latch_verifies_label_applied() -> None:
	# RC-2 regression guard: the latch must confirm ai:destructive-blocked
	# actually applied, distinguish a failed verification read from a
	# genuinely missing label, and avoid the old fire-and-forget
	# `gh issue view ... || true` false-negative path.
	destructive_block = _step_block_text("Destructive-commit guard — label + alert on rejection")
	assert "if latched_labels=\"$(gh issue view \"${ISSUE_NUMBER}\" --repo \"${{ github.repository }}\" --json labels -q '.labels[].name' 2>/dev/null)\"; then" in destructive_block
	assert "gh issue view \"${ISSUE_NUMBER}\" --repo \"${{ github.repository }}\" --json labels -q '.labels[].name' 2>/dev/null || true" not in destructive_block
	assert "::warning::Could not verify ai:destructive-blocked" in destructive_block
	assert "::error::FAILED to latch ai:destructive-blocked" in destructive_block
	assert "The workflow attempted to apply \\`ai:destructive-blocked\\`; verify that the label is present before relying on the \\`Validate approval phase\\` redispatch block." in destructive_block
	assert "remove \\`ai:destructive-blocked\\` from the issue if it is present and redispatch" in destructive_block
	assert destructive_block.count(
		"--description 'Implementation blocked for mass/destructive deletions; this issue ID now waits for human review'"
	) == 4
	assert "(gh label edit ai:destructive-blocked --repo ${{ github.repository }} --color b60205" in destructive_block
	assert "|| gh label create ai:destructive-blocked --repo ${{ github.repository }} --color b60205" in destructive_block
	assert ") && gh issue edit ${ISSUE_NUMBER} --repo ${{ github.repository }} --add-label ai:destructive-blocked" in destructive_block


def test_destructive_guard_handler_covers_unsafe_fetched_manifest_rejections() -> None:
	destructive_block = _step_block_text("Destructive-commit guard — label + alert on rejection")
	assert "unsafe-fetched-manifest" in destructive_block
	assert "artifact-cleanup manifest contained unsafe path(s)" in destructive_block
	assert "Unsafe manifest paths" in destructive_block
	assert "Inspect the runtime-fetched manifest producer" in destructive_block


def test_scope_guard_allowlist_and_workflow_rollback_contracts_present() -> None:
	commit_step = _step_block_text("Commit changes")
	commit_helper = _implement_commit_script_text()
	assert "bash scripts/implement_commit_changes.sh" in commit_step
	assert 'STEP_NAME="Commit changes"' not in commit_step
	assert "canonical_deletions" in commit_helper
	assert "ALLOW_WORKFLOW_EDITS" in commit_helper
	assert "destructive_commit_blocked=canonical-source" in commit_helper
	assert "destructive_commit_blocked=unsafe-fetched-manifest" in commit_helper
	assert "Non-numeric staged deletion count" in commit_helper
	assert "total_deletions=999999" in commit_helper
	assert "Non-numeric non-markdown deletion count" in commit_helper
	assert "non_md_count=1" in commit_helper

	protect_block = _step_block_text("Protect workflow files from implementation edits")
	assert 'git cat-file -e HEAD:.github/workflows >/dev/null 2>&1 || [ -d .github/workflows ]' in protect_block
	assert 'git cat-file -e HEAD:.github/workflows >/dev/null 2>&1' in protect_block
	assert "git restore --source=HEAD --staged --worktree .github/workflows" in protect_block
	assert "git clean -fd -- .github/workflows" in protect_block


def test_protect_workflow_files_restores_deleted_workflow_directory() -> None:
	with tempfile.TemporaryDirectory(prefix="test_protect_workflow_dir_") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)
		_copy_write_guard_assets(repo_dir)
		workflow_file = repo_dir / ".github" / "workflows" / "sample.yml"
		workflow_file.parent.mkdir(parents=True, exist_ok=True)
		workflow_file.write_text("name: sample\n", encoding="utf-8")
		_git(["git", "add", ".github/workflows/sample.yml"], cwd=repo_dir)
		_git(["git", "commit", "-m", "add workflow"], cwd=repo_dir)
		shutil.rmtree(workflow_file.parent)

		script = _render_github_expressions(_extract_run_script("Protect workflow files from implementation edits"))
		proc = _run_shell_script(script, cwd=repo_dir, env={**os.environ.copy(), "ALLOW_WORKFLOW_EDITS": "false"})
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert workflow_file.exists(), "workflow protection must restore tracked workflow files even when the directory was deleted"
		workflow_status = subprocess.run(
			["git", "status", "--porcelain", ".github/workflows"],
			cwd=str(repo_dir),
			env=_isolated_test_env(cwd=repo_dir),
			check=True,
			capture_output=True,
			text=True,
		).stdout.strip()
		assert workflow_status == "", "restored workflow tree should leave no pending .github/workflows changes"


def test_protect_workflow_files_cleans_untracked_workflow_directory_when_head_lacks_it() -> None:
	with tempfile.TemporaryDirectory(prefix="test_protect_workflow_dir_head_absent_") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)
		_copy_write_guard_assets(repo_dir)
		workflow_file = repo_dir / ".github" / "workflows" / "sample.yml"
		workflow_file.parent.mkdir(parents=True, exist_ok=True)
		workflow_file.write_text("name: sample\n", encoding="utf-8")

		script = _render_github_expressions(_extract_run_script("Protect workflow files from implementation edits"))
		proc = _run_shell_script(script, cwd=repo_dir, env={**os.environ.copy(), "ALLOW_WORKFLOW_EDITS": "false"})
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert not workflow_file.exists(), "workflow protection must clean untracked workflow files when HEAD has no tracked .github/workflows tree"
		workflow_status = subprocess.run(
			["git", "status", "--porcelain", ".github/workflows"],
			cwd=str(repo_dir),
			env=_isolated_test_env(cwd=repo_dir),
			check=True,
			capture_output=True,
			text=True,
		).stdout.strip()
		assert workflow_status == "", "untracked workflow tree should be removed cleanly when HEAD has no tracked workflows"


def test_successful_repair_path_still_flows_into_commit_gated_push_and_pr_steps() -> None:
	push_block = _step_block_text("Push branch")
	assert "if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'true'" in push_block
	assert "bash \"${health_script}\" repair" in push_block

	create_pr_block = _step_block_text("Create Pull Request")
	assert "if: env.SKIP_IMPLEMENT != 'true' && steps.commit_changes.outputs.did_commit == 'true'" in create_pr_block



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
		assert "Serena hints:" in stdin_prompt
		assert "Edit-tool discipline (apply_patch first with fallbacks" in stdin_prompt

		created_issues = state.get("created_issues", [])
		assert len(created_issues) == 1
		created = created_issues[0]
		assert created["repo"] == "owner/repo"
		assert created["title"] == "Repair syntax capture and parsing"
		assert "--label" in created["args"]
		assert created["args"].count("--label") == 2
		assert "ai:clarification" in created["args"]
		assert "ai:implement-fix-up" in created["args"]
		body = created["body"]
		assert "Type: implement-fix-up (post-codex-validation)" in body
		assert "Source issue: #948" in body
		assert f"Failed step: {failed_step}" in body

		created_labels = {entry.get("name") for entry in state.get("label_creates", [])}
		assert "ai:clarification" in created_labels
		assert "ai:implement-fix-up" in created_labels

		source_comments = [
			c.get("body", "")
			for c in state.get("api_comments", [])
			if c.get("issue") == "948"
		]
		assert source_comments, "expected a source-issue summary comment"
		match = re.search(
			r"<!-- IMPLEMENT_FIXUP_BLOCKERS_V1\n(.*?)\nIMPLEMENT_FIXUP_BLOCKERS_V1 -->",
			source_comments[-1],
			flags=re.S,
		)
		assert match is not None, "expected implement blocker metadata marker"
		blocker_payload = json.loads(match.group(1))
		assert blocker_payload["blocks_source_issue"] == 948
		assert blocker_payload["fixup_issue_numbers"] == [1001]

		labels = state.get("issue_labels", [])
		assert "ai:implementation-failed" in labels
		assert "ai:awaiting-approval" not in labels


def test_diagnose_invokes_codex_when_capture_exists_and_issue_is_not_already_failed():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		proc, _state, _runtime_dir, paths = _run_diagnose_step(
			tmp_path,
			issue_labels=["ai:implementing"],
			capture_contents="===== broken.yml =====\nerror\n",
			codex_mode="success",
			codex_output={
				"status": "needs_fixes",
				"diagnosis": "diag",
				"fix_issues": [{"id": "fix-1", "title": "Fix 1", "body": "Body", "priority": 1, "depends_on": []}],
				"harness_fixes": "",
			},
			failed_step_name="Validate syntax of changed files",
			issue_body="Tracking issue: #829\n",
		)

		assert proc.returncode == 0
		assert "handled=true" in _read_file(paths["github_output"])
		call_lines = [line for line in _read_file(paths["calls_file"]).splitlines() if line.strip()]
		assert len(call_lines) == 1
		call_args = json.loads(call_lines[0])
		assert "exec" in call_args
		assert "--model" in call_args


def test_diagnose_reuses_matching_issue_meta_body_without_issue_api_fallback() -> None:
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		issue_body = "Issue context\n\nTracking issue: #829\n"
		proc, state, runtime_dir, paths = _run_diagnose_step(
			tmp_path,
			issue_labels=["ai:implementing"],
			capture_contents="===== broken.yml =====\nerror\n",
			codex_mode="success",
			codex_output={
				"status": "harness_error",
				"diagnosis": "diag",
				"fix_issues": [],
				"harness_fixes": "rerun validator",
			},
			failed_step_name="Validate syntax of changed files",
			issue_body=issue_body,
			issue_meta_payload={
				"number": 948,
				"labels": [{"name": "ai:implementing"}],
				"body": issue_body,
			},
			write_issue_body_file=False,
		)

		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert state.get("issue_queries", []) == [], (
			"Matching ISSUE_META_FILE should satisfy diagnose label/body reads without hitting /issues/{n}"
		)
		assert _read_file(str(runtime_dir / "issue_body_from_api.txt")) == issue_body
		assert issue_body.strip() in _read_file(paths["stdin_file"])


def test_diagnose_uses_safe_issue_api_body_fallback_when_issue_meta_invalid_or_mismatched() -> None:
	for case_name, issue_meta_payload in (
		(
			"mismatched",
			{
				"number": 999,
				"labels": [{"name": "ai:implementing"}],
				"body": "wrong issue body\n",
			},
		),
		("invalid", '{"number": 948, "labels": ['),
	):
		with tempfile.TemporaryDirectory(prefix=f"test_diag_{case_name}_") as td:
			tmp_path = Path(td)
			issue_body = "Issue context\n\nTracking issue: #829\n"
			proc, state, runtime_dir, _paths = _run_diagnose_step(
				tmp_path,
				issue_labels=["ai:implementing"],
				capture_contents="===== broken.yml =====\nerror\n",
				codex_mode="success",
				codex_output={
					"status": "harness_error",
					"diagnosis": "diag",
					"fix_issues": [],
					"harness_fixes": "rerun validator",
				},
				failed_step_name="Validate syntax of changed files",
				issue_body=issue_body,
				issue_meta_payload=issue_meta_payload,
				write_issue_body_file=False,
			)

			assert proc.returncode == 0, f"case={case_name}\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			assert state.get("issue_queries", []) == [
				"repos/owner/repo/issues/948",
			], f"case={case_name}"
			issue_api_calls = [
				call
				for call in state.get("calls", [])
				if call
				and call[0] == "api"
				and "repos/owner/repo/issues/948" in call
			]
			assert ["api", "repos/owner/repo/issues/948"] in issue_api_calls, (
				f"case={case_name}: body fallback must use the safe _safe_gh_jq JSON fetch without --jq"
			)
			assert all("--jq" not in call for call in issue_api_calls), (
				f"case={case_name}: memoized fallback should reuse the same non--jq issue payload for labels and body"
			)
			assert _read_file(str(runtime_dir / "issue_body_from_api.txt")) == issue_body, (
				f"case={case_name}: body fallback should still materialize plain-text issue body content"
			)


def test_diagnose_retries_issue_api_after_exhausted_label_fetch_failures() -> None:
	previous_retry_budget = os.environ.get("GH_RETRY_MAX_ATTEMPTS")
	os.environ["GH_RETRY_MAX_ATTEMPTS"] = "2"
	try:
		with tempfile.TemporaryDirectory(prefix="test_diag_retry_") as td:
			tmp_path = Path(td)
			issue_body = "Issue context\n\nTracking issue: #829\n"
			proc, state, runtime_dir, paths = _run_diagnose_step(
				tmp_path,
				issue_labels=["ai:implementing"],
				capture_contents="===== broken.yml =====\nerror\n",
				codex_mode="success",
				codex_output={
					"status": "harness_error",
					"diagnosis": "diag",
					"fix_issues": [],
					"harness_fixes": "rerun validator",
				},
				failed_step_name="Validate syntax of changed files",
				issue_body=issue_body,
				issue_meta_payload='{"number": 948, "labels": [',
				write_issue_body_file=False,
				issue_api_failures_remaining=2,
			)

			assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			assert state.get("issue_queries", []) == [
				"repos/owner/repo/issues/948",
				"repos/owner/repo/issues/948",
				"repos/owner/repo/issues/948",
			]
			assert _read_file(str(runtime_dir / "issue_body_from_api.txt")) == issue_body
			assert issue_body.strip() in _read_file(paths["stdin_file"])
	finally:
		if previous_retry_budget is None:
			os.environ.pop("GH_RETRY_MAX_ATTEMPTS", None)
		else:
			os.environ["GH_RETRY_MAX_ATTEMPTS"] = previous_retry_budget


def test_diagnose_posts_dependency_notes_for_fix_issue_edges():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		proc, state, _runtime_dir, _paths = _run_diagnose_step(
			tmp_path,
			issue_labels=["ai:implementing"],
			capture_contents="===== broken.yml =====\nerror\n",
			codex_mode="success",
			codex_output={
				"status": "needs_fixes",
				"diagnosis": "diag",
				"fix_issues": [
					{"id": "fix-a", "title": "Fix A", "body": "Body A", "priority": 1, "depends_on": []},
					{"id": "fix-b", "title": "Fix B", "body": "Body B", "priority": 2, "depends_on": ["fix-a"]},
				],
				"harness_fixes": "",
			},
			failed_step_name="Validate syntax of changed files",
			issue_body="Tracking issue: #829\n",
		)

		assert proc.returncode == 0
		created_issues = state.get("created_issues", [])
		assert len(created_issues) == 2
		dep_comment = next((x for x in state.get("issue_comments", []) if x.get("issue") == "1002"), None)
		assert dep_comment is not None
		assert "Dependency Notes" in dep_comment.get("body", "")
		assert "#1001 (from fix-a)" in dep_comment.get("body", "")


def test_validator_capture_aggregates_multiple_files_before_nonzero_exit():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "validate-repo"
		_bootstrap_git_repo(repo_dir)

		validator_src = REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"
		validator_dst = repo_dir / "scripts" / "validate_changed_files_syntax.sh"
		validator_dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(validator_src, validator_dst)
		validator_dst.chmod(0o755)

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
		github_output = runtime_dir / "github_output.txt"
		env.update(
			{
				"RUNTIME_DIR": str(runtime_dir),
				"PATH": env.get("PATH", ""),
				"GITHUB_OUTPUT": str(github_output),
			}
		)

		proc = _run_shell_script(script, cwd=repo_dir, env=env)
		assert proc.returncode != 0, "expected syntax validator step script to fail on syntax errors"

		capture_file = runtime_dir / "post_codex_validation_errors.txt"
		assert capture_file.exists(), "expected capture file to be written"
		capture = capture_file.read_text(encoding="utf-8")
		assert "broken.py" in capture
		assert "python3 -m py_compile" in capture
		assert "broken.yml" in capture
		assert "python3 yaml.safe_load" in capture


def test_validator_scratch_repo_without_head_still_checks_untracked_files() -> None:
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "validate-repo"
		repo_dir.mkdir(parents=True, exist_ok=True)
		_git(["git", "init"], cwd=repo_dir)
		_git(["git", "config", "user.name", "tests"], cwd=repo_dir)
		_git(["git", "config", "user.email", "tests@example.com"], cwd=repo_dir)

		validator_src = REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"
		validator_dst = repo_dir / "scripts" / "validate_changed_files_syntax.sh"
		validator_dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(validator_src, validator_dst)
		validator_dst.chmod(0o755)

		(repo_dir / "broken.py").write_text("def nope(:\n\tpass\n", encoding="utf-8")
		capture_path = tmp_path / "captured.txt"
		env = _isolated_test_env(
			{
				"CAPTURE_FILE": str(capture_path),
				"ALLOW_WORKFLOW_EDITS": "true",
			},
			cwd=repo_dir,
		)

		result = subprocess.run(
			["bash", str(validator_dst)],
			cwd=str(repo_dir),
			env=env,
			capture_output=True,
			text=True,
		)

		assert result.returncode != 0, (
			"scratch repos without HEAD must still validate untracked files "
			"instead of silently succeeding"
		)
		combined = result.stdout + result.stderr
		assert "fatal: bad revision 'HEAD'" not in combined, (
			"the tolerated scratch-repo HEAD error must stay suppressed; "
			f"got:\n{combined}"
		)
		assert "::error file=broken.py::Syntax error in broken.py" in combined, (
			"the untracked python file must still be fed to py_compile in a "
			"scratch repo"
		)
		assert capture_path.exists(), "scratch-repo validation must still emit CAPTURE_FILE"
		assert "broken.py" in capture_path.read_text(encoding="utf-8")


def test_validator_surfaces_real_git_failures_instead_of_silently_skipping() -> None:
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "validate-repo"
		_bootstrap_git_repo(repo_dir)

		validator_src = REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"
		validator_dst = repo_dir / "scripts" / "validate_changed_files_syntax.sh"
		validator_dst.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(validator_src, validator_dst)
		validator_dst.chmod(0o755)

		missing_git_dir = tmp_path / "missing-git-dir"
		env = _isolated_test_env(
			{
				"ALLOW_WORKFLOW_EDITS": "true",
				"GIT_DIR": str(missing_git_dir),
			},
			cwd=repo_dir,
		)

		result = subprocess.run(
			["bash", str(validator_dst)],
			cwd=str(repo_dir),
			env=env,
			capture_output=True,
			text=True,
		)

		assert result.returncode != 0, "real git failures must abort the validator"
		combined = result.stdout + result.stderr
		assert "All changed files passed syntax validation." not in combined, (
			"real git failures must not degrade into a false success"
		)
		assert "Could not access 'HEAD'" in combined or "fatal:" in combined, (
			"the underlying git failure must still surface to stderr/stdout; "
			f"got:\n{combined}"
		)


def test_syntax_gate_step_fails_when_check_reports_unresolved_errors():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "validate-repo"
		_bootstrap_git_repo(repo_dir)

		script = _render_github_expressions(
			_extract_run_script("Enforce syntax validation outcome"),
			overrides={
				"steps.validate_syntax_changed_files.outcome": "failure",
				"steps.post_codex_syntax_repair.outputs.repaired || 'false'": "false",
			},
		)
		env = os.environ.copy()
		env.update(
			{
				"RUNTIME_DIR": str(tmp_path / "runtime"),
			}
		)

		proc = _run_shell_script(script, cwd=repo_dir, env=env)
		assert proc.returncode != 0, "expected syntax-enforcement step to fail when repair did not recover"
		assert "Syntax validation failed and post-Codex repair did not recover" in (proc.stderr + proc.stdout)


def test_syntax_gate_step_fails_when_check_did_not_report_status():
	with tempfile.TemporaryDirectory(prefix="test_diag_") as td:
		tmp_path = Path(td)
		repo_dir = tmp_path / "validate-repo"
		_bootstrap_git_repo(repo_dir)

		script = _render_github_expressions(
			_extract_run_script("Enforce syntax validation outcome"),
			overrides={
				"steps.validate_syntax_changed_files.outcome": "success",
				"steps.post_codex_syntax_repair.outputs.repaired || 'false'": "false",
			},
		)
		env = os.environ.copy()
		env.update(
			{
				"RUNTIME_DIR": str(tmp_path / "runtime"),
			}
		)

		proc = _run_shell_script(script, cwd=repo_dir, env=env)
		assert proc.returncode == 0, "expected syntax-enforcement step to pass when syntax validation succeeded"


def test_needs_fixes_labels_source_issue_and_generic_failure_step_is_bypassed():
	wf = _workflow_text()
	assert "--add-label 'ai:implementation-failed'" in wf
	assert "--remove-label 'ai:awaiting-approval'" in wf
	assert "if: (failure() || cancelled()) && steps.preflight_destructive_guard.outputs.destructive_commit_blocked == '' && steps.commit_changes.outputs.destructive_commit_blocked == '' && steps.diagnose_post_codex_failure.outputs.handled != 'true'" in wf


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
			assert created["args"].count("--label") == 2
			assert "ai:clarification" in created["args"]
			assert "ai:implement-fix-up" in created["args"]
			assert "ai:orchestrator-managed" not in created["args"]
			assert "The diagnose step could not produce a valid JSON contract" in created["body"]
			assert "yaml parse failed on alpha.yml" in created["body"]
			assert "Type: implement-fix-up (post-codex-validation)" in created["body"]
			assert "Tracking issue: #829" in created["body"]
			assert "ai:implementation-failed" in state.get("issue_labels", [])


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


def test_diagnose_reasoning_patch_preserves_serena_mcp_block() -> None:
	diagnose = (REPO_ROOT / "scripts" / "implement_diagnose_post_codex_failure.sh").read_text(encoding="utf-8")
	assert "top_level_lines = lines[:first_table_idx]" in diagnose
	assert "rest_lines = lines[first_table_idx:]" in diagnose
	assert 're.match(r"^(\\[[^\\]]+\\]|\\[\\[[^\\]]+\\]\\])(?:[ \\t]+#.*)?$", stripped)' in diagnose
	assert 'config_path.write_text("".join(updated_top + rest_lines), encoding="utf-8")' in diagnose
	assert 'if ! patch_diagnose_reasoning_into_config; then' in diagnose
	assert 'Failed to patch ~/.codex/config.toml for diagnose reasoning; leaving existing config unchanged.' in diagnose
	assert "[mcp_servers.serena]" not in diagnose.split("patch_diagnose_reasoning_into_config()", 1)[1].split("patch_diagnose_reasoning_into_config", 1)[0], (
		"Diagnose reasoning patch must update only the top-level model_reasoning_effort key without inlining Serena table rewrites"
	)


def test_repair_reasoning_patch_preserves_serena_mcp_block() -> None:
	repair_block = _extract_run_script("Attempt post-Codex syntax repair")
	repair_patcher = repair_block.split("upsert_repair_reasoning_into_config() {", 1)[1].split('if ! upsert_repair_reasoning_into_config', 1)[0]
	assert "\nfrom pathlib import Path\n" in repair_patcher
	assert "top_level_lines = lines[:first_table_idx]" in repair_patcher
	assert "rest_lines = lines[first_table_idx:]" in repair_patcher
	assert 're.match(r"^(\\[[^\\]]+\\]|\\[\\[[^\\]]+\\]\\])(?:[ \\t]+#.*)?$", stripped)' in repair_patcher
	assert 'config_path.write_text("".join(updated_top + rest_lines), encoding="utf-8")' in repair_patcher
	assert "[mcp_servers.serena]" not in repair_patcher


def test_repair_reasoning_heredoc_is_column_zero_after_yaml_strip() -> None:
	block = _step_block("Attempt post-Codex syntax repair")
	run_idx = next(i for i, line in enumerate(block) if line.strip() == "run: |")
	run_indent = len(block[run_idx]) - len(block[run_idx].lstrip(" "))
	opener_idx = next(
		i for i, line in enumerate(block)
		if 'PYTHONDONTWRITEBYTECODE=1 python3 - "${cfg}" "${REPAIR_REASONING}" <<\'PY\'' in line
	)
	body_line = block[opener_idx + 1]
	terminator_idx = next(i for i in range(opener_idx + 1, len(block)) if block[i].strip() == "PY")
	terminator_line = block[terminator_idx]
	assert len(body_line) - len(body_line.lstrip(" ")) == run_indent + 2, (
		"Repair heredoc Python must be flush with the run-block base indent so YAML stripping leaves Python at column 0"
	)
	assert len(terminator_line) - len(terminator_line.lstrip(" ")) == run_indent + 2, (
		"Repair heredoc terminator must share the run-block base indent so bash terminates <<'PY' correctly"
	)


def test_codex_pre_baseline_captured_before_retry_loop() -> None:
	codex_block = _step_block_text("Run Codex implementation")
	assert 'CODEX_PRE_BASELINE="${RUNTIME_DIR}/codex_pre_baseline.txt"' in codex_block, (
		"Pre-Codex baseline snapshot must be captured so detection/retry-nudge/no-op "
		"handlers can diff only real Codex-produced deltas"
	)
	assert 'git status --porcelain -uall | filter_runtime_status_noise > "${CODEX_PRE_BASELINE}"' in codex_block, (
		"Baseline must be written with --porcelain -uall so untracked directories "
		"expand to stable per-file lines while filtering runtime Serena noise"
	)
	# The baseline must be captured before the retry loop so it doesn't drift
	# between attempts.
	baseline_idx = codex_block.find('CODEX_PRE_BASELINE="${RUNTIME_DIR}/codex_pre_baseline.txt"')
	loop_idx = codex_block.find('for attempt in $(seq 1 "${max_attempts}"); do')
	assert baseline_idx != -1 and loop_idx != -1 and baseline_idx < loop_idx, (
		"CODEX_PRE_BASELINE must be captured BEFORE the attempt loop"
	)


def test_success_noop_flag_cleared_before_retry_loop() -> None:
	codex_block = _step_block_text("Run Codex implementation")
	# Proactive cleanup prevents a stale flag from a prior run_attempt (which
	# shares GITHUB_RUN_ID and therefore RUNTIME_DIR) from tricking Guard 0
	# into closing an issue this attempt never classified as success-no-op.
	assert 'rm -f "${RUNTIME_DIR}/codex_success_noop.flag"' in codex_block, (
		"Stale success-no-op flag must be cleared before the retry loop so a "
		"rerun on a non-ephemeral runner cannot falsely trigger Guard 0"
	)
	cleanup_idx = codex_block.find('rm -f "${RUNTIME_DIR}/codex_success_noop.flag"')
	loop_idx = codex_block.find('for attempt in $(seq 1 "${max_attempts}"); do')
	assert cleanup_idx != -1 and loop_idx != -1 and cleanup_idx < loop_idx, (
		"Success-no-op flag cleanup must happen BEFORE the attempt loop"
	)


def test_codex_success_detection_uses_baseline_diff() -> None:
	codex_block = _step_block_text("Run Codex implementation")
	assert 'filter_runtime_status_noise() {' in codex_block, (
		"Retry and success detection must share the Serena runtime-noise filter helper"
	)
	# Retry-nudge check must use baseline-filtered delta, not raw porcelain.
	assert 'codex_retry_delta="$(git status --porcelain -uall | filter_runtime_status_noise | grep -vxFf "${CODEX_PRE_BASELINE}"' in codex_block, (
		"Retry-nudge must use baseline-filtered delta so workflow bootstrap artifacts "
		"(.codex-workflow-src*, .serena/project.yml) don't falsely trigger the 'modify files' nudge"
	)
	# Success detection must also use baseline-filtered delta.
	assert 'codex_delta="$(git status --porcelain -uall | filter_runtime_status_noise | grep -vxFf "${CODEX_PRE_BASELINE}"' in codex_block, (
		"Success-detection must use baseline-filtered delta so bootstrap artifacts "
		"don't produce a false-positive 'Codex changed files' signal"
	)
	# Success-no-op flag must be written when delta is empty and output matches
	# an 'already implemented' pattern.
	assert ': > "${RUNTIME_DIR}/codex_success_noop.flag"' in codex_block, (
		"Success-no-op branch must touch the flag file so Handle no-op "
		"implementation can short-circuit to ai:closed instead of re-issuing"
	)
	assert 'no file changes were made|nothing to change|already' in codex_block, (
		"Success-no-op detection must match Codex's explicit 'already implemented' signals"
	)
	# The regex must cover multiple common completion phrasings so robustness
	# doesn't depend on a single LLM wording.
	for phrase in ("implemented", "done", "exists", "present", "complete"):
		assert phrase in codex_block, (
			f"Success-no-op regex should include the '{phrase}' completion signal"
		)
	# Phrases added after observed Codex outputs ("No repository changes
	# made.", "already satisfied.") on issue #1768 — keep them covered so
	# a future regex tightening can't silently regress them.
	for phrase in (
		"satisfied",
		"no repository changes",
		"no file changes made",
		"no repository changes (were )?required",
		"no files (were )?modified",
		"no repository changes (were )?needed",
		"no file changes (were )?needed",
	):
		assert phrase in codex_block, (
			f"Success-no-op regex should include the '{phrase}' completion signal "
			f"(observed Codex phrasings on issues #1768, #1816, #1859)"
		)


def test_serena_runtime_artifact_filter_uses_bootstrap_hash_and_commit_cleanup_is_content_aware() -> None:
	codex_block = _step_block_text("Run Codex implementation")
	assert 'SERENA_PROJECT_BOOTSTRAP_HASH' in _workflow_text(), (
		"Workflow must export a Serena bootstrap hash for runtime-noise filtering"
	)
	assert "bootstrap-owned .serena/ state" in codex_block, (
		"Runtime-noise filter should document that bootstrap-owned .serena state is filtered together"
	)
	assert "*' .serena/'*|*' .serena')" in codex_block, (
		"Run Codex implementation must filter bootstrap-owned .serena directory entries, not only project.yml"
	)
	assert 'current_hash="$(sha256sum .serena/project.yml' in codex_block, (
		"Run Codex implementation must hash .serena/project.yml when deciding whether it is only runtime noise"
	)
	assert 'if [ -n "${current_hash}" ] && [ "${current_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then' in codex_block, (
		"Only the unchanged bootstrap Serena project file should be filtered from no-op detection"
	)

	commit_helper = _implement_commit_script_text()
	assert 'current_serena_project_hash="$(sha256sum .serena/project.yml' in commit_helper, (
		"Commit cleanup must compare the current Serena project file against the bootstrap hash before deleting it"
	)
	assert 'if [ -n "${current_serena_project_hash}" ] && [ "${current_serena_project_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then' in commit_helper, (
		"Commit cleanup must preserve .serena/project.yml when Codex changed it"
	)
	assert 'if ! git ls-files --error-unmatch -- .serena >/dev/null 2>&1; then' in commit_helper, (
		"Commit cleanup must never remove a git-tracked .serena tree"
	)
	assert 'rm -rf .serena' in commit_helper, (
		"Commit cleanup must remove the full bootstrap-owned .serena directory once the project hash still matches"
	)


def test_serena_runtime_filter_and_cleanup_preserve_mutated_tree_and_drop_unchanged_bootstrap_state() -> None:
	with tempfile.TemporaryDirectory(prefix="test_serena_runtime_") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)

		run_codex_script = _extract_run_script("Run Codex implementation")
		filter_helper = "filter_runtime_status_noise() {" + run_codex_script.split("filter_runtime_status_noise() {", 1)[1].split(
			"\n\n# ────────────────────────────────────────────────────────────────", 1
		)[0]
		commit_script = _implement_commit_script_text()
		cleanup_start = 'if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && [ -f .serena/project.yml ]; then'
		cleanup_snippet = cleanup_start + commit_script.split(cleanup_start, 1)[1].split('porcelain_status="$(git status --porcelain)"', 1)[0]

		def _write_serena_tree(project_body: str) -> str:
			project_path = repo_dir / ".serena" / "project.yml"
			cache_path = repo_dir / ".serena" / "cache" / "state.json"
			cache_path.parent.mkdir(parents=True, exist_ok=True)
			project_path.write_text(project_body, encoding="utf-8")
			cache_path.write_text('{"runtime": true}\n', encoding="utf-8")
			return hashlib.sha256(project_path.read_bytes()).hexdigest()

		def _run_inline_bash(script: str, *, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
			env = _isolated_test_env(extra_env, cwd=repo_dir)
			return subprocess.run(
				["bash", "-s"],
				cwd=str(repo_dir),
				env=env,
				input=script,
				text=True,
				capture_output=True,
				timeout=60,
			)

		filter_script = "set -euo pipefail\n" + filter_helper + "\ngit status --porcelain -uall | filter_runtime_status_noise\n"
		cleanup_script = "set -euo pipefail\n" + cleanup_snippet + "\n"

		bootstrap_hash = _write_serena_tree("project_name: bootstrap\n")
		filter_result = _run_inline_bash(
			filter_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert filter_result.returncode == 0, filter_result.stderr
		assert ".serena/" not in filter_result.stdout, (
			"Unchanged bootstrap-owned .serena state should be filtered out of the no-op baseline delta"
		)

		(repo_dir / ".serena" / "project.yml").write_text("project_name: mutated\n", encoding="utf-8")
		mutated_filter_result = _run_inline_bash(
			filter_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert mutated_filter_result.returncode == 0, mutated_filter_result.stderr
		assert ".serena/project.yml" in mutated_filter_result.stdout, (
			"Changing project.yml must stop the filter from hiding .serena/project.yml"
		)
		assert ".serena/cache/state.json" in mutated_filter_result.stdout, (
			"Changing project.yml must stop the filter from hiding sibling .serena runtime files"
		)

		shutil.rmtree(repo_dir / ".serena")
		bootstrap_hash = _write_serena_tree("project_name: cleanup-bootstrap\n")
		cleanup_result = _run_inline_bash(
			cleanup_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert cleanup_result.returncode == 0, cleanup_result.stderr
		assert not (repo_dir / ".serena").exists(), (
			"Commit cleanup must drop the unchanged bootstrap-owned .serena tree before staging"
		)

		bootstrap_hash = _write_serena_tree("project_name: cleanup-mutated\n")
		(repo_dir / ".serena" / "project.yml").write_text("project_name: cleanup-mutated-again\n", encoding="utf-8")
		preserved_cleanup_result = _run_inline_bash(
			cleanup_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert preserved_cleanup_result.returncode == 0, preserved_cleanup_result.stderr
		assert (repo_dir / ".serena" / "project.yml").is_file(), (
			"Commit cleanup must preserve .serena/project.yml once its content diverges from the bootstrap hash"
		)
		assert (repo_dir / ".serena" / "cache" / "state.json").is_file(), (
			"Commit cleanup must preserve sibling .serena files once project.yml diverges from the bootstrap hash"
		)

		shutil.rmtree(repo_dir / ".serena")
		bootstrap_hash = _write_serena_tree("project_name: tracked-bootstrap\n")
		_git(["git", "add", ".serena/cache/state.json"], cwd=repo_dir)
		tracked_cleanup_result = _run_inline_bash(
			cleanup_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert tracked_cleanup_result.returncode == 0, tracked_cleanup_result.stderr
		assert (repo_dir / ".serena").is_dir(), (
			"Commit cleanup must preserve git-tracked .serena content even when the bootstrap hash still matches"
		)


def test_serena_preexistence_detection_runs_after_checkout() -> None:
	workflow = _workflow_text()
	assert 'echo "SERENA_PROJECT_PREEXISTED=false"' in _step_block_text("Create runtime workspace"), (
		"Runtime workspace should default Serena preexistence to false until checkout makes the repo visible"
	)
	detect_block = _step_block_text("Detect preexisting Serena project config")
	assert 'git ls-files --error-unmatch -- .serena/project.yml' in detect_block, (
		"Serena preexistence detection must query the checked-out repo, not the pre-checkout workspace"
	)
	assert workflow.find("- name: Checkout repository") < workflow.find("- name: Detect preexisting Serena project config") < workflow.find("- name: Setup Serena"), (
		"Serena preexistence detection must run after checkout and before Setup Serena records bootstrap hashes"
	)


def test_stage_support_files_preserves_consumer_owned_serena_template() -> None:
	stage_block = _step_block_text("Stage workflow support files")
	assert 'elif [ "${is_self_repo}" = "false" ] && git ls-files --error-unmatch -- "scripts/templates/serena_project.yml.j2" >/dev/null 2>&1; then' in stage_block, (
		"Stage workflow support files must preserve a consumer-owned Serena template instead of overwriting it"
	)
	assert "preserving caller-owned Serena template" in stage_block, (
		"Preserved consumer Serena templates should emit an audit notice"
	)


def test_retry_prompt_includes_exec_history_recap() -> None:
	"""Pin the cross-attempt exec-history recap so a future Codex CLI
	output-format change (or accidental edit to the awk parser) cannot
	silently disable the feature.

	The recap is critical for multi-file issues with xhigh reasoning:
	without it, every retry redoes the same recon and the wall-time
	budget runs out before any edit lands (observed on bitsafe.io
	issue #26 — see the agents.md historical note).
	"""
	codex_block = _step_block_text("Run Codex implementation")

	# (a) The retry-prompt expansion block must be present in the step.
	#     These are the marker strings the model sees on retry; if any
	#     drifts, the recap stops being an effective nudge.
	assert "codex_prompt_retry.txt" in codex_block, (
		"Retry-prompt path must be referenced so the nudged prompt is built"
	)
	assert "Prior-attempt exploration recap" in codex_block, (
		"Retry prompt must include the explicit exploration-recap header so the "
		"model knows the bullet list is what it has already tried"
	)
	assert "DO NOT redo these — go straight to editing" in codex_block, (
		"Recap header must instruct the model to skip re-exploration and edit"
	)
	assert "apply_patch or printf shell exec" in codex_block, (
		"Recap must point the model at the actual editing tools to use"
	)
	assert "Do NOT narrate — call a write tool" in codex_block, (
		"Recap must reinforce that narrating is not the same as a tool call"
	)

	# (b) The awk parser must match the Codex CLI stderr format we
	#     observe in production. We extract the parser fragment from the
	#     workflow and run it against a synthetic codex_log fixture that
	#     mirrors what Codex CLI prints (verified against real bitsafe.io
	#     run logs). If Codex CLI changes its `exec\n<cmd>` format, this
	#     test fails loudly instead of the recap silently going empty.
	assert "/^exec$/" in codex_block, (
		"awk pattern must anchor on `exec` lines as Codex CLI prints them"
	)
	assert "!seen[display]++" in codex_block, (
		"Dedup must be order-preserving (awk associative array), not sort -u, "
		"so the recap shows commands in the chronological order they were tried. "
		"(`display` rather than `cmd` because long commands are truncated to a "
		"display string before dedup so two identical truncated calls dedup as one)"
	)
	assert "${RUNTIME_DIR}/codex_log.txt" in codex_block, (
		"Parser must read codex_log.txt — the file accumulates across attempts "
		"via `tee -a` so attempt N+1 sees attempt 1..N's exec calls"
	)

	# (c) Run the actual awk command against a synthetic log to verify
	#     the parser still extracts what we expect. Pattern lifted
	#     verbatim from implement.yml so a regex/format edit there
	#     surfaces here as a parse mismatch.
	#
	#     The fixture intentionally exercises suffix variation that
	#     real Codex CLI logs exhibit (different `succeeded in <N>ms:`
	#     elapsed values, `exited <code> in <N>ms:` failure forms,
	#     non-zero ms values) so a parser that accidentally hard-codes
	#     `succeeded in 0ms:` would fail this test.
	# A long synthetic command (>500 chars) that mirrors MCP tool-call
	# bash serializations — these get serialized into one line and
	# routinely exceed 500 chars. The parser must show them (truncated)
	# rather than drop them, otherwise the model can't see they were
	# already tried and re-runs them on retry.
	long_cmd = "/bin/bash -lc 'rg -n find_symbol --name " + ("X" * 600) + "' in /repo succeeded in 99ms:"
	assert len(long_cmd) > 500, "long_cmd fixture must exceed 500 chars to exercise truncation"
	codex_log_fixture = (
		"some startup chatter\n"
		"exec\n"
		"/bin/bash -lc 'ls -1' in /repo succeeded in 0ms:\n"
		"file_a\n"
		"file_b\n"
		"exec\n"
		"/bin/bash -lc 'rg -n PATTERN_A' in /repo succeeded in 47ms:\n"
		"matches…\n"
		"exec\n"
		"/bin/bash -lc 'rg -n MISSING' in /repo exited 1 in 12ms:\n"
		"exec\n"
		"/bin/bash -lc 'rg -n PATTERN_A' in /repo succeeded in 47ms:\n"
		"duplicate of attempt 2 — should dedup\n"
		"exec\n"
		f"{long_cmd}\n"
		"exec\n"
		"/bin/bash -lc 'cat foo' in /repo succeeded in 3ms:\n"
		"tokens used 12345\n"
	)
	with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
		f.write(codex_log_fixture)
		log_path = f.name
	try:
		# Pattern verbatim from implement.yml retry-nudge block.
		awk_program = (
			'/^exec$/ { getline cmd; '
			'if (length(cmd) > 0) { '
			'display = (length(cmd) > 500) ? substr(cmd, 1, 500) " …[truncated]" : cmd; '
			'if (!seen[display]++) print "  - " display } }'
		)
		result = subprocess.run(
			["awk", awk_program, log_path],
			capture_output=True,
			text=True,
			check=True,
			timeout=10,
		)
	finally:
		os.unlink(log_path)
	lines = [line for line in result.stdout.splitlines() if line]
	expected_long_recap = "  - " + long_cmd[:500] + " …[truncated]"
	assert lines == [
		"  - /bin/bash -lc 'ls -1' in /repo succeeded in 0ms:",
		"  - /bin/bash -lc 'rg -n PATTERN_A' in /repo succeeded in 47ms:",
		"  - /bin/bash -lc 'rg -n MISSING' in /repo exited 1 in 12ms:",
		expected_long_recap,
		"  - /bin/bash -lc 'cat foo' in /repo succeeded in 3ms:",
	], (
		"awk parser must (1) capture each `exec`-followed command line "
		"regardless of trailing-suffix variability (succeeded vs exited, "
		"different ms values), (2) dedup duplicates while preserving "
		"chronological order, (3) skip non-`exec` chatter, "
		"(4) truncate commands >500 chars with a `…[truncated]` marker "
		"rather than dropping them silently. Got:\n"
		+ "\n".join(lines)
	)


def test_yaml_reserved_indicator_recipe_in_repair_prompts() -> None:
	"""Pin the YAML reserved-indicator recipe in both repair prompts so a
	future edit can't silently drop the guidance that prevents the
	`rg "...\\`..."` shell-quoting failure observed on
	tele-funtoken-msg-scoring run 25099535242.

	Both prompts must:
	- Name the diagnostic substring the recipe matches against
	- Tell the model to wrap the scalar in DOUBLE quotes
	- Tell the model to use sed (NOT rg/grep) to inspect the line
	- Provide the python3 yaml.safe_load verification command
	"""
	for prompt_path in (
		REPO_ROOT / "prompts" / "mode-implement-repair.txt",
		REPO_ROOT / "prompts" / "mode-implement-repair-syntax.txt",
	):
		body = prompt_path.read_text(encoding="utf-8")
		assert "yaml.scanner.ScannerError" in body, (
			f"{prompt_path.name} must reference the diagnostic substring "
			"`yaml.scanner.ScannerError` so the recipe matches at the right time"
		)
		assert "cannot start any token" in body, (
			f"{prompt_path.name} must include the ScannerError message tail "
			"so the model knows which exact diagnostic this recipe handles"
		)
		assert "double quotes" in body, (
			f"{prompt_path.name} must instruct the model to wrap the scalar "
			"in double quotes (the actual YAML fix)"
		)
		assert "sed -n" in body, (
			f"{prompt_path.name} must point the model at sed -n for line "
			"inspection — using rg/grep with literal backticks blows up "
			"shell quoting and burns the repair turn"
		)
		assert "python3 -c \"import yaml; yaml.safe_load" in body, (
			f"{prompt_path.name} must give the model an exact verification "
			"command so the repair doesn't ship without revalidation"
		)
		assert "Do NOT shell out to `rg`/`grep`" in body or "Do NOT shell out to `rg`" in body, (
			f"{prompt_path.name} must explicitly forbid the failed strategy "
			"(rg/grep with literal backtick)"
		)
		assert "{{SERENA_TOOL_HINTS}}" in body, (
			f"{prompt_path.name} must expose the Serena guidance placeholder"
		)


def test_yaml_quoting_clause_in_implement_prompt() -> None:
	"""Pin the YAML reserved-indicator quoting clause in the main
	implement prompt. Source-side prevention complements the repair
	recipe — without this, Codex keeps emitting bare scalars starting
	with backtick / @ / etc. in db/contracts/*.yml prose."""
	body = (REPO_ROOT / "prompts" / "mode-implement.txt").read_text(encoding="utf-8")
	assert "{{SERENA_TOOL_HINTS}}" in body, (
		"mode-implement.txt must expose the Serena guidance placeholder"
	)
	assert "YAML reserved-indicator quoting" in body, (
		"mode-implement.txt must have a labelled clause for YAML reserved-"
		"indicator quoting so it's discoverable on review"
	)
	# All YAML 1.2 reserved indicators that cannot start a plain scalar.
	for ch in ("`", "@", "*", "&", "|", ">", "!", "%", "#", "?", ",", "[", "]", "{", "}"):
		assert f"`{ch}`" in body, (
			f"mode-implement.txt must list the reserved indicator `{ch}` in "
			"the YAML quoting clause"
		)
	assert "double quotes" in body, (
		"mode-implement.txt must say to wrap reserved-indicator scalars "
		"in DOUBLE quotes specifically (single quotes don't escape backticks "
		"identically and the consistency matters for the repair regex)"
	)
	assert "validate_changed_files_syntax.sh" in body, (
		"mode-implement.txt must reference the validator that enforces "
		"the rule, so the model knows the cost of getting this wrong"
	)


def test_validator_diagnostic_surfaces_offending_lines() -> None:
	"""End-to-end test for `append_checker_error` line-context surfacing.

	Bootstrap a git repo with a YAML file that has a reserved-indicator
	scalar (`` ` ``) on a known line. Run
	`scripts/validate_changed_files_syntax.sh` with `CAPTURE_FILE` set,
	and verify the capture contains both the original ScannerError and
	an "Offending bytes" block with the backticked line numbered. This
	guards the failure mode where the repair prompt has to find the
	offending byte itself — when the validator surfaces the bytes
	inline, the repair model patches in one shot.
	"""
	with tempfile.TemporaryDirectory(prefix="test_validator_diag_") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)
		# Stage prior file so git diff sees the new YAML as a modification
		# (validator iterates `git diff --name-only HEAD ...` + untracked).
		# Just leaving as untracked is enough — the script's second list
		# (`git ls-files --others --exclude-standard`) covers it.
		yaml_path = repo_dir / "broken.yml"
		yaml_path.write_text(
			# 5 lines of valid YAML, then a reserved-indicator scalar on
			# line 6. Python's yaml.scanner.ScannerError will report
			# `line 6, column N`.
			"key1: value1\n"
			"key2: value2\n"
			"items:\n"
			"  - alpha\n"
			"  - beta\n"
			"  - `backticked-leader is invalid YAML\n",
			encoding="utf-8",
		)
		capture_path = repo_dir / "captured.txt"
		env = _isolated_test_env(
			{
				"CAPTURE_FILE": str(capture_path),
				"ALLOW_WORKFLOW_EDITS": "true",
			},
			cwd=repo_dir,
		)
		validator = REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"
		result = subprocess.run(
			["bash", str(validator)],
			cwd=str(repo_dir),
			env=env,
			capture_output=True,
			text=True,
		)
		# Validator should fail (YAML is broken).
		assert result.returncode != 0, (
			f"validator should fail on a YAML with reserved-indicator "
			f"scalar on line 6, got rc={result.returncode}\n"
			f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
		)
		assert capture_path.exists(), (
			f"validator must write the capture file at CAPTURE_FILE; "
			f"missing at {capture_path}"
		)
		captured = capture_path.read_text(encoding="utf-8")
		# (a) The original ScannerError stderr must be in the capture
		assert "yaml.scanner.ScannerError" in captured or "ScannerError" in captured, (
			f"capture must include the YAML ScannerError stderr. Got:\n{captured}"
		)
		# (b) The "Offending bytes" block must be present with the
		#     reported line number (6 in this fixture).
		assert "Offending bytes" in captured, (
			f"capture must include the new offending-bytes block so the "
			f"repair prompt sees the exact failing line inline. Got:\n{captured}"
		)
		assert "error reported at line 6" in captured, (
			f"capture must report the line number extracted from the "
			f"ScannerError (6 in fixture). Got:\n{captured}"
		)
		# (c) The actual offending line content must be in the capture,
		#     numbered.
		assert "6: " in captured and "backticked-leader" in captured, (
			f"capture must show the backticked line content, numbered, so "
			f"the repair prompt can patch it without re-grepping. Got:\n{captured}"
		)


def test_codex_request_user_input_bail_and_flag() -> None:
	"""Pin Fix #1: when Codex emits the `request_user_input is not
	supported in exec mode` error path (model wanted to ask the user a
	clarifying question, but exec mode rejected the request), the
	retry loop must bail immediately and leave a flag file so the
	orchestrator's `ai:implementation-failed` sweep can route the
	issue back to clarify instead of re-issuing as another implement.

	Production failure mode: tele-funtoken-msg-scoring run
	(2026-04-29) wasted ~80 min and ~1.7M tokens before exhausting
	all 5 attempts on an issue/plan ambiguity Codex wanted to ask
	about. Retrying with the same prompt cannot fix that; bail early.
	"""
	codex_block = _step_block_text("Run Codex implementation")
	assert "request_user_input is not supported in exec mode" in codex_block, (
		"loop must grep for the exact CLI error string; this is the "
		"canonical signal that the model needed user input"
	)
	# The grep must scope to the CURRENT attempt's per-attempt log,
	# not the cumulative codex_log.txt. The cumulative log accumulates
	# across attempts via `tee -a`, so grepping it would let a stale
	# error from attempt 1 trip a false bail on every subsequent
	# attempt and prevent legitimate later recovery (consensus finding
	# from multi-model review on commit 523cc99).
	assert 'grep -q \'request_user_input is not supported in exec mode\' \\\n                  "${attempt_log_file}"' in codex_block or \
		'"${attempt_log_file}"' in codex_block.split('request_user_input is not supported in exec mode')[1][:200], (
		"request_user_input grep MUST target ${attempt_log_file} (the "
		"per-attempt log), not the cumulative ${RUNTIME_DIR}/codex_log.txt. "
		"A stale error from an earlier attempt would otherwise trip a "
		"false bail on every subsequent attempt"
	)
	assert "codex_request_user_input.flag" in codex_block, (
		"loop must touch a request-user-input flag file so downstream "
		"routing (the orchestrator's ai:implementation-failed sweep) can "
		"send the issue back to clarify"
	)
	# The bail must be a `break`, not `continue` — the request-user-input
	# error means retrying with the same prompt cannot fix the underlying
	# ambiguity. The flag-touch must precede the break so the flag is
	# always present when the loop exits via this path.
	bail_idx = codex_block.find("codex_request_user_input.flag")
	assert bail_idx != -1
	assert "break" in codex_block[bail_idx:bail_idx + 200], (
		"request_user_input bail must `break` out of the retry loop, "
		"not `continue` — same prompt won't fix an ambiguity"
	)


def test_codex_empty_output_streak_bail_and_flag() -> None:
	"""Pin Fix #2: when Codex returns empty output for
	`empty_streak_threshold` (default 2) attempts in a row, the retry
	loop must bail and leave a flag file. Recovers ~60% of wasted
	budget on confirmed stuck-in-exploration scenarios where the
	exec-history retry nudge alone hasn't broken the loop.
	"""
	codex_block = _step_block_text("Run Codex implementation")
	assert "empty_streak=0" in codex_block, (
		"loop must initialise the empty-output streak counter to 0 before "
		"the for-loop so it accumulates across attempts"
	)
	assert "empty_streak_threshold=" in codex_block, (
		"threshold must be a named knob (not a magic 2) so it can be "
		"tuned without re-deriving it from the increment logic"
	)
	assert "empty_streak=$((empty_streak + 1))" in codex_block, (
		"streak must increment on the genuine empty-output path"
	)
	assert "else\n              empty_streak=0" in codex_block, (
		"streak must reset to 0 on any non-empty attempt outcome so a "
		"mid-run recovery doesn't re-tip the threshold from accumulated "
		"earlier empties"
	)
	assert "codex_stuck_in_exploration.flag" in codex_block, (
		"streak-bail must touch a stuck-in-exploration flag file so "
		"the orchestrator can distinguish exploration-stuck from other "
		"failure modes when picking the recovery action"
	)
	# Align with the post-#1864 wording which broadens the bail to also
	# cover the stuck-intent (announced-edit-without-changes) contributor
	# that now feeds the same `attempt_was_empty=true` counter. Strict
	# match (not "old OR new") so an accidental revert of the #1864
	# wording fails this regression test loudly.
	assert 'no actionable output ${empty_streak} attempts in a row' in codex_block, (
		"the bail error must explain WHY the loop is aborting, with the "
		"actual streak count, so the GHA log shows a one-line root cause"
	)
	# Watchdog-kill / non-zero-exit + empty stdout is the same
	# stuck/no-output failure mode and must also count toward the
	# streak. Without this, a series of watchdog kills (which exit
	# with non-zero AND empty stdout) would never trip the bail and
	# the loop would burn the full 5-attempt budget on stuck runs.
	assert 'Codex exited with code $cmd_rc and returned empty output' in codex_block, (
		"the cmd_rc != 0 branch must distinguish empty-stdout exits "
		"(stuck/no-output, count toward streak) from non-empty exits "
		"(legitimate work attempted, don't count). Without this, a run "
		"of watchdog kills would never trip the empty-streak bail"
	)


def _extract_announce_edit_regex() -> str:
	"""Pull the live ANNOUNCE_EDIT_REGEX value from implement.yml.

	Mirrors the workflow's own assignment: `ANNOUNCE_EDIT_REGEX='...'`
	on a single line inside the "Run Codex implementation" step. Asserting
	against the real string (not a copy) is what makes this a contract
	test — a regex regression in the workflow trips the test.
	"""
	codex_block = _step_block_text("Run Codex implementation")
	match = re.search(
		r"^\s*ANNOUNCE_EDIT_REGEX='(?P<regex>[^']+)'\s*$",
		codex_block,
		re.MULTILINE,
	)
	assert match is not None, (
		"ANNOUNCE_EDIT_REGEX must be defined as a single-quoted single-line "
		"shell assignment inside the 'Run Codex implementation' step so the "
		"contract test can extract its value verbatim"
	)
	return match.group("regex")


def _grep_matches(regex: str, text: str) -> bool:
	"""Return True iff `grep -iE <regex>` matches `text`.

	Uses the same POSIX ERE engine the workflow's matcher (`grep -qEi`)
	runs against, so any divergence between Python's `re` engine (no
	POSIX character classes by default) and POSIX ERE doesn't bite.
	The workflow uses `-q` for silent match-only; this test omits `-q`
	because we still want grep to surface stderr on engine errors via
	`capture_output=True`. Match/no-match is decided by exit code in
	either form. Stdin-fed so no temp files leak.
	"""
	result = subprocess.run(
		["grep", "-iE", regex],
		input=text,
		text=True,
		capture_output=True,
		check=False,
	)
	# grep returns 0 on match, 1 on no-match, >1 on error.
	assert result.returncode in (0, 1), (
		f"grep error rc={result.returncode}: {result.stderr.strip()}"
	)
	return result.returncode == 0


def test_announce_edit_regex_contract() -> None:
	"""Pin PR #2196: ANNOUNCE_EDIT_REGEX must catch the stuck-intent
	narration patterns observed across historical failure runs AND the
	new patterns from fun-token-multi-chain issue #201, while leaving
	benign control prose un-flagged.

	The workflow logic at implement.yml:1361 / :1397 / :1434 only fires
	this regex on attempts that already have an empty worktree delta
	(or empty stdout), so false positives don't regress correctness —
	but a false negative resets `empty_streak` to 0 and lets the agent
	loop burn the full retry budget on identical no-op repetitions.
	Run-25467038876-style failures (issue #201, all 5 attempts, 5+ min
	wasted) regress this contract by exactly that mechanism.
	"""
	regex = _extract_announce_edit_regex()

	# Strings the regex MUST match.
	#
	# Historical patterns (do not regress):
	#   - bare apply_patch token, "applying the patch", `i'll apply`,
	#     `will apply`, "patching `<path>`", "Applying the requested
	#     overwrite to <path>" (PR #1906 / run 25223836137 / issue
	#     #1909).
	# New patterns from issue #201:
	#   - "Applying the one-file edit now ..." (attempts 2 + 5)
	#   - "Implementing the one-file cleanup now ..." (attempt 3)
	#   - "Applying the planned one-file edit now ..." (attempt 4 —
	#     matched the pre-PR-#2196 regex via the substring "applying
	#     the plan", but listed for completeness so a future regex
	#     reshuffle that drops "plan" doesn't regress this case).
	must_match = [
		"I'll apply_patch to fix this.",
		"applying the patch now",
		"applying a minimal change",
		"patching `foo.py`",
		"editing src/main.ts",
		"I'll apply the patch",
		"will apply the implementation",
		"Applying the requested overwrite to tests/e2e_smoke_canary.txt now, with no other file changes.",
		"Applying the one-file edit now by removing the redundant `FunOFT._debitView(...)` override in `contracts/FunOFT.sol`.",
		"Implementing the one-file cleanup now by removing the redundant `_debitView(...)` override from `contracts/FunOFT.sol`, then I'll verify the diff landed.",
		"Applying the planned one-file edit now by removing the redundant `_debitView(...)` override from `contracts/FunOFT.sol`, then I'll verify the diff landed.",
		"Applying the one-file edit now by removing the redundant `_debitView(...)` override from `contracts/FunOFT.sol`, then verifying the diff landed.",
	]
	for s in must_match:
		assert _grep_matches(regex, s), (
			f"ANNOUNCE_EDIT_REGEX must match stuck-intent narration: {s!r}"
		)

	# Minimal atomic must-match cases for each verb/noun added in PR #2196,
	# so a future regression that removes one term from the alternation
	# fails this test specifically (without relying on any of the long
	# composite strings still passing through other subpatterns). Each
	# case is the shortest narration that exercises exactly one new
	# alternation: a `<gerund-verb> <noun>` for the wildcard-noun branch,
	# and a `<gerund-verb> \`<path>\`` for the direct-path branch.
	#
	# Wildcard-noun branch — new gerund verbs, paired with each new noun.
	new_verb_noun_must_match = [
		# Verbs added to the wildcard-noun list.
		"implementing the patch",
		"removing the patch",
		"deleting the patch",
		"cleaning the patch",
		# Nouns added to the wildcard-noun list (pair with one verb each).
		"applying the cleanup",
		"applying the removal",
		"applying the deletion",
		"applying the override",
		"applying the file",
	]
	for s in new_verb_noun_must_match:
		assert _grep_matches(regex, s), (
			"ANNOUNCE_EDIT_REGEX must match minimal new-verb/new-noun "
			f"combination: {s!r}"
		)

	# Direct-path branch — every gerund verb in the second alternation must
	# match `<verb> <path-token>` and `<verb> \`<path-token>\``. The
	# `cleaning` entry pins the post-claude-branch-review-#2196 fix that
	# adds it to the direct-path list (it was added to the wildcard-noun
	# list first and missed here, see consensus finding from 6/6 reviewers).
	direct_path_must_match = [
		"patching foo.py",
		"editing foo.py",
		"overwriting foo.py",
		"rewriting foo.py",
		"replacing foo.py",
		"writing foo.py",
		"implementing foo.py",
		"removing foo.py",
		"deleting foo.py",
		"cleaning foo.py",
		"cleaning `contracts/FunOFT.sol`",
	]
	for s in direct_path_must_match:
		assert _grep_matches(regex, s), (
			"ANNOUNCE_EDIT_REGEX direct-path branch must match minimal "
			f"`<gerund> <path>` narration: {s!r}"
		)

	# Benign control prose that MUST NOT match. False positives on these
	# would still be safe at the call sites (gated on empty-delta), but
	# matching arbitrary non-edit-narration prose would mask genuine
	# diagnostic signal in the workflow log, so the contract enforces
	# minimum precision.
	must_not_match = [
		"The user is happy with the result.",
		"This is a normal sentence with no edits announced.",
		"codex",
		"mcp startup: no servers",
		"Reading prompt from stdin...",
		# Substring-collision controls. The nouns added in PR #2196
		# (`override`, `file`, `cleanup`, `removal`, `deletion`) are not
		# word-anchored in the regex (consistent with PR #1906's existing
		# unanchored nouns like `change`/`edit`/`implementation`), so a
		# few specific spelling collisions are worth pinning so a future
		# refactor that DOES add anchors doesn't silently change behavior:
		#   - "profile" contains "file" but only as a non-prefix substring
		#     (the regex's `(noun)` alternation tries each alternative
		#     anchored at the position immediately after the consumed
		#     space-delimited tokens, not floating mid-word).
		#   - "overridden" does not start with "override" (`overridd...`
		#     vs `overrid...e`) so it is not a prefix and does not match.
		# Both verified to currently NOT match; the test pins that.
		"applying the profile",
		"applying the overridden behavior",
	]
	for s in must_not_match:
		assert not _grep_matches(regex, s), (
			f"ANNOUNCE_EDIT_REGEX must not match benign prose: {s!r}"
		)


def test_salvage_branches_gate_on_substantive_changes() -> None:
	"""Pin PR #2176: the empty-stdout and watchdog-kill salvage branches
	must require a non-whitespace worktree change before declaring
	`implement_succeeded=true`. Without the gate, the announce-without-
	emit failure mode (openai/codex#11151) ships a no-op PR when the
	model writes only a trailing newline / blank line — see fun-token-
	multi-chain run 25436981639 issue 200, where
	`printf '\\n' >> contracts/FunOFTAdapter.sol` rode through the
	cmd_rc==0+empty-stdout branch and the workflow opened PR #202
	with `+` (one blank line) as the entire diff.

	The flag pair `--ignore-space-at-eol --ignore-blank-lines` is
	intentional and pinned: `-w` would also drop leading-whitespace
	(indentation) changes, which are semantic in Python/YAML/Makefiles,
	so an indentation-only fix would be misclassified as trivial and
	the salvage branch would discard real work.
	"""
	codex_block = _step_block_text("Run Codex implementation")

	# Helper must be defined exactly once before the retry loop so it
	# is in scope at both salvage call sites.
	assert codex_block.count("delta_is_substantive() {") == 1, (
		"`delta_is_substantive` helper must be defined exactly once in the "
		"step (before the for-attempt loop) so both salvage branches see it"
	)

	# Both salvage branches must call the helper instead of the bare
	# `[ -n "${codex_delta}" ]` check that PR #2176 replaced. The
	# branch-message strings are also pinned so an accidental wording
	# regression on the warning text fails this test loudly.
	assert codex_block.count('if delta_is_substantive "${codex_delta}"; then') == 2, (
		"both salvage branches (cmd_rc==0+empty-stdout AND watchdog-kill / "
		"non-zero-exit) must gate `implement_succeeded=true` on "
		"`delta_is_substantive` — a bare `[ -n \"${codex_delta}\" ]` would "
		"accept any worktree change, including a `printf '\\n' >> file` "
		"trailing-newline append (PR #2176 root cause)"
	)
	assert (
		"Codex returned empty output on attempt ${attempt} and only made "
		"whitespace-only worktree changes — refusing to salvage; treating as empty."
	) in codex_block, (
		"empty-stdout branch must emit a distinct warning when the "
		"worktree delta is whitespace-only so the GHA log shows WHY the "
		"loop continues retrying instead of breaking"
	)
	assert (
		"Codex exited with code $cmd_rc on attempt ${attempt} and only made "
		"whitespace-only worktree changes — refusing to salvage; treating as empty."
	) in codex_block, (
		"watchdog-kill / non-zero-exit branch must emit the symmetric "
		"whitespace-only warning so a stuck-on-trivial-edits run is "
		"diagnosable from the log alone"
	)

	# The flag pair `--ignore-space-at-eol --ignore-blank-lines` is the
	# intentional choice (`-w` would discard semantic Python/YAML
	# indentation changes). Lock it in so a regression to `-w` fails
	# loudly and reviewers see why this matters.
	assert "git diff --quiet --ignore-space-at-eol --ignore-blank-lines" in codex_block, (
		"the substantive-change probe must use --ignore-space-at-eol + "
		"--ignore-blank-lines, NOT -w. -w drops leading-whitespace diffs, "
		"which are semantic in Python/YAML/Makefiles and would cause an "
		"indentation-only fix to be misclassified as trivial and discarded"
	)
	assert " -w " not in (
		"\n".join(
			line for line in codex_block.splitlines()
			if "git diff --quiet" in line and "ignore-blank-lines" in line
		)
	), (
		"the substantive-change probe must not use `-w`; the indentation-"
		"semantic regression case (Python dedent fixes scope, YAML re-"
		"indent restructures keys) requires `--ignore-space-at-eol` to "
		"keep semantic indentation changes substantive"
	)


def test_retry_nudge_includes_apply_patch_directive() -> None:
	"""Pin Fix #4-generic: the retry preamble that fires when an
	attempt produces no file changes must include actionable guidance
	(use apply_patch, don't read more files until first patch lands)
	rather than just chiding ("you MUST create or edit files"). The
	chiding-only version was already there; this clause is what
	moves the model from "describe the changes" → "execute them".
	"""
	codex_block = _step_block_text("Run Codex implementation")
	assert "apply_patch`" in codex_block, (
		"retry preamble must name `apply_patch` as the preferred edit "
		"tool — `sed`/`grep` shell-out workarounds routinely fail "
		"shell quoting on literal special chars and waste the attempt"
	)
	assert "Pick the single file you understand best" in codex_block, (
		"retry preamble must instruct the model to pick a single file and "
		"edit it before expanding scope, since the bitsafe.io / "
		"tele-funtoken-msg-scoring failure mode was the model spending the "
		"full budget on recon"
	)
	assert "before expanding scope" in codex_block, (
		"retry preamble must explicitly bound recon — the 'before expanding "
		"scope' clause is the constraint that prevents wide reads ahead of "
		"the first successful write"
	)
	assert "git diff --stat" in codex_block, (
		"retry preamble must point the model at `git diff --stat` as the "
		"verification step so it confirms the write tool actually landed "
		"before continuing — the announce-without-emit failure mode burns "
		"the budget when the model trusts its own narrative"
	)


def test_failure_diagnostics_posted_to_source_issue() -> None:
	"""Pin Fix #5: when the loop exits without success (5/5 attempts
	exhausted OR an early-bail flag tripped), the workflow must post
	a per-attempt diagnostics report to the source issue so the
	orchestrator and the next operator can debug without re-downloading
	the 24K-line job log. Mirrors the validation-harness exit-14
	pattern documented in the spec.
	"""
	codex_block = _step_block_text("Run Codex implementation")
	# Per-attempt log capture must be wired into the stderr tee.
	assert "codex_log_attempt_${attempt}.txt" in codex_block, (
		"per-attempt log files must accumulate alongside the cumulative "
		"codex_log.txt so the diagnostics block can tail each attempt"
	)
	# Final-failure diagnostics block must check implement_succeeded
	# AFTER the loop, branch on which flag tripped, and assemble a
	# diagnostics file before posting to the issue.
	assert 'if [ "${implement_succeeded}" != "true" ]; then' in codex_block, (
		"diagnostics block must run on any failure path (5/5 exhausted "
		"OR early-bail), not only when the final attempt fails"
	)
	assert "codex_failure_diagnostics.md" in codex_block, (
		"diagnostics file must have a stable name so future tooling "
		"can read it"
	)
	assert "Codex bailed: request_user_input rejected" in codex_block, (
		"diagnostics reason must distinguish the request_user_input "
		"bail so the orchestrator can route accordingly"
	)
	# Align with the post-#1864 wording which broadens the diag_reason
	# to cover both empty-output AND announced-edit-without-changes
	# contributors that now feed the same streak counter. Strict match
	# (not "old OR new") so an accidental revert of the #1864 wording
	# fails this regression test loudly.
	assert "consecutive attempts with no actionable output" in codex_block, (
		"diagnostics reason must distinguish the empty-streak bail "
		"from the 5/5 exhausted path"
	)
	assert "tail -n 40" in codex_block, (
		"diagnostics must include the last 40 lines of each per-attempt "
		"log — small enough to fit in a GitHub issue comment, large "
		"enough to capture the actual failure tail"
	)
	# SECURITY: the per-attempt stderr tail can include tool output
	# (apply_patch diffs, cat/sed results) carrying repo file content
	# — so credentials present in any touched file would leak into a
	# public GitHub issue comment without redaction. Pin the same
	# keyword list and high-entropy guard the validator uses, so a
	# regression dropping the redaction (or out-of-sync keyword lists)
	# fails the test.
	#
	# Naive substring (`"token" in codex_block`) is unreliable — words
	# like "token" / "secret" appear in many places in the step
	# (env names, GH_TOKEN, comments, gh-issue-comment messages). To
	# avoid incidental false-positives masking a regression, extract
	# the actual `match(lower, /^...(<alternation>)...$/)` regex
	# alternation from the awk block and check membership against
	# just that. The same check runs against
	# `scripts/validate_changed_files_syntax.sh` to catch drift
	# between the two duplicated redaction implementations — there is
	# only ONE policy, encoded in two places, and both must contain
	# every keyword.
	#
	# Membership is by EXACT alternation entry (`alt.split("|")`),
	# not substring — otherwise removing standalone `token` from the
	# alternation would still pass because `auth[_-]?token` contains
	# the substring `token`. The reviewer-flagged regression mode.
	def extract_redaction_alternation(text: str, source: str) -> list[str]:
		# The redaction regex looks like:
		#   match(lower, /^[[:space:]-]*[a-z0-9_.-]*(secret|token|...|bearer)[a-z0-9_.-]*/)
		# Anchor on `match(lower,` and the alternation parens; allow
		# arbitrary whitespace around the awk-regex bracket-class
		# prefix/suffix so harmless awk refactors (extra newlines,
		# different whitespace) don't break extraction. The
		# alternation runs to the matching `)` followed by the
		# `[a-z0-9_.-]*` suffix — anchoring on that suffix avoids
		# being fooled by any nested `)` inside future keyword
		# patterns.
		m = re.search(
			r"match\(\s*lower\s*,\s*/[^/]*?\(([^)]+)\)\[a-z0-9_\.-\]\*",
			text,
			re.DOTALL,
		)
		assert m is not None, (
			f"could not locate the secret-keyword alternation in {source}; "
			"either the redaction awk was removed or its shape changed "
			"materially and this test needs updating to match"
		)
		return m.group(1).split("|")

	expected_keywords = (
		"secret", "token", "password", "passwd", "credential",
		"api[_-]?key", "private[_-]?key", "access[_-]?key",
		"auth[_-]?token", "client[_-]?secret", "bearer",
	)

	# (i) Diagnostics-tail redaction in implement.yml's "Run Codex
	#     implementation" step.
	implement_alts = extract_redaction_alternation(
		codex_block, "implement.yml diagnostics-tail awk"
	)
	for keyword in expected_keywords:
		assert keyword in implement_alts, (
			f"diagnostics tail-redaction (implement.yml) must include the "
			f"'{keyword}' secret-key keyword as a STANDALONE alternation "
			f"entry in its awk regex. Found alternation: {implement_alts!r}. "
			"(Substring match is not enough — `token` substring-matches "
			"`auth[_-]?token`, so a regression dropping standalone `token` "
			"would slip through a substring check.)"
		)

	# (ii) Validator offending-bytes redaction in
	#      scripts/validate_changed_files_syntax.sh — the policy MUST
	#      stay synchronised with the diagnostics path, so check it
	#      against the same keyword list with the same exact-entry
	#      semantics.
	validator_text = (
		REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"
	).read_text(encoding="utf-8")
	validator_alts = extract_redaction_alternation(
		validator_text, "validate_changed_files_syntax.sh awk"
	)
	for keyword in expected_keywords:
		assert keyword in validator_alts, (
			f"validator offending-bytes redaction (validate_changed_files_"
			f"syntax.sh) must include the '{keyword}' secret-key keyword "
			f"as a STANDALONE alternation entry. Found alternation: "
			f"{validator_alts!r}. The validator capture is later embedded "
			"in prompts and posted to issues by the diagnose fallback path."
		)

	# (iii) Lockstep: the two alternations must be identical (same
	#       keywords, same order). Drift between them means one path
	#       redacts and the other doesn't.
	assert implement_alts == validator_alts, (
		"implement.yml diagnostics-tail and validate_changed_files_syntax.sh "
		"redaction alternations have drifted apart — they must stay "
		"identical so adding/removing a keyword affects both paths "
		"atomically.\n"
		f"implement.yml entries: {implement_alts!r}\n"
		f"validator.sh entries:  {validator_alts!r}"
	)

	assert "<redacted: secret-like key>" in codex_block, (
		"diagnostics tail-redaction must replace secret-keyword lines "
		"with the standard redaction marker (lockstep with the validator)"
	)
	assert "<redacted: long opaque token>" in codex_block, (
		"diagnostics tail-redaction must replace high-entropy value "
		"lines with the standard redaction marker"
	)
	assert "tolower(line)" in codex_block, (
		"diagnostics tail-redaction must use tolower() rather than "
		"GNU-only `IGNORECASE = 1` so mixed-case keys (Api_Token, "
		"AUTH_TOKEN) redact on POSIX/BSD/mawk runtimes too"
	)
	assert 'gh issue comment "${ISSUE_NUMBER}"' in codex_block, (
		"diagnostics must be posted to the source issue (not just left "
		"in /tmp); the orchestrator polls comments to pick recovery actions"
	)
	# gh failure must not abort the job — the diagnostics file is
	# preserved locally as a fallback.
	assert "Could not post Codex failure diagnostics" in codex_block, (
		"a failed gh issue comment must downgrade to a warning, not "
		"abort the job — the underlying Codex failure is still the "
		"primary signal"
	)
	# CRITICAL: the gh-issue-comment guard checks `[ -n "${GH_TOKEN:-}" ]`.
	# If GH_TOKEN isn't in the step's `env:` block, the guard always
	# fails and the entire diagnostics-posting branch is dead code —
	# the same regression that the multi-model consensus review caught
	# on commit 523cc99. Parse the workflow YAML and look up the
	# step's env directly so this assertion is robust to harmless
	# workflow refactors (re-ordering, run-syntax changes, etc.) — a
	# previous fixed-anchor implementation was flagged as brittle in
	# review.
	import yaml as _yaml
	wf_doc = _yaml.safe_load(_workflow_text())
	codex_step_env: dict | None = None
	for job in (wf_doc.get("jobs") or {}).values():
		for step in (job.get("steps") or []):
			if isinstance(step, dict) and step.get("name") == "Run Codex implementation":
				codex_step_env = step.get("env") or {}
				break
		if codex_step_env is not None:
			break
	assert codex_step_env is not None, (
		"could not locate `Run Codex implementation` step in workflow YAML"
	)
	assert "GH_TOKEN" in codex_step_env, (
		"`Run Codex implementation` step must declare GH_TOKEN in its "
		"env: block so the Fix #5 diagnostics-posting branch (`gh issue "
		"comment`) can authenticate. Without it, the `[ -n \"${GH_TOKEN:-}\" ]` "
		"guard is always false and the whole diagnostics feature is dead "
		f"code at runtime. Step env keys: {sorted(codex_step_env.keys())}"
	)
	assert "${{ secrets.GH_PAT }}" in str(codex_step_env["GH_TOKEN"]), (
		"GH_TOKEN must reference `${{ secrets.GH_PAT }}` (the workflow's "
		"canonical secret name for GitHub auth — every other gh-using "
		"step in this workflow uses the same name). Found: "
		f"{codex_step_env['GH_TOKEN']!r}"
	)


def test_validator_offending_bytes_redacts_secrets() -> None:
	"""Pin the SECURITY hardening on `append_checker_error`'s offending-
	bytes block. The block dumps file contents around a syntax error
	into the capture file, which is later embedded into prompts AND
	posted to GitHub issues by the diagnose fallback path. Without
	guardrails, a syntax error in a credential-bearing config could
	exfiltrate secrets to public issue text.

	Two layered guards (per Copilot review):
	(a) Path denylist: skip the dump for files whose names match
	    common secret-bearing patterns (.env, *secret*, *.pem, ...).
	(b) Per-line redaction: replace lines whose key contains
	    secret-like keywords (token/password/api_key/...) or whose
	    value contains a long opaque token with a redaction marker.
	"""
	with tempfile.TemporaryDirectory(prefix="test_validator_redact_") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)
		# (b) Per-line redaction: file with credential lines + a YAML
		#     syntax error on the line AFTER them. Includes a mixed-
		#     case key (`Api_Token`) on line 5 (within the ±2 window
		#     of the line-7 error) to exercise the portability fix
		#     for GNU-only `IGNORECASE = 1` (replaced with tolower()
		#     so non-GNU awk also redacts mixed-case keys).
		#
		# Layout (line numbers):
		#   1: service:
		#   2:   name: foo
		#   3:   url: http://example.com
		#   4:   api_token: sk-...     <- in window, exact-case
		#   5:   Api_Token: sk-...     <- in window, MIXED CASE (key fix)
		#   6:   password: hunter2-... <- in window
		#   7:   - `bad scalar         <- ERROR LINE
		(repo_dir / "config.yml").write_text(
			"service:\n"
			"  name: foo\n"
			"  url: http://example.com\n"
			"  api_token: sk-1234567890abcdef1234567890abcdef1234567890abcdef\n"
			"  Api_Token: sk-mixed-case-1234567890abcdef1234567890abcdef\n"
			"  password: hunter2-very-secret\n"
			"  - `bad scalar that breaks YAML\n",
			encoding="utf-8",
		)
		# (a) Path denylist: secret-named file with the same syntax error.
		(repo_dir / ".env.broken.yml").write_text(
			"items:\n"
			"  - alpha\n"
			"  - beta\n"
			"  - gamma\n"
			"  - delta\n"
			"  - `bad scalar\n",
			encoding="utf-8",
		)
		# Control: innocuous file with same error — should dump fully.
		(repo_dir / "innocuous.yml").write_text(
			"items:\n"
			"  - alpha\n"
			"  - beta\n"
			"  - gamma\n"
			"  - delta\n"
			"  - `bad scalar\n",
			encoding="utf-8",
		)
		capture_path = repo_dir / "captured.txt"
		env = _isolated_test_env(
			{
				"CAPTURE_FILE": str(capture_path),
				"ALLOW_WORKFLOW_EDITS": "true",
			},
			cwd=repo_dir,
		)
		validator = REPO_ROOT / "scripts" / "validate_changed_files_syntax.sh"
		subprocess.run(
			["bash", str(validator)],
			cwd=str(repo_dir),
			env=env,
			capture_output=True,
			text=True,
		)
		assert capture_path.exists(), "validator must produce CAPTURE_FILE"
		captured = capture_path.read_text(encoding="utf-8")
		# (b) Per-line redaction must hide credential values from
		#     config.yml. The actual secrets must NOT appear in the
		#     capture; the redaction marker must.
		assert "sk-1234567890abcdef" not in captured, (
			"api_token value MUST be redacted from the offending-bytes "
			"dump — leaking it would exfiltrate the secret into prompts "
			"and issue text. Got:\n" + captured
		)
		assert "sk-mixed-case-1234567890abcdef" not in captured, (
			"MIXED-CASE `Api_Token` value MUST be redacted on POSIX/BSD "
			"awk runtimes too — `BEGIN { IGNORECASE = 1 }` is a GNU "
			"extension and silently no-ops elsewhere. The portable "
			"replacement is tolower() + lowercase-only regex. If this "
			"fails, mixed-case secrets evade redaction on non-GNU awk. "
			"Got:\n" + captured
		)
		assert "hunter2-very-secret" not in captured, (
			"password value MUST be redacted from the offending-bytes "
			"dump. Got:\n" + captured
		)
		assert "<redacted: secret-like key>" in captured, (
			"redaction marker must replace each suppressed line so the "
			"repair model still sees the file structure (line numbers + "
			"placeholder) without the actual credentials"
		)
		# (a) Path denylist must suppress the entire dump for .env*.
		assert "Offending bytes (suppressed: file path matches secret-bearing pattern)" in captured, (
			"secret-named files (.env*, *.pem, etc.) must produce a "
			"path-denylist suppression marker instead of any file content. "
			"Got:\n" + captured
		)
		# Innocuous file must still dump in full — the redaction must
		# not be over-broad.
		assert "  6:   - `bad scalar" in captured, (
			"innocuous file (no secrets in name or content) must still "
			"surface its offending line content — the redaction is "
			"targeted, not an indiscriminate suppression. Got:\n" + captured
		)


def test_handle_noop_guard_zero_closes_with_ai_closed() -> None:
	noop_block = _step_block_text("Handle no-op implementation")
	# Guard 0 must run BEFORE Guard 1 (pathspec hard-fail) and Guard 2
	# (ancestor-chain cap); otherwise surviving .codex-workflow-src* paths
	# in remaining_changes would falsely trip Guard 1.
	g0_idx = noop_block.find('codex_success_noop.flag')
	g1_idx = noop_block.find("Guard 1 (Q2): pathspec hard-fail")
	g2_idx = noop_block.find("Guard 2 (Q1): ancestor-chain no-op cap")
	assert g0_idx != -1, "Guard 0 (success-no-op) flag check must be present"
	assert g1_idx != -1 and g0_idx < g1_idx, "Guard 0 must run before Guard 1"
	assert g2_idx != -1 and g0_idx < g2_idx, "Guard 0 must run before Guard 2"
	# Guard 0 closes with ai:closed (not ai:implementation-failed, not ai:needs-human).
	guard_zero_region = noop_block[g0_idx:g1_idx]
	assert "--add-label 'ai:closed'" in guard_zero_region, (
		"Guard 0 must apply ai:closed so the orchestrator treats it as terminal "
		"and the wave-completion judge verifies"
	)
	assert "gh issue close" in guard_zero_region, "Guard 0 must close the issue"
	assert "exit 0" in guard_zero_region, (
		"Guard 0 must exit 0 to prevent falling through to Guards 1/2 or the "
		"legacy ai:implementation-failed path"
	)


def test_failure_log_artifact_upload_contract() -> None:
	"""Pin the failure-only codex-log artifact upload added in #1940.

	The artifact captures per-attempt stderr (codex_log_attempt_*.txt — the
	only place the OpenRouter response / finish_reason / tool-router
	rejection surfaces), the combined codex log, codex stdout, the assembled
	prompt, the diagnose-step outputs, and the codex CLI's own ~/.codex/log
	directory (raw HTTP wire logs). This exists because the redacted last-
	40-stderr-lines diagnostic comment posted by the "Run Codex
	implementation" step is not enough to root-cause empty-completion
	failures.
	"""
	stage_block = _step_block_text("Stage codex logs for upload (failure only)")
	upload_block = _step_block_text("Upload codex logs (failure only)")
	cleanup_block = _step_block_text("Cleanup temporary artifacts")

	# Both staging and upload must gate on failure() OR cancelled() so a
	# successful run leaves zero artifact artefacts. Both also gate on the
	# env vars they read so an early-skip path (env unset) doesn't try to
	# stage a directory that doesn't exist.
	assert "if: (failure() || cancelled()) && env.RUNTIME_DIR != ''" in stage_block, (
		"Stage step must gate on failure() || cancelled() AND RUNTIME_DIR being "
		"set, so early-skip paths don't try to stage a non-existent dir"
	)
	assert "if: (failure() || cancelled()) && env.CODEX_FAILURE_LOG_DIR != ''" in upload_block, (
		"Upload step must gate on failure() || cancelled() AND the env var "
		"the staging step exports, so the upload doesn't fire when staging "
		"was skipped"
	)

	# Staging dir must live under RUNNER_TEMP, not directly in RUNTIME_DIR,
	# so it survives the cleanup step's rm -rf "${RUNTIME_DIR}".
	assert 'STAGE_DIR="${RUNNER_TEMP:-/tmp}/codex-implement-failure-logs"' in stage_block, (
		"Staging dir must be rooted at RUNNER_TEMP so the existing "
		"Cleanup step that wipes RUNTIME_DIR doesn't also wipe the "
		"about-to-be-uploaded files"
	)

	# The per-attempt stderr captures are the critical artefact — every
	# other file is bonus context. Pin the glob explicitly so a refactor
	# that drops it fails this test.
	assert 'codex_log_attempt_*.txt' in stage_block, (
		"Per-attempt stderr captures (codex_log_attempt_*.txt) are the "
		"primary artefact — they hold the only model-side output we have "
		"for empty-completion debugging"
	)
	# And the combined log + prompt + stdout + diagnose outputs.
	for f in (
		"codex_log.txt",
		"codex_output.txt",
		"codex_prompt.txt",
		"post_codex_validation_errors.txt",
		"implement_diagnose.log",
		"implement_diagnose_output.txt",
		"implement_diagnose_result.json",
	):
		assert f in stage_block, (
			f"Staging step must copy {f} so post-mortem has the full "
			f"context block, not just per-attempt stderr"
		)

	# ~/.codex/log must be copied WITHOUT a wall-clock filter — the job
	# can run for up to 180 min and a single Codex attempt up to 130 min,
	# so any -mmin -N filter risks dropping the earliest session log on a
	# multi-retry failure (Copilot review on PR #1940). The cleanup +
	# retention-days bound storage cost instead.
	assert '${CODEX_HOME:-$HOME/.codex}/log' in stage_block, (
		"Staging step must copy ~/.codex/log (the codex CLI's own session "
		"log directory — raw HTTP wire logs)"
	)
	assert '-mmin' not in stage_block, (
		"Do NOT use a -mmin filter on the ~/.codex/log copy: a single "
		"Codex attempt can run 130 min (IMPLEMENT_MAX_WALL=7800s) and a "
		"job can run 180 min, so any wall-clock filter risks dropping "
		"the earliest session log on a multi-retry failure — exactly "
		"the case this artifact exists for"
	)

	# The artifact name must be unique per (run_id, run_attempt) so a
	# rerun-failed-jobs invocation doesn't conflict with the prior
	# attempt's upload.
	assert 'codex-implement-failure-logs-${{ github.run_id }}-${{ github.run_attempt }}' in upload_block, (
		"Artifact name must be unique per (run_id, run_attempt) so "
		"rerun-failed-jobs doesn't 409 on a duplicate name"
	)
	assert 'if-no-files-found: ignore' in upload_block, (
		"Upload step must fail-open on empty staging dir so an early-skip "
		"failure path doesn't add a second cascading failure"
	)
	assert 'retention-days:' in upload_block, (
		"Upload step must declare retention-days explicitly (artifacts "
		"can otherwise hit the org-default ceiling and get pruned at "
		"unpredictable times)"
	)

	# The cleanup step must also rm the staging dir on a warm runner so
	# the next implement job on the same runner doesn't see a stale
	# CODEX_FAILURE_LOG_DIR pointing at a previous run's files.
	assert 'CODEX_FAILURE_LOG_DIR' in cleanup_block, (
		"Cleanup step must remove CODEX_FAILURE_LOG_DIR so warm runners "
		"don't leak stale staging dirs across implement jobs"
	)
	assert 'codex-implement-failure-logs' in cleanup_block, (
		"Cleanup step's rm guard must reference the same staging-dir "
		"name as the staging step — drift between the two would leave "
		"stale dirs on warm runners"
	)


def _run_smoke_detection_step(
	*,
	issue_title: str,
	issue_body: str,
	default_model: str = "openai/gpt-5.4",
) -> dict[str, str]:
	"""Run the implement.yml "Detect smoke test ..." step in isolation
	and return the GITHUB_ENV exports it produced.

	Mirrors the helper pattern used elsewhere in this file: extract the
	`run:` block from the workflow, render the GitHub expressions out,
	and execute under bash with the env vars the real job would have
	set in earlier steps.
	"""

	script = _render_github_expressions(_extract_run_script("Detect smoke test and silence Telegram alerts"))
	with tempfile.TemporaryDirectory(prefix="test_smoke_") as tmp:
		tmp_path = Path(tmp)
		github_env = tmp_path / "github_env"
		github_env.write_text("", encoding="utf-8")
		env = os.environ.copy()
		env.update(
			{
				"ISSUE_TITLE": issue_title,
				"ISSUE_BODY": issue_body,
				"MODEL_EDITOR": default_model,
				"SKIP_IMPLEMENT": "false",
				"GITHUB_ENV": str(github_env),
			}
		)
		proc = _run_shell_script(script, cwd=tmp_path, env=env)
		assert proc.returncode == 0, (
			"smoke detection step failed:\n"
			f"stdout:\n{proc.stdout}\n"
			f"stderr:\n{proc.stderr}\n"
		)
		return _parse_github_output(github_env)


def test_alt_model_smoke_override_parses_valid_body() -> None:
	"""[E2E Smoke Test alt-model] title + valid Note line → MODEL_EDITOR
	overridden to the model named in the issue body.

	Closes the propagation gap acknowledged in PR #1913 body and never
	wired since PR #1841 introduced e2e-alt-model-test.
	"""

	body = (
		"## Task\n"
		"Update canary file.\n\n"
		"## Constraints\n"
		"- Only modify `tests/e2e_smoke_canary.txt`.\n"
		"- Note: this run uses `anthropic/claude-sonnet-4-6` as the editor model override."
	)
	exports = _run_smoke_detection_step(
		issue_title="[E2E Smoke Test alt-model] update canary (run 123)",
		issue_body=body,
	)
	assert exports.get("IS_SMOKE_TEST") == "true"
	assert exports.get("MODEL_EDITOR") == "anthropic/claude-sonnet-4-6", (
		"alt-model override must propagate from issue body to MODEL_EDITOR; "
		f"got exports={exports}"
	)


def test_alt_model_override_inert_on_regular_smoke_title() -> None:
	"""Regular [E2E Smoke Test] (no alt-model sub-tag) leaves MODEL_EDITOR
	at the workflow-level default — even when the body carries a
	"this run uses ..." line. Prevents drift between the regular smoke
	job and the alt-model variant.
	"""

	body = "Note: this run uses `anthropic/claude-sonnet-4-6` as the editor model override."
	exports = _run_smoke_detection_step(
		issue_title="[E2E Smoke Test] update canary",
		issue_body=body,
	)
	assert exports.get("IS_SMOKE_TEST") == "true"
	assert "MODEL_EDITOR" not in exports, (
		"Regular smoke title must not trigger MODEL_EDITOR override; "
		f"got exports={exports}"
	)


def test_alt_model_override_inert_on_production_title() -> None:
	"""Production issue (no smoke title at all) cannot smuggle a model
	override via its body. Trust-boundary check — the body is
	user-controlled, so the gate must be the title.
	"""

	body = "Note: this run uses `evil/model` as the editor model override."
	exports = _run_smoke_detection_step(
		issue_title="Add a feature to the repo",
		issue_body=body,
	)
	assert "IS_SMOKE_TEST" not in exports
	assert "MODEL_EDITOR" not in exports, (
		"Production title must never accept body-supplied MODEL_EDITOR; "
		f"got exports={exports}"
	)


def test_alt_model_override_falls_back_on_malformed_body() -> None:
	"""Alt-model title with a body that fails the shape regex (shell
	metacharacters, missing slash, oversized) leaves MODEL_EDITOR alone
	rather than failing the implement run. Fail-open behaviour matches
	the dispatcher's own validation in test-and-mark-stable.yml.
	"""

	cases = [
		# Shell injection attempt — backtick token contains spaces / ;
		"Note: this run uses `evil; rm -rf /` as the editor model override.",
		# No slash → shape regex rejects
		"Note: this run uses `badmodel` as the editor model override.",
		# No Note line at all
		"Just a plain body without the override directive.",
	]
	for body in cases:
		exports = _run_smoke_detection_step(
			issue_title="[E2E Smoke Test alt-model] update canary",
			issue_body=body,
		)
		assert exports.get("IS_SMOKE_TEST") == "true"
		assert "MODEL_EDITOR" not in exports, (
			f"Malformed alt-model body must fall through to default MODEL_EDITOR; "
			f"body={body!r}, exports={exports}"
		)


def test_review_pipeline_integration_chain_module_runs_clean() -> None:
	integration_test = REPO_ROOT / "tests" / "test_review_pipeline_integration.py"
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(
		["python3", str(integration_test)],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		timeout=180,
	)
	assert result.returncode == 0, (
		"review pipeline integration test failed\n"
		f"stdout:\n{result.stdout}\n"
		f"stderr:\n{result.stderr}"
	)

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
