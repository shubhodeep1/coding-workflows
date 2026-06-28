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
from pathlib import Path


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

		result = subprocess.run(
			[
				"bash",
				"-c",
				"set -euo pipefail\n"
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


def test_review_pipeline_knobs_are_wired_into_codex_agent_env() -> None:
	workflow = _workflow_text()
	for expected in (
		"REVIEW_FLOOR_RULES_ENABLED: ${{ vars.REVIEW_FLOOR_RULES_ENABLED || '1' }}",
		"REVIEW_FLOOR_KEYWORDS_FILE: ${{ vars.REVIEW_FLOOR_KEYWORDS_FILE || '' }}",
		"REVIEW_CONSOLIDATOR_ENABLED: ${{ vars.REVIEW_CONSOLIDATOR_ENABLED || '1' }}",
		"REVIEW_CONSOLIDATOR_MODEL: ${{ vars.REVIEW_CONSOLIDATOR_MODEL || 'openai/gpt-5.4' }}",
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
		"REVIEWER_HEALTH_OPEN_THRESHOLD: ${{ vars.REVIEWER_HEALTH_OPEN_THRESHOLD || '3' }}",
		"REVIEWER_HEALTH_OPEN_TTL_SECS: ${{ vars.REVIEWER_HEALTH_OPEN_TTL_SECS || '1800' }}",
		"CONTEXT_BUDGET_WARN_RATIO: ${{ vars.CONTEXT_BUDGET_WARN_RATIO || '0.7' }}",
		"MAX_PROMPT_TOKENS_FOR_PHASE: ${{ vars.MAX_PROMPT_TOKENS_FOR_PHASE || '' }}",
		"CODEX_HEARTBEAT_ENABLED: ${{ vars.CODEX_HEARTBEAT_ENABLED || '1' }}",
		"CODEX_HEARTBEAT_INTERVAL_SECS: ${{ vars.CODEX_HEARTBEAT_INTERVAL_SECS || '30' }}",
		"REVIEWER_FILTER_UNINTERESTING_ENABLED: ${{ vars.REVIEWER_FILTER_UNINTERESTING_ENABLED || 'false' }}",
		"REVIEWER_FILTER_EXTRA_GLOBS: ${{ vars.REVIEWER_FILTER_EXTRA_GLOBS || '' }}",
		"REVIEWER_FILTER_EXEMPT_GLOBS: ${{ vars.REVIEWER_FILTER_EXEMPT_GLOBS || 'db/contracts/**,**/migrations/**,**/migrate/**' }}",
		"SLOP_SCAN_ENABLED: ${{ vars.SLOP_SCAN_ENABLED || 'true' }}",
		"AGENTS_MD_MATERIALITY_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_ENABLED || '1' }}",
		"AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED || '0' }}",
		"AGENTS_MD_MATERIALITY_MODEL: ${{ vars.AGENTS_MD_MATERIALITY_MODEL || 'openai/gpt-5.4-mini' }}",
		"AGENTS_MD_MATERIALITY_REASONING: ${{ vars.AGENTS_MD_MATERIALITY_REASONING || 'medium' }}",
		"REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED: ${{ vars.REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED || 'true' }}",
	):
		assert expected in workflow, f"Missing codex-agent env wiring: {expected}"

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
		"REVIEWER_HEALTH_OPEN_THRESHOLD: ${{ vars.REVIEWER_HEALTH_OPEN_THRESHOLD || '3' }}",
		"REVIEWER_HEALTH_OPEN_TTL_SECS: ${{ vars.REVIEWER_HEALTH_OPEN_TTL_SECS || '1800' }}",
		"AGENTS_MD_MATERIALITY_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_ENABLED || '1' }}",
		"AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED || '0' }}",
		"AGENTS_MD_MATERIALITY_MODEL: ${{ vars.AGENTS_MD_MATERIALITY_MODEL || 'openai/gpt-5.4-mini' }}",
		"AGENTS_MD_MATERIALITY_REASONING: ${{ vars.AGENTS_MD_MATERIALITY_REASONING || 'medium' }}",
		"REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED: ${{ vars.REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED || 'true' }}",
	):
		assert workflow.count(expected) >= 2, f"Missing workflow-level + codex-agent env wiring: {expected}"


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


def test_support_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets() -> None:
	stage_helper = _stage_helper_text()
	assert "validation_history.v1.json" in stage_helper
	assert "operator_bypass_audit.v1.json" in stage_helper
	assert "revalidate_events.v1.json" in stage_helper


def test_review_pipeline_summary_step_is_local_only_and_grep_friendly() -> None:
	block = _step_block("Append review pipeline iteration summary")
	assert "### Review Pipeline — Iteration ${iteration_label}" in block
	assert "reviewer_scope_label=\"full-diff\"" in block, (
		"Summary step must not overclaim scoped reviewer behaviour before "
		"review_run_reviewers.sh consumes REVIEW_REVIEWER_ITERATION_SCOPING"
	)
	for expected in (
		"| Reviewers run | ${reviewers_run} |",
		"| Reviewer scope | ${reviewer_scope_label} |",
		"| Raw bundle size (bytes) | ${bundle_bytes} |",
		"| Floor tags | ${floor_tag_count} |",
		"| Consolidator model | ${REVIEW_CONSOLIDATOR_MODEL:-openai/gpt-5.4} |",
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
	):
		assert expected in block, f"Missing summary row contract: {expected}"
	for artifact in (
		"${RUNTIME_DIR}/reviewer_bundle.txt",
		"${RUNTIME_DIR}/floor_tags.txt",
		"${RUNTIME_DIR}/consolidator_raw.txt",
		"${RUNTIME_DIR}/parser_stats.txt",
		"${RUNTIME_DIR}/ledger_status.txt",
		"grep -c 'CONSOLIDATOR_OVERRIDDEN:' \"${EDITOR_SUMMARY_FILE}\"",
		"EDITOR_COMMIT_PRODUCED: ${{ steps.commit_changes.outputs.did_commit }}",
	):
		assert artifact in block, f"Summary step is missing local metric source: {artifact}"
	assert "gh api" not in block
	assert "gh_retry" not in block
	assert "curl https://api.github.com" not in block


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
	assert "POST_MERGE_PR_TEXT_JSON: ${{ needs.gate.outputs.post_merge_pr_text_json }}" in wf, (
		"post-merge validate dispatch must consume cached PR text from gate outputs"
	)
	assert "POST_MERGE_LINKED_ISSUES_JSON: ${{ needs.gate.outputs.post_merge_linked_issues_json }}" in wf, (
		"post-merge validate dispatch must consume cached linked-issue labels from gate outputs"
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
	test_support_ai_memory_schema_bootstrap_includes_revalidate_lifecycle_assets()
	test_review_pipeline_summary_step_is_local_only_and_grep_friendly()
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
