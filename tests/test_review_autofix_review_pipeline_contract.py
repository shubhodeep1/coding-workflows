#!/usr/bin/env python3
"""Contract tests for review-pipeline plumbing in review_autofix.yml."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"
CONSOLIDATE = REPO_ROOT / "scripts" / "review_consolidate.sh"
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
AGENTS_MD_MATERIALITY = REPO_ROOT / "scripts" / "review_agents_md_materiality.sh"
METADATA_HELPER = REPO_ROOT / "scripts" / "review_collect_pr_metadata.sh"
AUTO_MERGE_HELPER = REPO_ROOT / "scripts" / "review_enable_auto_merge.sh"
CHECK_RUNS_HELPER = REPO_ROOT / "scripts" / "collect_pr_check_runs_context.py"
REVIEWER_FAILBACK_CHAINS = REPO_ROOT / "scripts" / "reviewer_failback_chains.json"
MODEL_CATALOG = REPO_ROOT / "scripts" / "codex_model_catalog.json"
FIXTURES_DIR = REPO_ROOT / "scripts" / "fixtures" / "cloudflare-learnings"
PHASE_A_ANTI_RULES_FIXTURE = FIXTURES_DIR / "phase-a-anti-rules-noisy-pr.patch"
PHASE_B_RISK_TIER_TRIVIAL_FIXTURE = FIXTURES_DIR / "phase-b-risk-tier-trivial.patch"
PHASE_B_RISK_TIER_LITE_FIXTURE = FIXTURES_DIR / "phase-b-risk-tier-lite.patch"
PHASE_B_RISK_TIER_FULL_FIXTURE = FIXTURES_DIR / "phase-b-risk-tier-full.patch"
PHASE_B_RISK_TIER_ALWAYS_FULL_FIXTURE = FIXTURES_DIR / "phase-b-risk-tier-always-full.patch"
PHASE_C_FILTER_FIXTURE = FIXTURES_DIR / "phase-c-lockfile-and-generated.patch"
PHASE_D_MATERIALITY_FIXTURE = FIXTURES_DIR / "phase-d-package-bump-no-agents-update.patch"
PHASE_G_FLAKY_REVIEWER_FIXTURE = FIXTURES_DIR / "phase-g-flaky-reviewer.patch"
PHASE_H_CONTEXT_BUDGET_FIXTURE = FIXTURES_DIR / "phase-h-context-budget-overflow.txt"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _stage_helper_text() -> str:
	return STAGE_HELPER.read_text(encoding="utf-8")


def _auto_merge_helper_text() -> str:
	return AUTO_MERGE_HELPER.read_text(encoding="utf-8")


def _reviewers_text() -> str:
	return REVIEWERS.read_text(encoding="utf-8")


def _review_collect_pr_metadata_graphql_helper_block() -> str:
	text = METADATA_HELPER.read_text(encoding="utf-8")
	match = re.search(r"(?ms)^_fetch_linked_issue_bodies_graphql\(\)\n\{.*?^\}\s*$", text)
	assert match, "missing _fetch_linked_issue_bodies_graphql helper"
	return match.group(0)


def _apply_fixes_text() -> str:
	return APPLY_FIXES.read_text(encoding="utf-8")


def _consolidate_text() -> str:
	return CONSOLIDATE.read_text(encoding="utf-8")


def _rb_judge_text() -> str:
	return RB_JUDGE.read_text(encoding="utf-8")


def _install_review_collect_mock_gh(bin_dir: Path, state_file: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag: str) -> str:
	for idx, arg in enumerate(args):
		if arg == flag and idx + 1 < len(args):
			return args[idx + 1]
	return ""


def api_path() -> str:
	idx = 1
	takes_value = {"--jq", "-f", "-F", "-H", "-X", "--input"}
	no_value = {"--paginate", "-i"}
	while idx < len(args):
		arg = args[idx]
		if arg in takes_value:
			idx += 2
			continue
		if arg in no_value:
			idx += 1
			continue
		if arg.startswith("-"):
			idx += 1
			continue
		return arg
	return ""


state.setdefault("calls", []).append(args)

if args[:2] == ["pr", "diff"]:
	pr_number = args[2] if len(args) > 2 else ""
	diff_text = (state.get("pr_diffs", {}) or {}).get(pr_number, state.get("pr_diff_default", ""))
	save()
	sys.stdout.write(diff_text)
	sys.exit(0)

if args[:1] == ["api"]:
	path = api_path()
	if path == "/rate_limit":
		save()
		sys.stdout.write("HTTP/1.1 200 OK\nx-ratelimit-reset: 0\n")
		sys.exit(0)
	api_responses = state.get("api_responses", {}) or {}
	matched = None
	for pattern, response in sorted(api_responses.items(), key=lambda item: len(item[0]), reverse=True):
		if pattern and pattern in path:
			matched = response
			break
	if matched is None:
		save()
		sys.stderr.write(f"mock gh: unsupported api path: {path}\n")
		sys.exit(1)
	jq_filter = first_value("--jq")
	save()
	if jq_filter == ".default_branch":
		print((matched or {}).get("default_branch", ""))
	elif jq_filter == ".data.repository.pullRequest.closingIssuesReferences.nodes // []":
		nodes = (((matched or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
		nodes = ((nodes.get("closingIssuesReferences") or {}).get("nodes") or [])
		print(json.dumps(nodes))
	elif jq_filter == '{number: (.number // 0), title: (.title // ""), body: (.body // "")}':
		print(json.dumps({
			"number": (matched or {}).get("number", 0) or 0,
			"title": (matched or {}).get("title", "") or "",
			"body": (matched or {}).get("body", "") or "",
		}))
	else:
		print(json.dumps(matched))
	sys.exit(0)

save()
sys.stderr.write(f"mock gh: unsupported args: {args!r}\n")
sys.exit(1)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)
	state_file.write_text("{}", encoding="utf-8")


def _run_review_collect_pr_metadata_harness(
	*,
	pr_number: str,
	claude_branch_review_mode: str,
	head_ref_override: str,
	head_sha_override: str,
	base_ref_override: str,
	review_break_glass_enabled: str = "false",
	mock_state: dict[str, object],
) -> dict[str, object]:
	with tempfile.TemporaryDirectory(prefix="review-collect-pr-metadata-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		runtime_dir = tmp / "runtime"
		bin_dir.mkdir()
		runtime_dir.mkdir()

		gh_state_file = runtime_dir / "gh_state.json"
		_install_review_collect_mock_gh(bin_dir, gh_state_file)
		gh_state_file.write_text(json.dumps(mock_state), encoding="utf-8")

		files = {
			"pr_payload": runtime_dir / "pr_payload.json",
			"pr_meta": runtime_dir / "pr_meta.json",
			"pr_issue_comments": runtime_dir / "pr_issue_comments.json",
			"pr_reviews": runtime_dir / "pr_reviews.json",
			"pr_review_comments": runtime_dir / "pr_review_comments.json",
			"linked_issue_context": runtime_dir / "linked_issue_context.txt",
			"comments_context": runtime_dir / "pr_all_comments_context.txt",
			"pr_diff": runtime_dir / "pr_diff.patch",
			"github_env": runtime_dir / "github_env.txt",
		}

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_REPOSITORY": "owner/repo",
			"GITHUB_REPOSITORY_OWNER": "owner",
			"GH_TOKEN": "test-token",
			"PR_NUMBER": pr_number,
			"CLAUDE_BRANCH_REVIEW_MODE": claude_branch_review_mode,
			"HEAD_REF_OVERRIDE_INPUT": head_ref_override,
			"HEAD_SHA_OVERRIDE_INPUT": head_sha_override,
			"BASE_REF_OVERRIDE_INPUT": base_ref_override,
			"PR_PAYLOAD_FILE": str(files["pr_payload"]),
			"PR_META_FILE": str(files["pr_meta"]),
			"PR_ISSUE_COMMENTS_FILE": str(files["pr_issue_comments"]),
			"PR_REVIEWS_FILE": str(files["pr_reviews"]),
			"PR_REVIEW_COMMENTS_FILE": str(files["pr_review_comments"]),
			"LINKED_ISSUE_CONTEXT_FILE": str(files["linked_issue_context"]),
			"PR_ALL_COMMENTS_CONTEXT_FILE": str(files["comments_context"]),
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"GITHUB_ENV": str(files["github_env"]),
			"REVIEW_BREAK_GLASS_ENABLED": review_break_glass_enabled,
			})

		result = subprocess.run(
			["bash", str(METADATA_HELPER)],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
			timeout=60,
		)

		github_env: dict[str, str] = {}
		for raw_line in files["github_env"].read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			github_env[key] = value

		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"github_env": github_env,
			"mock_state": json.loads(gh_state_file.read_text(encoding="utf-8")),
			"pr_payload": json.loads(files["pr_payload"].read_text(encoding="utf-8")),
			"pr_meta": json.loads(files["pr_meta"].read_text(encoding="utf-8")),
			"pr_issue_comments": json.loads(files["pr_issue_comments"].read_text(encoding="utf-8")),
			"pr_reviews": json.loads(files["pr_reviews"].read_text(encoding="utf-8")),
			"pr_review_comments": json.loads(files["pr_review_comments"].read_text(encoding="utf-8")),
			"linked_issue_context": files["linked_issue_context"].read_text(encoding="utf-8"),
			"comments_context": files["comments_context"].read_text(encoding="utf-8"),
			"pr_diff": files["pr_diff"].read_text(encoding="utf-8"),
		}


def _install_check_runs_mock_gh(bin_dir: Path, state_file: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def api_path() -> str:
	idx = 1
	takes_value = {"--jq", "-f", "-F", "-H", "-X", "--input"}
	no_value = {"--paginate", "--slurp", "-i"}
	while idx < len(args):
		arg = args[idx]
		if arg in takes_value:
			idx += 2
			continue
		if arg in no_value:
			idx += 1
			continue
		if arg.startswith("-"):
			idx += 1
			continue
		return arg
	return ""


def render_response(response: object) -> tuple[int, str, str]:
	if not isinstance(response, dict):
		return 1, "", "mock gh: malformed check-runs response\n"
	exit_code = int(response.get("exit_code", 0) or 0)
	stdout = response.get("stdout", "")
	stderr = response.get("stderr", "")
	if "json" in response:
		stdout = json.dumps(response["json"])
	return exit_code, str(stdout), str(stderr)


state.setdefault("calls", []).append(args)

if args[:1] == ["api"]:
	path = api_path()
	if path == "/rate_limit":
		save()
		sys.stdout.write("HTTP/1.1 200 OK\nx-ratelimit-reset: 0\n")
		sys.exit(0)
	if "/check-runs" in path:
		responses = state.get("check_runs_responses", []) or []
		idx = int(state.get("check_runs_index", 0) or 0)
		if idx < len(responses):
			response = responses[idx]
			state["check_runs_index"] = idx + 1
		else:
			response = state.get("check_runs_default", {"json": []})
		exit_code, stdout, stderr = render_response(response)
		save()
		if stdout:
			sys.stdout.write(stdout)
		if stderr:
			sys.stderr.write(stderr)
			if not stderr.endswith("\n"):
				sys.stderr.write("\n")
		sys.exit(exit_code)

save()
sys.stderr.write(f"mock gh: unsupported args: {args!r}\n")
sys.exit(1)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)
	state_file.write_text("{}", encoding="utf-8")


def _run_collect_pr_check_runs_harness(
	*,
	pr_payload: object,
	check_runs_responses: list[dict[str, object]] | None = None,
	check_runs_autofix_enabled: str = "true",
	self_run_id: str = "",
	wait_timeout_secs: str = "300",
	poll_interval_secs: str = "20",
	log_tail_bytes: str = "0",
	gh_retry_max_attempts: str = "1",
) -> dict[str, object]:
	with tempfile.TemporaryDirectory(prefix="collect-pr-check-runs-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		runtime_dir = tmp / "runtime"
		bin_dir.mkdir()
		runtime_dir.mkdir()

		gh_state_file = runtime_dir / "gh_state.json"
		_install_check_runs_mock_gh(bin_dir, gh_state_file)
		gh_state_file.write_text(json.dumps({
			"check_runs_responses": check_runs_responses or [],
		}), encoding="utf-8")

		pr_payload_file = runtime_dir / "pr_payload.json"
		if isinstance(pr_payload, str):
			pr_payload_file.write_text(pr_payload, encoding="utf-8")
		else:
			pr_payload_file.write_text(json.dumps(pr_payload), encoding="utf-8")
		context_file = runtime_dir / "pr_check_runs_context.txt"

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"PYTHONDONTWRITEBYTECODE": "1",
			"GH_TOKEN": "test-token",
			"GITHUB_REPOSITORY": "owner/repo",
			"PR_PAYLOAD_FILE": str(pr_payload_file),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(context_file),
			"CHECK_RUNS_AUTOFIX_ENABLED": check_runs_autofix_enabled,
			"CHECK_RUNS_WAIT_TIMEOUT_SECS": wait_timeout_secs,
			"CHECK_RUNS_POLL_INTERVAL_SECS": poll_interval_secs,
			"CHECK_RUNS_LOG_TAIL_BYTES": log_tail_bytes,
			"GH_RETRY_MAX_ATTEMPTS": gh_retry_max_attempts,
			"SELF_RUN_ID": self_run_id,
		})

		result = subprocess.run(
			["python3", str(CHECK_RUNS_HELPER)],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)

		return {
			"returncode": result.returncode,
			"stdout": result.stdout,
			"stderr": result.stderr,
			"context_text": context_file.read_text(encoding="utf-8"),
			"mock_state": json.loads(gh_state_file.read_text(encoding="utf-8")),
		}


def _step_block(step_name: str) -> str:
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
		return "\n".join(lines[idx:end])
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _job_block(job_name: str) -> str:
	lines = _workflow_text().splitlines()
	needle = f"{job_name}:"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		if len(line) - len(line.lstrip(" ")) != 2:
			continue
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().endswith(":") and len(candidate) - len(candidate.lstrip(" ")) == 2:
				end = j
				break
		return "\n".join(lines[idx:end])
	raise AssertionError(f"Job not found in workflow: {job_name}")


def _step_run_script(step_name: str) -> str:
	block_lines = _step_block(step_name).splitlines()
	run_idx = -1
	run_indent = -1
	for idx, line in enumerate(block_lines):
		if line.strip() == "run: |":
			run_idx = idx
			run_indent = len(line) - len(line.lstrip(" "))
			break
	if run_idx < 0:
		raise AssertionError(f"run block not found in workflow step: {step_name}")

	script_lines: list[str] = []
	for line in block_lines[run_idx + 1:]:
		if line.strip():
			indent = len(line) - len(line.lstrip(" "))
			if indent <= run_indent:
				break
		script_lines.append(line)

	script = textwrap.dedent("\n".join(script_lines)).strip("\n")
	return script + ("\n" if script else "")


def _extract_review_autofix_timeout_minutes(workflow_text: str) -> tuple[int, int]:
	workflow = yaml.safe_load(workflow_text) or {}
	jobs = workflow.get("jobs") or {}
	codex_agent_job = jobs.get("codex-agent") or {}
	job_timeout_minutes = codex_agent_job.get("timeout-minutes")
	resolver_timeout_minutes = None
	for step in codex_agent_job.get("steps") or []:
		if isinstance(step, dict) and step.get("name") == "Run Codex resolver, validate, stage, commit":
			resolver_timeout_minutes = step.get("timeout-minutes")
			break
	assert isinstance(job_timeout_minutes, int), "codex-agent timeout-minutes not found"
	assert isinstance(resolver_timeout_minutes, int), "resolver step timeout-minutes not found"
	return job_timeout_minutes, resolver_timeout_minutes


def _review_autofix_timeout_minutes() -> tuple[int, int]:
	return _extract_review_autofix_timeout_minutes(_workflow_text())


def _parse_env_file(path: Path) -> dict[str, str]:
	parsed: dict[str, str] = {}
	for raw_line in path.read_text(encoding="utf-8").splitlines():
		if "=" not in raw_line:
			continue
		key, value = raw_line.split("=", 1)
		parsed[key] = value
	return parsed


def _read_dir_text_files(path: Path, *, exclude_names: set[str] | None = None) -> dict[str, str]:
	exclude = exclude_names or set()
	if not path.exists():
		return {}
	return {
		child.name: child.read_text(encoding="utf-8")
		for child in sorted(path.iterdir())
		if child.is_file() and child.name not in exclude
	}


def _run_partial_finalize_publish_safety_gate_step(
	*,
	partial_finalize_requested: str = "true",
	job_start_epoch: str = "",
	codex_run_budget_start_epoch: str = "",
) -> dict[str, object]:
	gate_script = _step_run_script("Decide partial-finalize validation/push safety")
	with tempfile.TemporaryDirectory(prefix="review-partial-finalize-gate-") as td:
		tmp = Path(td)
		github_env_file = tmp / "github_env.txt"
		github_env_file.write_text("", encoding="utf-8")
		result = subprocess.run(
			["bash", "-c", f"set -euo pipefail\n{gate_script}"],
			cwd=str(REPO_ROOT),
			env=_git_clean_env({
				"GITHUB_ENV": str(github_env_file),
				"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": partial_finalize_requested,
				"JOB_START_EPOCH": job_start_epoch,
				"CODEX_RUN_BUDGET_START_EPOCH": codex_run_budget_start_epoch,
				"PYTHONDONTWRITEBYTECODE": "1",
			}),
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"github_env": _parse_env_file(github_env_file),
		}


def _git_clean_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
	env = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith("GIT_") and key not in {"BASH_ENV", "ENV"}
	}
	if overrides:
		env.update(overrides)
	return env


def _reviewer_harness_budget_env() -> dict[str, str]:
	run_budget_start_epoch = int(time.time())
	return {
		"JOB_START_EPOCH": str(run_budget_start_epoch),
		"CODEX_RUN_BUDGET_START_EPOCH": str(run_budget_start_epoch),
		"CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH": str(run_budget_start_epoch + 600),
		"CODEX_RUN_BUDGET_TOTAL_SECS": "600",
		"REVIEW_SOFT_DEADLINE_MINUTES": "10",
	}


def _init_git_repo(repo: Path) -> str:
	env = _git_clean_env()
	subprocess.run(["git", "init"], cwd=str(repo), env=env, check=True, capture_output=True, text=True)
	subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), env=env, check=True, capture_output=True, text=True)
	subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), env=env, check=True, capture_output=True, text=True)
	(repo / "README.md").write_text("seed\n", encoding="utf-8")
	subprocess.run(["git", "add", "README.md"], cwd=str(repo), env=env, check=True, capture_output=True, text=True)
	subprocess.run(["git", "commit", "-m", "seed"], cwd=str(repo), env=env, check=True, capture_output=True, text=True)
	return subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=str(repo),
		env=env,
		check=True,
		capture_output=True,
		text=True,
	).stdout.strip()


def _run_restore_same_head_resume_step(
	repo: Path,
	*,
	pr_number: str = "123",
	review_max_resume_rounds: str = "3",
	runtime_dir: Path | None = None,
	reviews_dir: Path | None = None,
) -> dict[str, object]:
	restore_script = _step_run_script("Restore same-head partial resume state")
	effective_runtime_dir = runtime_dir or (repo / "runtime")
	effective_reviews_dir = reviews_dir or (repo / "reviews")
	effective_runtime_dir.mkdir(parents=True, exist_ok=True)
	effective_reviews_dir.mkdir(parents=True, exist_ok=True)
	github_env_file = effective_runtime_dir / "github_env_restore.txt"
	github_env_file.write_text("", encoding="utf-8")
	result = subprocess.run(
		["bash", "-c", f"set -euo pipefail\n{restore_script}"],
		cwd=str(repo),
		env=_git_clean_env({
			"GITHUB_ENV": str(github_env_file),
			"PR_NUMBER": pr_number,
			"REVIEW_MAX_RESUME_ROUNDS": review_max_resume_rounds,
			"PREVIOUS_REVIEWS_DIR": str(effective_reviews_dir),
			"RUNTIME_DIR": str(effective_runtime_dir),
			"PYTHONDONTWRITEBYTECODE": "1",
		}),
		check=True,
		capture_output=True,
		text=True,
	)
	return {
		"stdout": result.stdout,
		"stderr": result.stderr,
		"github_env": _parse_env_file(github_env_file),
		"restored_runtime_files": _read_dir_text_files(effective_runtime_dir, exclude_names={github_env_file.name}),
		"restored_review_files": _read_dir_text_files(effective_reviews_dir),
	}


def _install_partial_finalize_mock_gh(bin_dir: Path, state_file: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def api_path() -> str:
	idx = 1
	takes_value = {"--jq", "-f", "-F", "-H", "-X", "--input"}
	no_value = {"--paginate", "--slurp", "-i"}
	while idx < len(args):
		arg = args[idx]
		if arg in takes_value:
			idx += 2
			continue
		if arg in no_value:
			idx += 1
			continue
		if arg.startswith("-"):
			idx += 1
			continue
		return arg
	return ""


def first_form_value(prefix: str) -> str:
	for idx, arg in enumerate(args):
		if arg == "-f" and idx + 1 < len(args):
			value = args[idx + 1]
			if value.startswith(prefix):
				return value.split("=", 1)[1]
	return ""


state.setdefault("calls", []).append(args)

if args[:1] == ["api"]:
	path = api_path()
	if path == "/rate_limit":
		save()
		sys.stdout.write("HTTP/1.1 200 OK\nx-ratelimit-reset: 0\n")
		sys.exit(0)
	if "/issues/" in path and path.endswith("/comments"):
		state.setdefault("issue_comments", []).append({
			"path": path,
			"body": first_form_value("body="),
		})
		save()
		sys.stdout.write("{}\n")
		sys.exit(0)

save()
sys.stderr.write(f"mock gh: unsupported args: {args!r}\n")
sys.exit(1)
'''
	mock_path = bin_dir / "gh"
	mock_path.write_text(gh_script, encoding="utf-8")
	mock_path.chmod(0o755)
	state_file.write_text("{}", encoding="utf-8")


def _dispatch_fallback_chain_slice(step_name: str) -> str:
	block = _step_block(step_name)
	lines = block.splitlines()
	needle = 'if [ "${caller_workflow}" != "review_autofix.yml" ]; then'
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		start_indent = len(line) - len(line.lstrip(" "))
		for end_idx in range(idx + 1, len(lines)):
			candidate = lines[end_idx]
			if candidate.strip() != "fi":
				continue
			end_indent = len(candidate) - len(candidate.lstrip(" "))
			if end_indent == start_indent:
				return textwrap.dedent("\n".join(lines[idx : end_idx + 1])).strip()
		break
	assert False, f"missing redispatch fallback chain in step: {step_name}"


def _reviewer_iteration_scope_helper_block() -> str:
	text = _reviewers_text()
	start = text.index("# ── Reviewer iteration-scoping helpers")
	end = text.index("# ── End reviewer iteration-scoping helpers", start)
	return text[start:end]


def _reviewer_filter_helper_block() -> str:
	text = _reviewers_text()
	start = text.index("# ── Reviewer uninteresting-file filter helpers")
	end = text.index("# ── End reviewer uninteresting-file filter helpers", start)
	return text[start:end]


def _reviewer_risk_tier_helper_block() -> str:
	text = _reviewers_text()
	start = text.index("# ── Reviewer risk-tier helpers")
	end = text.index("# ── End reviewer risk-tier helpers", start)
	return text[start:end]


def _reviewer_failback_helper_block() -> str:
	text = _reviewers_text()
	start = text.index("# ── Reviewer failback / health helpers")
	end = text.index("# ── End reviewer failback / health helpers", start)
	return text[start:end]


def _reviewer_partial_finalize_budget_helper_block() -> str:
	text = _reviewers_text()
	start = text.index('REVIEWER_PARTIAL_FINALIZE_REQUEST_FILE="${RUNTIME_DIR:-.}/reviewers_partial_finalize_request.txt"')
	end = text.index("resolve_ledger_substate_helper() {", start)
	return text[start:end]


def _reviewer_run_reviewer_block() -> str:
	text = _reviewers_text()
	start = text.index("run_reviewer() {")
	end = text.index("# ── Two-pass reviewer architecture", start)
	return text[start:end]


def _reviewer_run_reviewer_pass_block() -> str:
	text = _reviewers_text()
	start = text.index("run_reviewer_pass() {")
	end = text.index("# Wrap a consolidated pass-1 ledger", start)
	return text[start:end]


def _reviewer_zero_success_guard_block() -> str:
	text = _reviewers_text()
	start = text.index('if [ "${reviewers_successful}" -eq 0 ]; then')
	return text[start:]


def _workflow_reviewer_models() -> list[str]:
	lines = _workflow_text().splitlines()
	needle = "  REVIEWER_MODELS: |"
	for idx, line in enumerate(lines):
		if line != needle:
			continue
		models: list[str] = []
		for candidate in lines[idx + 1:]:
			if not candidate.startswith("    "):
				break
			stripped = candidate.strip()
			if stripped:
				models.append(stripped)
		if models:
			return models
		break
	raise AssertionError("REVIEWER_MODELS block not found in workflow")


def _reviewer_failback_chains() -> dict[str, list[str]]:
	payload = json.loads(REVIEWER_FAILBACK_CHAINS.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		raise AssertionError("reviewer failback chains must be a JSON object")

	chains: dict[str, list[str]] = {}
	for model, entry in payload.items():
		if isinstance(entry, str):
			entry = [entry]
		if not isinstance(model, str) or not isinstance(entry, list):
			raise AssertionError("reviewer failback chains entries must be string -> list[str]")
		candidates: list[str] = []
		for candidate in entry:
			if not isinstance(candidate, str) or not candidate.strip():
				raise AssertionError(
					f"reviewer failback chains candidates must be non-empty strings, got {candidate!r}"
				)
			candidates.append(candidate.strip())
		chains[model] = candidates
	return chains


def _catalog_declared_model_slugs() -> set[str]:
	payload = json.loads(MODEL_CATALOG.read_text(encoding="utf-8"))
	models = payload.get("models") if isinstance(payload, dict) else payload
	if not isinstance(models, list):
		raise AssertionError("model catalog must expose a top-level models list")

	declared: set[str] = set()
	for entry in models:
		if not isinstance(entry, dict):
			continue
		slug = entry.get("slug")
		if isinstance(slug, str) and slug.strip():
			declared.add(slug.strip())
	return declared


def _diff_changed_paths(diff_text: str) -> list[str]:
	paths: list[str] = []
	seen: set[str] = set()
	for raw_line in diff_text.splitlines():
		prefix = "diff --git a/"
		if not raw_line.startswith(prefix):
			continue
		path = raw_line[len(prefix):].split(" b/", 1)[0].strip()
		if not path or path in seen:
			continue
		seen.add(path)
		paths.append(path)
	return paths


def _gate_agents_md_materiality_classifier_script() -> str:
	gate_block = _step_block("Evaluate review gate").splitlines()
	start_idx = -1
	for idx, line in enumerate(gate_block):
		if "gate_materiality_json" in line and "python3 - <<'PY'" in line:
			start_idx = idx + 1
			break
	if start_idx < 0:
		raise AssertionError("AGENTS.md materiality gate classifier heredoc not found")
	body: list[str] = []
	for line in gate_block[start_idx:]:
		if line.strip() == "PY":
			return textwrap.dedent("\n".join(body))
		body.append(line)
	raise AssertionError("AGENTS.md materiality gate classifier heredoc missing terminator")


def _run_gate_agents_md_materiality_classifier(paths: list[str]) -> dict[str, object]:
	result = subprocess.run(
		["python3", "-c", _gate_agents_md_materiality_classifier_script()],
		cwd=str(REPO_ROOT),
		input=json.dumps([{"filename": path} for path in paths]),
		check=True,
		capture_output=True,
		text=True,
	)
	return json.loads(result.stdout)


def _phase_c_workspace_files() -> dict[str, str]:
	return {
		"package-lock.json": '{\n  "lockfileVersion": 3\n}\n',
		"src/generated/client.ts": "@generated\nexport const endpoint = '/v2';\n",
		"public/app.min.js": "console.log('minified');\n",
		"db/migrations/20260529000000_generated.sql": "-- GENERATED FILE\nCREATE TABLE widgets (id INT);\n",
		"scripts/migrate/seed.sh": "# GENERATED BY seed-tool\necho seed\n",
		"db/contracts/widgets.yml": "# GENERATED BY contract-tool\ncollection: widgets\n",
	}


def _phase_c_changed_files_text() -> str:
	return "\n".join(_phase_c_workspace_files().keys()) + "\n"


def _phase_c_diff_stat_text() -> str:
	return "\n".join([
		" package-lock.json                          | 2 +-",
		" src/generated/client.ts                   | 2 +-",
		" public/app.min.js                         | 2 +-",
		" db/migrations/20260529000000_generated.sql | 2 +-",
		" scripts/migrate/seed.sh                   | 2 +-",
		" db/contracts/widgets.yml                  | 2 +-",
		" 6 files changed, 6 insertions(+), 6 deletions(-)",
	]) + "\n"


def _run_uninteresting_filter_script(*, diff_text: str, workspace_files: dict[str, str]) -> dict[str, str]:
	with tempfile.TemporaryDirectory(prefix="reviewer-filter-script-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		diff_file = tmp / "input.patch"
		output_diff_file = tmp / "output.patch"
		kept_paths_file = tmp / "kept_paths.txt"
		skipped_paths_file = tmp / "skipped_paths.txt"

		for rel_path, text in workspace_files.items():
			target = workspace / rel_path
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text(text, encoding="utf-8")

		diff_file.write_text(diff_text, encoding="utf-8")
		result = subprocess.run(
			[
				"bash",
				str(REPO_ROOT / "scripts" / "review_filter_uninteresting_files.sh"),
				"--diff-file",
				str(diff_file),
				"--output-diff",
				str(output_diff_file),
				"--kept-paths-file",
				str(kept_paths_file),
				"--skipped-paths-file",
				str(skipped_paths_file),
				"--repo-root",
				str(workspace),
			],
			cwd=str(REPO_ROOT),
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"output_diff": output_diff_file.read_text(encoding="utf-8"),
			"kept_paths": kept_paths_file.read_text(encoding="utf-8"),
			"skipped_paths": skipped_paths_file.read_text(encoding="utf-8"),
		}


def _run_reviewer_filter_harness(*, filter_enabled: str, helper_mode: str) -> dict[str, str]:
	helper_block = _reviewer_filter_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-filter-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		runtime = tmp / "runtime"
		runtime.mkdir()

		for rel_path, text in _phase_c_workspace_files().items():
			target = workspace / rel_path
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text(text, encoding="utf-8")

		files = {
			"pr_diff": runtime / "pr_diff.patch",
			"original_pr_diff": runtime / "original_pr_diff.patch",
			"last_run_diff": runtime / "last_run_diff.patch",
			"pr_changed": runtime / "pr_changed_files.txt",
			"last_run_changed": runtime / "last_run_changed_files.txt",
			"last_run_diff_stat": runtime / "last_run_diff_stat.txt",
			"last_commit_stat": runtime / "last_commit_stat.txt",
			"symbol_diff": runtime / "symbol_diff_summary.txt",
			"state": runtime / "filter_state.txt",
		}
		fixture_text = PHASE_C_FILTER_FIXTURE.read_text(encoding="utf-8")
		files["pr_diff"].write_text(fixture_text, encoding="utf-8")
		files["original_pr_diff"].write_text(fixture_text, encoding="utf-8")
		files["last_run_diff"].write_text(fixture_text, encoding="utf-8")
		changed_files = _phase_c_changed_files_text()
		files["pr_changed"].write_text(changed_files, encoding="utf-8")
		files["last_run_changed"].write_text(changed_files, encoding="utf-8")
		diff_stat = _phase_c_diff_stat_text()
		files["last_run_diff_stat"].write_text(diff_stat, encoding="utf-8")
		files["last_commit_stat"].write_text(diff_stat, encoding="utf-8")
		files["symbol_diff"].write_text(
			"RAW SYMBOL SUMMARY\npackage-lock.json\nsrc/generated/client.ts\n",
			encoding="utf-8",
		)

		if helper_mode == "repo":
			support_scripts_dir = REPO_ROOT / "scripts"
		elif helper_mode == "missing":
			support_scripts_dir = tmp / "missing_support_scripts"
			support_scripts_dir.mkdir()
		elif helper_mode == "failing":
			support_scripts_dir = tmp / "failing_support_scripts"
			support_scripts_dir.mkdir()
			(support_scripts_dir / "review_filter_uninteresting_files.sh").write_text(
				"#!/usr/bin/env bash\nset -euo pipefail\nexit 42\n",
				encoding="utf-8",
			)
			(support_scripts_dir / "review_filter_uninteresting_files.sh").chmod(0o755)
		else:
			raise AssertionError(f"unknown helper_mode: {helper_mode}")

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(support_scripts_dir),
			"RUNTIME_DIR": str(runtime),
			"GITHUB_WORKSPACE": str(workspace),
			"REVIEWER_FILTER_UNINTERESTING_ENABLED": filter_enabled,
			"REVIEWER_FILTER_EXTRA_GLOBS": "",
			"REVIEWER_FILTER_EXEMPT_GLOBS": "db/contracts/**,**/migrations/**,**/migrate/**",
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"ORIGINAL_PR_DIFF_FILE": str(files["original_pr_diff"]),
			"LAST_RUN_DIFF_FILE": str(files["last_run_diff"]),
			"LAST_RUN_CHANGED_FILES_FILE": str(files["last_run_changed"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"LAST_RUN_DIFF_STAT_FILE": str(files["last_run_diff_stat"]),
			"LAST_COMMIT_STAT_FILE": str(files["last_commit_stat"]),
			"SYMBOL_DIFF_SUMMARY_FILE": str(files["symbol_diff"]),
			"HARNESS_STATE_FILE": str(files["state"]),
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{helper_block}\n"
				"prepare_reviewer_filtered_artifacts\n"
				"{\n"
				"\tprintf 'REVIEWER_FILTER_ACTIVE=%s\\n' \"${REVIEWER_FILTER_ACTIVE}\"\n"
				"\tprintf 'PR_DIFF_FILE=%s\\n' \"${PR_DIFF_FILE}\"\n"
				"\tprintf 'ORIGINAL_PR_DIFF_FILE=%s\\n' \"${ORIGINAL_PR_DIFF_FILE}\"\n"
				"\tprintf 'LAST_RUN_DIFF_FILE=%s\\n' \"${LAST_RUN_DIFF_FILE}\"\n"
				"\tprintf 'PR_CHANGED_FILES_FILE=%s\\n' \"${PR_CHANGED_FILES_FILE}\"\n"
				"\tprintf 'LAST_RUN_CHANGED_FILES_FILE=%s\\n' \"${LAST_RUN_CHANGED_FILES_FILE}\"\n"
				"\tprintf 'LAST_RUN_DIFF_STAT_FILE=%s\\n' \"${LAST_RUN_DIFF_STAT_FILE}\"\n"
				"\tprintf 'LAST_COMMIT_STAT_FILE=%s\\n' \"${LAST_COMMIT_STAT_FILE}\"\n"
				"\tprintf 'SYMBOL_DIFF_SUMMARY_FILE=%s\\n' \"${SYMBOL_DIFF_SUMMARY_FILE}\"\n"
				"} > \"${HARNESS_STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		state: dict[str, str] = {}
		for raw_line in files["state"].read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value
		artifact_contents = {
			f"{key}_CONTENT": Path(path).read_text(encoding="utf-8")
			for key, path in state.items()
			if key.endswith("_FILE")
		}
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**{key: value for key, value in state.items()},
			**artifact_contents,
		}


def _run_reviewer_stat_filter_harness(*, diff_stat_text: str, skipped_rows_text: str) -> str:
	helper_block = _reviewer_filter_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-filter-stat-") as td:
		tmp = Path(td)
		input_file = tmp / "diff_stat.txt"
		output_file = tmp / "filtered_diff_stat.txt"
		skipped_file = tmp / "skipped_paths.txt"
		input_file.write_text(diff_stat_text, encoding="utf-8")
		skipped_file.write_text(skipped_rows_text, encoding="utf-8")

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"RUNTIME_DIR": str(tmp),
			"PR_DIFF_FILE": str(input_file),
			"ORIGINAL_PR_DIFF_FILE": str(input_file),
			"LAST_RUN_DIFF_FILE": str(input_file),
			"LAST_RUN_CHANGED_FILES_FILE": str(input_file),
			"PR_CHANGED_FILES_FILE": str(input_file),
			"LAST_RUN_DIFF_STAT_FILE": str(input_file),
			"LAST_COMMIT_STAT_FILE": str(input_file),
			"SYMBOL_DIFF_SUMMARY_FILE": str(input_file),
			"INPUT_FILE": str(input_file),
			"OUTPUT_FILE": str(output_file),
			"SKIPPED_FILE": str(skipped_file),
		})
		subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{helper_block}\n"
				'filter_reviewer_stat_file_against_skips "${INPUT_FILE}" "${OUTPUT_FILE}" "${SKIPPED_FILE}"\n',
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return output_file.read_text(encoding="utf-8")


def _run_reviewer_risk_tier_harness(
	*,
	diff_text: str,
	current_changed_paths: list[str] | None = None,
	raw_changed_paths: list[str] | None = None,
	filter_active: str = "false",
	extra_env: dict[str, str] | None = None,
) -> dict[str, str | list[str]]:
	helper_block = _reviewer_risk_tier_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-risk-tier-") as td:
		tmp = Path(td)
		files = {
			"pr_diff": tmp / "pr_diff.patch",
			"pr_changed": tmp / "pr_changed_files.txt",
			"raw_pr_changed": tmp / "raw_pr_changed_files.txt",
			"state": tmp / "risk_tier_state.txt",
			"risk_tier": tmp / "reviewer_risk_tier.txt",
			"active_models": tmp / "reviewer_active_models.txt",
		}
		files["pr_diff"].write_text(diff_text, encoding="utf-8")
		visible_paths = current_changed_paths if current_changed_paths is not None else _diff_changed_paths(diff_text)
		files["pr_changed"].write_text("\n".join(visible_paths) + ("\n" if visible_paths else ""), encoding="utf-8")
		raw_paths = raw_changed_paths if raw_changed_paths is not None else visible_paths
		files["raw_pr_changed"].write_text("\n".join(raw_paths) + ("\n" if raw_paths else ""), encoding="utf-8")

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"RUNTIME_DIR": str(tmp),
			"PR_NUMBER": "123",
			"HAS_PR_DIFF": "true",
			"REVIEWER_FILTER_ACTIVE": filter_active,
			"REVIEWER_RISK_TIER_ENABLED": "true",
			"REVIEWER_MODELS": "\n".join(_workflow_reviewer_models()) + "\n",
			"REVIEWER_TIER_TRIVIAL_MODELS": "",
			"REVIEWER_TIER_LITE_MODELS": "",
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"RAW_REVIEWER_PR_CHANGED_FILES_FILE": str(files["raw_pr_changed"]),
			"STATE_FILE": str(files["state"]),
		})
		if extra_env:
			env.update(extra_env)

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{helper_block}\n"
				"classify_reviewer_risk_tier\n"
				"{\n"
				"\tprintf 'REVIEWER_RISK_TIER=%s\\n' \"${REVIEWER_RISK_TIER}\"\n"
				"\tprintf 'REVIEWER_RISK_TIER_FORCED_FULL=%s\\n' \"${REVIEWER_RISK_TIER_FORCED_FULL}\"\n"
				"\tprintf 'REVIEWER_RISK_TIER_LOC=%s\\n' \"${REVIEWER_RISK_TIER_LOC}\"\n"
				"\tprintf 'REVIEWER_RISK_TIER_FILES=%s\\n' \"${REVIEWER_RISK_TIER_FILES}\"\n"
				"\tprintf 'REVIEWER_ACTIVE_MODELS_SOURCE=%s\\n' \"${REVIEWER_ACTIVE_MODELS_SOURCE}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		state: dict[str, str] = {}
		for raw_line in files["state"].read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			"risk_tier_file": files["risk_tier"].read_text(encoding="utf-8").strip(),
			"active_models": files["active_models"].read_text(encoding="utf-8").splitlines(),
		}


def _run_review_tier_harness(
	*,
	diff_text: str,
	current_changed_paths: list[str] | None = None,
	raw_changed_paths: list[str] | None = None,
	extra_env: dict[str, str] | None = None,
) -> dict[str, str | list[str]]:
	helper_block = _reviewer_risk_tier_helper_block()
	with tempfile.TemporaryDirectory(prefix="review-tier-") as td:
		tmp = Path(td)
		files = {
			"pr_diff": tmp / "pr_diff.patch",
			"pr_changed": tmp / "pr_changed_files.txt",
			"raw_pr_changed": tmp / "raw_pr_changed_files.txt",
			"state": tmp / "review_tier_state.txt",
			"review_tier": tmp / "review_tier.txt",
			"active_models": tmp / "reviewer_active_models.txt",
			"github_env": tmp / "github_env.txt",
		}
		files["pr_diff"].write_text(diff_text, encoding="utf-8")
		visible_paths = current_changed_paths if current_changed_paths is not None else _diff_changed_paths(diff_text)
		files["pr_changed"].write_text("\n".join(visible_paths) + ("\n" if visible_paths else ""), encoding="utf-8")
		raw_paths = raw_changed_paths if raw_changed_paths is not None else visible_paths
		files["raw_pr_changed"].write_text("\n".join(raw_paths) + ("\n" if raw_paths else ""), encoding="utf-8")
		files["github_env"].write_text("", encoding="utf-8")

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"RUNTIME_DIR": str(tmp),
			"PR_NUMBER": "123",
			"HAS_PR_DIFF": "true",
			"REVIEW_TIER_RESOLVER_ENABLED": "true",
			"REVIEW_TIER_LITE_MAX_LOC": "50",
			"REVIEW_TIER_LITE_REVIEWER_SLUG": "qwen/qwen3.6-plus",
			"REVIEW_TIER_STANDARD_MAX_LOC": "200",
			"REVIEW_TIER_STANDARD_REVIEWER_SLUGS": "minimax/minimax-m2.5,deepseek/deepseek-v4-pro,x-ai/grok-4.20",
			"REVIEWER_MODELS": "\n".join(_workflow_reviewer_models()) + "\n",
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"ORIGINAL_PR_DIFF_FILE": str(files["pr_diff"]),
			"RAW_REVIEWER_PR_DIFF_FILE": str(files["pr_diff"]),
			"RAW_REVIEWER_ORIGINAL_PR_DIFF_FILE": str(files["pr_diff"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"RAW_REVIEWER_PR_CHANGED_FILES_FILE": str(files["raw_pr_changed"]),
			"GITHUB_ENV": str(files["github_env"]),
			"STATE_FILE": str(files["state"]),
		})
		if extra_env:
			env.update(extra_env)

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{helper_block}\n"
				"classify_review_tier\n"
				"{\n"
				"\tprintf 'REVIEW_TIER=%s\\n' \"${REVIEW_TIER}\"\n"
				"\tprintf 'REVIEW_TIER_REASON=%s\\n' \"${REVIEW_TIER_REASON}\"\n"
				"\tprintf 'REVIEW_TIER_FORCED_FULL=%s\\n' \"${REVIEW_TIER_FORCED_FULL}\"\n"
				"\tprintf 'REVIEW_TIER_LOC=%s\\n' \"${REVIEW_TIER_LOC}\"\n"
				"\tprintf 'REVIEW_TIER_SCOPE=%s\\n' \"${REVIEW_TIER_SCOPE}\"\n"
				"\tprintf 'REVIEW_TIER_ACTIVE_MODELS_SOURCE=%s\\n' \"${REVIEW_TIER_ACTIVE_MODELS_SOURCE}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		state: dict[str, str] = {}
		for raw_line in files["state"].read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			"review_tier_file": files["review_tier"].read_text(encoding="utf-8").strip(),
			"active_models": files["active_models"].read_text(encoding="utf-8").splitlines(),
			"github_env": files["github_env"].read_text(encoding="utf-8"),
		}


def _run_agents_md_materiality_harness(
	*,
	diff_text: str,
	changed_paths: list[str] | None = None,
	workspace_files: dict[str, str] | None = None,
	enabled: str = "true",
) -> dict[str, object]:
	with tempfile.TemporaryDirectory(prefix="agents-md-materiality-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		files = {
			"diff": tmp / "pr_diff.patch",
			"changed": tmp / "pr_changed_files.txt",
			"result": tmp / "agents_md_materiality_result.json",
			"comment": tmp / "agents_md_materiality_comment.md",
		}
		files["diff"].write_text(diff_text, encoding="utf-8")
		visible_paths = changed_paths if changed_paths is not None else _diff_changed_paths(diff_text)
		files["changed"].write_text("\n".join(visible_paths) + ("\n" if visible_paths else ""), encoding="utf-8")

		for rel_path, text in (workspace_files or {"agents.md": "# repo agents\n"}).items():
			target = workspace / rel_path
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text(text, encoding="utf-8")

		result = subprocess.run(
			["bash", str(AGENTS_MD_MATERIALITY)],
			cwd=str(REPO_ROOT),
			env={
				**os.environ,
				"AGENTS_MD_MATERIALITY_ENABLED": enabled,
				"AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED": "0",
				"AGENTS_MD_MATERIALITY_MODEL": "openai/gpt-5.4-mini",
				"AGENTS_MD_MATERIALITY_REASONING": "medium",
				"AGENTS_MD_MATERIALITY_RESULT_FILE": str(files["result"]),
				"AGENTS_MD_MATERIALITY_COMMENT_FILE": str(files["comment"]),
				"PR_CHANGED_FILES_FILE": str(files["changed"]),
				"PR_DIFF_FILE": str(files["diff"]),
				"GITHUB_WORKSPACE": str(workspace),
				"REPOSITORY": "octo/example",
				"PR_NUMBER": "123",
				"GITHUB_SERVER_URL": "https://github.com",
				"GITHUB_RUN_ID": "456",
				"BASE_BRANCH": "main",
			},
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"result": json.loads(files["result"].read_text(encoding="utf-8")),
			"comment": files["comment"].read_text(encoding="utf-8"),
		}


def _run_reviewer_scope_harness(*, scope_mode: str, last_run_changed_text: str, ledger_text: str) -> dict[str, str]:
	helper_block = _reviewer_iteration_scope_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-iteration-scope-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		files = {
			"last_run_changed": tmp / "last_run_changed_files.txt",
			"ledger": tmp / "ledger_status.txt",
			"scope_paths": tmp / "reviewer_scope_paths.txt",
			"scope_summary": tmp / "reviewer_scope_summary.txt",
			"scope_context": tmp / "reviewer_scoped_files_context.txt",
			"scope_query_seed": tmp / "reviewer_scope_query_seed.txt",
			"scope_context_source": tmp / "scope_context_source.txt",
			"semble_query": tmp / "reviewer_semble_query.txt",
			"context_sections": tmp / "context_sections.txt",
			"scoped_active": tmp / "scoped_active.txt",
			"symbol_diff": tmp / "symbol_diff_summary.txt",
			"original_pr_diff": tmp / "original_pr_diff.patch",
			"last_run_diff": tmp / "last_run_diff.patch",
			"pr_changed": tmp / "pr_changed_files.txt",
			"last_run_diff_stat": tmp / "last_run_diff_stat.txt",
			"last_commit_stat": tmp / "last_commit_stat.txt",
			"comments": tmp / "comments.txt",
			"checks": tmp / "checks.txt",
			"pr_diff": tmp / "pr_diff.patch",
		}
		files["last_run_changed"].write_text(last_run_changed_text, encoding="utf-8")
		files["ledger"].write_text(ledger_text, encoding="utf-8")
		files["scope_paths"].write_text("", encoding="utf-8")
		files["scope_summary"].write_text("", encoding="utf-8")
		files["scope_context"].write_text("", encoding="utf-8")
		files["scope_context_source"].write_text(
			"=== TARGETED FILE CONTEXT ===\nScoped reviewer file context sentinel\n",
			encoding="utf-8",
		)
		files["symbol_diff"].write_text("symbol diff sentinel\n", encoding="utf-8")
		files["original_pr_diff"].write_text("original pr diff sentinel\n", encoding="utf-8")
		files["last_run_diff"].write_text("last run diff sentinel\n", encoding="utf-8")
		files["pr_changed"].write_text("scripts/review_run_reviewers.sh\nextra/pr_scope.py\n", encoding="utf-8")
		files["last_run_diff_stat"].write_text("1 file changed\n", encoding="utf-8")
		files["last_commit_stat"].write_text("commit stat sentinel\n", encoding="utf-8")
		files["comments"].write_text("comments sentinel\n", encoding="utf-8")
		files["checks"].write_text("checks sentinel\n", encoding="utf-8")
		files["pr_diff"].write_text("full pr diff sentinel\n", encoding="utf-8")
		(workspace / "scripts").mkdir()
		(workspace / "tests").mkdir()
		(workspace / "scripts" / "review_run_reviewers.sh").write_text("scoped shell target\n", encoding="utf-8")
		(workspace / "tests" / "test_review_autofix_review_pipeline_contract.py").write_text("scoped test target\n", encoding="utf-8")

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"LAST_RUN_CHANGED_FILES_FILE": str(files["last_run_changed"]),
			"LEDGER_STATUS_FILE": str(files["ledger"]),
			"REVIEWER_SCOPE_PATHS_FILE": str(files["scope_paths"]),
			"REVIEWER_SCOPE_SUMMARY_FILE": str(files["scope_summary"]),
			"REVIEWER_SCOPED_FILES_CONTEXT_FILE": str(files["scope_context"]),
			"REVIEWER_SCOPE_QUERY_SEED_FILE": str(files["scope_query_seed"]),
			"SCOPE_CONTEXT_SOURCE_FILE": str(files["scope_context_source"]),
			"REVIEWER_SEMBLE_QUERY_FILE": str(files["semble_query"]),
			"OUTPUT_CONTEXT_FILE": str(files["context_sections"]),
			"SCOPED_ACTIVE_FILE": str(files["scoped_active"]),
			"SYMBOL_DIFF_SUMMARY_FILE": str(files["symbol_diff"]),
			"ORIGINAL_PR_DIFF_FILE": str(files["original_pr_diff"]),
			"LAST_RUN_DIFF_FILE": str(files["last_run_diff"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"LAST_RUN_DIFF_STAT_FILE": str(files["last_run_diff_stat"]),
			"LAST_COMMIT_STAT_FILE": str(files["last_commit_stat"]),
			"PR_ALL_COMMENTS_CONTEXT_FILE": str(files["comments"]),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(files["checks"]),
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"TARGETED_FILE_CONTEXT_SCRIPT": str(REPO_ROOT / "scripts" / "targeted_file_context.py"),
			"TARGETED_FILE_CONTEXT_MAX_BYTES": "8192",
			"GITHUB_WORKSPACE": str(workspace),
			"SEMBLE_INDEX_AVAILABLE": "false",
			"SCOPE_MODE": scope_mode,
			"USE_PREPARE": "0",
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				"_embed_input_file() { local _p=\"${1:-}\"; if [ -z \"${_p}\" ] || [ ! -e \"${_p}\" ]; then printf '(missing)\\n'; return 0; fi; if [ ! -s \"${_p}\" ]; then printf '(empty)\\n'; return 0; fi; cat \"${_p}\"; }\n"
				f"{helper_block}\n"
				"if [ \"${SCOPE_MODE}\" = \"auto\" ]; then\n"
				"\tREVIEWER_SCOPED_CONTEXT_ACTIVE=false\n"
				"\tif [ \"${USE_PREPARE}\" = \"1\" ]; then\n"
				"\t\tif prepare_reviewer_scoped_context; then\n"
				"\t\t\tREVIEWER_SCOPED_CONTEXT_ACTIVE=true\n"
				"\t\tfi\n"
				"\telif build_reviewer_iteration_scope_artifacts \"${LAST_RUN_CHANGED_FILES_FILE}\" \"${LEDGER_STATUS_FILE}\" \"${REVIEWER_SCOPE_PATHS_FILE}\" \"${REVIEWER_SCOPE_SUMMARY_FILE}\"; then\n"
				"\t\tREVIEWER_SCOPED_CONTEXT_ACTIVE=true\n"
				"\t\tcp \"${SCOPE_CONTEXT_SOURCE_FILE}\" \"${REVIEWER_SCOPED_FILES_CONTEXT_FILE}\"\n"
				"\tfi\n"
				"else\n"
				"\tREVIEWER_SCOPED_CONTEXT_ACTIVE=false\n"
				"\twrite_reviewer_scope_summary \"full-diff\" \"first iteration — keep full PR context\"\n"
				"fi\n"
				"emit_reviewer_prompt_context_sections > \"${OUTPUT_CONTEXT_FILE}\"\n"
				"build_reviewer_semble_query\n"
				"printf '%s\\n' \"${REVIEWER_SCOPED_CONTEXT_ACTIVE}\" > \"${SCOPED_ACTIVE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"context_sections": files["context_sections"].read_text(encoding="utf-8"),
			"scope_summary": files["scope_summary"].read_text(encoding="utf-8"),
			"scope_paths": files["scope_paths"].read_text(encoding="utf-8"),
			"scope_context": files["scope_context"].read_text(encoding="utf-8"),
			"semble_query": files["semble_query"].read_text(encoding="utf-8"),
			"scoped_active": files["scoped_active"].read_text(encoding="utf-8").strip(),
		}


def _run_prepare_reviewer_scope_harness(*, last_run_changed_text: str, ledger_text: str, missing_targeted_script: bool = False, workspace_files: dict[str, str] | None = None) -> dict[str, str]:
	helper_block = _reviewer_iteration_scope_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-iteration-scope-prepare-") as td:
		tmp = Path(td)
		workspace = tmp / "workspace"
		workspace.mkdir()
		files = {
			"last_run_changed": tmp / "last_run_changed_files.txt",
			"ledger": tmp / "ledger_status.txt",
			"scope_paths": tmp / "reviewer_scope_paths.txt",
			"scope_summary": tmp / "reviewer_scope_summary.txt",
			"scope_context": tmp / "reviewer_scoped_files_context.txt",
			"scope_query_seed": tmp / "reviewer_scope_query_seed.txt",
			"scope_context_source": tmp / "scope_context_source.txt",
			"semble_query": tmp / "reviewer_semble_query.txt",
			"context_sections": tmp / "context_sections.txt",
			"scoped_active": tmp / "scoped_active.txt",
			"symbol_diff": tmp / "symbol_diff_summary.txt",
			"original_pr_diff": tmp / "original_pr_diff.patch",
			"last_run_diff": tmp / "last_run_diff.patch",
			"pr_changed": tmp / "pr_changed_files.txt",
			"last_run_diff_stat": tmp / "last_run_diff_stat.txt",
			"last_commit_stat": tmp / "last_commit_stat.txt",
			"comments": tmp / "comments.txt",
			"checks": tmp / "checks.txt",
			"pr_diff": tmp / "pr_diff.patch",
		}
		files["last_run_changed"].write_text(last_run_changed_text, encoding="utf-8")
		files["ledger"].write_text(ledger_text, encoding="utf-8")
		files["scope_paths"].write_text("", encoding="utf-8")
		files["scope_summary"].write_text("", encoding="utf-8")
		files["scope_context"].write_text("", encoding="utf-8")
		files["scope_query_seed"].write_text("", encoding="utf-8")
		files["scope_context_source"].write_text("unused sentinel\n", encoding="utf-8")
		files["symbol_diff"].write_text("symbol diff sentinel\n", encoding="utf-8")
		files["original_pr_diff"].write_text("original pr diff sentinel\n", encoding="utf-8")
		files["last_run_diff"].write_text("last run diff sentinel\n", encoding="utf-8")
		files["pr_changed"].write_text("scripts/review_run_reviewers.sh\nextra/pr_scope.py\n", encoding="utf-8")
		files["last_run_diff_stat"].write_text("1 file changed\n", encoding="utf-8")
		files["last_commit_stat"].write_text("commit stat sentinel\n", encoding="utf-8")
		files["comments"].write_text("comments sentinel\n", encoding="utf-8")
		files["checks"].write_text("checks sentinel\n", encoding="utf-8")
		files["pr_diff"].write_text("full pr diff sentinel\n", encoding="utf-8")

		for rel_path, text in (workspace_files or {
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"tests/test_review_autofix_review_pipeline_contract.py": "scoped test target\n",
		}).items():
			target = workspace / rel_path
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text(text, encoding="utf-8")

		targeted_script = tmp / "missing_targeted_file_context.py" if missing_targeted_script else REPO_ROOT / "scripts" / "targeted_file_context.py"

		env = os.environ.copy()
		env.update({
			"SUPPORT_ROOT_DIR": str(REPO_ROOT),
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"LAST_RUN_CHANGED_FILES_FILE": str(files["last_run_changed"]),
			"LEDGER_STATUS_FILE": str(files["ledger"]),
			"REVIEWER_SCOPE_PATHS_FILE": str(files["scope_paths"]),
			"REVIEWER_SCOPE_SUMMARY_FILE": str(files["scope_summary"]),
			"REVIEWER_SCOPED_FILES_CONTEXT_FILE": str(files["scope_context"]),
			"REVIEWER_SCOPE_QUERY_SEED_FILE": str(files["scope_query_seed"]),
			"SCOPE_CONTEXT_SOURCE_FILE": str(files["scope_context_source"]),
			"REVIEWER_SEMBLE_QUERY_FILE": str(files["semble_query"]),
			"OUTPUT_CONTEXT_FILE": str(files["context_sections"]),
			"SCOPED_ACTIVE_FILE": str(files["scoped_active"]),
			"SYMBOL_DIFF_SUMMARY_FILE": str(files["symbol_diff"]),
			"ORIGINAL_PR_DIFF_FILE": str(files["original_pr_diff"]),
			"LAST_RUN_DIFF_FILE": str(files["last_run_diff"]),
			"PR_CHANGED_FILES_FILE": str(files["pr_changed"]),
			"LAST_RUN_DIFF_STAT_FILE": str(files["last_run_diff_stat"]),
			"LAST_COMMIT_STAT_FILE": str(files["last_commit_stat"]),
			"PR_ALL_COMMENTS_CONTEXT_FILE": str(files["comments"]),
			"PR_CHECK_RUNS_CONTEXT_FILE": str(files["checks"]),
			"PR_DIFF_FILE": str(files["pr_diff"]),
			"TARGETED_FILE_CONTEXT_SCRIPT": str(targeted_script),
			"TARGETED_FILE_CONTEXT_MAX_BYTES": "8192",
			"GITHUB_WORKSPACE": str(workspace),
			"SEMBLE_INDEX_AVAILABLE": "false",
			"SCOPE_MODE": "auto",
			"USE_PREPARE": "1",
		})
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				"_embed_input_file() { local _p=\"${1:-}\"; if [ -z \"${_p}\" ] || [ ! -e \"${_p}\" ]; then printf '(missing)\\n'; return 0; fi; if [ ! -s \"${_p}\" ]; then printf '(empty)\\n'; return 0; fi; cat \"${_p}\"; }\n"
				f"{helper_block}\n"
				"REVIEWER_SCOPED_CONTEXT_ACTIVE=false\n"
				"if prepare_reviewer_scoped_context; then\n"
				"\tREVIEWER_SCOPED_CONTEXT_ACTIVE=true\n"
				"fi\n"
				"emit_reviewer_prompt_context_sections > \"${OUTPUT_CONTEXT_FILE}\"\n"
				"build_reviewer_semble_query\n"
				"printf '%s\\n' \"${REVIEWER_SCOPED_CONTEXT_ACTIVE}\" > \"${SCOPED_ACTIVE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"context_sections": files["context_sections"].read_text(encoding="utf-8"),
			"scope_summary": files["scope_summary"].read_text(encoding="utf-8"),
			"scope_paths": files["scope_paths"].read_text(encoding="utf-8"),
			"scope_context": files["scope_context"].read_text(encoding="utf-8"),
			"semble_query": files["semble_query"].read_text(encoding="utf-8"),
			"scoped_active": files["scoped_active"].read_text(encoding="utf-8").strip(),
		}
def _run_reviewer_failback_harness() -> dict[str, object]:
	partial_finalize_budget_helper_block = _reviewer_partial_finalize_budget_helper_block()
	helper_block = _reviewer_failback_helper_block()
	run_reviewer_block = _reviewer_run_reviewer_block()
	run_reviewer_pass_block = _reviewer_run_reviewer_pass_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-failback-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		runtime = tmp / "runtime"
		home = tmp / "home"
		runner_temp = tmp / "runner_temp"
		bin_dir = tmp / "bin"
		seed_codex_home = tmp / "codex_home_seed"
		reviews.mkdir()
		runtime.mkdir()
		home.mkdir()
		runner_temp.mkdir()
		bin_dir.mkdir()
		seed_codex_home.mkdir()

		prompt_file = runtime / "reviewer_prompt.patch"
		prompt_file.write_text(PHASE_G_FLAKY_REVIEWER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

		failback_file = tmp / "reviewer_failback_chains.json"
		failback_file.write_text(REVIEWER_FAILBACK_CHAINS.read_text(encoding="utf-8"), encoding="utf-8")

		catalog_file = tmp / "codex_model_catalog.json"
		catalog_file.write_text(MODEL_CATALOG.read_text(encoding="utf-8"), encoding="utf-8")

		health_file = tmp / ".ai" / "review_runtime" / "pr-123" / "reviewer_health_state.json"
		health_file.parent.mkdir(parents=True)
		attempt_log_file = tmp / "attempts.tsv"
		state_file = tmp / "state.txt"
		github_env_file = tmp / "github_env.txt"

		codex_stub = bin_dir / "codex"
		codex_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		codex_stub.chmod(0o755)

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNTIME_DIR": str(runtime),
			"PROMPT_FILE": str(prompt_file),
			"PR_NUMBER": "123",
			"RUNNER_TEMP": str(runner_temp),
			"HOME": str(home),
			"CODEX_HOME": str(seed_codex_home),
			"GITHUB_ENV": str(github_env_file),
			"REVIEWER_CIRCUIT_BREAKER_ENABLED": "1",
			"REVIEWER_FAILBACK_MAX_RETRIES": "1",
			"REVIEWER_HEALTH_OPEN_THRESHOLD": "1",
			"REVIEWER_HEALTH_OPEN_TTL_SECS": "600",
			"REVIEWER_FAILBACK_CHAINS_FILE": str(failback_file),
			"REVIEWER_MODEL_CATALOG_FILE": str(catalog_file),
			"REVIEWER_HEALTH_STATE_FILE": str(health_file),
			"REVIEWER_REASONING_EFFORT": "xhigh",
			"HEARTBEAT_IDLE_TIMEOUT": "30",
			"HEARTBEAT_MAX_WALL": "30",
			"REVIEW_PR_STATE_POLL_INTERVAL_SECS": "10",
			"ATTEMPT_LOG_FILE": str(attempt_log_file),
			"STATE_FILE": str(state_file),
		})
		env.update(_reviewer_harness_budget_env())

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{partial_finalize_budget_helper_block}\n"
				f"{helper_block}\n"
				f"{run_reviewer_block}\n"
				f"{run_reviewer_pass_block}\n"
				"execute_reviewer_attempt() {\n"
				"\tlocal _attempt_label=\"${1:-}\"\n"
				"\tlocal _attempt_number=\"${2:-}\"\n"
				"\tlocal _attempt_reasoning=\"${3:-}\"\n"
				"\tprintf '%s\t%s\t%s\t%s\n' \"${slot_model}\" \"${effective_model}\" \"${_attempt_reasoning:-<empty>}\" \"${_attempt_label}\" >> \"${ATTEMPT_LOG_FILE}\"\n"
				"\tcase \"${slot_model}\" in\n"
				"\t\tx-ai/grok-4.20)\n"
				"\t\t\tcase \"${effective_model}\" in\n"
				"\t\t\t\tx-ai/grok-4.20)\n"
				"\t\t\t\t\tREVIEWER_ATTEMPT_OUTCOME=\"retryable_failure\"\n"
				"\t\t\t\t\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"rate_limit\"\n"
				"\t\t\t\t\tREVIEWER_ATTEMPT_CMD_RC=1\n"
				"\t\t\t\t\treturn 0\n"
				"\t\t\t\t\t;;\n"
				"\t\t\t\tx-ai/grok-4.1-fast)\n"
				"\t\t\t\t\tprintf 'fallback success for %s\\n' \"${slot_model}\" > \"${output_file}\"\n"
				"\t\t\t\t\tREVIEWER_ATTEMPT_OUTCOME=\"success\"\n"
				"\t\t\t\t\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"\"\n"
				"\t\t\t\t\tREVIEWER_ATTEMPT_CMD_RC=0\n"
				"\t\t\t\t\treturn 0\n"
				"\t\t\t\t\t;;\n"
				"\t\t\tesac\n"
				"\t\t\t;;\n"
				"\t\tmoonshotai/kimi-k2.5)\n"
				"\t\t\tREVIEWER_ATTEMPT_OUTCOME=\"retryable_failure\"\n"
				"\t\t\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"server_error\"\n"
				"\t\t\tREVIEWER_ATTEMPT_CMD_RC=1\n"
				"\t\t\treturn 0\n"
				"\t\t\t;;\n"
				"\tesac\n"
				"\tREVIEWER_ATTEMPT_OUTCOME=\"failed\"\n"
				"\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"\"\n"
				"\tREVIEWER_ATTEMPT_CMD_RC=1\n"
				"\treturn 0\n"
				"}\n"
				"get_active_reviewer_models_text() {\n"
				"\tprintf 'x-ai/grok-4.20\\n'\n"
				"}\n"
				"prepare_reviewer_prompt_for_model() {\n"
				"\tprintf '%s\\n' \"${2:-}\"\n"
				"}\n"
				"mapped_model=\"x-ai/grok-4.20\"\n"
				"mapped_safe_name=\"$(printf '%s' \"${mapped_model}\" | tr '/.:' '___')\"\n"
				"unmapped_model=\"moonshotai/kimi-k2.5\"\n"
				"unmapped_safe_name=\"$(printf '%s' \"${unmapped_model}\" | tr '/.:' '___')\"\n"
				"run_reviewer \"${mapped_model}\" \"${mapped_safe_name}\" \"mapped\" \"${PROMPT_FILE}\" \"xhigh\"\n"
				"cached_successes=\"$(run_reviewer_pass \"cached\" \"${PROMPT_FILE}\" \"xhigh\")\"\n"
				"run_reviewer \"${unmapped_model}\" \"${unmapped_safe_name}\" \"unmapped\" \"${PROMPT_FILE}\" \"xhigh\"\n"
				"{\n"
				"\tprintf 'MAPPED_STATUS_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/status_mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_OUTPUT_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_LOG_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.log\"\n"
				"\tprintf 'CACHED_STATUS_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/status_cached_${mapped_safe_name}.txt\"\n"
				"\tprintf 'CACHED_OUTPUT_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/cached_${mapped_safe_name}.txt\"\n"
				"\tprintf 'CACHED_LOG_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/cached_${mapped_safe_name}.log\"\n"
				"\tprintf 'UNMAPPED_STATUS_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/status_unmapped_${unmapped_safe_name}.txt\"\n"
				"\tprintf 'UNMAPPED_OUTPUT_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/unmapped_${unmapped_safe_name}.txt\"\n"
				"\tprintf 'UNMAPPED_LOG_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/unmapped_${unmapped_safe_name}.log\"\n"
				"\tprintf 'ATTEMPT_LOG_FILE=%s\\n' \"${ATTEMPT_LOG_FILE}\"\n"
				"\tprintf 'CACHED_SUCCESSES=%s\\n' \"${cached_successes}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		state: dict[str, str] = {}
		for raw_line in state_file.read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value

		artifact_contents = {
			f"{key}_CONTENT": Path(path).read_text(encoding="utf-8")
			for key, path in state.items()
			if key.endswith("_FILE")
		}

		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			**artifact_contents,
			"health_state": json.loads(health_file.read_text(encoding="utf-8")),
		}


def _run_reviewer_slot_retry_budget_harness() -> dict[str, object]:
	partial_finalize_budget_helper_block = _reviewer_partial_finalize_budget_helper_block()
	helper_block = _reviewer_failback_helper_block()
	run_reviewer_block = _reviewer_run_reviewer_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-slot-retry-budget-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		runtime = tmp / "runtime"
		home = tmp / "home"
		runner_temp = tmp / "runner_temp"
		bin_dir = tmp / "bin"
		seed_codex_home = tmp / "codex_home_seed"
		reviews.mkdir()
		runtime.mkdir()
		home.mkdir()
		runner_temp.mkdir()
		bin_dir.mkdir()
		seed_codex_home.mkdir()

		prompt_file = runtime / "reviewer_prompt.patch"
		prompt_file.write_text(PHASE_G_FLAKY_REVIEWER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

		failback_file = tmp / "reviewer_failback_chains.json"
		failback_file.write_text(REVIEWER_FAILBACK_CHAINS.read_text(encoding="utf-8"), encoding="utf-8")

		catalog_file = tmp / "codex_model_catalog.json"
		catalog_file.write_text(MODEL_CATALOG.read_text(encoding="utf-8"), encoding="utf-8")

		attempt_log_file = tmp / "attempts.tsv"
		state_file = tmp / "state.txt"
		github_env_file = tmp / "github_env.txt"

		codex_stub = bin_dir / "codex"
		codex_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		codex_stub.chmod(0o755)

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNTIME_DIR": str(runtime),
			"PROMPT_FILE": str(prompt_file),
			"PR_NUMBER": "123",
			"RUNNER_TEMP": str(runner_temp),
			"HOME": str(home),
			"CODEX_HOME": str(seed_codex_home),
			"GITHUB_ENV": str(github_env_file),
			"REVIEWER_CIRCUIT_BREAKER_ENABLED": "0",
			"REVIEWER_FAILBACK_MAX_RETRIES": "1",
			"REVIEWER_FAILBACK_CHAINS_FILE": str(failback_file),
			"REVIEWER_MODEL_CATALOG_FILE": str(catalog_file),
			"REVIEWER_SLOT_RETRYABLE_FAILURE_LIMIT": "3",
			"REVIEWER_SLOT_BACKOFF_BASE_SECS": "0",
			"REVIEWER_SLOT_BACKOFF_CAP_SECS": "0",
			"REVIEWER_SLOT_BACKOFF_BUDGET_RATIO": "0.05",
			"REVIEWER_REASONING_EFFORT": "xhigh",
			"HEARTBEAT_IDLE_TIMEOUT": "30",
			"HEARTBEAT_MAX_WALL": "30",
			"REVIEW_PR_STATE_POLL_INTERVAL_SECS": "10",
			"ATTEMPT_LOG_FILE": str(attempt_log_file),
			"STATE_FILE": str(state_file),
		})
		env.update(_reviewer_harness_budget_env())

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{partial_finalize_budget_helper_block}\n"
				f"{helper_block}\n"
				f"{run_reviewer_block}\n"
				"execute_reviewer_attempt() {\n"
				"\tlocal _attempt_label=\"${1:-}\"\n"
				"\tlocal _attempt_number=\"${2:-}\"\n"
				"\tlocal _attempt_reasoning=\"${3:-}\"\n"
				"\treviewer_log_cache_attempt \"${_attempt_number}\" \"${PROMPT_FILE}\" \"${PROMPT_FILE}\" \"${log_file}\" \"${slot_model}\" \"${effective_model}\"\n"
				"\tprintf '%s\t%s\t%s\t%s\n' \"${slot_model}\" \"${effective_model}\" \"${_attempt_reasoning:-<empty>}\" \"${_attempt_label}\" >> \"${ATTEMPT_LOG_FILE}\"\n"
				"\tif [ \"${_attempt_number}\" -gt 1 ]; then\n"
				"\t\tcache_read_input_tokens=3500\n"
				"\telse\n"
				"\t\tcache_read_input_tokens=0\n"
				"\tfi\n"
				"\tprintf 'INFO: openrouter usage phase=review call=mapped model=%s cache_enabled=true cache_breakpoint_enabled=na cache_breakpoint_fallback_retry=na prompt_tokens=4000 completion_tokens=100 total_tokens=4100 cache_creation_input_tokens=0 cache_read_input_tokens=%s\n' \"${effective_model}\" \"${cache_read_input_tokens}\" >> \"${log_file}\"\n"
				"\tREVIEWER_ATTEMPT_OUTCOME=\"retryable_failure\"\n"
				"\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"rate_limit\"\n"
				"\tREVIEWER_ATTEMPT_CMD_RC=1\n"
				"\treturn 0\n"
				"}\n"
				"prepare_reviewer_prompt_for_model() {\n"
				"\tprintf '%s\\n' \"${2:-}\"\n"
				"}\n"
				"mapped_model=\"x-ai/grok-4.20\"\n"
				"mapped_safe_name=\"$(printf '%s' \"${mapped_model}\" | tr '/.:' '___')\"\n"
				"run_reviewer \"${mapped_model}\" \"${mapped_safe_name}\" \"mapped\" \"${PROMPT_FILE}\" \"xhigh\"\n"
				"{\n"
				"\tprintf 'MAPPED_STATUS_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/status_mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_OUTPUT_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_LOG_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.log\"\n"
				"\tprintf 'ATTEMPT_LOG_FILE=%s\\n' \"${ATTEMPT_LOG_FILE}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		state: dict[str, str] = {}
		for raw_line in state_file.read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value

		artifact_contents = {
			f"{key}_CONTENT": Path(path).read_text(encoding="utf-8")
			for key, path in state.items()
			if key.endswith("_FILE")
		}

		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			**artifact_contents,
		}


def _run_reviewer_stall_recovery_harness() -> dict[str, object]:
	partial_finalize_budget_helper_block = _reviewer_partial_finalize_budget_helper_block()
	helper_block = _reviewer_failback_helper_block()
	run_reviewer_block = _reviewer_run_reviewer_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-stall-recovery-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		runtime = tmp / "runtime"
		home = tmp / "home"
		runner_temp = tmp / "runner_temp"
		bin_dir = tmp / "bin"
		seed_codex_home = tmp / "codex_home_seed"
		reviews.mkdir()
		runtime.mkdir()
		home.mkdir()
		runner_temp.mkdir()
		bin_dir.mkdir()
		seed_codex_home.mkdir()

		prompt_file = runtime / "reviewer_prompt.patch"
		prompt_file.write_text(PHASE_G_FLAKY_REVIEWER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

		failback_file = tmp / "reviewer_failback_chains.json"
		failback_file.write_text(REVIEWER_FAILBACK_CHAINS.read_text(encoding="utf-8"), encoding="utf-8")

		catalog_file = tmp / "codex_model_catalog.json"
		catalog_file.write_text(MODEL_CATALOG.read_text(encoding="utf-8"), encoding="utf-8")

		health_file = tmp / ".ai" / "review_runtime" / "pr-123" / "reviewer_health_state.json"
		health_file.parent.mkdir(parents=True)
		attempt_log_file = tmp / "attempts.tsv"
		state_file = tmp / "state.txt"
		github_env_file = tmp / "github_env.txt"

		codex_stub = bin_dir / "codex"
		codex_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		codex_stub.chmod(0o755)

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNTIME_DIR": str(runtime),
			"PROMPT_FILE": str(prompt_file),
			"PR_NUMBER": "123",
			"RUNNER_TEMP": str(runner_temp),
			"HOME": str(home),
			"CODEX_HOME": str(seed_codex_home),
			"GITHUB_ENV": str(github_env_file),
			"REVIEWER_CIRCUIT_BREAKER_ENABLED": "1",
			"REVIEWER_FAILBACK_MAX_RETRIES": "1",
			"REVIEWER_HEALTH_OPEN_THRESHOLD": "1",
			"REVIEWER_HEALTH_OPEN_TTL_SECS": "600",
			"REVIEWER_FAILBACK_CHAINS_FILE": str(failback_file),
			"REVIEWER_MODEL_CATALOG_FILE": str(catalog_file),
			"REVIEWER_HEALTH_STATE_FILE": str(health_file),
			"REVIEWER_REASONING_EFFORT": "xhigh",
			"HEARTBEAT_IDLE_TIMEOUT": "30",
			"HEARTBEAT_MAX_WALL": "30",
			"REVIEW_PR_STATE_POLL_INTERVAL_SECS": "10",
			"ATTEMPT_LOG_FILE": str(attempt_log_file),
			"STATE_FILE": str(state_file),
		})
		env.update(_reviewer_harness_budget_env())

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{partial_finalize_budget_helper_block}\n"
				f"{helper_block}\n"
				f"{run_reviewer_block}\n"
				"execute_reviewer_attempt() {\n"
				"\tlocal _attempt_label=\"${1:-}\"\n"
				"\tlocal _attempt_number=\"${2:-}\"\n"
				"\tlocal _attempt_reasoning=\"${3:-}\"\n"
				"\tprintf '%s\t%s\t%s\t%s\n' \"${slot_model}\" \"${effective_model}\" \"${_attempt_reasoning:-<empty>}\" \"${_attempt_label}\" >> \"${ATTEMPT_LOG_FILE}\"\n"
				"\tcase \"${effective_model}:${_attempt_number}\" in\n"
				"\t\tx-ai/grok-4.20:1|x-ai/grok-4.20:2)\n"
				"\t\t\tprintf 'Reviewer slot %s (%s) recorded codex_stall_killed on %s (exit=137).\\n' \"${slot_model}\" \"${effective_model}\" \"${_attempt_label}\" >> \"${log_file}\"\n"
				"\t\t\tprintf 'Reviewer slot %s (%s) failure classified as retryable (stall_guard) on %s.\\n' \"${slot_model}\" \"${effective_model}\" \"${_attempt_label}\" >> \"${log_file}\"\n"
				"\t\t\tREVIEWER_ATTEMPT_OUTCOME=\"retryable_failure\"\n"
				"\t\t\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"stall_guard\"\n"
				"\t\t\tREVIEWER_ATTEMPT_CMD_RC=137\n"
				"\t\t\treturn 0\n"
				"\t\t\t;;\n"
				"\t\tx-ai/grok-4.1-fast:3)\n"
				"\t\t\tprintf 'fallback success for %s\\n' \"${slot_model}\" > \"${output_file}\"\n"
				"\t\t\tREVIEWER_ATTEMPT_OUTCOME=\"success\"\n"
				"\t\t\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"\"\n"
				"\t\t\tREVIEWER_ATTEMPT_CMD_RC=0\n"
				"\t\t\treturn 0\n"
				"\t\t\t;;\n"
				"\tesac\n"
				"\tREVIEWER_ATTEMPT_OUTCOME=\"failed\"\n"
				"\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"\"\n"
				"\tREVIEWER_ATTEMPT_CMD_RC=1\n"
				"\treturn 0\n"
				"}\n"
				"prepare_reviewer_prompt_for_model() {\n"
				"\tprintf '%s\\n' \"${2:-}\"\n"
				"}\n"
				"mapped_model=\"x-ai/grok-4.20\"\n"
				"mapped_safe_name=\"$(printf '%s' \"${mapped_model}\" | tr '/.:' '___')\"\n"
				"run_reviewer \"${mapped_model}\" \"${mapped_safe_name}\" \"mapped\" \"${PROMPT_FILE}\" \"xhigh\"\n"
				"{\n"
				"\tprintf 'MAPPED_STATUS_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/status_mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_OUTPUT_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_LOG_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.log\"\n"
				"\tprintf 'ATTEMPT_LOG_FILE=%s\\n' \"${ATTEMPT_LOG_FILE}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		state: dict[str, str] = {}
		for raw_line in state_file.read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value

		artifact_contents = {
			f"{key}_CONTENT": Path(path).read_text(encoding="utf-8")
			for key, path in state.items()
			if key.endswith("_FILE")
		}

		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			**artifact_contents,
			"health_state": json.loads(health_file.read_text(encoding="utf-8")),
		}


def _run_reviewer_silent_retry_harness() -> dict[str, object]:
	partial_finalize_budget_helper_block = _reviewer_partial_finalize_budget_helper_block()
	helper_block = _reviewer_failback_helper_block()
	run_reviewer_block = _reviewer_run_reviewer_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-silent-retry-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		runtime = tmp / "runtime"
		home = tmp / "home"
		runner_temp = tmp / "runner_temp"
		bin_dir = tmp / "bin"
		seed_codex_home = tmp / "codex_home_seed"
		reviews.mkdir()
		runtime.mkdir()
		home.mkdir()
		runner_temp.mkdir()
		bin_dir.mkdir()
		seed_codex_home.mkdir()

		prompt_file = runtime / "reviewer_prompt.patch"
		prompt_file.write_text(PHASE_G_FLAKY_REVIEWER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

		attempt_log_file = tmp / "attempts.txt"
		state_file = tmp / "state.txt"
		github_env_file = tmp / "github_env.txt"

		codex_stub = bin_dir / "codex"
		codex_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		codex_stub.chmod(0o755)

		env = os.environ.copy()
		env.update({
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNTIME_DIR": str(runtime),
			"PROMPT_FILE": str(prompt_file),
			"PR_NUMBER": "123",
			"RUNNER_TEMP": str(runner_temp),
			"HOME": str(home),
			"CODEX_HOME": str(seed_codex_home),
			"GITHUB_ENV": str(github_env_file),
			"REVIEWER_CIRCUIT_BREAKER_ENABLED": "0",
			"REVIEWER_REASONING_EFFORT": "xhigh",
			"HEARTBEAT_IDLE_TIMEOUT": "30",
			"HEARTBEAT_MAX_WALL": "30",
			"REVIEW_PR_STATE_POLL_INTERVAL_SECS": "10",
			"ATTEMPT_LOG_FILE": str(attempt_log_file),
			"STATE_FILE": str(state_file),
		})
		env.update(_reviewer_harness_budget_env())

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{partial_finalize_budget_helper_block}\n"
				f"{helper_block}\n"
				f"{run_reviewer_block}\n"
				"execute_reviewer_attempt() {\n"
				"\tlocal _attempt_label=\"${1:-}\"\n"
				"\tprintf '%s\\n' \"${_attempt_label}\" >> \"${ATTEMPT_LOG_FILE}\"\n"
				"\tREVIEWER_ATTEMPT_OUTCOME=\"silent_retry\"\n"
				"\tREVIEWER_ATTEMPT_RETRYABLE_CLASS=\"\"\n"
				"\tREVIEWER_ATTEMPT_CMD_RC=0\n"
				"\treturn 0\n"
				"}\n"
				"prepare_reviewer_prompt_for_model() {\n"
				"\tprintf '%s\\n' \"${2:-}\"\n"
				"}\n"
				"mapped_model=\"x-ai/grok-4.20\"\n"
				"mapped_safe_name=\"$(printf '%s' \"${mapped_model}\" | tr '/.:' '___')\"\n"
				"run_reviewer \"${mapped_model}\" \"${mapped_safe_name}\" \"mapped\" \"${PROMPT_FILE}\" \"xhigh\"\n"
				"{\n"
				"\tprintf 'MAPPED_STATUS_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/status_mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_OUTPUT_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.txt\"\n"
				"\tprintf 'MAPPED_LOG_FILE=%s\\n' \"${PREVIOUS_REVIEWS_DIR}/mapped_${mapped_safe_name}.log\"\n"
				"\tprintf 'ATTEMPT_LOG_FILE=%s\\n' \"${ATTEMPT_LOG_FILE}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		state: dict[str, str] = {}
		for raw_line in state_file.read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value

		artifact_contents = {
			f"{key}_CONTENT": Path(path).read_text(encoding="utf-8")
			for key, path in state.items()
			if key.endswith("_FILE")
		}

		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			**artifact_contents,
		}


def _run_review_pipeline_summary_step_harness(*, extra_env: dict[str, str] | None = None) -> dict[str, object]:
	summary_script = _step_run_script("Append review pipeline iteration summary")
	with tempfile.TemporaryDirectory(prefix="review-pipeline-summary-") as td:
		tmp = Path(td)
		runtime = tmp / "runtime"
		reviews = tmp / "reviews"
		runtime.mkdir()
		reviews.mkdir()

		(runtime / "reviewer_bundle.txt").write_text("bundle sentinel\n", encoding="utf-8")
		(runtime / "floor_tags.txt").write_text("CORRECTNESS & LOGIC\n", encoding="utf-8")
		(runtime / "consolidator_raw.txt").write_text("consolidator sentinel\n", encoding="utf-8")
		(runtime / "parser_stats.txt").write_text("parsed_blocks=1\npassthrough_blocks=0\nline_unverified=0\n", encoding="utf-8")
		(runtime / "ledger_status.txt").write_text(
			"issue-1\tNEW\t0\tscripts/review_run_reviewers.sh:1\tCORRECTNESS & LOGIC\t[]\n",
			encoding="utf-8",
		)
		(runtime / "editor_summary.txt").write_text("editor summary sentinel\n", encoding="utf-8")
		(runtime / "committed_files.txt").write_text("", encoding="utf-8")

		(reviews / "status_review_model_one.txt").write_text("success\n", encoding="utf-8")
		(reviews / "review_model_one.txt").write_text("fresh reviewer output\n", encoding="utf-8")
		(reviews / "review_model_one.log").write_text(
			"REVIEWER_CACHE: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 attempt=1 status=supported prompt_reused=true\n"
			"Reviewer slot x-ai/grok-4.20 (x-ai/grok-4.20) recorded codex_stall_killed on attempt 1 (exit=137).\n"
			"Reviewer slot x-ai/grok-4.20 (x-ai/grok-4.20) failure classified as retryable (stall_guard) on attempt 1.\n"
			"REVIEWER_ADVANCE: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 reason=stall_guard next_action=retry_cheaper_reasoning next_attempt=2 next_model=x-ai/grok-4.20\n"
			"REVIEWER_BACKOFF: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 reason=stall_guard next_action=retry_cheaper_reasoning next_attempt=2 sleep_secs=2 total_sleep_secs=2\n"
			"REVIEWER_CACHE: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 attempt=2 status=supported prompt_reused=true\n"
			"INFO: openrouter usage phase=review call=review model=x-ai/grok-4.20 cache_enabled=true cache_breakpoint_enabled=na cache_breakpoint_fallback_retry=na prompt_tokens=4000 completion_tokens=100 total_tokens=4100 cache_creation_input_tokens=0 cache_read_input_tokens=120\n"
			"Reviewer slot x-ai/grok-4.20 (x-ai/grok-4.20) succeeded on attempt 2.\n"
			"REVIEWER_SLOT_STATE: slot=x-ai/grok-4.20 retryable_failure_count=1 retryable_failure_classes=stall_guard backoff_sleep_secs_total=2 slot_retry_budget_exhausted=false fallback_model_used=false cache_status=supported cache_reuse_attempted=true\n",
			encoding="utf-8",
		)

		(reviews / "status_review_model_two.txt").write_text("skipped_unmapped\n", encoding="utf-8")
		(reviews / "review_model_two.txt").write_text(
			"Reviewer slot moonshotai/kimi-k2.5 skipped after retryable failure (timeout); no same-family failback mapping is available.\n",
			encoding="utf-8",
		)
		(reviews / "review_model_two.log").write_text(
			"REVIEWER_CACHE: slot=moonshotai/kimi-k2.5 model=moonshotai/kimi-k2.5 attempt=1 status=unsupported prompt_reused=true\n"
			"Reviewer slot moonshotai/kimi-k2.5 (moonshotai/kimi-k2.5) recorded codex_stall_killed on attempt 1 (exit=137).\n"
			"Reviewer slot moonshotai/kimi-k2.5 (moonshotai/kimi-k2.5) failure classified as retryable (timeout) on attempt 1.\n"
			"REVIEWER_ADVANCE: slot=moonshotai/kimi-k2.5 model=moonshotai/kimi-k2.5 reason=stall_guard next_action=skip_unmapped\n"
			"INFO: openrouter usage phase=review call=review model=moonshotai/kimi-k2.5 cache_enabled=true cache_breakpoint_enabled=na cache_breakpoint_fallback_retry=na prompt_tokens=2800 completion_tokens=80 total_tokens=2880 cache_creation_input_tokens=0 cache_read_input_tokens=0\n"
			"REVIEWER_SLOT_STATE: slot=moonshotai/kimi-k2.5 retryable_failure_count=1 retryable_failure_classes=timeout backoff_sleep_secs_total=0 slot_retry_budget_exhausted=false fallback_model_used=false cache_status=unsupported cache_reuse_attempted=false\n",
			encoding="utf-8",
		)

		step_summary = tmp / "step_summary.md"
		env = os.environ.copy()
		env.update({
			"RUNTIME_DIR": str(runtime),
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"EDITOR_SUMMARY_FILE": str(runtime / "editor_summary.txt"),
			"COMMITTED_FILES_FILE": str(runtime / "committed_files.txt"),
			"GITHUB_STEP_SUMMARY": str(step_summary),
			"AUTOFIX_ITERATION_METRIC": "3",
			"REVIEWERS_SUCCESSFUL": "1",
			"REVIEW_CONSOLIDATOR_ENABLED": "1",
		})
		if extra_env:
			env.update(extra_env)

		result = subprocess.run(
			["bash", "-c", f"set -euo pipefail\n{summary_script}"],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		summary_line = next(
			line for line in result.stdout.splitlines() if line.startswith("REVIEW_AUTOFIX_RUN_SUMMARY_V1 ")
		)
		summary = json.loads(summary_line.split(" ", 1)[1])
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			"step_summary": step_summary.read_text(encoding="utf-8"),
			"summary": summary,
		}


def _run_reviewer_partial_finalize_budget_harness() -> dict[str, object]:
	partial_finalize_budget_helper_block = _reviewer_partial_finalize_budget_helper_block()
	run_reviewer_block = _reviewer_run_reviewer_block()
	run_reviewer_pass_block = _reviewer_run_reviewer_pass_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-partial-finalize-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		runtime = tmp / "runtime"
		home = tmp / "home"
		runner_temp = tmp / "runner_temp"
		seed_codex_home = tmp / "codex_home_seed"
		reviews.mkdir()
		runtime.mkdir()
		home.mkdir()
		runner_temp.mkdir()
		seed_codex_home.mkdir()

		prompt_file = runtime / "reviewer_prompt.patch"
		prompt_file.write_text(PHASE_G_FLAKY_REVIEWER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

		state_file = tmp / "state.txt"
		github_env_file = tmp / "github_env.txt"
		call_marker = tmp / "run_reviewer_called.txt"

		env = os.environ.copy()
		env.update({
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNTIME_DIR": str(runtime),
			"PROMPT_FILE": str(prompt_file),
			"PR_NUMBER": "123",
			"RUNNER_TEMP": str(runner_temp),
			"HOME": str(home),
			"CODEX_HOME": str(seed_codex_home),
			"GITHUB_ENV": str(github_env_file),
			"REVIEWER_REASONING_EFFORT": "xhigh",
			"REVIEW_PR_STATE_POLL_INTERVAL_SECS": "10",
			"REVIEW_SOFT_DEADLINE_MINUTES": "210",
			"JOB_START_EPOCH": "",
			"CODEX_RUN_BUDGET_START_EPOCH": "",
			"CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH": "",
			"CODEX_RUN_BUDGET_TOTAL_SECS": "",
			"STATE_FILE": str(state_file),
			"CALL_MARKER": str(call_marker),
		})

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{partial_finalize_budget_helper_block}\n"
				f"{run_reviewer_block}\n"
				f"{run_reviewer_pass_block}\n"
				"run_reviewer() {\n"
				"\t: > \"${CALL_MARKER}\"\n"
				"\treturn 97\n"
				"}\n"
				"get_active_reviewer_models_text() {\n"
				"\tprintf 'x-ai/grok-4.20\\n'\n"
				"}\n"
				"prepare_reviewer_prompt_for_model() {\n"
				"\tprintf '%s\\n' \"${2:-}\"\n"
				"}\n"
				"pass_successful=\"$(run_reviewer_pass \"review\" \"${PROMPT_FILE}\" \"xhigh\")\"\n"
				"{\n"
				"\tprintf 'PASS_SUCCESSFUL=%s\\n' \"${pass_successful}\"\n"
				"\tprintf 'REQUEST_FILE=%s\\n' \"${REVIEWER_PARTIAL_FINALIZE_REQUEST_FILE}\"\n"
				"\tprintf 'GITHUB_ENV_FILE=%s\\n' \"${GITHUB_ENV}\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		state: dict[str, str] = {}
		for raw_line in state_file.read_text(encoding="utf-8").splitlines():
			if "=" not in raw_line:
				continue
			key, value = raw_line.split("=", 1)
			state[key] = value

		request_file = Path(state["REQUEST_FILE"])
		github_env = Path(state["GITHUB_ENV_FILE"])
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			"request_file_content": request_file.read_text(encoding="utf-8"),
			"github_env_content": github_env.read_text(encoding="utf-8"),
			"reviewer_called": call_marker.exists(),
		}


def _run_reviewer_health_dispatch_logging_harness() -> dict[str, str]:
	helper_block = _reviewer_failback_helper_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-health-dispatch-") as td:
		tmp = Path(td)
		health_file = tmp / "reviewer_health_state.json"
		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				f"{helper_block}\n"
				"reviewer_health_state_action() {\n"
				"\tcat <<'EOF'\n"
				"decision=run\n"
				"state=healthy\n"
				"transition=healthy\n"
				"reason=open_ttl_expired\n"
				"consecutive_retryable_failures=0\n"
				"effective_model=x-ai/grok-4.1-fast\n"
				"open_until_epoch=0\n"
				"EOF\n"
				"}\n"
				"reviewer_health_dispatch_prepare 'x-ai/grok-4.20'\n",
			],
			cwd=str(REPO_ROOT),
			env={
				**os.environ,
				"REVIEWER_CIRCUIT_BREAKER_ENABLED": "1",
				"REVIEWER_HEALTH_STATE_FILE": str(health_file),
			},
			check=True,
			capture_output=True,
			text=True,
		)

		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
		}


def _run_reviewer_zero_success_guard_harness(*, statuses: list[str]) -> dict[str, object]:
	guard_block = _reviewer_zero_success_guard_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-zero-success-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		reviews.mkdir()
		github_env_file = tmp / "github_env.txt"

		for idx, status in enumerate(statuses, 1):
			(reviews / f"status_review_model{idx}.txt").write_text(f"{status}\n", encoding="utf-8")
			(reviews / f"review_model{idx}.log").write_text(f"status={status}\n", encoding="utf-8")

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				"reviewers_successful=0\n"
				f"{guard_block}\n",
			],
			cwd=str(REPO_ROOT),
			env={
				**os.environ,
				"PREVIOUS_REVIEWS_DIR": str(reviews),
				"PR_NUMBER": "123",
				"GITHUB_ENV": str(github_env_file),
			},
			capture_output=True,
			text=True,
		)

		return {
			"returncode": result.returncode,
			"stdout": result.stdout,
			"stderr": result.stderr,
			"github_env": github_env_file.read_text(encoding="utf-8") if github_env_file.exists() else "",
		}


def _run_restore_same_head_resume_harness(
	*,
	markers: list[dict[str, object]],
	review_max_resume_rounds: str = "3",
	artifacts_by_round: dict[int, dict[str, dict[str, str]]] | None = None,
) -> dict[str, object]:
	with tempfile.TemporaryDirectory(prefix="review-resume-restore-") as td:
		tmp = Path(td)
		repo = tmp / "repo"
		repo.mkdir()
		head_sha = _init_git_repo(repo)
		marker_root = repo / ".ai" / "review_runtime" / "pr-123"
		marker_root.mkdir(parents=True)

		for marker in markers:
			payload = dict(marker)
			if payload.get("head_sha") == "__HEAD__":
				payload["head_sha"] = head_sha
			round_value = int(payload["resume_round"])
			marker_dir = marker_root / f"round-{round_value}"
			marker_dir.mkdir(parents=True, exist_ok=True)
			(marker_dir / "partial_finalize.json").write_text(
				json.dumps(payload, sort_keys=True, indent=2) + "\n",
				encoding="utf-8",
			)
			for directory_name, file_map in (artifacts_by_round or {}).get(round_value, {}).items():
				directory = marker_dir / directory_name
				directory.mkdir(parents=True, exist_ok=True)
				for file_name, contents in file_map.items():
					(directory / file_name).write_text(str(contents), encoding="utf-8")

		result = _run_restore_same_head_resume_step(
			repo,
			review_max_resume_rounds=review_max_resume_rounds,
			runtime_dir=tmp / "runtime_restored",
			reviews_dir=tmp / "reviews_restored",
		)
		return {
			**result,
			"head_sha": head_sha,
		}


def _build_partial_finalize_step_context(tmp: Path) -> dict[str, object]:
	repo = tmp / "repo"
	repo.mkdir()
	head_sha = _init_git_repo(repo)
	runtime = repo / "runtime"
	reviews = repo / "reviews"
	runtime.mkdir()
	reviews.mkdir()
	support_scripts_dir = tmp / "support_scripts"
	support_scripts_dir.mkdir()
	(support_scripts_dir / "gh_helpers.sh").write_text(
		"#!/usr/bin/env bash\nset -euo pipefail\ngh_retry() { \"$@\"; }\n",
		encoding="utf-8",
	)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh_state_file = tmp / "gh_state.json"
	_install_partial_finalize_mock_gh(bin_dir, gh_state_file)

	paths = {
		"repo": repo,
		"runtime": runtime,
		"reviews": reviews,
		"support_scripts_dir": support_scripts_dir,
		"bin_dir": bin_dir,
		"gh_state_file": gh_state_file,
		"editor_summary": runtime / "editor_summary.txt",
		"committed_files": runtime / "committed_files.txt",
	}
	(reviews / "status_review_model_one.txt").write_text("success\n", encoding="utf-8")
	(reviews / "review_model_one.txt").write_text("cached reviewer output\n", encoding="utf-8")
	(runtime / "reviewer_consensus.txt").write_text("consensus sentinel\n", encoding="utf-8")
	(runtime / "reviewer_bundle.txt").write_text("bundle sentinel\n", encoding="utf-8")
	(runtime / "floor_tags.txt").write_text("CORRECTNESS & LOGIC\n", encoding="utf-8")
	(runtime / "consolidator_raw.txt").write_text("consolidator sentinel\n", encoding="utf-8")
	(runtime / "parser_stats.txt").write_text("parsed_blocks=1\npassthrough_blocks=0\nline_unverified=0\n", encoding="utf-8")
	(runtime / "review_issues.txt").write_text("issue sentinel\n", encoding="utf-8")
	(runtime / "ledger_status.txt").write_text(
		"issue-1\tNEW\t0\tscripts/review_apply_fixes.sh:1\tCORRECTNESS & LOGIC\t[]\n",
		encoding="utf-8",
	)
	Path(paths["editor_summary"]).write_text("editor summary sentinel\n", encoding="utf-8")
	Path(paths["committed_files"]).write_text("", encoding="utf-8")
	return {"head_sha": head_sha, **paths}


def _run_partial_finalize_step(
	context: dict[str, object],
	*,
	previous_env: dict[str, str] | None = None,
	editor_summary_text: str | None = None,
	committed_files_text: str | None = None,
	runtime_dir: Path | None = None,
	reviews_dir: Path | None = None,
	review_max_resume_rounds: str = "3",
	partial_phase: str = "editor",
	partial_reason: str = "soft_deadline",
	edits_pushed: str = "false",
	editor_commit_produced: str = "false",
	validation_tail_can_complete: str = "true",
	edits_withheld_for_safety: str = "false",
	withheld_reason: str = "none",
) -> dict[str, object]:
	partial_script = _step_run_script("Post partial finalize comment and persist runtime marker")
	repo = Path(context["repo"])
	runtime = runtime_dir or Path(context["runtime"])
	reviews = reviews_dir or Path(context["reviews"])
	gh_state_file = Path(context["gh_state_file"])
	runtime.mkdir(parents=True, exist_ok=True)
	reviews.mkdir(parents=True, exist_ok=True)
	editor_summary = runtime / "editor_summary.txt"
	committed_files = runtime / "committed_files.txt"

	if editor_summary_text is not None:
		editor_summary.write_text(editor_summary_text, encoding="utf-8")
	if committed_files_text is not None:
		committed_files.write_text(committed_files_text, encoding="utf-8")

	run_index = len(json.loads(gh_state_file.read_text(encoding="utf-8") or "{}").get("issue_comments", [])) + 1
	github_env_file = runtime / f"github_env_run_{run_index}.txt"
	github_env_file.write_text("", encoding="utf-8")
	resume_env = previous_env or {}
	result = subprocess.run(
		["bash", "-c", f"set -euo pipefail\n{partial_script}"],
		cwd=str(repo),
			env=_git_clean_env({
				"PATH": f"{context['bin_dir']}:{os.environ.get('PATH', '')}",
			"MOCK_GH_STATE_FILE": str(gh_state_file),
			"SUPPORT_SCRIPTS_DIR": str(context["support_scripts_dir"]),
			"GITHUB_ENV": str(github_env_file),
			"GH_TOKEN": "test-token",
			"PR_NUMBER": "123",
			"RUNTIME_DIR": str(runtime),
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"EDITOR_SUMMARY_FILE": str(editor_summary),
			"COMMITTED_FILES_FILE": str(committed_files),
			"GITHUB_REPOSITORY": "owner/repo",
				"WORKFLOW_RUN_URL": "https://github.com/owner/repo/actions/runs/12345",
				"AUTOFIX_PARTIAL_FINALIZE_REASON": partial_reason,
				"AUTOFIX_PARTIAL_FINALIZE_PHASE": partial_phase,
				"AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": validation_tail_can_complete,
				"AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY": edits_withheld_for_safety,
				"AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON": withheld_reason,
				"AUTOFIX_EDITS_PUSHED": edits_pushed,
				"EDITOR_COMMIT_PRODUCED": editor_commit_produced,
			"REVIEW_MAX_RESUME_ROUNDS": review_max_resume_rounds,
			"AUTOFIX_RESUME_ROUND": resume_env.get("AUTOFIX_RESUME_ROUND", "0"),
			"AUTOFIX_RESUME_PROGRESS_FINGERPRINT": resume_env.get("AUTOFIX_RESUME_PROGRESS_FINGERPRINT", ""),
			"AUTOFIX_RESUME_HEAD_SHA": resume_env.get("AUTOFIX_RESUME_HEAD_SHA", str(context["head_sha"])),
			"AUTOFIX_RESUME_ROUND_LIMIT": resume_env.get("AUTOFIX_RESUME_ROUND_LIMIT", review_max_resume_rounds),
			"PYTHONDONTWRITEBYTECODE": "1",
		}),
		check=True,
		capture_output=True,
		text=True,
	)
	github_env = _parse_env_file(github_env_file)
	marker_path = Path(github_env["AUTOFIX_PARTIAL_MARKER_FILE"])
	if not marker_path.is_absolute():
		marker_path = repo / marker_path
	marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
	gh_state = json.loads(gh_state_file.read_text(encoding="utf-8"))
	issue_comments = gh_state.get("issue_comments", []) if isinstance(gh_state, dict) else []
	latest_comment = issue_comments[-1]["body"] if issue_comments else ""
	return {
		"stdout": result.stdout,
		"stderr": result.stderr,
		"github_env": github_env,
		"marker_payload": marker_payload,
		"latest_comment": latest_comment,
		"gh_state": gh_state,
	}


def _run_reviewer_resume_cached_success_harness() -> dict[str, object]:
	partial_finalize_budget_helper_block = _reviewer_partial_finalize_budget_helper_block()
	run_reviewer_pass_block = _reviewer_run_reviewer_pass_block()
	with tempfile.TemporaryDirectory(prefix="reviewer-resume-cached-success-") as td:
		tmp = Path(td)
		reviews = tmp / "reviews"
		runtime = tmp / "runtime"
		reviews.mkdir()
		runtime.mkdir()

		prompt_file = runtime / "reviewer_prompt.patch"
		prompt_file.write_text(PHASE_G_FLAKY_REVIEWER_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
		called_models_file = tmp / "called_models.txt"
		state_file = tmp / "state.txt"
		github_env_file = tmp / "github_env.txt"

		cached_model = "x-ai/grok-4.20"
		rerun_model = "moonshotai/kimi-k2.5"
		cached_safe_name = cached_model.translate(str.maketrans({"/": "_", ".": "_", ":": "_"}))
		rerun_safe_name = rerun_model.translate(str.maketrans({"/": "_", ".": "_", ":": "_"}))
		(reviews / f"status_review_{cached_safe_name}.txt").write_text("success\n", encoding="utf-8")
		(reviews / f"review_{cached_safe_name}.txt").write_text("cached reviewer output\n", encoding="utf-8")
		(reviews / f"review_{cached_safe_name}.log").write_text("cached log\n", encoding="utf-8")
		(reviews / f"status_review_{rerun_safe_name}.txt").write_text("failed\n", encoding="utf-8")
		(reviews / f"review_{rerun_safe_name}.txt").write_text("stale failure output\n", encoding="utf-8")
		env = {
			**os.environ,
			"PREVIOUS_REVIEWS_DIR": str(reviews),
			"RUNTIME_DIR": str(runtime),
			"PROMPT_FILE": str(prompt_file),
			"GITHUB_ENV": str(github_env_file),
			"REVIEWER_REASONING_EFFORT": "xhigh",
			"REVIEW_PR_STATE_POLL_INTERVAL_SECS": "10",
			"AUTOFIX_RESUME_RESTORED": "true",
			"AUTOFIX_RESUME_SHOULD_CONTINUE": "true",
			"AUTOFIX_RESUME_STATE": "resumable",
			"CALLED_MODELS_FILE": str(called_models_file),
			"STATE_FILE": str(state_file),
		}
		env.update(_reviewer_harness_budget_env())

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
				"emit_run_budget_gate_note() { :; }\n"
				"reviewer_circuit_breaker_enabled() { return 1; }\n"
				f"{partial_finalize_budget_helper_block}\n"
				f"{run_reviewer_pass_block}\n"
				"run_reviewer() {\n"
				"\tlocal model=\"$1\"\n"
				"\tlocal safe_name=\"$2\"\n"
				"\tlocal output_prefix=\"${3:-review}\"\n"
				"\tprintf '%s\\n' \"${model}\" >> \"${CALLED_MODELS_FILE}\"\n"
				"\tprintf 'fresh reviewer output for %s\\n' \"${model}\" > \"${PREVIOUS_REVIEWS_DIR}/${output_prefix}_${safe_name}.txt\"\n"
				"\techo success > \"${PREVIOUS_REVIEWS_DIR}/status_${output_prefix}_${safe_name}.txt\"\n"
				"\tprintf 'fresh log for %s\\n' \"${model}\" > \"${PREVIOUS_REVIEWS_DIR}/${output_prefix}_${safe_name}.log\"\n"
				"}\n"
				"get_active_reviewer_models_text() {\n"
				f"\tprintf '{cached_model}\\n{rerun_model}\\n'\n"
				"}\n"
				"pass_successful=\"$(run_reviewer_pass \"review\" \"${PROMPT_FILE}\" \"xhigh\")\"\n"
				"{\n"
				"\tprintf 'PASS_SUCCESSFUL=%s\\n' \"${pass_successful}\"\n"
				"\tprintf 'CALLED_MODELS_FILE=%s\\n' \"${CALLED_MODELS_FILE}\"\n"
				f"\tprintf 'CACHED_LOG_FILE=%s\\n' \"${{PREVIOUS_REVIEWS_DIR}}/review_{cached_safe_name}.log\"\n"
				f"\tprintf 'RERUN_OUTPUT_FILE=%s\\n' \"${{PREVIOUS_REVIEWS_DIR}}/review_{rerun_safe_name}.txt\"\n"
				"} > \"${STATE_FILE}\"\n",
			],
			cwd=str(REPO_ROOT),
			env=env,
			check=True,
			capture_output=True,
			text=True,
		)

		state = _parse_env_file(state_file)
		return {
			"stdout": result.stdout,
			"stderr": result.stderr,
			**state,
			"called_models": called_models_file.read_text(encoding="utf-8").splitlines() if called_models_file.exists() else [],
			"cached_log": Path(state["CACHED_LOG_FILE"]).read_text(encoding="utf-8"),
			"rerun_output": Path(state["RERUN_OUTPUT_FILE"]).read_text(encoding="utf-8"),
		}


def test_review_pipeline_knobs_are_wired_into_codex_agent_env() -> None:
	workflow = _workflow_text()
	for expected in (
		"REVIEW_FLOOR_RULES_ENABLED: ${{ vars.REVIEW_FLOOR_RULES_ENABLED || '1' }}",
		"REVIEW_FLOOR_KEYWORDS_FILE: ${{ vars.REVIEW_FLOOR_KEYWORDS_FILE || '' }}",
		"REVIEW_CONSOLIDATOR_ENABLED: ${{ vars.REVIEW_CONSOLIDATOR_ENABLED || '1' }}",
		"REVIEW_CONSOLIDATOR_MODEL: ${{ vars.REVIEW_CONSOLIDATOR_MODEL || 'openai/gpt-5.5' }}",
		"REVIEW_CONSOLIDATOR_REASONING: ${{ vars.REVIEW_CONSOLIDATOR_REASONING || 'xhigh' }}",
		"REVIEW_CONSOLIDATOR_TIMEOUT_SECS: ${{ vars.REVIEW_CONSOLIDATOR_TIMEOUT_SECS || '300' }}",
		"REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT: ${{ vars.REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT || '16000' }}",
		"REVIEW_PARSER_FAILOPEN: ${{ vars.REVIEW_PARSER_FAILOPEN || '1' }}",
		"CONSOLIDATOR_REJECT_SCHEMA_ENABLED: ${{ vars.CONSOLIDATOR_REJECT_SCHEMA_ENABLED || 'false' }}",
		"REVIEW_LEDGER_ENABLED: ${{ vars.REVIEW_LEDGER_ENABLED || '1' }}",
		"REVIEW_LEDGER_PERSIST_LIMIT: ${{ vars.REVIEW_LEDGER_PERSIST_LIMIT || '2' }}",
		"REVIEW_LEDGER_PATH: ${{ vars.REVIEW_LEDGER_PATH || format('.ai/review_issue_ledger/pr-{0}.txt', inputs.pr_number || github.event.inputs.pr_number || github.event.pull_request.number || '0') }}",
		"REVIEW_REVIEWER_CHECKLIST_ENABLED: ${{ vars.REVIEW_REVIEWER_CHECKLIST_ENABLED || '1' }}",
		"REVIEW_REVIEWER_ITERATION_SCOPING: ${{ vars.REVIEW_REVIEWER_ITERATION_SCOPING || '1' }}",
		"FORCE_FULL_REVIEW_TIER: ${{ needs.gate.outputs.force_full_review_tier || 'false' }}",
		"REVIEW_TIER_RESOLVER_ENABLED: ${{ vars.REVIEW_TIER_RESOLVER_ENABLED || 'false' }}",
		"REVIEW_TIER_LITE_MAX_LOC: ${{ vars.REVIEW_TIER_LITE_MAX_LOC || '50' }}",
		"REVIEW_TIER_LITE_REVIEWER_SLUG: ${{ vars.REVIEW_TIER_LITE_REVIEWER_SLUG || 'qwen/qwen3.6-plus' }}",
		"REVIEW_TIER_STANDARD_MAX_LOC: ${{ vars.REVIEW_TIER_STANDARD_MAX_LOC || '200' }}",
		"REVIEW_TIER_STANDARD_REVIEWER_SLUGS: ${{ vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS || 'minimax/minimax-m2.5,deepseek/deepseek-v4-pro,x-ai/grok-4.20' }}",
		"REVIEWER_RISK_TIER_ENABLED: ${{ vars.REVIEWER_RISK_TIER_ENABLED || '0' }}",
		"REVIEWER_RISK_TIER_TRIVIAL_LOC: ${{ vars.REVIEWER_RISK_TIER_TRIVIAL_LOC || '10' }}",
		"REVIEWER_RISK_TIER_TRIVIAL_FILES: ${{ vars.REVIEWER_RISK_TIER_TRIVIAL_FILES || '20' }}",
		"REVIEWER_RISK_TIER_LITE_LOC: ${{ vars.REVIEWER_RISK_TIER_LITE_LOC || '100' }}",
		"REVIEWER_RISK_TIER_LITE_FILES: ${{ vars.REVIEWER_RISK_TIER_LITE_FILES || '20' }}",
		"REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX: ${{ vars.REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX || '^(scripts/|\\.github/workflows/|\\.github/ai/|prompts/|workflow-templates/|db/contracts/|ai-memory/)' }}",
		"REVIEWER_TIER_TRIVIAL_MODELS: ${{ vars.REVIEWER_TIER_TRIVIAL_MODELS || '' }}",
		"REVIEWER_TIER_LITE_MODELS: ${{ vars.REVIEWER_TIER_LITE_MODELS || '' }}",
		"REVIEWER_CIRCUIT_BREAKER_ENABLED: ${{ vars.REVIEWER_CIRCUIT_BREAKER_ENABLED || '0' }}",
		"REVIEWER_FAILBACK_MAX_RETRIES: ${{ vars.REVIEWER_FAILBACK_MAX_RETRIES || '1' }}",
		"REVIEWER_SLOT_RETRYABLE_FAILURE_LIMIT: ${{ vars.REVIEWER_SLOT_RETRYABLE_FAILURE_LIMIT || '3' }}",
		"REVIEWER_SLOT_BACKOFF_BASE_SECS: ${{ vars.REVIEWER_SLOT_BACKOFF_BASE_SECS || '2' }}",
		"REVIEWER_SLOT_BACKOFF_CAP_SECS: ${{ vars.REVIEWER_SLOT_BACKOFF_CAP_SECS || '30' }}",
		"REVIEWER_SLOT_BACKOFF_BUDGET_RATIO: ${{ vars.REVIEWER_SLOT_BACKOFF_BUDGET_RATIO || '0.05' }}",
		"REVIEWER_HEALTH_OPEN_THRESHOLD: ${{ vars.REVIEWER_HEALTH_OPEN_THRESHOLD || '3' }}",
		"REVIEWER_HEALTH_OPEN_TTL_SECS: ${{ vars.REVIEWER_HEALTH_OPEN_TTL_SECS || '1800' }}",
		"REVIEW_SOFT_DEADLINE_MINUTES: ${{ vars.REVIEW_SOFT_DEADLINE_MINUTES || '210' }}",
		"CONTEXT_BUDGET_WARN_RATIO: ${{ vars.CONTEXT_BUDGET_WARN_RATIO || '0.7' }}",
		"MAX_PROMPT_TOKENS_FOR_PHASE: ${{ vars.MAX_PROMPT_TOKENS_FOR_PHASE || '' }}",
		"CODEX_HEARTBEAT_ENABLED: ${{ vars.CODEX_HEARTBEAT_ENABLED || '1' }}",
		"CODEX_HEARTBEAT_INTERVAL_SECS: ${{ vars.CODEX_HEARTBEAT_INTERVAL_SECS || '30' }}",
		"CODEX_STALL_GUARD_ENABLED: ${{ vars.CODEX_STALL_GUARD_ENABLED || 'true' }}",
		"CODEX_STALL_TIMEOUT_SECONDS: ${{ vars.CODEX_STALL_TIMEOUT_SECONDS || '600' }}",
		"CODEX_STALL_KILL_GRACE_SECONDS: ${{ vars.CODEX_STALL_KILL_GRACE_SECONDS || '30' }}",
		"REVIEWER_FILTER_UNINTERESTING_ENABLED: ${{ vars.REVIEWER_FILTER_UNINTERESTING_ENABLED || 'false' }}",
		"REVIEWER_FILTER_EXTRA_GLOBS: ${{ vars.REVIEWER_FILTER_EXTRA_GLOBS || '' }}",
		"REVIEWER_FILTER_EXEMPT_GLOBS: ${{ vars.REVIEWER_FILTER_EXEMPT_GLOBS || 'db/contracts/**,**/migrations/**,**/migrate/**' }}",
		"SLOP_SCAN_ENABLED: ${{ vars.SLOP_SCAN_ENABLED || 'true' }}",
		"REVIEW_MAX_RESUME_ROUNDS: ${{ vars.REVIEW_MAX_RESUME_ROUNDS || '3' }}",
		"AGENTS_MD_MATERIALITY_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_ENABLED || '1' }}",
		"AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED || '0' }}",
		"AGENTS_MD_MATERIALITY_MODEL: ${{ vars.AGENTS_MD_MATERIALITY_MODEL || 'openai/gpt-5.4-mini' }}",
		"AGENTS_MD_MATERIALITY_REASONING: ${{ vars.AGENTS_MD_MATERIALITY_REASONING || 'medium' }}",
		"REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED: ${{ vars.REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED || 'true' }}",
	):
		assert expected in workflow, f"Missing codex-agent env wiring: {expected}"

	for expected in (
		"CODEX_STALL_GUARD_ENABLED: ${{ vars.CODEX_STALL_GUARD_ENABLED || 'true' }}",
		"CODEX_STALL_TIMEOUT_SECONDS: ${{ vars.CODEX_STALL_TIMEOUT_SECONDS || '600' }}",
		"CODEX_STALL_KILL_GRACE_SECONDS: ${{ vars.CODEX_STALL_KILL_GRACE_SECONDS || '30' }}",
	):
		assert workflow.count(expected) >= 2, f"Review stall wiring must appear in both workflow env blocks: {expected}"

	stage_step_block = _step_block("Stage workflow support files")
	assert '.codex-workflow-src/scripts/stage_workflow_support.sh' in stage_step_block
	assert '.codex-workflow-src-main/scripts/stage_workflow_support.sh' in stage_step_block
	assert "REQUIRED_BOOTSTRAP_SCRIPTS=" not in stage_step_block
	assert 'mkdir -p "${SUPPORT_SCRIPTS_DIR}"' not in stage_step_block
	required_bootstrap_line = next(
		line for line in _stage_helper_text().splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line
	)
	assert "codex_heartbeat.sh" in required_bootstrap_line, required_bootstrap_line
	assert "cost_audit.py" in required_bootstrap_line, required_bootstrap_line

	for expected in (
		"REVIEW_TIER_RESOLVER_ENABLED: ${{ vars.REVIEW_TIER_RESOLVER_ENABLED || 'false' }}",
		"REVIEW_TIER_LITE_MAX_LOC: ${{ vars.REVIEW_TIER_LITE_MAX_LOC || '50' }}",
		"REVIEW_TIER_LITE_REVIEWER_SLUG: ${{ vars.REVIEW_TIER_LITE_REVIEWER_SLUG || 'qwen/qwen3.6-plus' }}",
		"REVIEW_TIER_STANDARD_MAX_LOC: ${{ vars.REVIEW_TIER_STANDARD_MAX_LOC || '200' }}",
		"REVIEW_TIER_STANDARD_REVIEWER_SLUGS: ${{ vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS || 'minimax/minimax-m2.5,deepseek/deepseek-v4-pro,x-ai/grok-4.20' }}",
		"REVIEWER_RISK_TIER_ENABLED: ${{ vars.REVIEWER_RISK_TIER_ENABLED || '0' }}",
		"REVIEWER_RISK_TIER_TRIVIAL_LOC: ${{ vars.REVIEWER_RISK_TIER_TRIVIAL_LOC || '10' }}",
		"REVIEWER_RISK_TIER_TRIVIAL_FILES: ${{ vars.REVIEWER_RISK_TIER_TRIVIAL_FILES || '20' }}",
		"REVIEWER_RISK_TIER_LITE_LOC: ${{ vars.REVIEWER_RISK_TIER_LITE_LOC || '100' }}",
		"REVIEWER_RISK_TIER_LITE_FILES: ${{ vars.REVIEWER_RISK_TIER_LITE_FILES || '20' }}",
		"REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX: ${{ vars.REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX || '^(scripts/|\\.github/workflows/|\\.github/ai/|prompts/|workflow-templates/|db/contracts/|ai-memory/)' }}",
		"REVIEWER_TIER_TRIVIAL_MODELS: ${{ vars.REVIEWER_TIER_TRIVIAL_MODELS || '' }}",
		"REVIEWER_TIER_LITE_MODELS: ${{ vars.REVIEWER_TIER_LITE_MODELS || '' }}",
		"REVIEWER_CIRCUIT_BREAKER_ENABLED: ${{ vars.REVIEWER_CIRCUIT_BREAKER_ENABLED || '0' }}",
		"REVIEWER_FAILBACK_MAX_RETRIES: ${{ vars.REVIEWER_FAILBACK_MAX_RETRIES || '1' }}",
		"REVIEWER_SLOT_RETRYABLE_FAILURE_LIMIT: ${{ vars.REVIEWER_SLOT_RETRYABLE_FAILURE_LIMIT || '3' }}",
		"REVIEWER_SLOT_BACKOFF_BASE_SECS: ${{ vars.REVIEWER_SLOT_BACKOFF_BASE_SECS || '2' }}",
		"REVIEWER_SLOT_BACKOFF_CAP_SECS: ${{ vars.REVIEWER_SLOT_BACKOFF_CAP_SECS || '30' }}",
		"REVIEWER_SLOT_BACKOFF_BUDGET_RATIO: ${{ vars.REVIEWER_SLOT_BACKOFF_BUDGET_RATIO || '0.05' }}",
		"REVIEWER_HEALTH_OPEN_THRESHOLD: ${{ vars.REVIEWER_HEALTH_OPEN_THRESHOLD || '3' }}",
		"REVIEWER_HEALTH_OPEN_TTL_SECS: ${{ vars.REVIEWER_HEALTH_OPEN_TTL_SECS || '1800' }}",
		"REVIEW_SOFT_DEADLINE_MINUTES: ${{ vars.REVIEW_SOFT_DEADLINE_MINUTES || '210' }}",
		"AGENTS_MD_MATERIALITY_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_ENABLED || '1' }}",
		"AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED || '0' }}",
		"AGENTS_MD_MATERIALITY_MODEL: ${{ vars.AGENTS_MD_MATERIALITY_MODEL || 'openai/gpt-5.4-mini' }}",
		"AGENTS_MD_MATERIALITY_REASONING: ${{ vars.AGENTS_MD_MATERIALITY_REASONING || 'medium' }}",
		"REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED: ${{ vars.REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED || 'true' }}",
	):
		assert workflow.count(expected) >= 2, f"Missing workflow-level + codex-agent env wiring: {expected}"


def test_review_soft_deadline_budget_contract_is_wired() -> None:
	init_step_block = _step_block("Initialize runtime workspace")
	for expected in (
		'review_soft_deadline_minutes="${REVIEW_SOFT_DEADLINE_MINUTES:-210}"',
		'review_soft_deadline_minutes_raw="${review_soft_deadline_minutes}"',
		"Invalid REVIEW_SOFT_DEADLINE_MINUTES=",
		'review_soft_deadline_minutes="$(( 10#${review_soft_deadline_minutes} ))"',
		'if [ "${review_soft_deadline_minutes}" -le 0 ]; then',
		'echo "REVIEW_SOFT_DEADLINE_MINUTES=${review_soft_deadline_minutes}"',
		'echo "CODEX_RUN_BUDGET_START_EPOCH=${job_start_epoch}"',
		'echo "CODEX_RUN_BUDGET_SOFT_DEADLINE_EPOCH=${codex_run_budget_soft_deadline_epoch}"',
		'echo "CODEX_RUN_BUDGET_TOTAL_SECS=${codex_run_budget_total_secs}"',
	):
		assert expected in init_step_block, f"missing budget init wiring: {expected}"
	assert "0[0-9]*" not in init_step_block, "workflow init should accept zero-padded soft deadlines"

	reviewers_text = _reviewers_text()
	for expected in (
		'WATCHDOG_HELPERS="${SUPPORT_SCRIPTS_DIR:-scripts}/watchdog_helpers.sh"',
		'codex_run_budget_export "${JOB_START_EPOCH:-}" "${REVIEW_SOFT_DEADLINE_MINUTES:-}"',
		"codex_run_budget_summary",
		"codex_run_budget_phase_may_start",
		'REVIEWER_SOFT_DEADLINE_DEFAULT_MINUTES="210"',
		"reviewer_budget_remaining_secs_fallback",
		'REVIEWER_PARTIAL_FINALIZE_REQUEST_FILE="${RUNTIME_DIR:-.}/reviewers_partial_finalize_request.txt"',
		'reviewer_pass_minimum_secs=300',
		'reviewer_request_partial_finalize "soft_deadline"',
		'AUTOFIX_PARTIAL_FINALIZE_REQUESTED=true',
		'AUTOFIX_PARTIAL_FINALIZE_PHASE=reviewers',
		'echo "skipped_budget" > "${status_file}"',
		'| tee -a "${budget_log_file}" >&2 || true',
	):
		assert expected in reviewers_text, f"missing reviewer budget wiring: {expected}"

	apply_fixes_text = _apply_fixes_text()
	for expected in (
		'codex_run_budget_export "${JOB_START_EPOCH:-}" "${REVIEW_SOFT_DEADLINE_MINUTES:-}"',
		'REVIEW_SOFT_DEADLINE_MINUTES_NORMALIZED="$(normalize_review_soft_deadline_minutes "${REVIEW_SOFT_DEADLINE_MINUTES:-}")"',
		"codex_run_budget_remaining_secs",
		"budget_deadline_label=\"soft deadline\"",
		"request_editor_partial_finalize",
		'editor_partial_finalize_reason="soft_deadline"',
		'editor_partial_finalize_reason="refusal"',
		'editor_partial_finalize_reason="recoverable_failure"',
		'AUTOFIX_PARTIAL_FINALIZE_PHASE=editor',
	):
		assert expected in apply_fixes_text, f"missing editor budget wiring: {expected}"


def test_review_collect_pr_metadata_helper_is_bootstrapped_and_delegated() -> None:
	required_bootstrap_line = next(
		line for line in _stage_helper_text().splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line
	)
	block = _step_block("Collect PR metadata")
	helper_text = METADATA_HELPER.read_text(encoding="utf-8")

	assert METADATA_HELPER.exists(), f"missing helper: {METADATA_HELPER}"
	assert "review_collect_pr_metadata.sh" in required_bootstrap_line, required_bootstrap_line
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_collect_pr_metadata.sh"' in block
	assert 'gh_retry "${PR_PAYLOAD_FILE}"' not in block
	assert 'source "${SCRIPT_DIR}/gh_helpers.sh"' in helper_text
	assert 'gh_retry_to_file "${outfile}" gh "$@"' in helper_text
	assert 'review_collect_pr_metadata.XXXXXX' in helper_text
	assert '::error::Unable to determine PR base branch' in helper_text


def test_review_enable_auto_merge_helper_is_bootstrapped_and_delegated() -> None:
	required_bootstrap_line = next(
		line for line in _stage_helper_text().splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line
	)
	block = _step_block("Enable auto-merge on PR")
	helper_text = _auto_merge_helper_text()

	assert AUTO_MERGE_HELPER.exists(), f"missing helper: {AUTO_MERGE_HELPER}"
	assert "review_enable_auto_merge.sh" in required_bootstrap_line, required_bootstrap_line
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_enable_auto_merge.sh"' in block
	assert 'gh pr merge "${PR_NUMBER}"' not in block
	assert 'source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh"' not in block
	assert 'source "${SCRIPT_DIR}/gh_helpers.sh"' in helper_text
	assert 'type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }' in helper_text
	assert "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" in helper_text
	assert "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" in helper_text
	assert 'gh pr merge "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --squash --auto' in helper_text


def test_collect_pr_check_runs_helper_is_bootstrapped_and_delegated() -> None:
	required_bootstrap_line = next(
		line for line in _stage_helper_text().splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line
	)
	block = _step_block("Collect PR check-run failures (CI/lint autofix context)")
	helper_text = CHECK_RUNS_HELPER.read_text(encoding="utf-8")

	assert CHECK_RUNS_HELPER.exists(), f"missing helper: {CHECK_RUNS_HELPER}"
	assert "collect_pr_check_runs_context.py" in required_bootstrap_line, required_bootstrap_line
	assert 'python3 "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py"' in block
	assert 'gh api --paginate --slurp' not in block
	assert 'gh_retry gh api --paginate --slurp' in helper_text
	assert 'NamedTemporaryFile' in helper_text
	assert '_write_text(out_path, "")' in helper_text
	assert 'with _LOG_REDIRECT_OPENER.open(api_req, timeout=10):' in helper_text
	assert 'exc.close()' in helper_text
	assert 'traceback.print_exc(file=sys.stderr)' in helper_text
	assert 'CHECK_RUNS_AUTOFIX_WRITER_ERROR' in helper_text


def test_collect_pr_check_runs_helper_closes_direct_log_redirect_response() -> None:
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_test", CHECK_RUNS_HELPER)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	class _DirectResponse:
		def __init__(self) -> None:
			self.closed = False

		def __enter__(self):
			return self

		def __exit__(self, exc_type, exc, tb) -> None:
			self.closed = True

	class _FakeOpener:
		def __init__(self, response: _DirectResponse) -> None:
			self._response = response

		def open(self, req, timeout=10):
			return self._response

	response = _DirectResponse()
	original_opener = module._LOG_REDIRECT_OPENER
	try:
		module._LOG_REDIRECT_OPENER = _FakeOpener(response)
		result = module._fetch_log_tail(
			details_url="https://github.com/owner/repo/actions/runs/41/job/82",
			log_tail_bytes=64,
			repository="owner/repo",
			token="test-token",
		)
	finally:
		module._LOG_REDIRECT_OPENER = original_opener

	assert result == ""
	assert response.closed is True


def test_collect_pr_check_runs_helper_ready_contract_preserves_self_run_exclusion() -> None:
	result = _run_collect_pr_check_runs_harness(
		pr_payload={"head": {"sha": "abc123"}},
		self_run_id="777",
		check_runs_responses=[{
			"json": [
				{
					"check_runs": [
						{
							"id": 41,
							"name": "unit-tests",
							"status": "completed",
							"conclusion": "failure",
							"app": {"slug": "github-actions"},
							"html_url": "https://github.com/owner/repo/runs/41",
							"details_url": "https://github.com/owner/repo/actions/runs/41/job/82",
							"output": {"title": "Tests failed", "summary": ""},
						},
					],
				},
				{
					"check_runs": [
						{
							"id": 99,
							"name": "review / codex-agent",
							"status": "in_progress",
							"html_url": "https://github.com/owner/repo/runs/99",
							"details_url": "https://github.com/owner/repo/actions/runs/777/job/99",
						},
					],
				},
			],
		}],
	)

	assert result["returncode"] == 0, result
	assert "collection_status: ready\n" in result["context_text"]
	assert "total_check_runs: 2\n" in result["context_text"]
	assert "failed_count: 1\n" in result["context_text"]
	assert "incomplete_count: 1\n" in result["context_text"]
	assert "failed[0].name: unit-tests\n" in result["context_text"]
	assert "failed[0].summary: \n" in result["context_text"]
	assert "failed[0].log_tail (0 chars):\n" in result["context_text"]
	assert "incomplete[0].name: review / codex-agent\n" in result["context_text"]
	assert "Check-run context bytes:" in result["stdout"]
	assert "Check-run context sha256:" in result["stdout"]
	call_texts = [" ".join(call) for call in result["mock_state"]["calls"]]
	assert any("--paginate" in call and "--slurp" in call and "/check-runs?per_page=100" in call for call in call_texts)


def test_collect_pr_check_runs_helper_fail_open_contracts() -> None:
	disabled = _run_collect_pr_check_runs_harness(
		pr_payload={"head": {"sha": "abc123"}},
		check_runs_autofix_enabled="false",
	)
	assert disabled["returncode"] == 0, disabled
	assert disabled["context_text"] == (
		"PR_CHECK_RUNS_CONTEXT\n"
		"head_sha: \n"
		"collection_status: disabled\n"
		"total_check_runs: 0\n"
		"failed_count: 0\n"
		"incomplete_count: 0\n"
		"\n"
		"Check-run autofix context collection is disabled (CHECK_RUNS_AUTOFIX_ENABLED=false).\n"
	)
	assert "CHECK_RUNS_AUTOFIX disabled via CHECK_RUNS_AUTOFIX_ENABLED=false\n" == disabled["stdout"]
	assert disabled["mock_state"].get("calls", []) == []

	api_error = _run_collect_pr_check_runs_harness(
		pr_payload={"head": {"sha": "abc123"}},
		check_runs_responses=[{"exit_code": 1, "stderr": "mock gh: check-runs failure"}],
	)
	assert api_error["returncode"] == 0, api_error
	assert "collection_status: api_error\n" in api_error["context_text"]
	assert "Check-run API call failed; treat absence of failures as unknown rather than confirmed-passing.\n" in api_error["context_text"]
	assert "mock gh: check-runs failure\n" in api_error["stderr"]
	assert "Check-run context bytes:" in api_error["stdout"]


def test_collect_pr_check_runs_helper_writer_error_is_observable_and_fail_open() -> None:
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_test", CHECK_RUNS_HELPER)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	with tempfile.TemporaryDirectory(prefix="collect-pr-check-runs-writer-error-") as td:
		tmp = Path(td)
		pr_payload_file = tmp / "pr_payload.json"
		pr_payload_file.write_text(json.dumps({"head": {"sha": "abc123"}}), encoding="utf-8")
		context_file = tmp / "pr_check_runs_context.txt"
		context_file.write_text("stale-context\n", encoding="utf-8")

		def _fake_run_check_runs_api(*, repository: str, head_sha: str, script_dir: Path) -> subprocess.CompletedProcess[str]:
			return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")

		def _boom(*, raw_text: str, head_sha: str, final_status: str) -> str:
			raise RuntimeError("boom")

		original_env = os.environ.copy()
		original_run = module._run_check_runs_api
		original_build = module._build_context_text
		try:
			os.environ.update({
				"PR_PAYLOAD_FILE": str(pr_payload_file),
				"PR_CHECK_RUNS_CONTEXT_FILE": str(context_file),
				"CHECK_RUNS_AUTOFIX_ENABLED": "true",
				"CHECK_RUNS_WAIT_TIMEOUT_SECS": "300",
				"CHECK_RUNS_POLL_INTERVAL_SECS": "20",
				"CHECK_RUNS_LOG_TAIL_BYTES": "0",
				"GITHUB_REPOSITORY": "owner/repo",
				"SELF_RUN_ID": "",
			})
			module._run_check_runs_api = _fake_run_check_runs_api
			module._build_context_text = _boom
			stdout = io.StringIO()
			stderr = io.StringIO()
			with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
				returncode = module.main()
		finally:
			os.environ.clear()
			os.environ.update(original_env)
			module._run_check_runs_api = original_run
			module._build_context_text = original_build

		assert returncode == 0
		assert context_file.read_text(encoding="utf-8") == (
			"PR_CHECK_RUNS_CONTEXT\n"
			"head_sha: abc123\n"
			"collection_status: writer_error\n"
			"total_check_runs: 0\n"
			"failed_count: 0\n"
			"incomplete_count: 0\n"
			"\n"
			"Check-run snapshot writer failed; treat absence of failures as unknown rather than confirmed-passing.\n"
		)
		assert "stale-context" not in context_file.read_text(encoding="utf-8")
		assert "RuntimeError: boom" in stderr.getvalue()
		assert "::warning::CHECK_RUNS_AUTOFIX_WRITER_ERROR head_sha=abc123 writer_ok=False" in stdout.getvalue()


def test_collect_pr_check_runs_helper_top_level_exception_is_fail_open() -> None:
	spec = importlib.util.spec_from_file_location("collect_pr_check_runs_context_test", CHECK_RUNS_HELPER)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	with tempfile.TemporaryDirectory(prefix="collect-pr-check-runs-top-level-") as td:
		tmp = Path(td)
		pr_payload_file = tmp / "pr_payload.json"
		pr_payload_file.write_text(json.dumps({"head": {"sha": "abc123"}}), encoding="utf-8")
		context_file = tmp / "pr_check_runs_context.txt"
		context_file.write_text("stale-context\n", encoding="utf-8")

		def _fake_run_check_runs_api(*, repository: str, head_sha: str, script_dir: Path) -> subprocess.CompletedProcess[str]:
			return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")

		def _boom_wait_view(raw_text: str, self_run_id: str):
			raise RuntimeError("wait-view boom")

		original_env = os.environ.copy()
		original_run = module._run_check_runs_api
		original_wait_view = module._build_wait_view
		try:
			os.environ.update({
				"PR_PAYLOAD_FILE": str(pr_payload_file),
				"PR_CHECK_RUNS_CONTEXT_FILE": str(context_file),
				"CHECK_RUNS_AUTOFIX_ENABLED": "true",
				"CHECK_RUNS_WAIT_TIMEOUT_SECS": "300",
				"CHECK_RUNS_POLL_INTERVAL_SECS": "20",
				"CHECK_RUNS_LOG_TAIL_BYTES": "0",
				"GITHUB_REPOSITORY": "owner/repo",
				"SELF_RUN_ID": "",
			})
			module._run_check_runs_api = _fake_run_check_runs_api
			module._build_wait_view = _boom_wait_view
			stdout = io.StringIO()
			stderr = io.StringIO()
			with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
				returncode = module.main()
		finally:
			os.environ.clear()
			os.environ.update(original_env)
			module._run_check_runs_api = original_run
			module._build_wait_view = original_wait_view

		assert returncode == 0
		assert context_file.read_text(encoding="utf-8") == (
			"PR_CHECK_RUNS_CONTEXT\n"
			"head_sha: abc123\n"
			"collection_status: writer_error\n"
			"total_check_runs: 0\n"
			"failed_count: 0\n"
			"incomplete_count: 0\n"
			"\n"
			"Check-run snapshot writer failed; treat absence of failures as unknown rather than confirmed-passing.\n"
		)
		assert "stale-context" not in context_file.read_text(encoding="utf-8")
		assert "RuntimeError: wait-view boom" in stderr.getvalue()
		assert "::warning::CHECK_RUNS_AUTOFIX_WRITER_ERROR head_sha=abc123 writer_ok=False" in stdout.getvalue()


def test_review_collect_pr_metadata_helper_supports_no_pr_synthetic_mode() -> None:
	result = _run_review_collect_pr_metadata_harness(
		pr_number="",
		claude_branch_review_mode="true",
		head_ref_override="claude/test-no-pr",
		head_sha_override="deadbeef",
		base_ref_override="",
		mock_state={
			"api_responses": {
				"repos/owner/repo": {"default_branch": "main"},
			},
		},
	)

	assert result["pr_payload"] == {
		"title": "",
		"body": "",
		"head": {
			"ref": "claude/test-no-pr",
			"sha": "deadbeef",
			"repo": {"full_name": "owner/repo"},
		},
		"base": {"ref": "main"},
	}
	assert result["pr_meta"] == {
		"title": "",
		"body": "",
		"baseRefName": "main",
		"headRefName": "claude/test-no-pr",
		"headRepoFullName": "owner/repo",
	}
	assert result["pr_issue_comments"] == []
	assert result["pr_reviews"] == []
	assert result["pr_review_comments"] == []
	assert result["linked_issue_context"] == "No linked issues found."
	assert "issue_comments_count: 0" in result["comments_context"]
	assert "reviews_count: 0" in result["comments_context"]
	assert "review_comments_count: 0" in result["comments_context"]
	assert "AUTOFIX_NO_PR_METADATA_SYNTHESIZED head_ref=claude/test-no-pr head_sha=deadbeef base_ref=main" in result["stdout"]
	assert result["github_env"]["LINKED_ISSUES_JSON"] == "[]"
	assert result["github_env"]["PR_DIFF_ATTEMPTED_PATHS"].endswith("/pr_diff.patch")
	assert result["github_env"]["HAS_PR_DIFF"] == "false"
	assert result["github_env"]["PR_DIFF_SOURCE"] == "gh_pr_diff_empty"
	assert result["pr_diff"] == ""
	assert result["github_env"]["BASE_BRANCH"] == "main"
	assert any(call[:2] == ["api", "repos/owner/repo"] for call in result["mock_state"]["calls"])
	assert not any(call[:2] == ["api", "graphql"] for call in result["mock_state"]["calls"])
	assert not any(call[:2] == ["pr", "diff"] for call in result["mock_state"]["calls"])
	assert not any("repos/owner/repo/pulls/" in " ".join(call) for call in result["mock_state"]["calls"])


def test_review_collect_pr_metadata_helper_skips_optional_pr_reviews_by_default() -> None:
	result = _run_review_collect_pr_metadata_harness(
		pr_number="42",
		claude_branch_review_mode="false",
		head_ref_override="",
		head_sha_override="",
		base_ref_override="",
		mock_state={
			"api_responses": {
				"repos/owner/repo/pulls/42/comments": [
					{
						"id": 13,
						"user": {"login": "carol"},
						"created_at": "2026-06-03T00:00:00Z",
						"updated_at": "2026-06-03T01:00:00Z",
						"path": "scripts/helper.sh",
						"line": 7,
						"body": "Review comment body",
					},
				],
				"repos/owner/repo/pulls/42/reviews": [
					{
						"id": 12,
						"user": {"login": "bob"},
						"submitted_at": "2026-06-02T00:00:00Z",
						"updated_at": "2026-06-02T01:00:00Z",
						"state": "COMMENTED",
						"body": "Review body",
					},
				],
				"repos/owner/repo/issues/42/comments": [
					{
						"id": 11,
						"user": {"login": "alice"},
						"created_at": "2026-06-01T00:00:00Z",
						"updated_at": "2026-06-01T01:00:00Z",
						"body": "Issue comment body",
					},
				],
				"repos/owner/repo/pulls/42": {
					"title": "Synthetic PR title",
					"body": "Fixes #7\n\nContext body",
					"base": {"ref": "main"},
					"head": {
						"ref": "feature/ref",
						"sha": "abc123",
						"repo": {"full_name": "owner/repo"},
					},
				},
				"graphql": {
					"data": {
						"repository": {
							"pullRequest": {
								"closingIssuesReferences": {
									"nodes": [],
								},
							},
							"i0": {
								"__typename": "Issue",
								"number": 7,
								"title": "Linked fallback issue",
								"body": "Linked fallback body",
							},
						},
					},
				},
			},
			"pr_diffs": {"42": "pr diff sentinel\n"},
		},
	)

	assert result["pr_meta"] == {
		"title": "Synthetic PR title",
		"body": "Fixes #7\n\nContext body",
		"baseRefName": "main",
		"headRefName": "feature/ref",
		"headRepoFullName": "owner/repo",
	}
	assert len(result["pr_issue_comments"]) == 1
	assert result["pr_reviews"] == []
	assert len(result["pr_review_comments"]) == 1
	assert result["linked_issue_context"].splitlines()[:2] == [
		"Issue #7: Linked fallback issue",
		"Linked fallback body",
	]
	assert "issue_comments_count: 1" in result["comments_context"]
	assert "reviews_count: 0" in result["comments_context"]
	assert "review_comments_count: 1" in result["comments_context"]
	assert "total_entries: 2" in result["comments_context"]
	assert "entry[0].kind: issue_comment" in result["comments_context"]
	assert "entry[1].kind: review_comment" in result["comments_context"]
	assert result["pr_diff"] == "pr diff sentinel\n"
	assert result["github_env"]["LINKED_ISSUES_JSON"] == "[]"
	assert result["github_env"]["LINKED_ISSUE_FALLBACK_NUMBERS_JSON"] == "[7]"
	assert result["github_env"]["HAS_PR_DIFF"] == "true"
	assert result["github_env"]["PR_DIFF_SOURCE"] == "gh_pr_diff"
	assert result["github_env"]["BASE_BRANCH"] == "main"
	assert any(call[:2] == ["api", "repos/owner/repo/pulls/42"] for call in result["mock_state"]["calls"])
	call_texts = [" ".join(call) for call in result["mock_state"]["calls"]]
	graphql_call_texts = [call for call in call_texts if call.startswith("api graphql ")]
	assert len(graphql_call_texts) == 2
	fallback_call = next(call for call in graphql_call_texts if "issueOrPullRequest(number:" in call)
	assert "i0: issueOrPullRequest(number: 7)" in fallback_call
	assert not any("repos/owner/repo/issues/7" in call for call in call_texts)
	assert not any("repos/owner/repo/pulls/42/reviews" in call for call in call_texts)


def test_review_collect_pr_metadata_helper_fetches_top_level_reviews_when_break_glass_enabled() -> None:
	result = _run_review_collect_pr_metadata_harness(
		pr_number="42",
		claude_branch_review_mode="false",
		head_ref_override="",
		head_sha_override="",
		base_ref_override="",
		review_break_glass_enabled="true",
		mock_state={
			"api_responses": {
				"repos/owner/repo/pulls/42/comments": [
					{
						"id": 13,
						"user": {"login": "carol"},
						"created_at": "2026-06-03T00:00:00Z",
						"updated_at": "2026-06-03T01:00:00Z",
						"path": "scripts/helper.sh",
						"line": 7,
						"body": "Review comment body",
					},
				],
				"repos/owner/repo/pulls/42/reviews": [
					{
						"id": 12,
						"user": {"login": "bob"},
						"submitted_at": "2026-06-02T00:00:00Z",
						"updated_at": "2026-06-02T01:00:00Z",
						"state": "COMMENTED",
						"body": "Review body",
					},
				],
				"repos/owner/repo/issues/42/comments": [
					{
						"id": 11,
						"user": {"login": "alice"},
						"created_at": "2026-06-01T00:00:00Z",
						"updated_at": "2026-06-01T01:00:00Z",
						"body": "Issue comment body",
					},
				],
				"repos/owner/repo/pulls/42": {
					"title": "Synthetic PR title",
					"body": "Fixes #7\n\nContext body",
					"base": {"ref": "main"},
					"head": {
						"ref": "feature/ref",
						"sha": "abc123",
						"repo": {"full_name": "owner/repo"},
					},
				},
				"repos/owner/repo/issues/7": {
					"number": 7,
					"title": "Linked fallback issue",
					"body": "Linked fallback body",
				},
				"graphql": {
					"data": {
						"repository": {
							"pullRequest": {
								"closingIssuesReferences": {
									"nodes": [],
								},
							},
						},
					},
				},
			},
			"pr_diffs": {"42": "pr diff sentinel\n"},
		},
	)

	assert len(result["pr_reviews"]) == 1
	assert "reviews_count: 1" in result["comments_context"]
	assert "total_entries: 3" in result["comments_context"]
	assert "entry[1].kind: review" in result["comments_context"]
	assert "entry[2].kind: review_comment" in result["comments_context"]
	assert any("repos/owner/repo/pulls/42/reviews" in " ".join(call) for call in result["mock_state"]["calls"])


def test_review_collect_pr_metadata_helper_fails_open_on_non_array_batch_input() -> None:
	helper_block = _review_collect_pr_metadata_graphql_helper_block()
	script = textwrap.dedent(
		"""\
		set -euo pipefail
		TMP_RUNTIME_DIR="$(mktemp -d)"
		REPOSITORY_OWNER="owner"
		REPOSITORY_NAME="repo"
		gh_retry() {
			echo "unexpected gh_retry call" >&2
			return 99
		}
		"""
	) + helper_block + "\n_fetch_linked_issue_bodies_graphql $'1\\n2'\n"

	result = subprocess.run(
		["bash"],
		cwd=str(REPO_ROOT),
		input=script,
		capture_output=True,
		text=True,
		check=False,
		timeout=60,
	)

	assert result.returncode == 0
	assert result.stdout.strip() == "[]"
	assert "unexpected gh_retry call" not in result.stderr


def test_review_collect_pr_metadata_helper_strict_fallback_drops_bare_mentions() -> None:
	result = _run_review_collect_pr_metadata_harness(
		pr_number="42",
		claude_branch_review_mode="false",
		head_ref_override="",
		head_sha_override="",
		base_ref_override="",
		mock_state={
			"api_responses": {
				"repos/owner/repo/pulls/42/comments": [],
				"repos/owner/repo/pulls/42/reviews": [],
				"repos/owner/repo/issues/42/comments": [],
				"repos/owner/repo/pulls/42": {
					"title": "Docs update referencing issue #7 and issues/8 plus someotherowner/repo/issues/15",
					"body": "Fixes #10\nIgnore Fixes #11a and owner/repo/issues/16abc\nAlso see owner/repo/issues/12 and github.com/owner/repo/issues/13\nCloses: #14",
					"base": {"ref": "main"},
					"head": {
						"ref": "feature/ref",
						"sha": "abc123",
						"repo": {"full_name": "owner/repo"},
					},
				},
				"graphql": {
					"data": {
						"repository": {
							"pullRequest": {
								"closingIssuesReferences": {
									"nodes": [],
								},
							},
							"i0": {
								"__typename": "Issue",
								"number": 10,
								"title": "Closing keyword match",
								"body": "Fix keyword body",
							},
							"i1": {
								"__typename": "Issue",
								"number": 12,
								"title": "Repo path match",
								"body": "Path body",
							},
							"i2": {
								"__typename": "Issue",
								"number": 13,
								"title": "Repo URL match",
								"body": "URL body",
							},
						},
					},
				},
			},
			"pr_diffs": {"42": "pr diff sentinel\n"},
		},
	)

	assert result["github_env"]["LINKED_ISSUES_JSON"] == "[]"
	assert result["github_env"]["LINKED_ISSUE_FALLBACK_NUMBERS_JSON"] == "[10,12,13]"
	assert "Issue #10: Closing keyword match" in result["linked_issue_context"]
	assert "Issue #12: Repo path match" in result["linked_issue_context"]
	assert "Issue #13: Repo URL match" in result["linked_issue_context"]
	assert "Issue #7:" not in result["linked_issue_context"]
	assert "Issue #8:" not in result["linked_issue_context"]
	assert "Issue #11:" not in result["linked_issue_context"]
	assert "Issue #14:" not in result["linked_issue_context"]
	assert "Issue #15:" not in result["linked_issue_context"]
	assert "Issue #16:" not in result["linked_issue_context"]
	call_texts = [" ".join(call) for call in result["mock_state"]["calls"]]
	graphql_call_texts = [call for call in call_texts if call.startswith("api graphql ")]
	assert len(graphql_call_texts) == 2
	fallback_call = next(call for call in graphql_call_texts if "issueOrPullRequest(number:" in call)
	assert "i0: issueOrPullRequest(number: 10)" in fallback_call
	assert "i1: issueOrPullRequest(number: 12)" in fallback_call
	assert "i2: issueOrPullRequest(number: 13)" in fallback_call
	assert not any("repos/owner/repo/issues/7" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/8" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/10" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/11" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/12" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/13" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/14" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/15" in call for call in call_texts)
	assert not any("repos/owner/repo/issues/16" in call for call in call_texts)


def test_review_collect_pr_metadata_helper_warns_when_fallback_graphql_returns_errors_without_data() -> None:
	result = _run_review_collect_pr_metadata_harness(
		pr_number="42",
		claude_branch_review_mode="false",
		head_ref_override="",
		head_sha_override="",
		base_ref_override="",
		mock_state={
			"api_responses": {
				"repos/owner/repo/pulls/42/comments": [],
				"repos/owner/repo/pulls/42/reviews": [],
				"repos/owner/repo/issues/42/comments": [],
				"repos/owner/repo/pulls/42": {
					"title": "Synthetic PR title",
					"body": "Fixes #7\n\nContext body",
					"base": {"ref": "main"},
					"head": {
						"ref": "feature/ref",
						"sha": "abc123",
						"repo": {"full_name": "owner/repo"},
					},
				},
				"graphql": {
					"errors": [{"message": "synthetic graphql failure"}],
					"data": None,
				},
			},
			"pr_diffs": {"42": "pr diff sentinel\n"},
		},
	)

	assert result["github_env"]["LINKED_ISSUES_JSON"] == "[]"
	assert result["github_env"]["LINKED_ISSUE_FALLBACK_NUMBERS_JSON"] == "[7]"
	assert result["linked_issue_context"] == "No linked issues found."
	assert "::warning::Linked-issue body-text fallback: batched GraphQL issue hydration failed; skipping" in result["stdout"]
	call_texts = [" ".join(call) for call in result["mock_state"]["calls"]]
	assert len([call for call in call_texts if call.startswith("api graphql ")]) == 2
	assert not any("repos/owner/repo/issues/7" in call for call in call_texts)

	partial_result = _run_review_collect_pr_metadata_harness(
		pr_number="42",
		claude_branch_review_mode="false",
		head_ref_override="",
		head_sha_override="",
		base_ref_override="",
		mock_state={
			"api_responses": {
				"repos/owner/repo/pulls/42/comments": [],
				"repos/owner/repo/pulls/42/reviews": [],
				"repos/owner/repo/issues/42/comments": [],
				"repos/owner/repo/pulls/42": {
					"title": "Synthetic PR title",
					"body": "Fixes #7\nFixes #8\n\nContext body",
					"base": {"ref": "main"},
					"head": {
						"ref": "feature/ref",
						"sha": "abc123",
						"repo": {"full_name": "owner/repo"},
					},
				},
				"graphql": {
					"errors": [{"message": "synthetic partial graphql failure"}],
					"data": {
						"repository": {
							"pullRequest": {
								"closingIssuesReferences": {
									"nodes": [],
								},
							},
							"i0": {
								"__typename": "Issue",
								"number": 7,
								"title": "Linked fallback issue",
								"body": "Linked fallback body",
							},
							"i1": None,
						},
					},
				},
			},
			"pr_diffs": {"42": "pr diff sentinel\n"},
		},
	)

	assert partial_result["github_env"]["LINKED_ISSUE_FALLBACK_NUMBERS_JSON"] == "[7,8]"
	assert "Issue #7: Linked fallback issue" in partial_result["linked_issue_context"]
	assert "Issue #8:" not in partial_result["linked_issue_context"]
	assert "Linked-issue body-text fallback resolved 1 issue(s) for context" in partial_result["stdout"]
	assert (
		"::warning::Linked-issue body-text fallback: batched GraphQL issue hydration returned partial data "
		"(hydrated 1 of 2 references); continuing with available context."
		in partial_result["stderr"]
	)


def test_review_collect_pr_metadata_helper_caps_fallback_graphql_batch_at_twenty_issues() -> None:
	referenced_numbers = list(range(1, 23))
	graphql_repository = {
		"pullRequest": {
			"closingIssuesReferences": {
				"nodes": [],
			},
		},
	}
	for idx, number in enumerate(referenced_numbers[:20]):
		graphql_repository[f"i{idx}"] = {
			"__typename": "Issue",
			"number": number,
			"title": f"Issue {number}",
			"body": f"Body {number}",
		}

	result = _run_review_collect_pr_metadata_harness(
		pr_number="42",
		claude_branch_review_mode="false",
		head_ref_override="",
		head_sha_override="",
		base_ref_override="",
		mock_state={
			"api_responses": {
				"repos/owner/repo/pulls/42/comments": [],
				"repos/owner/repo/pulls/42/reviews": [],
				"repos/owner/repo/issues/42/comments": [],
				"repos/owner/repo/pulls/42": {
					"title": "Synthetic PR title",
					"body": "\n".join(f"Fixes #{number}" for number in referenced_numbers),
					"base": {"ref": "main"},
					"head": {
						"ref": "feature/ref",
						"sha": "abc123",
						"repo": {"full_name": "owner/repo"},
					},
				},
				"graphql": {
					"data": {
						"repository": graphql_repository,
					},
				},
			},
			"pr_diffs": {"42": "pr diff sentinel\n"},
		},
	)

	assert result["github_env"]["LINKED_ISSUES_JSON"] == "[]"
	assert result["github_env"]["LINKED_ISSUE_FALLBACK_NUMBERS_JSON"] == json.dumps(
		referenced_numbers,
		separators=(",", ":"),
	)
	assert "Issue #20: Issue 20" in result["linked_issue_context"]
	assert "Issue #21:" not in result["linked_issue_context"]
	assert "Issue #22:" not in result["linked_issue_context"]
	call_texts = [" ".join(call) for call in result["mock_state"]["calls"]]
	graphql_call_texts = [call for call in call_texts if call.startswith("api graphql ")]
	assert len(graphql_call_texts) == 2
	fallback_call = next(call for call in graphql_call_texts if "issueOrPullRequest(number:" in call)
	for idx, number in enumerate(referenced_numbers[:20]):
		assert f"i{idx}: issueOrPullRequest(number: {number})" in fallback_call
	assert "issueOrPullRequest(number: 21)" not in fallback_call
	assert "issueOrPullRequest(number: 22)" not in fallback_call
	for number in referenced_numbers:
		assert not any(
			call[:2] == ["api", f"repos/owner/repo/issues/{number}"]
			for call in result["mock_state"]["calls"]
		)


def test_extract_repo_scoped_issue_refs_rejects_malformed_repository_input() -> None:
	helpers_path = (REPO_ROOT / "scripts" / "gh_helpers.sh").as_posix()
	script = textwrap.dedent(
		f"""\
		set -euo pipefail
		source \"{helpers_path}\"
		extract_repo_scoped_issue_refs_from_text \"$REPOSITORY_INPUT\" \"$TEXT_INPUT\"
		"""
	)

	for repository_input in ("owner", "owner/repo/extra"):
		env = os.environ.copy()
		env.update({
			"REPOSITORY_INPUT": repository_input,
			"TEXT_INPUT": "Fixes #12\nowner/repo/issues/13",
		})
		result = subprocess.run(
			["bash", "-lc", script],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
			check=True,
		)
		assert result.stdout == ""


def test_review_scripts_emit_context_budget_warn_signals() -> None:
	for script_text, expected_call in (
		(_reviewers_text(), 'emit_context_budget_warn_for_prompt "review"'),
		(_consolidate_text(), 'emit_context_budget_warn_for_prompt "consolidator"'),
		(_apply_fixes_text(), 'emit_context_budget_warn_for_prompt "editor"'),
		(_rb_judge_text(), 'emit_context_budget_warn_for_prompt "review_blocked_judge"'),
	):
		assert "build_context_budget_warn_line_for_file" in script_text
		assert expected_call in script_text


def test_review_consolidator_prompt_is_staged_for_review_runtime_support() -> None:
	stage_helper = _stage_helper_text()
	consolidate = _consolidate_text()

	assert 'PROMPT_TEMPLATE="${SUPPORT_PROMPTS_DIR:-prompts}/review-consolidator.txt"' in consolidate
	assert 'if [ ! -f "${SUPPORT_PROMPTS_DIR}/review-consolidator.txt" ]; then' in stage_helper
	assert 'src=".codex-workflow-src/prompts/review-consolidator.txt"' in stage_helper
	assert 'src=".codex-workflow-src-main/prompts/review-consolidator.txt"' in stage_helper
	assert 'install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/review-consolidator.txt"' in stage_helper
	assert 'review-consolidator.txt not found in checked-out support sources' in stage_helper
	assert 'REVIEW_CONSOLIDATOR_ENABLED=true' in stage_helper
	assert 'rm -f "${SUPPORT_PROMPTS_DIR}/review-consolidator.txt"' in stage_helper


def test_review_pipeline_slop_scan_wiring_is_flagged_fail_open_and_pre_commit_cleaned() -> None:
	workflow = _workflow_text()
	collect_block = _step_block("Collect local slop-scan findings")
	cleanup_block = _step_block("Remove slop-scan runtime artifact")

	assert 'echo "SLOP_SCAN_FINDINGS_FILE=${GITHUB_WORKSPACE}/.ai/slop_scan/findings.json"' in workflow
	assert "continue-on-error: true" in collect_block
	assert 'write_slop_scan_sentinel "disabled"' in collect_block
	assert 'write_slop_scan_sentinel "scan_error"' in collect_block
	assert '"${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/slop_scan_local.py"' in collect_block
	assert '--output "${SLOP_SCAN_FINDINGS_FILE}"' in collect_block
	assert 'if: always()' in cleanup_block
	assert 'rm -f "${SLOP_SCAN_FINDINGS_FILE}"' in cleanup_block
	assert 'rmdir "${expected_dir}"' in cleanup_block
	assert workflow.index("- name: Remove slop-scan runtime artifact") < workflow.index("- name: Commit changes")


def test_reviewer_and_consolidator_slop_scan_context_is_wired() -> None:
	reviewers = _reviewers_text()
	consolidate = _consolidate_text()
	prompt_text = (REPO_ROOT / "prompts" / "review-consolidator.txt").read_text(encoding="utf-8")

	assert 'SLOP_SCAN_FINDINGS_FILE="${SLOP_SCAN_FINDINGS_FILE:-${GITHUB_WORKSPACE:-$(pwd)}/.ai/slop_scan/findings.json}"' in reviewers
	assert '=== BEGIN UNTRUSTED ${SLOP_SCAN_FINDINGS_FILE} (heuristic local slop-scan findings' in reviewers
	assert 'SLOP_SCAN_FINDINGS_FILE="${SLOP_SCAN_FINDINGS_FILE:-${GITHUB_WORKSPACE:-$PWD}/.ai/slop_scan/findings.json}"' in consolidate
	assert "'SLOP SCAN FINDINGS'" in consolidate
	assert "=== BEGIN UNTRUSTED SLOP SCAN FINDINGS ===" in prompt_text
	assert "best-effort cleanup helpers" in prompt_text
	assert "catch-and-log boundaries" in prompt_text


def test_review_filter_smoke_fixtures_are_present() -> None:
	assert PHASE_A_ANTI_RULES_FIXTURE.exists(), f"missing fixture: {PHASE_A_ANTI_RULES_FIXTURE}"
	assert PHASE_C_FILTER_FIXTURE.exists(), f"missing fixture: {PHASE_C_FILTER_FIXTURE}"
	assert PHASE_B_RISK_TIER_TRIVIAL_FIXTURE.exists(), f"missing fixture: {PHASE_B_RISK_TIER_TRIVIAL_FIXTURE}"
	assert PHASE_B_RISK_TIER_LITE_FIXTURE.exists(), f"missing fixture: {PHASE_B_RISK_TIER_LITE_FIXTURE}"
	assert PHASE_B_RISK_TIER_FULL_FIXTURE.exists(), f"missing fixture: {PHASE_B_RISK_TIER_FULL_FIXTURE}"
	assert PHASE_B_RISK_TIER_ALWAYS_FULL_FIXTURE.exists(), f"missing fixture: {PHASE_B_RISK_TIER_ALWAYS_FULL_FIXTURE}"
	assert PHASE_D_MATERIALITY_FIXTURE.exists(), f"missing fixture: {PHASE_D_MATERIALITY_FIXTURE}"
	assert PHASE_G_FLAKY_REVIEWER_FIXTURE.exists(), f"missing fixture: {PHASE_G_FLAKY_REVIEWER_FIXTURE}"
	assert PHASE_H_CONTEXT_BUDGET_FIXTURE.exists(), f"missing fixture: {PHASE_H_CONTEXT_BUDGET_FIXTURE}"


def test_reviewer_risk_tier_classifier_honours_thresholds_and_always_full_regex() -> None:
	reviewer_models = _workflow_reviewer_models()
	assert len(reviewer_models) >= 2, "workflow reviewer list should provide default trivial/lite subsets"

	for fixture, expected_tier, expected_models, expected_loc, expected_files, forced_full in (
		(PHASE_B_RISK_TIER_TRIVIAL_FIXTURE, "trivial", reviewer_models[:1], "2", "1", "false"),
		(PHASE_B_RISK_TIER_LITE_FIXTURE, "lite", reviewer_models[:2], "50", "1", "false"),
		(PHASE_B_RISK_TIER_FULL_FIXTURE, "full", reviewer_models, "42", "21", "false"),
		(PHASE_B_RISK_TIER_ALWAYS_FULL_FIXTURE, "full", reviewer_models, "2", "1", "true"),
	):
		result = _run_reviewer_risk_tier_harness(diff_text=fixture.read_text(encoding="utf-8"))
		assert result["REVIEWER_RISK_TIER"] == expected_tier
		assert result["risk_tier_file"] == expected_tier
		assert result["active_models"] == expected_models
		assert result["REVIEWER_RISK_TIER_LOC"] == expected_loc
		assert result["REVIEWER_RISK_TIER_FILES"] == expected_files
		assert result["REVIEWER_RISK_TIER_FORCED_FULL"] == forced_full
		assert f"REVIEWER_RISK_TIER: tier={expected_tier}" in result["stdout"]

	always_full_result = _run_reviewer_risk_tier_harness(diff_text=PHASE_B_RISK_TIER_ALWAYS_FULL_FIXTURE.read_text(encoding="utf-8"))
	assert "matched_path=scripts/review_helper.sh" in always_full_result["stdout"]


def test_review_tier_resolver_routes_lite_standard_and_full_and_handles_overrides() -> None:
	reviewer_models = _workflow_reviewer_models()
	lite_diff = textwrap.dedent(
		"""\
		diff --git a/README.md b/README.md
		index 1111111..2222222 100644
		--- a/README.md
		+++ b/README.md
		@@ -1 +1,2 @@
		-old line
		+new line
		+second line
		"""
	)
	standard_diff = textwrap.dedent(
		"""\
		diff --git a/scripts/review_helper.sh b/scripts/review_helper.sh
		index 1111111..2222222 100644
		--- a/scripts/review_helper.sh
		+++ b/scripts/review_helper.sh
		@@ -1 +1,3 @@
		-echo old
		+echo new
		+echo newer
		+echo newest
		"""
	)
	workflow_diff = textwrap.dedent(
		"""\
		diff --git a/.github/workflows/review.yml b/.github/workflows/review.yml
		index 1111111..2222222 100644
		--- a/.github/workflows/review.yml
		+++ b/.github/workflows/review.yml
		@@ -1 +1,2 @@
		-name: Old review
		+name: New review
		+run-name: Reviewer update
		"""
	)
	full_diff = textwrap.dedent(
		"""\
		diff --git a/scripts/review_helper.sh b/scripts/review_helper.sh
		index 1111111..2222222 100644
		--- a/scripts/review_helper.sh
		+++ b/scripts/review_helper.sh
		@@ -1 +1,2 @@
		-echo old
		+echo new
		+echo newer
		diff --git a/tests/test_review_helper.py b/tests/test_review_helper.py
		index 3333333..4444444 100644
		--- a/tests/test_review_helper.py
		+++ b/tests/test_review_helper.py
		@@ -1 +1,2 @@
		-old_test()
		+new_test()
		+more_test()
		"""
	)

	lite_result = _run_review_tier_harness(diff_text=lite_diff)
	assert lite_result["REVIEW_TIER"] == "lite"
	assert lite_result["REVIEW_TIER_REASON"] == "doc_only_<=50_loc"
	assert lite_result["review_tier_file"] == "lite"
	assert lite_result["active_models"] == ["qwen/qwen3.6-plus"]
	assert lite_result["REVIEW_TIER_FORCED_FULL"] == "false"
	assert "REVIEW_CONSOLIDATOR_ENABLED=0\n" in lite_result["github_env"]
	assert "REVIEW_TIER: tier=lite" in lite_result["stdout"]

	standard_result = _run_review_tier_harness(diff_text=standard_diff)
	assert standard_result["REVIEW_TIER"] == "standard"
	assert standard_result["REVIEW_TIER_REASON"] == "code_<=200_loc_single_dir"
	assert standard_result["REVIEW_TIER_SCOPE"] == "scripts/"
	assert standard_result["review_tier_file"] == "standard"
	assert standard_result["active_models"] == [
		"minimax/minimax-m2.5",
		"deepseek/deepseek-v4-pro",
		"x-ai/grok-4.20",
	]
	assert "REVIEW_CONSOLIDATOR_ENABLED=0\n" not in standard_result["github_env"]

	workflow_result = _run_review_tier_harness(diff_text=workflow_diff)
	assert workflow_result["REVIEW_TIER"] == "standard"
	assert workflow_result["REVIEW_TIER_SCOPE"] == ".github/workflows/"

	full_result = _run_review_tier_harness(diff_text=full_diff)
	assert full_result["REVIEW_TIER"] == "full"
	assert full_result["REVIEW_TIER_REASON"] == "default"
	assert full_result["review_tier_file"] == "full"
	assert full_result["active_models"] == reviewer_models

	force_full_result = _run_review_tier_harness(
		diff_text=lite_diff,
		extra_env={"FORCE_FULL_REVIEW_TIER": "true"},
	)
	assert force_full_result["REVIEW_TIER"] == "full"
	assert force_full_result["REVIEW_TIER_REASON"] == "force_review_marker"
	assert force_full_result["REVIEW_TIER_FORCED_FULL"] == "true"
	assert force_full_result["active_models"] == reviewer_models

	invalid_lite_result = _run_review_tier_harness(
		diff_text=lite_diff,
		extra_env={"REVIEW_TIER_LITE_REVIEWER_SLUG": "unknown/model"},
	)
	assert invalid_lite_result["REVIEW_TIER"] == "full"
	assert invalid_lite_result["REVIEW_TIER_REASON"] == "invalid_lite_reviewer_slug"
	assert invalid_lite_result["active_models"] == reviewer_models

	invalid_standard_result = _run_review_tier_harness(
		diff_text=standard_diff,
		extra_env={"REVIEW_TIER_STANDARD_REVIEWER_SLUGS": "unknown/model"},
	)
	assert invalid_standard_result["REVIEW_TIER"] == "full"
	assert invalid_standard_result["REVIEW_TIER_REASON"] == "invalid_standard_reviewer_slugs"
	assert invalid_standard_result["active_models"] == reviewer_models


def test_review_filter_helper_wiring_is_flag_gated_and_fail_open() -> None:
	workflow = _workflow_text()
	stage_helper = _stage_helper_text()
	preflight_block = _step_block('"Preflight: Verify required files before reviewer invocation"')
	reviewers = _reviewers_text()

	assert "REVIEWER_FILTER_UNINTERESTING_ENABLED: ${{ vars.REVIEWER_FILTER_UNINTERESTING_ENABLED || 'false' }}" in workflow
	assert "REVIEWER_FILTER_EXTRA_GLOBS: ${{ vars.REVIEWER_FILTER_EXTRA_GLOBS || '' }}" in workflow
	assert "REVIEWER_FILTER_EXEMPT_GLOBS: ${{ vars.REVIEWER_FILTER_EXEMPT_GLOBS || 'db/contracts/**,**/migrations/**,**/migrate/**' }}" in workflow
	assert 'if [ ! -f "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh" ]; then' in stage_helper
	assert 'src=".codex-workflow-src/scripts/review_filter_uninteresting_files.sh"' in stage_helper
	assert 'src=".codex-workflow-src-main/scripts/review_filter_uninteresting_files.sh"' in stage_helper
	assert 'install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh"' in stage_helper
	assert 'review_filter_uninteresting_files.sh not found in checked-out support sources' in stage_helper
	assert 'check_soft_file "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh"' in preflight_block
	assert 'REVIEWER_FILTER_SCRIPT="${SUPPORT_SCRIPTS_DIR:-scripts}/review_filter_uninteresting_files.sh"' in reviewers
	assert 'prepare_reviewer_filtered_artifacts' in reviewers
	assert 'REVIEWER_FILTER_SKIP:' in reviewers
	assert 'review_filter_uninteresting_files.sh unavailable' in reviewers


def test_agents_md_materiality_classifier_and_workflow_wiring() -> None:
	positive = _run_agents_md_materiality_harness(
		diff_text=PHASE_D_MATERIALITY_FIXTURE.read_text(encoding="utf-8"),
		workspace_files={"agents.md": "# repo agents\n"},
	)
	positive_result = positive["result"]
	assert positive_result["materiality"] == "high"
	assert positive_result["advisory_required"] is True
	assert positive_result["agents_md_changed"] is False
	assert "## AI Materiality Advisory" in positive["comment"]
	assert "`package.json`" in positive["comment"]
	assert "informational only" in positive["comment"]
	assert "AGENTS_MD_MATERIALITY: materiality=high advisory=true" in positive["stdout"]

	satisfied = _run_agents_md_materiality_harness(
		diff_text=PHASE_D_MATERIALITY_FIXTURE.read_text(encoding="utf-8"),
		changed_paths=["package.json", "agents.md"],
		workspace_files={"agents.md": "# repo agents\n"},
	)
	satisfied_result = satisfied["result"]
	assert satisfied_result["materiality"] == "high"
	assert satisfied_result["advisory_required"] is False
	assert satisfied_result["agents_md_changed"] is True
	assert satisfied_result["reason"] == "agents_md_changed"
	assert satisfied["comment"] == ""

	low = _run_agents_md_materiality_harness(
		diff_text=PHASE_B_RISK_TIER_TRIVIAL_FIXTURE.read_text(encoding="utf-8"),
		workspace_files={"agents.md": "# repo agents\n"},
	)
	low_result = low["result"]
	assert low_result["materiality"] == "low"
	assert low_result["advisory_required"] is False
	assert low_result["reason"] == "low_materiality"
	assert low["comment"] == ""

	gate_docs_client = _run_gate_agents_md_materiality_classifier(["docs/client.js"])
	assert gate_docs_client["materiality"] == "low"
	assert gate_docs_client["agents_md_changed"] is False

	gate_api_client = _run_gate_agents_md_materiality_classifier(["sdk/client.go"])
	assert gate_api_client["materiality"] == "medium"
	assert gate_api_client["agents_md_changed"] is False

	workflow = _workflow_text()
	stage_helper = _stage_helper_text()
	consolidate = _consolidate_text()
	preflight_block = _step_block('"Preflight: Verify required files before reviewer invocation"')
	reviewer_block = _step_block("Run reviewer models")
	advisory_block = _step_block("Post AI Materiality Advisory comment")
	gate_block = _step_block("Evaluate review gate")
	prompt_text = (REPO_ROOT / "prompts" / "review-consolidator.txt").read_text(encoding="utf-8")

	assert "AGENTS_MD_MATERIALITY_RESULT_FILE=${RUNTIME_DIR}/agents_md_materiality_result.json" in workflow
	assert "AGENTS_MD_MATERIALITY_COMMENT_FILE=${RUNTIME_DIR}/agents_md_materiality_comment.md" in workflow
	assert "REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED: ${{ vars.REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED || 'true' }}" in workflow
	assert 'if [ ! -f "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh" ]; then' in stage_helper
	assert 'src=".codex-workflow-src/scripts/review_agents_md_materiality.sh"' in stage_helper
	assert 'src=".codex-workflow-src-main/scripts/review_agents_md_materiality.sh"' in stage_helper
	assert 'install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh"' in stage_helper
	assert 'review_agents_md_materiality.sh not found in checked-out support sources' in stage_helper
	assert 'check_soft_file "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh"' in preflight_block
	assert 'REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED="${REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED:-true}"' in consolidate
	assert 'AGENTS_MD_MATERIALITY_RESULT_FILE="${AGENTS_MD_MATERIALITY_RESULT_FILE:-${RUNTIME_DIR}/agents_md_materiality_result.json}"' in consolidate
	assert 'if is_truthy "${REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED}" && [ -s "${AGENTS_MD_MATERIALITY_RESULT_FILE}" ]; then' in consolidate
	assert "'AGENTS MD MATERIALITY RESULT'" in consolidate
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh" &' in reviewer_block
	assert 'materiality_pid="$!"' in reviewer_block
	assert 'AGENTS_MD_MATERIALITY_ENABLED:-0' in reviewer_block
	assert "continue-on-error: true" in advisory_block
	assert "AGENTS_MD_MATERIALITY_RESULT_FILE" in advisory_block
	assert "PR_ISSUE_COMMENTS_FILE" in advisory_block
	assert 'issues/comments/${existing_comment_id}' in advisory_block
	assert 'issues/${PR_NUMBER}/comments' in advisory_block
	assert 'AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=agents_md_materiality' in gate_block
	assert 'AGENTS_MD_MATERIALITY_ENABLED:-0' in gate_block
	assert 'PR_FILES_JSON="${pr_files_json}" python3 - <<\'PY\'' in gate_block
	assert "=== BEGIN UNTRUSTED AGENTS MD MATERIALITY RESULT ===" in prompt_text
	assert "SEVERITY: high` by default" in prompt_text


def test_reviewer_failback_wiring_stages_asset_and_restores_cache_before_reviewers() -> None:
	workflow = _workflow_text()
	stage_helper = _stage_helper_text()
	preflight_block = _step_block('"Preflight: Verify required files before reviewer invocation"')
	restore_block = _step_block("Restore review-issue ledger")
	reviewers = _reviewers_text()

	assert 'failback_src=".codex-workflow-src/scripts/reviewer_failback_chains.json"' in stage_helper
	assert 'failback_src=".codex-workflow-src-main/scripts/reviewer_failback_chains.json"' in stage_helper
	assert 'install -m 0644 "${failback_src}" "${SUPPORT_SCRIPTS_DIR}/reviewer_failback_chains.json"' in stage_helper
	assert 'reviewer_failback_chains.json not found in checked-out support sources' in stage_helper
	assert 'check_soft_file "${SUPPORT_SCRIPTS_DIR}/reviewer_failback_chains.json"' in preflight_block
	assert 'REVIEWER_FAILBACK_CHAINS_FILE="${REVIEWER_FAILBACK_CHAINS_FILE:-${SUPPORT_SCRIPTS_DIR:-scripts}/reviewer_failback_chains.json}"' in reviewers
	assert '.ai/review_runtime/' in restore_block
	assert workflow.index('- name: Restore review-issue ledger') < workflow.index('- name: Run reviewer models')


def test_reviewer_failback_mapping_covers_live_reviewer_roster() -> None:
	# reviewer_failback_target_for_model() only checks whether a candidate slug is
	# declared in scripts/codex_model_catalog.json, so the contract test mirrors
	# that lookup rather than inspecting supported_in_api.
	reviewer_models = _workflow_reviewer_models()
	chains = _reviewer_failback_chains()
	catalog_slugs = _catalog_declared_model_slugs()
	mapped: list[str] = []
	unmapped: list[str] = []

	for model in reviewer_models:
		provider_prefix = f"{model.split('/', 1)[0]}/"
		catalog_alternates = sorted(
			slug for slug in catalog_slugs
			if slug.startswith(provider_prefix) and slug != model
		)
		configured_candidates = chains.get(model, [])
		for candidate in configured_candidates:
			assert candidate.startswith(provider_prefix)
			assert candidate != model
			assert candidate in catalog_slugs
		if catalog_alternates:
			mapped.append(model)
			assert configured_candidates, (
				f"live reviewer {model} has catalog-declared same-provider fallback(s) "
				f"{catalog_alternates} but reviewer_failback_chains.json leaves it unmapped"
			)
		else:
			unmapped.append(model)
			assert model not in chains, (
				f"live reviewer {model} should stay unmapped until the catalog ships "
				"a same-provider fallback slug"
			)

	assert sorted(mapped) == [
		"deepseek/deepseek-v4-pro",
		"qwen/qwen3.6-plus",
		"x-ai/grok-4.20",
	]
	assert sorted(unmapped) == [
		"minimax/minimax-m2.5",
		"mistralai/mistral-small-2603",
		"moonshotai/kimi-k2.5",
	]
	assert chains["deepseek/deepseek-v4-pro"] == ["deepseek/deepseek-v3.2"]
	assert chains["qwen/qwen3.6-plus"] == ["qwen/qwen3-coder-plus"]
	assert chains["x-ai/grok-4.20"] == ["x-ai/grok-4.1-fast"]


def test_reviewer_failback_harness_reuses_cached_open_state_and_skips_unmapped_models() -> None:
	result = _run_reviewer_failback_harness()
	health_state = result["health_state"]
	assert isinstance(health_state, dict)
	assert health_state["version"] == 1

	mapped_entry = health_state["reviewers"]["x-ai/grok-4.20"]
	assert mapped_entry["state"] == "open"
	assert mapped_entry["effective_model"] == "x-ai/grok-4.1-fast"
	assert mapped_entry["last_failure_kind"] == "rate_limit"
	assert mapped_entry["open_until_epoch"] > 0

	unmapped_entry = health_state["reviewers"]["moonshotai/kimi-k2.5"]
	assert unmapped_entry["state"] == "open"
	assert unmapped_entry["effective_model"] == ""
	assert unmapped_entry["last_failure_kind"] == "server_error"

	assert result["MAPPED_STATUS_FILE_CONTENT"].strip() == "success"
	assert "fallback success for x-ai/grok-4.20" in result["MAPPED_OUTPUT_FILE_CONTENT"]
	assert "REVIEWER_FAILBACK: x-ai/grok-4.20 -> x-ai/grok-4.1-fast reason=rate_limit" in result["MAPPED_LOG_FILE_CONTENT"]
	assert "REVIEWER_HEALTH: x-ai/grok-4.20 open reason=rate_limit failures=1 effective_model=x-ai/grok-4.1-fast" in result["MAPPED_LOG_FILE_CONTENT"]

	assert result["CACHED_SUCCESSES"] == "0"
	assert result["CACHED_STATUS_FILE_CONTENT"].strip() == "skipped_open"
	assert "cached reviewer health state is open" in result["CACHED_OUTPUT_FILE_CONTENT"]
	assert "cached reviewer health state is open" in result["CACHED_LOG_FILE_CONTENT"]
	assert "cached_effective_model=x-ai/grok-4.1-fast" in result["CACHED_LOG_FILE_CONTENT"]

	assert result["UNMAPPED_STATUS_FILE_CONTENT"].strip() == "skipped_unmapped"
	assert "no same-family failback mapping is available" in result["UNMAPPED_OUTPUT_FILE_CONTENT"]
	assert "REVIEWER_FAILBACK_UNMAPPED: moonshotai/kimi-k2.5" in result["UNMAPPED_LOG_FILE_CONTENT"]

	attempt_lines = result["ATTEMPT_LOG_FILE_CONTENT"].splitlines()
	assert attempt_lines == [
		"x-ai/grok-4.20\tx-ai/grok-4.20\txhigh\tattempt 1",
		"x-ai/grok-4.20\tx-ai/grok-4.20\thigh\tattempt 2 (cheaper reasoning high)",
		"x-ai/grok-4.20\tx-ai/grok-4.1-fast\txhigh\tattempt 3 (failback x-ai/grok-4.1-fast)",
		"moonshotai/kimi-k2.5\tmoonshotai/kimi-k2.5\txhigh\tattempt 1",
		"moonshotai/kimi-k2.5\tmoonshotai/kimi-k2.5\thigh\tattempt 2 (cheaper reasoning high)",
	]


def test_slot_retry_budget_stops_rate_limited_slot_after_bounded_attempts() -> None:
	result = _run_reviewer_slot_retry_budget_harness()
	assert result["MAPPED_STATUS_FILE_CONTENT"].strip() == "failed"
	assert "slot retryable-failure limit (3)" in result["MAPPED_OUTPUT_FILE_CONTENT"]
	assert result["ATTEMPT_LOG_FILE_CONTENT"].splitlines() == [
		"x-ai/grok-4.20\tx-ai/grok-4.20\txhigh\tattempt 1",
		"x-ai/grok-4.20\tx-ai/grok-4.20\thigh\tattempt 2 (cheaper reasoning high)",
		"x-ai/grok-4.20\tx-ai/grok-4.1-fast\txhigh\tattempt 3 (failback x-ai/grok-4.1-fast)",
	]
	assert result["MAPPED_LOG_FILE_CONTENT"].count("REVIEWER_BACKOFF: ") == 2
	assert "attempt 4" not in result["MAPPED_LOG_FILE_CONTENT"]
	assert (
		"REVIEWER_ADVANCE: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 reason=rate_limit "
		"next_action=retry_cheaper_reasoning next_attempt=2 next_model=x-ai/grok-4.20"
	) in result["MAPPED_LOG_FILE_CONTENT"]
	assert (
		"REVIEWER_ADVANCE: slot=x-ai/grok-4.20 model=x-ai/grok-4.1-fast reason=rate_limit "
		"next_action=failback next_attempt=3 next_model=x-ai/grok-4.1-fast"
	) in result["MAPPED_LOG_FILE_CONTENT"]
	assert (
		"REVIEWER_SLOT_STATE: slot=x-ai/grok-4.20 retryable_failure_count=3 "
		"retryable_failure_classes=rate_limit backoff_sleep_secs_total=0 "
		"slot_retry_budget_exhausted=false fallback_model_used=true cache_status=supported "
		"cache_reuse_attempted=true"
	) in result["MAPPED_LOG_FILE_CONTENT"]
	assert (
		"REVIEWER_CACHE: slot=x-ai/grok-4.20 model=x-ai/grok-4.1-fast attempt=3 "
		"status=supported prompt_reused=true"
	) in result["MAPPED_LOG_FILE_CONTENT"]


def test_stall_guard_retryable_failures_log_deterministic_reviewer_advance() -> None:
	result = _run_reviewer_stall_recovery_harness()
	health_state = result["health_state"]
	assert result["MAPPED_STATUS_FILE_CONTENT"].strip() == "success"
	assert "fallback success for x-ai/grok-4.20" in result["MAPPED_OUTPUT_FILE_CONTENT"]
	assert (
		"REVIEWER_ADVANCE: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 reason=stall_guard "
		"next_action=retry_cheaper_reasoning next_attempt=2 next_model=x-ai/grok-4.20"
	) in result["MAPPED_LOG_FILE_CONTENT"]
	assert (
		"REVIEWER_ADVANCE: slot=x-ai/grok-4.20 model=x-ai/grok-4.1-fast reason=stall_guard "
		"next_action=failback next_attempt=3 next_model=x-ai/grok-4.1-fast"
	) in result["MAPPED_LOG_FILE_CONTENT"]
	assert health_state["reviewers"]["x-ai/grok-4.20"]["last_failure_kind"] == "stall_guard"
	assert result["ATTEMPT_LOG_FILE_CONTENT"].splitlines() == [
		"x-ai/grok-4.20\tx-ai/grok-4.20\txhigh\tattempt 1",
		"x-ai/grok-4.20\tx-ai/grok-4.20\thigh\tattempt 2 (cheaper reasoning high)",
		"x-ai/grok-4.20\tx-ai/grok-4.1-fast\txhigh\tattempt 3 (failback x-ai/grok-4.1-fast)",
	]


def test_silent_retry_exhaustion_logs_terminal_failure_reason() -> None:
	result = _run_reviewer_silent_retry_harness()
	assert result["MAPPED_STATUS_FILE_CONTENT"].strip() == "failed"
	assert "REVIEWER_ADVANCE: slot=x-ai/grok-4.20 model=x-ai/grok-4.20 reason=silent_retry next_action=terminal_failure" in result["MAPPED_LOG_FILE_CONTENT"]
	assert "reason=unknown" not in result["MAPPED_LOG_FILE_CONTENT"]
	assert result["ATTEMPT_LOG_FILE_CONTENT"].splitlines() == ["attempt 1", "attempt 2", "attempt 3"]


def test_reviewer_soft_deadline_fallback_requests_partial_finalize_and_exits_green() -> None:
	result = _run_reviewer_partial_finalize_budget_harness()
	assert result["PASS_SUCCESSFUL"] == "0"
	assert result["reviewer_called"] is False
	assert "remaining run budget fallback is below 300s (0s remain)" in result["stderr"]
	assert "AUTOFIX_PARTIAL_FINALIZE_REQUESTED=true" in result["request_file_content"]
	assert "AUTOFIX_PARTIAL_FINALIZE_REASON=soft_deadline" in result["request_file_content"]
	assert "AUTOFIX_PARTIAL_FINALIZE_PHASE=reviewers" in result["request_file_content"]
	assert "AUTOFIX_PARTIAL_FINALIZE_REQUESTED=true" in result["github_env_content"]
	assert "AUTOFIX_PARTIAL_FINALIZE_REASON=soft_deadline" in result["github_env_content"]
	assert "AUTOFIX_PARTIAL_FINALIZE_PHASE=reviewers" in result["github_env_content"]


def test_reviewer_health_dispatch_logs_to_stderr_only() -> None:
	result = _run_reviewer_health_dispatch_logging_harness()
	assert result["stdout"] == ""
	assert "REVIEWER_HEALTH: x-ai/grok-4.20 healthy reason=open_ttl_expired failures=0 effective_model=x-ai/grok-4.1-fast" in result["stderr"]


def test_reviewer_zero_success_guard_fails_open_when_every_review_slot_was_skipped() -> None:
	result = _run_reviewer_zero_success_guard_harness(statuses=["skipped_open", "skipped_unmapped"])
	assert result["returncode"] == 0
	assert "REVIEWERS_SUCCESSFUL=0\n" == result["github_env"]
	assert "all review slots were skipped fail-open" in result["stdout"]


def test_reviewer_filter_harness_strips_low_signal_paths_and_preserves_exemptions() -> None:
	result = _run_reviewer_filter_harness(filter_enabled="true", helper_mode="repo")
	pr_diff = result["PR_DIFF_FILE_CONTENT"]
	pr_changed = result["PR_CHANGED_FILES_FILE_CONTENT"]
	last_run_changed = result["LAST_RUN_CHANGED_FILES_FILE_CONTENT"]
	last_run_diff_stat = result["LAST_RUN_DIFF_STAT_FILE_CONTENT"]
	last_commit_stat = result["LAST_COMMIT_STAT_FILE_CONTENT"]
	symbol_summary = result["SYMBOL_DIFF_SUMMARY_FILE_CONTENT"]

	assert result["REVIEWER_FILTER_ACTIVE"] == "true"
	assert result["PR_DIFF_FILE"].endswith("reviewer_filtered_pr_diff.patch")
	for skipped_path in ("package-lock.json", "src/generated/client.ts", "public/app.min.js"):
		assert skipped_path not in pr_diff
		assert skipped_path not in pr_changed
		assert skipped_path not in last_run_changed
		assert skipped_path not in last_run_diff_stat
		assert skipped_path not in last_commit_stat
		assert skipped_path not in symbol_summary
	for kept_path in (
		"db/migrations/20260529000000_generated.sql",
		"scripts/migrate/seed.sh",
		"db/contracts/widgets.yml",
	):
		assert kept_path in pr_diff
		assert kept_path in pr_changed
		assert kept_path in last_run_changed
		assert kept_path in symbol_summary
	assert "REVIEWER_FILTER_SKIP: package-lock.json path-glob:package-lock.json" in result["stdout"]
	assert "REVIEWER_FILTER_SKIP: src/generated/client.ts generated-marker:@generated" in result["stdout"]
	assert "REVIEWER_FILTER_SKIP: public/app.min.js path-glob:*.min.js" in result["stdout"]


def test_reviewer_filter_script_preserves_nested_exempt_paths() -> None:
	diff_text = "\n".join([
		"diff --git a/db/contracts/nested/widgets.yml b/db/contracts/nested/widgets.yml",
		"index 1111111..2222222 100644",
		"--- a/db/contracts/nested/widgets.yml",
		"+++ b/db/contracts/nested/widgets.yml",
		"@@ -1,2 +1,2 @@",
		"-collection: widget_versions",
		"+collection: widgets",
		"diff --git a/db/migrations/nested/20260529000000_generated.sql b/db/migrations/nested/20260529000000_generated.sql",
		"index 3333333..4444444 100644",
		"--- a/db/migrations/nested/20260529000000_generated.sql",
		"+++ b/db/migrations/nested/20260529000000_generated.sql",
		"@@ -1,2 +1,2 @@",
		"-CREATE TABLE widgets (id INTEGER);",
		"+CREATE TABLE widgets (id INT);",
		"diff --git a/scripts/migrate/nested/seed.sh b/scripts/migrate/nested/seed.sh",
		"index 5555555..6666666 100644",
		"--- a/scripts/migrate/nested/seed.sh",
		"+++ b/scripts/migrate/nested/seed.sh",
		"@@ -1,2 +1,2 @@",
		"-echo old-seed",
		"+echo seed",
		"diff --git a/src/generated/deep/client.ts b/src/generated/deep/client.ts",
		"index 7777777..8888888 100644",
		"--- a/src/generated/deep/client.ts",
		"+++ b/src/generated/deep/client.ts",
		"@@ -1,2 +1,2 @@",
		"-export const endpoint = '/v1';",
		"+export const endpoint = '/v2';",
	]) + "\n"
	result = _run_uninteresting_filter_script(
		diff_text=diff_text,
		workspace_files={
			"db/contracts/nested/widgets.yml": "# GENERATED BY contract-tool\ncollection: widgets\n",
			"db/migrations/nested/20260529000000_generated.sql": "-- GENERATED FILE\nCREATE TABLE widgets (id INT);\n",
			"scripts/migrate/nested/seed.sh": "# GENERATED BY seed-tool\necho seed\n",
			"src/generated/deep/client.ts": "@generated export const endpoint = '/v2';\n",
		},
	)

	assert "db/contracts/nested/widgets.yml" in result["output_diff"]
	assert "db/migrations/nested/20260529000000_generated.sql" in result["output_diff"]
	assert "scripts/migrate/nested/seed.sh" in result["output_diff"]
	assert "src/generated/deep/client.ts" not in result["output_diff"]
	assert "db/contracts/nested/widgets.yml\n" in result["kept_paths"]
	assert "db/migrations/nested/20260529000000_generated.sql\n" in result["kept_paths"]
	assert "scripts/migrate/nested/seed.sh\n" in result["kept_paths"]
	assert "src/generated/deep/client.ts\tgenerated-marker:@generated\n" in result["skipped_paths"]


def test_reviewer_filter_script_preserves_root_level_migration_exempt_paths() -> None:
	diff_text = "\n".join([
		"diff --git a/migrations/20260529000000_generated.sql b/migrations/20260529000000_generated.sql",
		"index 1111111..2222222 100644",
		"--- a/migrations/20260529000000_generated.sql",
		"+++ b/migrations/20260529000000_generated.sql",
		"@@ -1,2 +1,2 @@",
		"--- GENERATED FILE",
		"-CREATE TABLE widgets (id INTEGER);",
		"+CREATE TABLE widgets (id INT);",
		"diff --git a/migrate/seed.sh b/migrate/seed.sh",
		"index 3333333..4444444 100644",
		"--- a/migrate/seed.sh",
		"+++ b/migrate/seed.sh",
		"@@ -1,2 +1,2 @@",
		"-# GENERATED BY seed-tool",
		"+# GENERATED BY seed-tool",
		"-echo old-seed",
		"+echo seed",
		"diff --git a/src/generated/client.ts b/src/generated/client.ts",
		"index 5555555..6666666 100644",
		"--- a/src/generated/client.ts",
		"+++ b/src/generated/client.ts",
		"@@ -1,2 +1,2 @@",
		"-@generated export const endpoint = '/v1';",
		"+@generated export const endpoint = '/v2';",
	]) + "\n"
	result = _run_uninteresting_filter_script(
		diff_text=diff_text,
		workspace_files={
			"migrations/20260529000000_generated.sql": "-- GENERATED FILE\nCREATE TABLE widgets (id INT);\n",
			"migrate/seed.sh": "# GENERATED BY seed-tool\necho seed\n",
			"src/generated/client.ts": "@generated export const endpoint = '/v2';\n",
		},
	)

	assert "migrations/20260529000000_generated.sql" in result["output_diff"]
	assert "migrate/seed.sh" in result["output_diff"]
	assert "src/generated/client.ts" not in result["output_diff"]
	assert "migrations/20260529000000_generated.sql\n" in result["kept_paths"]
	assert "migrate/seed.sh\n" in result["kept_paths"]
	assert "src/generated/client.ts\tgenerated-marker:@generated\n" in result["skipped_paths"]


def test_reviewer_filter_script_strips_deleted_generated_file_when_workspace_copy_is_missing() -> None:
	diff_text = "\n".join([
		"diff --git a/src/generated/deleted_client.ts b/src/generated/deleted_client.ts",
		"deleted file mode 100644",
		"index 1111111..0000000",
		"--- a/src/generated/deleted_client.ts",
		"+++ /dev/null",
		"@@ -1,2 +0,0 @@",
		"-@generated",
		"-export const endpoint = '/v1';",
	]) + "\n"
	result = _run_uninteresting_filter_script(diff_text=diff_text, workspace_files={})

	assert result["output_diff"] == ""
	assert result["kept_paths"] == ""
	assert result["skipped_paths"] == "src/generated/deleted_client.ts\tgenerated-marker:@generated\n"


def test_reviewer_filter_script_keeps_deleted_file_when_first_hunk_starts_later() -> None:
	diff_text = "\n".join([
		"diff --git a/src/generated/deleted_client.ts b/src/generated/deleted_client.ts",
		"deleted file mode 100644",
		"index 1111111..0000000",
		"--- a/src/generated/deleted_client.ts",
		"+++ /dev/null",
		"@@ -10,2 +0,0 @@",
		"-@generated",
		"-export const endpoint = '/v1';",
	]) + "\n"
	result = _run_uninteresting_filter_script(diff_text=diff_text, workspace_files={})

	assert result["output_diff"] == diff_text
	assert result["kept_paths"] == "src/generated/deleted_client.ts\n"
	assert result["skipped_paths"] == ""


def test_reviewer_filter_script_ignores_later_hunk_marker_when_first_hunk_has_no_marker() -> None:
	diff_text = "\n".join([
		"diff --git a/src/manual/client.ts b/src/manual/client.ts",
		"deleted file mode 100644",
		"index 1111111..0000000",
		"--- a/src/manual/client.ts",
		"+++ /dev/null",
		"@@ -1,2 +0,0 @@",
		"-const endpoint = '/v1';",
		"-const timeoutMs = 5000;",
		"@@ -50,2 +0,0 @@",
		"-@generated",
		"-console.log('later marker');",
	]) + "\n"
	result = _run_uninteresting_filter_script(diff_text=diff_text, workspace_files={})

	assert result["output_diff"] == diff_text
	assert result["kept_paths"] == "src/manual/client.ts\n"
	assert result["skipped_paths"] == ""


def test_reviewer_filter_script_keeps_existing_file_with_marker_on_line_six() -> None:
	diff_text = "\n".join([
		"diff --git a/src/manual/client.ts b/src/manual/client.ts",
		"index 1111111..2222222 100644",
		"--- a/src/manual/client.ts",
		"+++ b/src/manual/client.ts",
		"@@ -1,6 +1,6 @@",
		" line01",
		" line02",
		" line03",
		" line04",
		"-line05",
		"+line05 updated",
		" @generated",
	]) + "\n"
	result = _run_uninteresting_filter_script(
		diff_text=diff_text,
		workspace_files={
			"src/manual/client.ts": "\n".join([
				"line01",
				"line02",
				"line03",
				"line04",
				"line05 updated",
				"@generated",
			]) + "\n",
		},
	)

	assert result["output_diff"] == diff_text
	assert result["kept_paths"] == "src/manual/client.ts\n"
	assert result["skipped_paths"] == ""


def test_reviewer_filter_script_keeps_deleted_file_with_marker_on_line_six() -> None:
	diff_text = "\n".join([
		"diff --git a/src/manual/deleted_client.ts b/src/manual/deleted_client.ts",
		"deleted file mode 100644",
		"index 1111111..0000000",
		"--- a/src/manual/deleted_client.ts",
		"+++ /dev/null",
		"@@ -1,6 +0,0 @@",
		"-line01",
		"-line02",
		"-line03",
		"-line04",
		"-line05",
		"-@generated",
	]) + "\n"
	result = _run_uninteresting_filter_script(diff_text=diff_text, workspace_files={})

	assert result["output_diff"] == diff_text
	assert result["kept_paths"] == "src/manual/deleted_client.ts\n"
	assert result["skipped_paths"] == ""


def test_reviewer_filter_script_ignores_existing_file_marker_beyond_header_lines() -> None:
	diff_text = "\n".join([
		"diff --git a/src/manual/client.ts b/src/manual/client.ts",
		"index 1111111..2222222 100644",
		"--- a/src/manual/client.ts",
		"+++ b/src/manual/client.ts",
		"@@ -1,20 +1,20 @@",
		" line01",
		" line02",
		" line03",
		" line04",
		" line05",
		" line06",
		" line07",
		" line08",
		" line09",
		" line10",
		" line11",
		" line12",
		" line13",
		" line14",
		" line15",
		" line16",
		" line17",
		" line18",
		"-line19",
		"+line19 updated",
		" @generated",
	]) + "\n"
	result = _run_uninteresting_filter_script(
		diff_text=diff_text,
		workspace_files={
			"src/manual/client.ts": "\n".join([
				"line01",
				"line02",
				"line03",
				"line04",
				"line05",
				"line06",
				"line07",
				"line08",
				"line09",
				"line10",
				"line11",
				"line12",
				"line13",
				"line14",
				"line15",
				"line16",
				"line17",
				"line18",
				"line19 updated",
				"@generated",
			]) + "\n",
		},
	)

	assert result["output_diff"] == diff_text
	assert result["kept_paths"] == "src/manual/client.ts\n"
	assert result["skipped_paths"] == ""


def test_reviewer_filter_script_ignores_deleted_file_marker_beyond_header_lines() -> None:
	diff_text = "\n".join([
		"diff --git a/src/manual/deleted_client.ts b/src/manual/deleted_client.ts",
		"deleted file mode 100644",
		"index 1111111..0000000",
		"--- a/src/manual/deleted_client.ts",
		"+++ /dev/null",
		"@@ -1,20 +0,0 @@",
		"-line01",
		"-line02",
		"-line03",
		"-line04",
		"-line05",
		"-line06",
		"-line07",
		"-line08",
		"-line09",
		"-line10",
		"-line11",
		"-line12",
		"-line13",
		"-line14",
		"-line15",
		"-line16",
		"-line17",
		"-line18",
		"-line19",
		"-@generated",
	]) + "\n"
	result = _run_uninteresting_filter_script(diff_text=diff_text, workspace_files={})

	assert result["output_diff"] == diff_text
	assert result["kept_paths"] == "src/manual/deleted_client.ts\n"
	assert result["skipped_paths"] == ""


def test_reviewer_filter_harness_fails_open_when_disabled_missing_or_failing() -> None:
	disabled = _run_reviewer_filter_harness(filter_enabled="false", helper_mode="repo")
	assert disabled["REVIEWER_FILTER_ACTIVE"] == "false"
	assert disabled["PR_DIFF_FILE"].endswith("pr_diff.patch")
	assert disabled["PR_DIFF_FILE_CONTENT"] == PHASE_C_FILTER_FIXTURE.read_text(encoding="utf-8")
	assert disabled["SYMBOL_DIFF_SUMMARY_FILE_CONTENT"] == "RAW SYMBOL SUMMARY\npackage-lock.json\nsrc/generated/client.ts\n"
	assert "REVIEWER_FILTER_SKIP:" not in disabled["stdout"]

	missing = _run_reviewer_filter_harness(filter_enabled="true", helper_mode="missing")
	assert missing["REVIEWER_FILTER_ACTIVE"] == "false"
	assert missing["PR_DIFF_FILE"].endswith("pr_diff.patch")
	assert "review_filter_uninteresting_files.sh unavailable" in missing["stdout"]
	assert missing["SYMBOL_DIFF_SUMMARY_FILE_CONTENT"] == "RAW SYMBOL SUMMARY\npackage-lock.json\nsrc/generated/client.ts\n"

	failing = _run_reviewer_filter_harness(filter_enabled="true", helper_mode="failing")
	assert failing["REVIEWER_FILTER_ACTIVE"] == "false"
	assert failing["PR_DIFF_FILE"].endswith("pr_diff.patch")
	assert "review_filter_uninteresting_files.sh failed for" in failing["stdout"]
	assert failing["SYMBOL_DIFF_SUMMARY_FILE_CONTENT"] == "RAW SYMBOL SUMMARY\npackage-lock.json\nsrc/generated/client.ts\n"


def test_reviewer_filter_stat_harness_handles_brace_expansion_renames() -> None:
	output = _run_reviewer_stat_filter_harness(
		diff_stat_text="\n".join([
			" src/{generated => api}/client.ts | 2 +-",
			" lib/{ => util}/helpers.py | 2 +-",
			" db/contracts/{legacy => nested}/widgets.yml | 2 +-",
			" 3 files changed, 3 insertions(+), 3 deletions(-)",
		]) + "\n",
		skipped_rows_text="\n".join([
			"src/api/client.ts\tgenerated-marker:@generated",
			"lib/util/helpers.py\tgenerated-marker:@generated",
		]) + "\n",
	)

	assert "src/{generated => api}/client.ts" not in output
	assert "lib/{ => util}/helpers.py" not in output
	assert "db/contracts/{legacy => nested}/widgets.yml" in output
	assert "3 files changed, 3 insertions(+), 3 deletions(-)" not in output


def test_reject_verifier_bootstrap_and_stage_order_contract() -> None:
	stage_helper = _stage_helper_text()
	apply_fixes = _apply_fixes_text()
	assert "review_apply_fixes.sh review_reject_verify.sh review_rb_judge.sh" in stage_helper
	parse_idx = apply_fixes.index('if parse_script="$(resolve_support_script review_parse_consolidator.sh)"; then')
	verify_idx = apply_fixes.index('if verify_script="$(resolve_support_script review_reject_verify.sh)"; then')
	ledger_idx = apply_fixes.index('if ledger_script="$(resolve_support_script review_issue_ledger.sh)"; then')
	assert parse_idx < verify_idx < ledger_idx
	assert 'CONSOLIDATOR_REJECT_SCHEMA_ENABLED="${CONSOLIDATOR_REJECT_SCHEMA_ENABLED:-false}"' in apply_fixes


def test_render_prompt_py_is_main_primary_so_validator_fixes_reach_wedged_branches() -> None:
	# Regression guard: render_prompt.py validates arbitrary embedded PR-diff
	# text before every reviewer/editor call. When it was branch-primary
	# (staged from SCRIPT_REF), a false-positive fix landed on main (#3593 —
	# lone `${{` in the diff tripping the unmatched-delimiter check) never
	# reached an in-flight PR whose branch predated the fix, wedging that PR's
	# review indefinitely (PR #3592, run 28888093412). It must be main-primary
	# so a render_prompt fix on main immediately protects wedged branches.
	stage_helper = _stage_helper_text()
	main_primary_line = next(
		(line for line in stage_helper.splitlines() if "MAIN_PRIMARY_BOOTSTRAP_SCRIPTS=" in line),
		"",
	)
	optional_line = next(
		(line for line in stage_helper.splitlines() if "OPTIONAL_BOOTSTRAP_SCRIPTS=" in line),
		"",
	)
	required_line = next(
		(line for line in stage_helper.splitlines() if "REQUIRED_BOOTSTRAP_SCRIPTS=" in line),
		"",
	)
	assert "render_prompt.py" in main_primary_line, (
		"render_prompt.py must be in MAIN_PRIMARY_BOOTSTRAP_SCRIPTS so main-side "
		"validator fixes reach wedged/in-flight PR branches"
	)
	assert "render_prompt.py" not in optional_line, (
		"render_prompt.py must not be branch-primary in OPTIONAL_BOOTSTRAP_SCRIPTS"
	)
	# Regex word-boundary check so render_prompt.sh membership does not mask a
	# stray render_prompt.py entry in the required (branch-primary) list.
	assert re.search(r"\brender_prompt\.py\b", required_line) is None, (
		"render_prompt.py must not be branch-primary in REQUIRED_BOOTSTRAP_SCRIPTS"
	)


def test_support_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	stage_helper = _stage_helper_text()
	assert "validation_history.v1.json" in stage_helper
	assert "operator_bypass_audit.v1.json" in stage_helper
	assert "revalidate_events.v1.json" in stage_helper


def test_editor_changes_lost_redispatch_matches_post_commit_fallback_chain() -> None:
	post_commit_block = _step_block("Re-trigger review via workflow_dispatch")
	changes_lost_block = _step_block("Re-dispatch review on editor-changes-lost")

	for block in (post_commit_block, changes_lost_block):
		assert 'if gh workflow run "review_autofix.yml" \\' in block
		assert '-f pr_number="${PR_NUMBER}" \\' in block
		assert '-f allow_workflow_edits="${ALLOW_WORKFLOW_EDITS}"; then' in block
		assert 'caller_workflow="internal-review.yml"' in block

	assert _dispatch_fallback_chain_slice("Re-trigger review via workflow_dispatch") == _dispatch_fallback_chain_slice(
		"Re-dispatch review on editor-changes-lost"
	)
	assert (
		'echo "CHANGES_LOST_REDISPATCHED=${dispatched}" >> "$GITHUB_ENV"' in changes_lost_block
	)
	assert (
		'Could not dispatch review workflow for editor-changes-lost recovery. The next synchronize event or manual re-run is needed.'
		in changes_lost_block
	)


def test_review_pipeline_summary_step_is_local_only_and_grep_friendly() -> None:
	block = _step_block("Append review pipeline iteration summary")
	assert "### Review Pipeline — Iteration ${iteration_label}" in block
	assert "REVIEW_AUTOFIX_RUN_SUMMARY_V1" in block
	assert "printf '\\n### Review Autofix Run Summary\\n\\n' >> \"${GITHUB_STEP_SUMMARY}\"" in block
	assert "printf '%s\\n' \"${structured_summary_line}\" | tee -a \"${GITHUB_STEP_SUMMARY}\"" in block
	for expected in (
		'"completed_phases":',
		'"skipped_phases":',
		'"slot_results":',
		'"attempt_count":',
		'"status":',
		'"failure_class":',
		'"stall_kill_count":',
		'"stall_recovery_next_action":',
		'"stall_recovered":',
		'"retryable_failure_count":',
		'"retryable_failure_classes":',
		'"backoff_sleep_secs_total":',
		'"slot_retry_budget_exhausted":',
		'"fallback_model_used":',
		'"cache_status":',
		'"cache_reuse_attempted":',
		'"cache_read_input_tokens_total":',
		'"budget_elapsed_secs":',
		'"budget_total_secs":',
		'"budget_remaining_secs":',
		'"stall_recovery":',
		'"finalize_reason":',
		'"partial_finalize":',
		'"partial_finalize_reason":',
		'"partial_finalize_phase":',
		'"partial_finalize_validation_tail_can_complete":',
		'"partial_finalize_edits_withheld_for_safety":',
		'"partial_finalize_withheld_reason":',
		'"partial_comment_posted":',
		'"incomplete_phases":',
		'"edits_pushed":',
		'"resume_round":',
		'"resume_restored":',
		'"resume_head_sha":',
		'"resume_round_limit":',
		'"resume_state":',
		'"resume_should_continue":',
		'"resume_source_reason":',
		'"resume_source_phase":',
		'"resume_comment_posted":',
		'"resume_completed_scope":',
		'"resume_incomplete_scope":',
	):
		assert expected in block, f"Missing structured summary key contract: {expected}"
	assert "reviewer_scope_label=\"full-diff\"" in block, (
		"Summary step must not overclaim scoped reviewer behaviour before "
		"review_run_reviewers.sh consumes REVIEW_REVIEWER_ITERATION_SCOPING"
	)
	for expected in (
		"| Reviewers run | ${reviewers_run} |",
		"| Reviewer scope | ${reviewer_scope_label} |",
		"| Raw bundle size (bytes) | ${bundle_bytes} |",
		"| Floor tags | ${floor_tag_count} |",
		"| Consolidator model | ${REVIEW_CONSOLIDATOR_MODEL:-openai/gpt-5.5} |",
		"| Consolidator invoked | ${consolidator_invoked} |",
		"| Consolidator output bytes | ${consolidator_output_bytes} |",
		"| Parsed issue blocks | ${parsed_blocks} |",
		"| Passthrough blocks | ${passthrough_blocks} |",
		"| Line-unverified blocks | ${line_unverified} |",
		"| Ledger entries total | ${ledger_total} |",
		"| NEW | ${ledger_new} |",
		"| PERSISTING | ${ledger_persisting} |",
		"| FIXED | ${ledger_fixed} |",
		"| RESURGENT | ${ledger_resurgent} |",
		"| accepted-residual | ${ledger_accepted_residual} |",
		"| Editor invoked | ${editor_invoked} |",
		"| CONSOLIDATOR_OVERRIDDEN count | ${override_count} |",
		"| Editor commit produced | ${editor_commit_produced} |",
		"| Partial validation tail can complete | ${partial_finalize_validation_tail_can_complete} |",
		"| Partial edits withheld for safety | ${partial_finalize_edits_withheld_for_safety} |",
		"| Partial withheld reason | ${partial_finalize_withheld_reason} |",
		"| Resume round | ${resume_round} |",
		"| Resume restored | ${resume_restored} |",
		"| Resume state | ${resume_state} |",
		"| Resume round limit | ${resume_round_limit} |",
		"| Resume should continue | ${resume_should_continue} |",
		"| Resume head SHA | ${resume_head_sha:-none} |",
	):
		assert expected in block, f"Missing summary row contract: {expected}"
	for artifact in (
		"${RUNTIME_DIR}/reviewer_bundle.txt",
		"${RUNTIME_DIR}/floor_tags.txt",
		"${RUNTIME_DIR}/consolidator_raw.txt",
		"${RUNTIME_DIR}/parser_stats.txt",
		"${RUNTIME_DIR}/ledger_status.txt",
		'COMMITTED_FILES_FILE="${committed_files_file}"',
		"grep -c 'CONSOLIDATOR_OVERRIDDEN:' \"${EDITOR_SUMMARY_FILE}\"",
		"EDITOR_COMMIT_PRODUCED: ${{ steps.commit_changes.outputs.did_commit }}",
		"MAX_ITERATIONS_REACHED: ${{ steps.retrigger_guard.outputs.max_iterations_reached }}",
		"SKIP_JUDGE: ${{ steps.retrigger_guard.outputs.skip_judge }}",
		"RB_JUDGE_STATUS: ${{ steps.rb_judge.outputs.rb_judge_status }}",
		"JUDGE_HANDLED: ${{ steps.rb_judge.outputs.judge_handled }}",
		"JUDGE_ACTION: ${{ steps.rb_judge.outputs.judge_action }}",
		"JUDGE_SKIP_REASON: ${{ steps.rb_judge.outputs.judge_skip_reason }}",
		'budget_total_secs="$(sanitize_nonnegative_int "${CODEX_RUN_BUDGET_TOTAL_SECS:-0}")"',
		'budget_start_epoch="$(sanitize_nonnegative_int "${CODEX_RUN_BUDGET_START_EPOCH:-${JOB_START_EPOCH:-0}}")"',
		'resume_round="${AUTOFIX_RESUME_ROUND:-${RESUME_ROUND:-0}}"',
		'resume_round_limit="$(sanitize_nonnegative_int "${AUTOFIX_RESUME_ROUND_LIMIT:-0}")"',
		'resume_restored="${AUTOFIX_RESUME_RESTORED:-false}"',
		'resume_head_sha="${AUTOFIX_RESUME_HEAD_SHA:-}"',
		'resume_state="${AUTOFIX_RESUME_STATE:-fresh}"',
		'resume_should_continue="${AUTOFIX_RESUME_SHOULD_CONTINUE:-false}"',
		'partial_finalize_validation_tail_can_complete="${AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE:-false}"',
		'partial_finalize_edits_withheld_for_safety="${AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY:-false}"',
		'partial_finalize_withheld_reason="${AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON:-none}"',
		'push_allowed = bool_env("CAN_PUSH")',
		'edits_pushed = bool_env("AUTOFIX_EDITS_PUSHED")',
		'partial_finalize = bool_env("AUTOFIX_PARTIAL_FINALIZE_REQUESTED")',
		'partial_finalize_validation_tail_can_complete = bool_env("AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE")',
		'partial_finalize_edits_withheld_for_safety = bool_env("AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY")',
		'partial_finalize_withheld_reason = env("AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON").strip() or "none"',
		'partial_comment_posted = bool_env("AUTOFIX_PARTIAL_COMMENT_POSTED")',
		'resume_restored = bool_env("AUTOFIX_RESUME_RESTORED")',
		'resume_head_sha = env("AUTOFIX_RESUME_HEAD_SHA").strip()',
		'resume_round_limit = int_env("AUTOFIX_RESUME_ROUND_LIMIT")',
		'resume_state = env("AUTOFIX_RESUME_STATE", "fresh").strip() or "fresh"',
		'resume_should_continue = bool_env("AUTOFIX_RESUME_SHOULD_CONTINUE")',
		'resume_source_reason = env("AUTOFIX_RESUME_REASON").strip()',
		'resume_source_phase = env("AUTOFIX_RESUME_PHASE").strip()',
		'resume_comment_posted = bool_env("AUTOFIX_RESUME_COMMENT_POSTED")',
		'resume_completed_scope = csv_env_list("AUTOFIX_RESUME_COMPLETED_SCOPE")',
		'resume_incomplete_scope = csv_env_list("AUTOFIX_RESUME_INCOMPLETE_SCOPE")',
		'"REVIEWER_BACKOFF: "',
		'"REVIEWER_CACHE: "',
		'"REVIEWER_SLOT_STATE: "',
		'"INFO: openrouter usage "',
		'status_pass1_*.txt',
		'if status == "skipped_budget":',
		'if resume_state in {"no_progress", "round_budget_exhausted"} and not resume_should_continue:',
		'return "soft_deadline"',
	):
		assert artifact in block, f"Summary step is missing local metric source: {artifact}"
	assert '"failure_class": "push_not_allowed"' in block
	assert '"failure_class": "withheld_for_safety"' in block
	assert "gh api" not in block
	assert "gh_retry" not in block
	assert "curl https://api.github.com" not in block


def test_review_pipeline_summary_reports_stall_recovery_for_retried_and_skipped_slots() -> None:
	result = _run_review_pipeline_summary_step_harness()
	summary = result["summary"]
	slots = {slot["slot"]: slot for slot in summary["slot_results"]["reviewers"]}

	assert "REVIEW_AUTOFIX_RUN_SUMMARY_V1" in result["step_summary"]
	assert slots["model_one"]["failure_class"] == "none"
	assert slots["model_one"]["stall_kill_count"] == 1
	assert slots["model_one"]["stall_recovery_next_action"] == "retry_cheaper_reasoning"
	assert slots["model_one"]["stall_recovered"] is True
	assert slots["model_one"]["retryable_failure_count"] == 1
	assert slots["model_one"]["retryable_failure_classes"] == ["stall_guard"]
	assert slots["model_one"]["backoff_sleep_secs_total"] == 2
	assert slots["model_one"]["slot_retry_budget_exhausted"] is False
	assert slots["model_one"]["fallback_model_used"] is False
	assert slots["model_one"]["cache_status"] == "supported"
	assert slots["model_one"]["cache_reuse_attempted"] is True
	assert slots["model_one"]["cache_read_input_tokens_total"] == 120

	assert slots["model_two"]["failure_class"] == "stall_guard"
	assert slots["model_two"]["stall_kill_count"] == 1
	assert slots["model_two"]["stall_recovery_next_action"] == "skip_unmapped"
	assert slots["model_two"]["stall_recovered"] is False
	assert slots["model_two"]["retryable_failure_count"] == 1
	assert slots["model_two"]["retryable_failure_classes"] == ["timeout"]
	assert slots["model_two"]["backoff_sleep_secs_total"] == 0
	assert slots["model_two"]["slot_retry_budget_exhausted"] is False
	assert slots["model_two"]["fallback_model_used"] is False
	assert slots["model_two"]["cache_status"] == "unsupported"
	assert slots["model_two"]["cache_reuse_attempted"] is False
	assert slots["model_two"]["cache_read_input_tokens_total"] == 0

	assert summary["stall_recovery"] == {
		"advanced_slots": 2,
		"killed_attempts": 2,
		"partial_finalize_requested": False,
		"recovered_slots": 1,
	}


def test_review_pipeline_summary_reports_partial_finalize_validated_push() -> None:
	result = _run_review_pipeline_summary_step_harness(
		extra_env={
			"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true",
			"AUTOFIX_PARTIAL_FINALIZE_REASON": "soft_deadline",
			"AUTOFIX_PARTIAL_FINALIZE_PHASE": "editor",
			"AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "true",
			"AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY": "false",
			"AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON": "none",
			"AUTOFIX_PARTIAL_COMMENT_POSTED": "true",
			"AUTOFIX_RESUME_ROUND": "1",
			"AUTOFIX_RESUME_ROUND_LIMIT": "3",
			"AUTOFIX_RESUME_STATE": "resumable",
			"AUTOFIX_RESUME_SHOULD_CONTINUE": "true",
			"AUTOFIX_RESUME_HEAD_SHA": "head-sha-123",
			"DID_COMMIT": "true",
			"AUTOFIX_EDITS_PUSHED": "true",
		},
	)
	summary = result["summary"]

	assert summary["partial_finalize"] is True
	assert summary["partial_finalize_reason"] == "soft_deadline"
	assert summary["partial_finalize_phase"] == "editor"
	assert summary["partial_finalize_validation_tail_can_complete"] is True
	assert summary["partial_finalize_edits_withheld_for_safety"] is False
	assert summary["partial_finalize_withheld_reason"] == "none"
	assert summary["finalize_reason"] == "partial_finalize"
	assert summary["slot_results"]["commit"]["failure_class"] == "none"
	assert summary["slot_results"]["push"]["failure_class"] == "none"
	assert summary["edits_pushed"] is True
	assert "| Partial validation tail can complete | true |" in result["step_summary"]
	assert "| Partial edits withheld for safety | false |" in result["step_summary"]
	assert "| Partial withheld reason | none |" in result["step_summary"]


def test_review_pipeline_summary_reports_partial_finalize_withheld_for_safety() -> None:
	result = _run_review_pipeline_summary_step_harness(
		extra_env={
			"AUTOFIX_PARTIAL_FINALIZE_REQUESTED": "true",
			"AUTOFIX_PARTIAL_FINALIZE_REASON": "soft_deadline",
			"AUTOFIX_PARTIAL_FINALIZE_PHASE": "editor",
			"AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "false",
			"AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY": "true",
			"AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON": "insufficient_budget_for_validation_tail",
			"AUTOFIX_PARTIAL_COMMENT_POSTED": "true",
			"AUTOFIX_RESUME_ROUND": "1",
			"AUTOFIX_RESUME_ROUND_LIMIT": "3",
			"AUTOFIX_RESUME_STATE": "resumable",
			"AUTOFIX_RESUME_SHOULD_CONTINUE": "true",
			"AUTOFIX_RESUME_HEAD_SHA": "head-sha-456",
		},
	)
	summary = result["summary"]

	assert summary["partial_finalize"] is True
	assert summary["partial_finalize_reason"] == "soft_deadline"
	assert summary["partial_finalize_phase"] == "editor"
	assert summary["partial_finalize_validation_tail_can_complete"] is False
	assert summary["partial_finalize_edits_withheld_for_safety"] is True
	assert summary["partial_finalize_withheld_reason"] == "insufficient_budget_for_validation_tail"
	assert summary["finalize_reason"] == "partial_finalize"
	assert summary["slot_results"]["commit"] == {
		"attempt_count": 1,
		"status": "skipped",
		"failure_class": "withheld_for_safety",
	}
	assert summary["slot_results"]["push"] == {
		"attempt_count": 0,
		"status": "skipped",
		"failure_class": "withheld_for_safety",
	}
	assert summary["edits_pushed"] is False
	assert "| Partial validation tail can complete | false |" in result["step_summary"]
	assert "| Partial edits withheld for safety | true |" in result["step_summary"]
	assert "| Partial withheld reason | insufficient_budget_for_validation_tail |" in result["step_summary"]


def test_review_partial_finalize_publish_safety_gate_is_wired() -> None:
	block = _step_block("Decide partial-finalize validation/push safety")
	for expected in (
		'if [ "${AUTOFIX_PARTIAL_FINALIZE_REQUESTED:-false}" = "true" ]; then',
		'workflow_timeout_source_path=".codex-workflow-src/.github/workflows/review_autofix.yml"',
		'workflow_timeout_source_path=".github/workflows/review_autofix.yml"',
		'import sys, yaml',
		'workflow = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}',
		'codex_agent_job = jobs.get("codex-agent") or {}',
		'resolver_timeout_minutes = next((step.get("timeout-minutes")',
		'step.get("name") == "Run Codex resolver, validate, stage, commit"',
		'sys.exit(1) if not isinstance(job_timeout_minutes, int) or not isinstance(resolver_timeout_minutes, int) else print(job_timeout_minutes, resolver_timeout_minutes)',
		'job_timeout_total_secs=$(( job_timeout_minutes * 60 ))',
		'partial_finalize_validation_minimum_secs=$(( (resolver_timeout_minutes * 60) + 600 ))',
		'hard_timeout_remaining_secs=$(( job_deadline_epoch - now_epoch ))',
		'withheld_reason="insufficient_budget_for_validation_tail"',
		'echo "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE=${validation_tail_can_complete}"',
		'echo "AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY=${edits_withheld_for_safety}"',
		'echo "AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON=${withheld_reason}"',
	):
		assert expected in block, f"missing partial-finalize safety-gate contract detail: {expected}"


def test_review_partial_finalize_timeout_extractor_handles_structured_yaml_layout() -> None:
	workflow_text = textwrap.dedent(
		"""\
		jobs:
		  codex-agent:
		    steps:
		      - name: Bootstrap
		        timeout-minutes: 5
		      - name: Run Codex resolver, validate, stage, commit
		        timeout-minutes: 170
		    timeout-minutes: 240
		"""
	)

	assert _extract_review_autofix_timeout_minutes(workflow_text) == (240, 170)


def test_review_partial_finalize_publish_safety_gate_keeps_validated_path_when_budget_remains() -> None:
	job_timeout_minutes, resolver_timeout_minutes = _review_autofix_timeout_minutes()
	assert job_timeout_minutes * 60 > (resolver_timeout_minutes * 60) + 600
	now_epoch = int(time.time())
	result = _run_partial_finalize_publish_safety_gate_step(job_start_epoch=str(now_epoch - 60))

	assert "validated tail remains available" in result["stdout"]
	assert result["github_env"] == {
		"AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "true",
		"AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY": "false",
		"AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON": "none",
	}


def test_review_partial_finalize_publish_safety_gate_prefers_codex_budget_start_epoch_when_withholding() -> None:
	job_timeout_minutes, _resolver_timeout_minutes = _review_autofix_timeout_minutes()
	now_epoch = int(time.time())
	result = _run_partial_finalize_publish_safety_gate_step(
		job_start_epoch=str(now_epoch - 60),
		codex_run_budget_start_epoch=str(now_epoch - (job_timeout_minutes * 60)),
	)

	assert "findings-only fallback" in result["stdout"]
	assert result["github_env"] == {
		"AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE": "false",
		"AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY": "true",
		"AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON": "insufficient_budget_for_validation_tail",
	}


def test_review_partial_finalize_workflow_path_is_wired() -> None:
	early_save_block = _step_block("Save review-issue ledger")
	restore_block = _step_block("Restore same-head partial resume state")
	partial_block = _step_block("Post partial finalize comment and persist runtime marker")
	late_save_block = _step_block("Save review-issue ledger after partial finalize")

	assert "env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED != 'true'" in early_save_block
	for expected in (
		'CURRENT_PREVIOUS_REVIEWS_DIR="${PREVIOUS_REVIEWS_DIR:-}"',
		'CURRENT_RUNTIME_DIR="${RUNTIME_DIR:-}"',
		'"AUTOFIX_RESUME_RESTORED_ARTIFACT_COUNT": "0",',
		'selected_marker_root / "previous_reviews"',
		'selected_marker_root / "runtime"',
	):
		assert expected in restore_block, f"missing same-head restore artifact contract detail: {expected}"
	assert "env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED == 'true'" in partial_block
	assert "always() && env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED == 'true'" in partial_block
	for expected in (
		"<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->",
		"partial_finalize=true",
		"reason=${AUTOFIX_PARTIAL_FINALIZE_REASON:-unknown}",
		"phase=${AUTOFIX_PARTIAL_FINALIZE_PHASE:-unknown}",
		"completed_scope=${completed_scope_csv}",
		"incomplete_scope=${incomplete_scope_csv}",
		"validated_edits_committed=${validated_edits_committed}",
		"edits_pushed=${edits_pushed}",
		"validation_tail_can_complete=${validation_tail_can_complete}",
		"edits_withheld_for_safety=${edits_withheld_for_safety}",
		"withheld_reason=${withheld_reason}",
		"head_sha=${current_head_sha}",
		"resume_round=${resume_round}",
		"resume_round_limit=${resume_round_limit}",
		"resume_state=${RESUME_STATE}",
		"resume_should_continue=${RESUME_SHOULD_CONTINUE}",
		'VALIDATION_TAIL_CAN_COMPLETE="${validation_tail_can_complete}"',
		'EDITS_WITHHELD_FOR_SAFETY="${edits_withheld_for_safety}"',
		'WITHHELD_REASON="${withheld_reason}"',
		'"validation_tail_can_complete": parse_bool("VALIDATION_TAIL_CAN_COMPLETE"),',
		'"edits_withheld_for_safety": parse_bool("EDITS_WITHHELD_FOR_SAFETY"),',
		'"withheld_reason": os.environ.get("WITHHELD_REASON", "").strip() or "none",',
		"partial_finalize.json",
		'echo "AUTOFIX_PARTIAL_COMMENT_POSTED=${partial_comment_posted}"',
		'echo "AUTOFIX_RESUME_ROUND=${resume_round}"',
		'echo "AUTOFIX_RESUME_HEAD_SHA=${current_head_sha}"',
		'echo "AUTOFIX_RESUME_ROUND_LIMIT=${resume_round_limit}"',
		'echo "AUTOFIX_RESUME_STATE=${RESUME_STATE}"',
		'echo "AUTOFIX_RESUME_SHOULD_CONTINUE=${RESUME_SHOULD_CONTINUE}"',
		'"progress_fingerprint": os.environ.get("PROGRESS_FINGERPRINT_VALUE", "").strip(),',
		'reviewer_consensus.txt',
		'partial_marker_dir / "previous_reviews"',
		'partial_marker_dir / "runtime"',
	):
		assert expected in partial_block, f"missing partial-finalize contract detail: {expected}"
	for expected in (
		".ai/review_issue_ledger/",
		".ai/review_runtime/",
		"REVIEW_LEDGER_PATH",
	):
		assert expected in late_save_block, f"missing persisted partial-finalize state path: {expected}"


def test_review_partial_finalize_comment_and_marker_report_validated_push_state() -> None:
	with tempfile.TemporaryDirectory(prefix="partial-finalize-validated-push-") as td:
		context = _build_partial_finalize_step_context(Path(td))
		result = _run_partial_finalize_step(
			context,
			edits_pushed="true",
			editor_commit_produced="true",
			validation_tail_can_complete="true",
			edits_withheld_for_safety="false",
			withheld_reason="none",
		)

	assert "so the validated finalize tail could complete before the hard job timeout." in result["latest_comment"]
	assert "- Validated edits committed: true" in result["latest_comment"]
	assert "- Edits pushed: true" in result["latest_comment"]
	assert "- Validation tail can complete: true" in result["latest_comment"]
	assert "- Edits withheld for safety: false" in result["latest_comment"]
	assert "- Withheld reason: none" in result["latest_comment"]
	assert "validated_edits_committed=true" in result["latest_comment"]
	assert "edits_pushed=true" in result["latest_comment"]
	assert "validation_tail_can_complete=true" in result["latest_comment"]
	assert "edits_withheld_for_safety=false" in result["latest_comment"]
	assert "withheld_reason=none" in result["latest_comment"]
	assert result["marker_payload"]["validated_edits_committed"] is True
	assert result["marker_payload"]["edits_pushed"] is True
	assert result["marker_payload"]["validation_tail_can_complete"] is True
	assert result["marker_payload"]["edits_withheld_for_safety"] is False
	assert result["marker_payload"]["withheld_reason"] == "none"


def test_review_partial_finalize_comment_and_marker_report_withheld_state() -> None:
	with tempfile.TemporaryDirectory(prefix="partial-finalize-withheld-") as td:
		context = _build_partial_finalize_step_context(Path(td))
		result = _run_partial_finalize_step(
			context,
			validation_tail_can_complete="false",
			edits_withheld_for_safety="true",
			withheld_reason="insufficient_budget_for_validation_tail",
		)

	assert "The remaining job headroom could not cover the existing validation/publish tail, so the workflow posted findings only and withheld any unpushed edits for safety." in result["latest_comment"]
	assert "- Validated edits committed: false" in result["latest_comment"]
	assert "- Edits pushed: false" in result["latest_comment"]
	assert "- Validation tail can complete: false" in result["latest_comment"]
	assert "- Edits withheld for safety: true" in result["latest_comment"]
	assert "- Withheld reason: insufficient_budget_for_validation_tail" in result["latest_comment"]
	assert "validated_edits_committed=false" in result["latest_comment"]
	assert "edits_pushed=false" in result["latest_comment"]
	assert "validation_tail_can_complete=false" in result["latest_comment"]
	assert "edits_withheld_for_safety=true" in result["latest_comment"]
	assert "withheld_reason=insufficient_budget_for_validation_tail" in result["latest_comment"]
	assert result["marker_payload"]["validated_edits_committed"] is False
	assert result["marker_payload"]["edits_pushed"] is False
	assert result["marker_payload"]["validation_tail_can_complete"] is False
	assert result["marker_payload"]["edits_withheld_for_safety"] is True
	assert result["marker_payload"]["withheld_reason"] == "insufficient_budget_for_validation_tail"


def test_review_partial_finalize_skips_remaining_expensive_steps() -> None:
	for step_name in (
		"Pre-editor stale-base gate",
		"Install project dependencies (best-effort)",
		"Switch reasoning effort for editor",
		"Setup Serena for editor",
		"Apply fixes with editor model",
		"Post editor summary comment",
		"Mark linked issues ready to merge",
		"Enable auto-merge on PR",
		"Telegram success",
	):
		block = _step_block(step_name)
		assert "env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED != 'true'" in block, (
			f"step should skip during partial finalize: {step_name}"
		)
	for step_name in (
		"Run interim judge",
		"Synthesize behavioural smoke",
		"Detect editor-claimed-but-uncommitted changes",
		"Validate editor no-op disposition",
		"Detect merge conflicts",
		"Prepare merge-conflict resolver prompt and pre-snapshot",
		"Run Codex resolver, validate, stage, commit",
		"Push all pending commits",
	):
		block = _step_block(step_name)
		assert "(env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED != 'true' || env.AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE == 'true')" in block, (
			f"step should stay available only when the partial-finalize validation tail can complete: {step_name}"
		)


def test_review_partial_finalize_keeps_commit_and_push_path_available() -> None:
	commit_block = _step_block("Commit changes")
	push_block = _step_block("Push all pending commits")
	assert "env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED != 'true'" not in commit_block
	assert "(env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED != 'true' || env.AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE == 'true')" in push_block


def test_review_pipeline_summary_finalize_reason_marks_partial_runs() -> None:
	block = _step_block("Append review pipeline iteration summary")
	assert re.search(
		r'if resume_state in \{"no_progress", "round_budget_exhausted"\} and not resume_should_continue:\n\s+return resume_state\n\s+if partial_finalize:\n\s+return "partial_finalize"',
		block,
	), "summary finalize_reason contract should classify partial-finalize runs"


def test_same_head_partial_resume_restore_prefers_latest_matching_marker_and_ignores_other_heads() -> None:
	result = _run_restore_same_head_resume_harness(
		markers=[
			{
				"resume_round": 1,
				"head_sha": "other-head-one",
				"resume_state": "resumable",
				"resume_should_continue": True,
				"completed_scope": ["reviewers"],
				"incomplete_scope": ["editor"],
				"progress_fingerprint": "fp-other-1",
				"reason": "soft_deadline",
				"phase": "editor",
				"comment_posted": False,
			},
			{
				"resume_round": 2,
				"head_sha": "__HEAD__",
				"resume_state": "resumable",
				"resume_should_continue": True,
				"completed_scope": ["reviewers"],
				"incomplete_scope": ["editor"],
				"progress_fingerprint": "fp-match-2",
				"reason": "soft_deadline",
				"phase": "editor",
				"comment_posted": False,
			},
			{
				"resume_round": 4,
				"head_sha": "__HEAD__",
				"resume_state": "round_budget_exhausted",
				"resume_should_continue": False,
				"completed_scope": ["reviewers", "consolidator", "parser"],
				"incomplete_scope": ["ledger", "editor"],
				"progress_fingerprint": "fp-match-4",
				"reason": "soft_deadline",
				"phase": "editor",
				"comment_posted": True,
			},
		],
	)
	github_env = result["github_env"]
	assert github_env["AUTOFIX_RESUME_RESTORED"] == "true"
	assert github_env["AUTOFIX_RESUME_TERMINAL"] == "true"
	assert github_env["AUTOFIX_RESUME_ROUND"] == "4"
	assert github_env["AUTOFIX_RESUME_ROUND_LIMIT"] == "3"
	assert github_env["AUTOFIX_RESUME_STATE"] == "round_budget_exhausted"
	assert github_env["AUTOFIX_RESUME_SHOULD_CONTINUE"] == "false"
	assert github_env["AUTOFIX_RESUME_HEAD_SHA"] == result["head_sha"]
	assert github_env["AUTOFIX_RESUME_COMPLETED_SCOPE"] == "reviewers,consolidator,parser"
	assert github_env["AUTOFIX_RESUME_INCOMPLETE_SCOPE"] == "ledger,editor"
	assert github_env["AUTOFIX_RESUME_PROGRESS_FINGERPRINT"] == "fp-match-4"
	assert github_env["AUTOFIX_RESUME_REASON"] == "soft_deadline"
	assert github_env["AUTOFIX_RESUME_PHASE"] == "editor"
	assert github_env["AUTOFIX_RESUME_COMMENT_POSTED"] == "true"
	assert github_env["AUTOFIX_RESUME_MARKER_FILE"].endswith("round-4/partial_finalize.json")
	assert "Restored same-head partial resume state" in result["stdout"]


def test_same_head_partial_resume_restore_fails_open_when_no_marker_matches_head() -> None:
	result = _run_restore_same_head_resume_harness(
		markers=[
			{
				"resume_round": 1,
				"head_sha": "other-head-one",
				"resume_state": "resumable",
				"resume_should_continue": True,
				"completed_scope": ["reviewers"],
				"incomplete_scope": ["editor"],
				"progress_fingerprint": "fp-other-1",
				"reason": "soft_deadline",
				"phase": "editor",
				"comment_posted": False,
			},
			{
				"resume_round": 3,
				"head_sha": "other-head-two",
				"resume_state": "no_progress",
				"resume_should_continue": False,
				"completed_scope": ["reviewers", "consolidator"],
				"incomplete_scope": ["editor"],
				"progress_fingerprint": "fp-other-3",
				"reason": "soft_deadline",
				"phase": "editor",
				"comment_posted": True,
			},
		],
	)
	github_env = result["github_env"]
	assert github_env["AUTOFIX_RESUME_RESTORED"] == "false"
	assert github_env["AUTOFIX_RESUME_TERMINAL"] == "false"
	assert github_env["AUTOFIX_RESUME_ROUND"] == "0"
	assert github_env["AUTOFIX_RESUME_ROUND_LIMIT"] == "3"
	assert github_env["AUTOFIX_RESUME_STATE"] == "fresh"
	assert github_env["AUTOFIX_RESUME_SHOULD_CONTINUE"] == "false"
	assert github_env["AUTOFIX_RESUME_HEAD_SHA"] == result["head_sha"]
	assert github_env["AUTOFIX_RESUME_MARKER_FILE"] == ""
	assert f"No cached same-head partial resume state matched HEAD={result['head_sha']}" in result["stdout"]


def test_reviewer_resume_reuses_cached_successes_and_reruns_only_incomplete_slots() -> None:
	result = _run_reviewer_resume_cached_success_harness()
	assert result["PASS_SUCCESSFUL"] == "2"
	assert result["called_models"] == ["moonshotai/kimi-k2.5"]
	assert result["cached_log"].startswith("cached log\n")
	assert result["rerun_output"] == "fresh reviewer output for moonshotai/kimi-k2.5\n"
	assert "Resume: reusing cached reviewer success for x-ai/grok-4.20 (review)." in result["stderr"]


def test_review_partial_resume_path_does_not_workflow_dispatch_without_new_push() -> None:
	block = _step_block("Re-trigger review via workflow_dispatch")
	assert "env.AUTOFIX_PARTIAL_FINALIZE_REQUESTED != 'true'" in block
	assert "env.AUTOFIX_RESUME_TERMINAL != 'true'" in block


def test_review_partial_finalize_marker_sets_no_progress_terminal_state() -> None:
	with tempfile.TemporaryDirectory(prefix="partial-finalize-no-progress-") as td:
		context = _build_partial_finalize_step_context(Path(td))
		first = _run_partial_finalize_step(context)
		second = _run_partial_finalize_step(context, previous_env=first["github_env"])

	assert first["github_env"]["AUTOFIX_RESUME_STATE"] == "resumable"
	assert first["github_env"]["AUTOFIX_RESUME_SHOULD_CONTINUE"] == "true"
	assert second["github_env"]["AUTOFIX_RESUME_ROUND"] == "2"
	assert second["github_env"]["AUTOFIX_RESUME_STATE"] == "no_progress"
	assert second["github_env"]["AUTOFIX_RESUME_SHOULD_CONTINUE"] == "false"
	assert second["github_env"]["AUTOFIX_RESUME_TERMINAL"] == "true"
	assert second["marker_payload"]["resume_round"] == 2
	assert second["marker_payload"]["resume_state"] == "no_progress"
	assert second["marker_payload"]["resume_should_continue"] is False
	assert second["marker_payload"]["progress_fingerprint"] == first["marker_payload"]["progress_fingerprint"]
	assert "resume_state=no_progress" in second["latest_comment"]
	assert "resume_should_continue=false" in second["latest_comment"]


def test_review_partial_finalize_marker_sets_round_budget_exhausted_terminal_state() -> None:
	with tempfile.TemporaryDirectory(prefix="partial-finalize-round-budget-") as td:
		context = _build_partial_finalize_step_context(Path(td))
		first = _run_partial_finalize_step(context, review_max_resume_rounds="2")
		second = _run_partial_finalize_step(
			context,
			previous_env=first["github_env"],
			review_max_resume_rounds="2",
			editor_summary_text="editor summary changed\n",
		)

	assert first["github_env"]["AUTOFIX_RESUME_STATE"] == "resumable"
	assert second["github_env"]["AUTOFIX_RESUME_ROUND"] == "2"
	assert second["github_env"]["AUTOFIX_RESUME_ROUND_LIMIT"] == "2"
	assert second["github_env"]["AUTOFIX_RESUME_STATE"] == "round_budget_exhausted"
	assert second["github_env"]["AUTOFIX_RESUME_SHOULD_CONTINUE"] == "false"
	assert second["github_env"]["AUTOFIX_RESUME_TERMINAL"] == "true"
	assert second["marker_payload"]["resume_round"] == 2
	assert second["marker_payload"]["resume_round_limit"] == 2
	assert second["marker_payload"]["resume_state"] == "round_budget_exhausted"
	assert second["marker_payload"]["resume_should_continue"] is False
	assert second["marker_payload"]["progress_fingerprint"] != first["marker_payload"]["progress_fingerprint"]
	assert "resume_state=round_budget_exhausted" in second["latest_comment"]
	assert "resume_should_continue=false" in second["latest_comment"]


def test_same_head_partial_resume_restore_rehydrates_cached_artifacts_and_preserves_no_progress_across_run_dirs() -> None:
	with tempfile.TemporaryDirectory(prefix="partial-finalize-cross-run-") as td:
		root = Path(td)
		context = _build_partial_finalize_step_context(root)
		first = _run_partial_finalize_step(context)
		restored_runtime = root / "runtime_restored"
		restored_reviews = root / "reviews_restored"
		restore = _run_restore_same_head_resume_step(
			Path(context["repo"]),
			runtime_dir=restored_runtime,
			reviews_dir=restored_reviews,
		)
		second = _run_partial_finalize_step(
			context,
			previous_env=restore["github_env"],
			runtime_dir=restored_runtime,
			reviews_dir=restored_reviews,
		)

	assert restore["github_env"]["AUTOFIX_RESUME_RESTORED"] == "true"
	assert int(restore["github_env"]["AUTOFIX_RESUME_RESTORED_ARTIFACT_COUNT"]) > 0
	assert restore["restored_review_files"]["review_model_one.txt"] == "cached reviewer output\n"
	assert restore["restored_review_files"]["status_review_model_one.txt"] == "success\n"
	assert restore["restored_runtime_files"]["reviewer_consensus.txt"] == "consensus sentinel\n"
	assert restore["restored_runtime_files"]["editor_summary.txt"] == "editor summary sentinel\n"
	assert second["github_env"]["AUTOFIX_RESUME_STATE"] == "no_progress"
	assert second["github_env"]["AUTOFIX_RESUME_SHOULD_CONTINUE"] == "false"
	assert second["marker_payload"]["progress_fingerprint"] == first["marker_payload"]["progress_fingerprint"]


def test_push_step_exports_edits_pushed_sentinel_for_summary_contract() -> None:
	block = _step_block("Push all pending commits")
	assert 'echo "AUTOFIX_EDITS_PUSHED=true" >> "$GITHUB_ENV"' in block


def test_review_pipeline_summary_finalize_reason_distinguishes_push_not_allowed() -> None:
	block = _step_block("Append review pipeline iteration summary")
	assert re.search(
		r'if max_iterations_reached and not skip_judge:.*?return "rb_judge_review_blocked"\n\s+if push_needed and not push_allowed:\n\s+return "push_not_allowed"\n\s+if push_needed and not edits_pushed:\n\s+return "push_failed"',
		block,
		re.S,
	), "Summary finalize_reason contract must distinguish push-disabled runs before push_failed"


def test_auto_merge_guard_honours_configured_orchestrator_branch_pattern() -> None:
	block = _step_block("Enable auto-merge on PR")
	helper_text = _auto_merge_helper_text()
	assert "ORCH_INTEGRATION_BRANCH_PATTERN: ${{ vars.ORCH_INTEGRATION_BRANCH_PATTERN || '^orchestrator/project-' }}" in block
	assert 'grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"' in helper_text
	assert 'if [ -z "${_orch_pr_head_ref}" ]; then' in helper_text
	assert "empty/null .head.ref" in helper_text
	assert "refs:?[[:space:]]*#[0-9]+" in helper_text
	assert "(closes|fixes|resolves):?[[:space:]]*#[0-9]+" in helper_text
	assert "matches ORCH_INTEGRATION_BRANCH_PATTERN='${ORCH_INTEGRATION_BRANCH_PATTERN}'" in helper_text
	assert "falling back to canonical '^orchestrator/project-([0-9]+)$' auto-merge suppressor" in helper_text
	assert "falling back to canonical '^orchestrator/project-[0-9]+$' auto-merge suppressor" not in helper_text


def test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_codex_agent_path() -> None:
	# forward-merge-stable-to-main.yml opens fallback PRs with head ref
	# `auto/forward-merge-stable-<run-id>-<attempt>`. These MUST be merged
	# via "Create a merge commit" so stable's tip stays in main's ancestry —
	# the regular auto-merge call `gh pr merge --squash --auto` would strip
	# that ancestry and break the next promote-main-to-stable.yml run. By
	# default (FORWARD_MERGE_FALLBACK_AUTO_MERGE='true') the codex-agent
	# "Enable auto-merge on PR" step instead enables auto-merge with a REAL
	# merge commit (`gh pr merge --merge --auto`) — the unattended equivalent
	# of "Create a merge commit", which preserves ancestry — and short-circuits
	# (exit 0) BEFORE reaching the orchestrator block and the squash-auto tail.
	# Setting the var to any non-'true' value restores the old behaviour of
	# leaving the PR for a manual merge commit; both branches are verified.
	helper_text = _auto_merge_helper_text()
	assert "Scoped opt-out for forward-merge fallback PRs" in helper_text, (
		"Forward-merge fallback suppressor comment is missing"
	)
	assert "grep -Eq '^auto/forward-merge-stable-'" in helper_text, (
		"Forward-merge fallback head-ref regex is missing or has drifted"
	)
	assert "matches forward-merge fallback pattern '^auto/forward-merge-stable-'" in helper_text, (
		"Forward-merge suppressor log line is missing the canonical phrasing"
	)
	assert "promote-main-to-stable.yml" in helper_text, (
		"Suppressor must explain WHY (ancestry / promote-main-to-stable) for operator debuggability"
	)
	# The forward-merge suppressor must run BEFORE the orchestrator pattern
	# block — otherwise a forward-merge head ref that someone retrofitted
	# to also look orchestrator-shaped (or any future suppressor that
	# moves on) would be evaluated in the wrong order. Concretely: the
	# suppressor must appear above the first reference to the configured
	# ORCH_INTEGRATION_BRANCH_PATTERN match attempt.
	idx_forward = helper_text.find("matches forward-merge fallback pattern")
	idx_orch_match = helper_text.find('grep -Eq -- "${ORCH_INTEGRATION_BRANCH_PATTERN}"')
	assert idx_forward != -1
	assert idx_orch_match != -1
	assert idx_forward < idx_orch_match, (
		"Forward-merge suppressor must short-circuit before the orchestrator-pattern match attempt"
	)
	# Default behaviour: gated by FORWARD_MERGE_FALLBACK_AUTO_MERGE and merges
	# via a real merge commit (NOT squash) so stable's ancestry is preserved.
	assert "FORWARD_MERGE_FALLBACK_AUTO_MERGE" in helper_text, (
		"Forward-merge auto-merge must be gated by the FORWARD_MERGE_FALLBACK_AUTO_MERGE var"
	)
	assert 'gh pr merge "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --merge --auto' in helper_text, (
		"Forward-merge fallback PRs must auto-merge via a real merge commit (--merge --auto), not squash"
	)
	# The merge-commit enable must sit on the forward-merge branch, before the
	# exit 0 that prevents falling through to the --squash --auto tail. Anchor
	# on the full command lines so comment mentions of "--squash --auto" above
	# the call site do not perturb the ordering check.
	idx_fm_merge = helper_text.find('--repo "${GITHUB_REPOSITORY}" --merge --auto')
	idx_squash = helper_text.find('--repo "${GITHUB_REPOSITORY}" --squash --auto')
	assert idx_fm_merge != -1 and idx_squash != -1
	assert idx_fm_merge < idx_squash, (
		"Forward-merge merge-commit call must precede (and short-circuit before) the squash tail"
	)
	# Opt-out branch still suppresses + explains when the var is not 'true'.
	assert "FORWARD_MERGE_FALLBACK_AUTO_MERGE != 'true' — auto-merge suppressed" in helper_text, (
		"Forward-merge path must still suppress + log when the opt-out var is set"
	)


def test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_deterministic_skip_path() -> None:
	# Defense in depth: a small/doc-only forward-merge fallback PR would
	# otherwise short-circuit through deterministic-skip-merge's auto-merge
	# call before the codex-agent path's suppressor ran. The
	# deterministic-skip job must apply the same head-ref guard, sourced
	# from the gate job's existing /pulls/{n} fetch (§15 API hygiene — no
	# duplicate API call).
	block = _step_block("Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")
	assert "PR_HEAD_REF" in block, (
		"deterministic-skip-merge must read the gate's head_ref output"
	)
	assert "grep -Eq '^auto/forward-merge-stable-'" in block, (
		"Forward-merge fallback head-ref regex is missing from deterministic-skip-merge"
	)
	assert "auto-merge suppressed on the deterministic-skip path" in block, (
		"Deterministic-skip suppressor log line is missing the canonical phrasing"
	)
	# The check must run BEFORE the `gh pr merge --squash --auto` call.
	idx_guard = block.find("grep -Eq '^auto/forward-merge-stable-'")
	idx_merge = block.find("gh pr merge")
	assert idx_guard != -1
	assert idx_merge != -1
	assert idx_guard < idx_merge, (
		"Forward-merge suppressor must short-circuit before the gh pr merge --squash --auto call"
	)
	assert 'auto_merge_summary="SUPPRESSED (forward-merge fallback head ref' in block, (
		"deterministic-skip-merge must track suppressed auto-merge state for the step summary (opt-out branch)"
	)
	assert 'echo "- **Auto-merge:** ${auto_merge_summary}"' in block, (
		"Deterministic-skip summary must report the actual auto-merge outcome"
	)
	# Default behaviour mirrors the codex-agent path: gated by
	# FORWARD_MERGE_FALLBACK_AUTO_MERGE and merged via a real merge commit so a
	# small/doc-only forward-merge fallback PR routed through deterministic-skip
	# does not get squash-merged and strip stable's ancestry.
	assert "FORWARD_MERGE_FALLBACK_AUTO_MERGE" in block, (
		"deterministic-skip-merge forward-merge handling must be gated by FORWARD_MERGE_FALLBACK_AUTO_MERGE"
	)
	assert 'gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" --merge --auto' in block, (
		"deterministic-skip-merge must auto-merge forward-merge fallback PRs via a real merge commit"
	)
	assert 'auto_merge_summary="ENABLED (merge commit)"' in block, (
		"deterministic-skip-merge must record the merge-commit auto-merge outcome for the step summary"
	)


def test_gate_emits_head_ref_output_for_forward_merge_suppressor_reuse() -> None:
	# The deterministic-skip-merge suppressor sources head ref from the
	# gate's /pulls/{n} fetch (§15: don't repeat an API call). Verify the
	# gate exposes head_ref as an output and the deterministic-skip-merge
	# job reads it via needs.gate.outputs.head_ref.
	wf = _workflow_text()
	assert "head_ref: ${{ steps.evaluate.outputs.head_ref }}" in wf, (
		"Gate job must expose head_ref output for downstream forward-merge suppressors"
	)
	assert 'echo "head_ref=${pr_head_ref}"' in wf, (
		"Gate evaluate step must emit head_ref to GITHUB_OUTPUT"
	)
	assert "PR_HEAD_REF: ${{ needs.gate.outputs.head_ref }}" in wf, (
		"deterministic-skip-merge must consume head_ref from gate outputs"
	)
	assert "post_merge_pr_text_json: ${{ steps.evaluate.outputs.post_merge_pr_text_json }}" in wf, (
		"Gate job must expose cached PR title/body for the post-merge validation dispatch"
	)


def test_gate_emits_force_full_review_tier_output_for_phase_i_resolver() -> None:
	wf = _workflow_text()
	assert "force_full_review_tier: ${{ steps.evaluate.outputs.force_full_review_tier }}" in wf, (
		"Gate job must expose the force-full review-tier signal for downstream reviewer routing"
	)
	assert 'echo "force_full_review_tier=${FORCE_FULL_REVIEW_TIER}"' in wf, (
		"Gate evaluate step must emit the force-full review-tier signal to GITHUB_OUTPUT"
	)
	assert "FORCE_FULL_REVIEW_TIER: ${{ needs.gate.outputs.force_full_review_tier || 'false' }}" in wf, (
		"codex-agent must consume the gate's force-full review-tier output"
	)
	assert "post_merge_linked_issues_json: ${{ steps.evaluate.outputs.post_merge_linked_issues_json }}" in wf, (
		"Gate job must expose cached linked-issue labels for the post-merge validation dispatch"
	)
	assert "post_merge_validate_context_definitely_empty: ${{ steps.evaluate.outputs.post_merge_validate_context_definitely_empty }}" in wf, (
		"Gate job must expose the definitely-empty post-merge validation guard output"
	)
	assert 'echo "post_merge_validate_context_definitely_empty=${POST_MERGE_VALIDATE_CONTEXT_DEFINITELY_EMPTY}"' in wf, (
		"Gate evaluate step must emit the definitely-empty post-merge validation guard"
	)
	assert "POST_MERGE_PR_TEXT_JSON: ${{ needs.gate.outputs.post_merge_pr_text_json }}" in wf, (
		"post-merge validate dispatch must consume cached PR text from gate outputs"
	)
	assert "POST_MERGE_LINKED_ISSUES_JSON: ${{ needs.gate.outputs.post_merge_linked_issues_json }}" in wf, (
		"post-merge validate dispatch must consume cached linked-issue labels from gate outputs"
	)
	assert "needs.gate.outputs.post_merge_validate_context_definitely_empty != 'true'" in wf, (
		"post-merge validate dispatch must skip only when the gate has already proven the context is definitely empty"
	)


def test_post_merge_validate_dispatch_warns_on_degraded_github_reads() -> None:
	block = _step_block("Dispatch standalone validate for orchestrator short-circuit issues")
	assert "Unable to refresh closingIssuesReferences for merged PR" in block, (
		"post-merge validate dispatch must warn when linked-issue GraphQL retries exhaust"
	)
	assert "Unable to refresh PR title/body for merged PR" in block, (
		"post-merge validate dispatch must warn when PR text retries exhaust"
	)
	assert "GitHub read retries were exhausted; leaving validation state unchanged." in block, (
		"post-merge validate dispatch must not report an unknown linked-issue state as a clean no-op"
	)
	assert "PR body/title fallback helper was unavailable; leaving validation state unchanged." in block, (
		"post-merge validate dispatch must not report an unavailable PR-text fallback helper as a clean no-op"
	)
	assert "Unable to read labels for issue #${issue_number} after retries" in block, (
		"post-merge validate dispatch must warn when per-issue label hydration retries exhaust"
	)


def test_post_merge_validate_dispatch_inlines_retry_wrapper_when_helper_load_fails() -> None:
	script = _step_run_script("Dispatch standalone validate for orchestrator short-circuit issues")
	assert 'type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }' not in script, (
		"post-merge validate dispatch must not fall back to a no-op gh_retry wrapper"
	)
	assert "if ! type gh_retry >/dev/null 2>&1; then" in script, (
		"post-merge validate dispatch must define an inline retry wrapper when gh_helpers.sh is unavailable"
	)
	assert "local n=0 max=4 delay=2" in script, (
		"post-merge validate dispatch inline retry wrapper must preserve the lightweight retry contract"
	)


def test_post_merge_force_poll_is_not_blocked_by_definitely_empty_validate_context() -> None:
	job = _job_block("post-merge-force-poll")
	assert "needs.gate.outputs.post_merge_dispatch == 'true' && needs.gate.outputs.claude_branch_review != 'true'" in job, (
		"post-merge force poll must run whenever merged-PR handling is active for non-claude branches"
	)
	assert "post_merge_validate_context_definitely_empty" not in job, (
		"post-merge force poll must stay independent of the definitely-empty validate-dispatch guard"
	)


def test_deterministic_skip_warns_when_closing_issue_resolution_is_unknown() -> None:
	block = _step_block("Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge")
	assert 'if issue_numbers="$(gh_retry gh api graphql \\' in block, (
		"deterministic-skip-merge must retry closingIssuesReferences lookups"
	)
	assert "Unable to resolve closingIssuesReferences for PR #${PR_NUMBER} after retries" in block, (
		"deterministic-skip-merge must warn when linked-issue resolution stays unknown"
	)
	assert "linked-issue state is unknown" in block, (
		"deterministic-skip-merge must distinguish read failures from real empty linked-issue results"
	)


def test_reviewer_prompt_output_rules_still_forbid_scripts() -> None:
	reviewers = _reviewers_text()
	assert "OUTPUT RULES" in reviewers
	assert "No scripts" in reviewers


def test_reviewer_iteration_scope_first_iteration_keeps_full_diff_context() -> None:
	result = _run_reviewer_scope_harness(
		scope_mode="full",
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text="",
	)

	assert result["scoped_active"] == "false"
	assert "full change set of the pull request" in result["context_sections"]
	assert "full PR patch; secondary context" in result["context_sections"]
	assert "scoped reviewer focus derived from latest autofix changes" not in result["context_sections"]
	assert "PR changed files:" in result["semble_query"]
	assert "Scoped reviewer focus files:" not in result["semble_query"]


def test_reviewer_iteration_scope_valid_artifacts_narrow_to_last_run_and_actionable_ledger_files() -> None:
	ledger_text = "\n".join([
		"issue-1\tPERSISTING\t1\tscripts/review_run_reviewers.sh:10\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tNEW\t0\ttests/test_review_autofix_review_pipeline_contract.py:20\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tFIXED\t0\tignored/fixed.py:30\tCORRECTNESS & LOGIC\t[]",
		"issue-4\taccepted-residual\t0\tignored/residual.py:40\tCORRECTNESS & LOGIC\t[]",
		"issue-5\tRESURGENT\t0\ttests/test_review_autofix_review_pipeline_contract.py:22\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_reviewer_scope_harness(
		scope_mode="auto",
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"tests/test_review_autofix_review_pipeline_contract.py",
	]
	assert "ignored/fixed.py" not in result["scope_paths"]
	assert "ignored/residual.py" not in result["scope_paths"]
	assert "Reviewer iteration scoping mode: scoped" in result["scope_summary"]
	assert "Actionable statuses: NEW, PERSISTING, RESURGENT" in result["scope_summary"]
	assert "ledger:PERSISTING" in result["scope_summary"]
	assert "ledger:NEW, ledger:RESURGENT" in result["scope_summary"]
	assert "scoped reviewer focus derived from latest autofix changes + still-actionable ledger rows" in result["context_sections"]
	assert "current contents of the scoped reviewer focus files" in result["context_sections"]
	assert "full change set of the pull request" not in result["context_sections"]
	assert "full PR patch; secondary context" not in result["context_sections"]
	assert "Scoped reviewer focus summary:" in result["semble_query"]
	assert "Scoped reviewer focus files:" in result["semble_query"]
	assert "PR changed files:" not in result["semble_query"]


def test_reviewer_iteration_scope_fails_open_on_bad_scope_artifacts() -> None:
	result = _run_reviewer_scope_harness(
		scope_mode="auto",
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text="",
	)

	assert result["scoped_active"] == "false"
	assert "Reviewer iteration scoping mode: full-diff" in result["scope_summary"]
	assert "Reason: empty LEDGER_STATUS_FILE" in result["scope_summary"]
	assert result["scope_paths"] == ""
	assert "full change set of the pull request" in result["context_sections"]
	assert "full PR patch; secondary context" in result["context_sections"]
	assert "scoped reviewer focus derived from latest autofix changes" not in result["context_sections"]
	assert "PR changed files:" in result["semble_query"]
	assert "Scoped reviewer focus files:" not in result["semble_query"]


def test_reviewer_iteration_scope_uses_targeted_context_helper_and_scoped_semble_labels() -> None:
	reviewers = _reviewers_text()
	assert 'TARGETED_FILE_CONTEXT_SCRIPT="${TARGETED_FILE_CONTEXT_SCRIPT:-${SUPPORT_SCRIPTS_DIR:-scripts}/targeted_file_context.py}"' in reviewers
	assert 'python3 "${TARGETED_FILE_CONTEXT_SCRIPT}"' in reviewers
	assert '--paths-file "${REVIEWER_SCOPE_PATHS_FILE}"' in reviewers
	assert 'Scoped reviewer focus summary:' in reviewers
	assert 'Scoped reviewer focus files:' in reviewers
	assert 'SCOPED REVIEWER FOCUS SUMMARY / FILE LIST / TARGETED FILE CONTEXT' in reviewers


def test_reviewer_iteration_scope_prepare_path_accepts_root_level_actionable_files() -> None:
	ledger_text = "\n".join([
		"issue-1\tNEW\t0\tLICENSE:3\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tPERSISTING\t1\tgo.mod:2\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tRESURGENT\t0\t.gitignore:1\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		workspace_files={
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"LICENSE": "test license\n",
			"go.mod": "module example.com/test\n",
			".gitignore": "__pycache__/\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"LICENSE",
		"go.mod",
		".gitignore",
	]
	assert "- LICENSE [ledger:NEW]" in result["scope_summary"]
	assert "- go.mod [ledger:PERSISTING]" in result["scope_summary"]
	assert "- .gitignore [ledger:RESURGENT]" in result["scope_summary"]
	assert "=== TARGETED FILE CONTEXT ===" in result["scope_context"]
	assert "--- FILE: LICENSE" in result["scope_context"]
	assert "--- FILE: go.mod" in result["scope_context"]
	assert "--- FILE: .gitignore" in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_trims_trailing_parenthesis_from_root_level_actionable_files() -> None:
	ledger_text = "\n".join([
		"issue-1\tNEW\t0\tLICENSE):3\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tPERSISTING\t1\tgo.mod):2\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tRESURGENT\t0\t.gitignore):1\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		workspace_files={
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"LICENSE": "test license\n",
			"go.mod": "module example.com/test\n",
			".gitignore": "__pycache__/\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"LICENSE",
		"go.mod",
		".gitignore",
	]
	assert "- LICENSE [ledger:NEW]" in result["scope_summary"]
	assert "- go.mod [ledger:PERSISTING]" in result["scope_summary"]
	assert "- .gitignore [ledger:RESURGENT]" in result["scope_summary"]
	assert "--- FILE: LICENSE" in result["scope_context"]
	assert "--- FILE: go.mod" in result["scope_context"]
	assert "--- FILE: .gitignore" in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_preserves_literal_root_level_trailing_punctuation() -> None:
	ledger_text = "\n".join([
		"issue-1\tNEW\t0\tREADME.:3\tCORRECTNESS & LOGIC\t[]",
		"issue-2\tPERSISTING\t1\tgo.mod.:2\tCORRECTNESS & LOGIC\t[]",
		"issue-3\tRESURGENT\t0\t.env.:1\tCORRECTNESS & LOGIC\t[]",
	]) + "\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		workspace_files={
			"scripts/review_run_reviewers.sh": "scoped shell target\n",
			"README.": "literal trailing dot\n",
			"go.mod.": "module example.com/literal\n",
			".env.": "TOKEN=test\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		"scripts/review_run_reviewers.sh",
		"README.",
		"go.mod.",
		".env.",
	]
	assert "- README. [ledger:NEW]" in result["scope_summary"]
	assert "- go.mod. [ledger:PERSISTING]" in result["scope_summary"]
	assert "- .env. [ledger:RESURGENT]" in result["scope_summary"]
	assert "--- FILE: README." in result["scope_context"]
	assert "--- FILE: go.mod." in result["scope_context"]
	assert "--- FILE: .env." in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_preserves_hidden_directory_prefixes() -> None:
	ledger_text = "issue-1\tNEW\t0\t.github/workflows/review_autofix.yml:3\tCORRECTNESS & LOGIC\t[]\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text=".github/workflows/review_autofix.yml\n.config/tool.toml\n",
		ledger_text=ledger_text,
		workspace_files={
			".github/workflows/review_autofix.yml": "name: review\n",
			".config/tool.toml": "enabled = true\n",
		},
	)

	assert result["scoped_active"] == "true"
	assert result["scope_paths"].splitlines() == [
		".github/workflows/review_autofix.yml",
		".config/tool.toml",
	]
	assert "- .github/workflows/review_autofix.yml [last-run-changed, ledger:NEW]" in result["scope_summary"]
	assert "- .config/tool.toml [last-run-changed]" in result["scope_summary"]
	assert "--- FILE: .github/workflows/review_autofix.yml" in result["scope_context"]
	assert "--- FILE: .config/tool.toml" in result["scope_context"]


def test_reviewer_iteration_scope_prepare_path_reports_missing_targeted_context_helper() -> None:
	ledger_text = "issue-1\tNEW\t0\tscripts/review_run_reviewers.sh:3\tCORRECTNESS & LOGIC\t[]\n"
	result = _run_prepare_reviewer_scope_harness(
		last_run_changed_text="scripts/review_run_reviewers.sh\n",
		ledger_text=ledger_text,
		missing_targeted_script=True,
	)

	assert result["scoped_active"] == "false"
	assert "Reviewer iteration scoping mode: full-diff" in result["scope_summary"]
	assert "Reason: missing targeted_file_context.py" in result["scope_summary"]
	assert result["scope_paths"] == ""
	assert "full change set of the pull request" in result["context_sections"]


def main() -> int:
	test_review_pipeline_knobs_are_wired_into_codex_agent_env()
	test_review_soft_deadline_budget_contract_is_wired()
	test_review_collect_pr_metadata_helper_is_bootstrapped_and_delegated()
	test_collect_pr_check_runs_helper_is_bootstrapped_and_delegated()
	test_collect_pr_check_runs_helper_closes_direct_log_redirect_response()
	test_collect_pr_check_runs_helper_ready_contract_preserves_self_run_exclusion()
	test_collect_pr_check_runs_helper_fail_open_contracts()
	test_collect_pr_check_runs_helper_writer_error_is_observable_and_fail_open()
	test_collect_pr_check_runs_helper_top_level_exception_is_fail_open()
	test_review_collect_pr_metadata_helper_supports_no_pr_synthetic_mode()
	test_review_collect_pr_metadata_helper_skips_optional_pr_reviews_by_default()
	test_review_collect_pr_metadata_helper_fetches_top_level_reviews_when_break_glass_enabled()
	test_review_collect_pr_metadata_helper_fails_open_on_non_array_batch_input()
	test_review_collect_pr_metadata_helper_strict_fallback_drops_bare_mentions()
	test_review_collect_pr_metadata_helper_warns_when_fallback_graphql_returns_errors_without_data()
	test_review_collect_pr_metadata_helper_caps_fallback_graphql_batch_at_twenty_issues()
	test_extract_repo_scoped_issue_refs_rejects_malformed_repository_input()
	test_review_scripts_emit_context_budget_warn_signals()
	test_review_consolidator_prompt_is_staged_for_review_runtime_support()
	test_review_filter_smoke_fixtures_are_present()
	test_reviewer_risk_tier_classifier_honours_thresholds_and_always_full_regex()
	test_review_filter_helper_wiring_is_flag_gated_and_fail_open()
	test_agents_md_materiality_classifier_and_workflow_wiring()
	test_reviewer_failback_wiring_stages_asset_and_restores_cache_before_reviewers()
	test_reviewer_failback_harness_reuses_cached_open_state_and_skips_unmapped_models()
	test_stall_guard_retryable_failures_log_deterministic_reviewer_advance()
	test_silent_retry_exhaustion_logs_terminal_failure_reason()
	test_reviewer_soft_deadline_fallback_requests_partial_finalize_and_exits_green()
	test_reviewer_health_dispatch_logs_to_stderr_only()
	test_reviewer_zero_success_guard_fails_open_when_every_review_slot_was_skipped()
	test_reviewer_filter_harness_strips_low_signal_paths_and_preserves_exemptions()
	test_reviewer_filter_script_preserves_nested_exempt_paths()
	test_reviewer_filter_script_preserves_root_level_migration_exempt_paths()
	test_reviewer_filter_script_strips_deleted_generated_file_when_workspace_copy_is_missing()
	test_reviewer_filter_script_keeps_deleted_file_when_first_hunk_starts_later()
	test_reviewer_filter_script_ignores_later_hunk_marker_when_first_hunk_has_no_marker()
	test_reviewer_filter_script_keeps_existing_file_with_marker_on_line_six()
	test_reviewer_filter_script_keeps_deleted_file_with_marker_on_line_six()
	test_reviewer_filter_script_ignores_existing_file_marker_beyond_header_lines()
	test_reviewer_filter_script_ignores_deleted_file_marker_beyond_header_lines()
	test_reviewer_filter_harness_fails_open_when_disabled_missing_or_failing()
	test_reviewer_filter_stat_harness_handles_brace_expansion_renames()
	test_reject_verifier_bootstrap_and_stage_order_contract()
	test_render_prompt_py_is_main_primary_so_validator_fixes_reach_wedged_branches()
	test_support_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_editor_changes_lost_redispatch_matches_post_commit_fallback_chain()
	test_review_pipeline_summary_step_is_local_only_and_grep_friendly()
	test_review_pipeline_summary_reports_stall_recovery_for_retried_and_skipped_slots()
	test_review_pipeline_summary_reports_partial_finalize_validated_push()
	test_review_pipeline_summary_reports_partial_finalize_withheld_for_safety()
	test_review_partial_finalize_publish_safety_gate_is_wired()
	test_review_partial_finalize_timeout_extractor_handles_structured_yaml_layout()
	test_review_partial_finalize_publish_safety_gate_keeps_validated_path_when_budget_remains()
	test_review_partial_finalize_publish_safety_gate_prefers_codex_budget_start_epoch_when_withholding()
	test_review_partial_finalize_workflow_path_is_wired()
	test_review_partial_finalize_comment_and_marker_report_validated_push_state()
	test_review_partial_finalize_comment_and_marker_report_withheld_state()
	test_same_head_partial_resume_restore_prefers_latest_matching_marker_and_ignores_other_heads()
	test_same_head_partial_resume_restore_fails_open_when_no_marker_matches_head()
	test_reviewer_resume_reuses_cached_successes_and_reruns_only_incomplete_slots()
	test_review_partial_resume_path_does_not_workflow_dispatch_without_new_push()
	test_review_partial_finalize_marker_sets_no_progress_terminal_state()
	test_review_partial_finalize_marker_sets_round_budget_exhausted_terminal_state()
	test_same_head_partial_resume_restore_rehydrates_cached_artifacts_and_preserves_no_progress_across_run_dirs()
	test_review_pipeline_slop_scan_wiring_is_flagged_fail_open_and_pre_commit_cleaned()
	test_reviewer_and_consolidator_slop_scan_context_is_wired()
	test_review_tier_resolver_routes_lite_standard_and_full_and_handles_overrides()
	test_auto_merge_guard_honours_configured_orchestrator_branch_pattern()
	test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_codex_agent_path()
	test_auto_merge_guard_suppresses_forward_merge_fallback_pr_on_deterministic_skip_path()
	test_gate_emits_head_ref_output_for_forward_merge_suppressor_reuse()
	test_gate_emits_force_full_review_tier_output_for_phase_i_resolver()
	test_reviewer_prompt_output_rules_still_forbid_scripts()
	test_reviewer_iteration_scope_first_iteration_keeps_full_diff_context()
	test_reviewer_iteration_scope_valid_artifacts_narrow_to_last_run_and_actionable_ledger_files()
	test_reviewer_iteration_scope_fails_open_on_bad_scope_artifacts()
	test_reviewer_iteration_scope_uses_targeted_context_helper_and_scoped_semble_labels()
	test_reviewer_iteration_scope_prepare_path_accepts_root_level_actionable_files()
	test_reviewer_iteration_scope_prepare_path_trims_trailing_parenthesis_from_root_level_actionable_files()
	test_reviewer_iteration_scope_prepare_path_preserves_literal_root_level_trailing_punctuation()
	test_reviewer_iteration_scope_prepare_path_preserves_hidden_directory_prefixes()
	test_reviewer_iteration_scope_prepare_path_reports_missing_targeted_context_helper()
	print("OK: review_autofix review-pipeline plumbing contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
