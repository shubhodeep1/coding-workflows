#!/usr/bin/env python3
"""Deterministic tests for orchestrate_poll_process.sh validation state handling."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

# Upper bound for a single poller invocation under test. The mocked poller
# should complete in a few seconds; anything longer indicates a hang (e.g. an
# infinite loop in the script-under-test or a subprocess the mock never
# releases). Bounding the wait lets a hang surface as a test failure with
# captured output in a few minutes, instead of silently stalling the CI job
# until the workflow-level `timeout-minutes` kills it.
POLLER_SUBPROCESS_TIMEOUT_SEC = 180.0

_TEST_RUNNER_EVENT_PREFIX = "TEST_CASE_EVENT: "
_TEST_RUNNER_HEARTBEAT_INTERVAL_SEC = 60.0
_TEST_RUNNER_HEARTBEAT_THREAD_PREFIX = "orchestrate-test-heartbeat:"
_TEST_RUNNER_SLOWEST_LIMIT = 10
_TEST_RUNNER_OUTPUT_LOCK = threading.Lock()


# Directories and top-level files that the poller script under test needs
# to resolve at runtime via relative paths (helper scripts, prompt files,
# schema JSON, canonical instruction markdown). Each sandbox run gets an
# isolated copy of these so any file mutations the script performs are
# confined to the sandbox and cannot reach the real coding-workflows
# checkout the test runner started from.
_SANDBOX_DIRS = ("scripts", "prompts", ".github/ai", "ai-memory")
_SANDBOX_FILES = (
	"agents.md",
	"unattended_system_instructions.md",
	"ai_pipeline.md",
)


def _git_test_env() -> dict[str, str]:
	"""Return a subprocess env without repo-scoped git/worktree overrides."""
	env = os.environ.copy()
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(key, None)
	return env


def _make_poller_sandbox(target: Path) -> None:
	"""Populate ``target`` with a minimal copy of the coding-workflows tree
	the poller expects at runtime and initialize a throwaway git repo
	inside it.

	The sandbox contains **real copies**, not symlinks, so ``rm``/``git
	rm``/``rm -rf`` operations the script performs touch only the
	sandbox. The sandbox's git origin is deliberately set to a URL that
	does **not** match ``*/coding-workflows`` so the poller's
	consumer-repo cleanup path is exercised against throwaway copies —
	the real checkout is never at risk regardless of how that cleanup
	path is gated in a future revision.

	The fixture also creates a deterministic integration branch
	(``orchestrator/project-192``) containing
	``.orchestrator_judge_context_sentinel.txt`` so tests can prove judge
	context came from integration branch state rather than default-branch
	state.
	"""
	git_env = _git_test_env()
	for rel in _SANDBOX_DIRS:
		src = REPO_ROOT / rel
		if src.exists():
			shutil.copytree(src, target / rel)
	for rel in _SANDBOX_FILES:
		src = REPO_ROOT / rel
		if src.exists():
			dst = target / rel
			dst.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(src, dst)
	subprocess.run(
		["git", "init", "--quiet", str(target)],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "config", "user.email", "sandbox@example.com"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "config", "user.name", "Poller Sandbox"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	# Defensively neutralise commit signing for the throwaway repo. Some
	# CI/dev environments install a global commit.gpgsign hook (or a
	# git-credential signing helper) that blocks `git commit` with a
	# non-trivial exit code unrelated to the test itself. The sandbox
	# repo never leaves the tempdir, so signing has no security value
	# here and an unsigned commit is identical to the signed one for
	# every assertion the suite makes.
	for _key in ("commit.gpgsign", "commit.gpgSign", "tag.gpgsign", "tag.gpgSign"):
			subprocess.run(
				["git", "-C", str(target), "config", _key, "false"],
				check=False,
				env=git_env,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
			)
	subprocess.run(
		["git", "-C", str(target), "checkout", "-B", "main"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "add", "-A"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		[
			"git", "-C", str(target),
			"-c", "commit.gpgsign=false",
			"-c", "commit.gpgSign=false",
			"commit", "--allow-empty", "-m", "sandbox init", "--quiet",
		],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "checkout", "-B", "orchestrator/project-192"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	(target / ".orchestrator_judge_context_sentinel.txt").write_text(
		"integration-branch-only-symbol\n",
		encoding="utf-8",
	)
	subprocess.run(
		["git", "-C", str(target), "add", ".orchestrator_judge_context_sentinel.txt"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "commit", "-m", "integration sentinel", "--quiet"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "checkout", "main"],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		[
			"git", "-C", str(target), "remote", "add",
			"origin", "https://github.com/test-harness/poller-sandbox.git",
		],
		check=True,
		env=git_env,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
def _rewrite_cmd_for_sandbox(cmd: list, sandbox: Path) -> list:
	"""Rewrite any command-line argument that is an absolute path under
	``REPO_ROOT`` so it resolves to the equivalent path inside
	``sandbox``. Leaves all other arguments untouched.

	Existing callers pass ``str(POLLER_SCRIPT)`` as the script to exec,
	which is an absolute path into ``REPO_ROOT/scripts``; after rewrite
	they exec the sandbox's copy instead.
	"""
	rewritten: list = []
	repo_root_resolved = REPO_ROOT.resolve()
	for arg in cmd:
		if isinstance(arg, (str, os.PathLike)):
			arg_str = os.fspath(arg)
			if os.path.isabs(arg_str):
				try:
					rel = Path(arg_str).resolve().relative_to(repo_root_resolved)
					rewritten.append(str(sandbox / rel))
					continue
				except (OSError, ValueError):
					pass
			rewritten.append(arg_str)
		else:
			rewritten.append(arg)
	return rewritten


def _run_poller_subprocess(
	cmd: list,
	*,
	cwd: str | None = None,
	env: dict,
	sandbox: Path | None = None,
) -> subprocess.CompletedProcess:
	"""Run the poller under test in an isolated sandbox directory with a
	bounded timeout, reaping the whole process group on timeout.

	Previously this helper ran the poller with ``cwd=REPO_ROOT`` so the
	script could find its sibling helper scripts under the real
	coding-workflows checkout. That made the real working tree the
	blast radius of any destructive code path the script happened to
	execute — and in fact did so destructively at least twice (PRs
	#917/#931), where a consumer-repo cleanup block gated on
	``GITHUB_REPOSITORY`` fired while pytest was running from the real
	checkout and deleted ~28 tracked source files. The fix in the
	poller itself (switch to a git-remote-URL gate) closes that
	specific hole, but sandboxing the subprocess here is the
	defense-in-depth layer: any future destructive path, regardless of
	how it is gated, can only ever touch files inside a throwaway
	tempdir.

	The poller spawns a tree of bash/sleep helpers. Using
	``start_new_session`` puts every descendant in its own process
	group so a timeout can kill the entire tree via ``killpg`` rather
	than leaking orphan children that keep the CI runner alive.

	The ``cwd`` argument is accepted for source-level compatibility
	with earlier call sites but its value is ignored — the sandbox
	directory always wins as the cwd. Tests that need to inspect
	sandbox state after the run may pre-stage and pass a ``sandbox``
	path; otherwise one is auto-created per call and removed when the
	call returns.
	"""
	del cwd  # explicit: sandbox always overrides caller cwd
	owns_sandbox = False
	if sandbox is None:
		sandbox = Path(tempfile.mkdtemp(prefix="poller-sandbox-"))
		owns_sandbox = True
		_make_poller_sandbox(sandbox)
	try:
		env = dict(env)
		# The review-autofix runner injects a BASH_ENV helper that cd's every
		# bash subprocess back to WORKSPACE_PATH. Sandbox-based poller tests
		# rely on cwd=str(sandbox), so strip that hook (and its companion
		# workspace vars) before launching the child process.
		env.pop("BASH_ENV", None)
		env.pop("ENV", None)
		env.pop("WORKSPACE_PATH", None)
		env["PWD"] = str(sandbox)
		env.pop("OLDPWD", None)
		cmd = _rewrite_cmd_for_sandbox(cmd, sandbox)
		proc = subprocess.Popen(
			cmd,
			cwd=str(sandbox),
			env=env,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			start_new_session=True,
		)
		try:
			stdout, stderr = proc.communicate(timeout=POLLER_SUBPROCESS_TIMEOUT_SEC)
		except subprocess.TimeoutExpired:
			# Kill the entire process group, not just the direct child, so the
			# bash -> bash -> sleep descendants observed in hung jobs get reaped.
			try:
				os.killpg(proc.pid, signal.SIGKILL)
			except ProcessLookupError:
				pass
			try:
				stdout, stderr = proc.communicate(timeout=5)
			except subprocess.TimeoutExpired:
				stdout, stderr = "", ""
			raise AssertionError(
				"poller did not exit within "
				f"{POLLER_SUBPROCESS_TIMEOUT_SEC:.0f}s; process group killed\n"
				f"stdout:\n{stdout}\n"
				f"stderr:\n{stderr}"
			)
		return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
	finally:
		if owns_sandbox:
			shutil.rmtree(sandbox, ignore_errors=True)


def test_judge_reasoning_effort_uses_configured_value_without_downgrade():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert 'JUDGE_INVOCATION_CYCLE=$((JUDGE_CYCLE + 1))' in script
	# No adaptive downgrade — configured effort is used as-is for all cycles.
	assert 'EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE' not in script
	# Per PR #2196 the codex config is written via the shared helper
	# scripts/write_codex_config.sh; the configured judge reasoning
	# effort flows through the helper's --reasoning arg (which becomes
	# `model_reasoning_effort = "..."` in the emitted TOML — see
	# tests/test_write_codex_config.py for that contract). Pin the
	# call-site shape so an accidental refactor that drops the
	# `${MODEL_REASONING_EFFORT_JUDGE:-xhigh}` substitution and silently
	# swaps in another env var is caught here.
	assert '--reasoning "${MODEL_REASONING_EFFORT_JUDGE:-xhigh}"' in script


def test_parameterized_search_issues_calls_pin_get_only_on_targeted_poller_paths():
	poller_source_text = POLLER_SCRIPT.read_text(encoding="utf-8")
	parameterized_search_calls = [
		line.strip()
		for line in poller_source_text.splitlines()
		if "gh_retry gh api" in line and '"search/issues"' in line
	]
	explicit_get_api_lines = [
		line.strip()
		for line in poller_source_text.splitlines()
		if "gh_retry gh api --method GET" in line
	]

	assert len(parameterized_search_calls) == 4
	assert all("--method GET" in call for call in parameterized_search_calls)
	assert sum("--paginate" in call for call in parameterized_search_calls) == 2
	assert explicit_get_api_lines == parameterized_search_calls

	# Preserve the two marker-search fallbacks and their paginated aggregation.
	assert '-f per_page=100 -f q="${q_state}"' in poller_source_text
	assert '-f per_page=100 -f q="${q_clarify}"' in poller_source_text
	assert "jq -s '[.[].items[]? | {number}] | unique_by(.number)' 2>/dev/null || echo '[]'" in poller_source_text

	# Preserve reconstruction's fail-open default and deferred creation's
	# fail-closed conditional while pinning only their HTTP method.
	assert '-f q="repo:${GITHUB_REPOSITORY} \\"Tracking issue: #${TRACKING_NUM}\\" in:body"' in poller_source_text
	assert "--jq '.items // []' 2>/dev/null || echo '[]')\"" in poller_source_text
	assert '-f q="repo:${GITHUB_REPOSITORY} is:issue \\"Tracking issue: #${TRACKING_NUM}\\" in:body"' in poller_source_text
	assert "--jq '.items // []' 2>/dev/null)\"; then" in poller_source_text


def test_judge_context_issue_numbers_are_normalized_without_globbing():
	poller_source_text = POLLER_SCRIPT.read_text(encoding="utf-8")
	block_start_marker = '  MERGED_PR_SUMMARIES=""\n  OPEN_PR_SUMMARIES=""\n'
	block_end_marker = '  unset _sorted_issue_nums _issue_status\n'
	block_start_index = poller_source_text.index(block_start_marker)
	block_end_index = poller_source_text.index(block_end_marker, block_start_index)
	production_block = poller_source_text[
		block_start_index:block_end_index + len(block_end_marker)
	]

	with tempfile.TemporaryDirectory(prefix="judge-issue-number-normalization-") as tmp:
		worktree = Path(tmp)
		# Numeric filenames make both `*` and `[0-9]` expose any regression to
		# unquoted shell expansion in the production block.
		(worktree / "3").touch()
		(worktree / "17").touch()
		runner = worktree / "run-normalization.sh"
		runner.write_text(
			"#!/usr/bin/env bash\n"
			"set -euo pipefail\n"
			"LOOKUP_LOG=\"${1}\"\n"
			"ISSUE_NUMS=\"${2-}\"\n"
			"_issue_cross_ref_pr_number_last()\n"
			"{\n"
			"  printf '%s\\n' \"${1}\" >> \"${LOOKUP_LOG}\"\n"
			"}\n"
			f"{production_block}",
			encoding="utf-8",
		)
		runner_env = os.environ.copy()
		for inherited_name in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
			runner_env.pop(inherited_name, None)
		cases = (
			("mixed", "20 3\n20\t11", ["3", "11", "20"]),
			("empty", "", []),
			("whitespace", " \n\t ", []),
			("glob-garbage", "8 * [0-9] 12x 2?", ["8"]),
		)
		for case_name, raw_issue_numbers, expected_lookups in cases:
			lookup_log = worktree / f"{case_name}.log"
			result = subprocess.run(
				["bash", str(runner), str(lookup_log), raw_issue_numbers],
				cwd=worktree,
				env=runner_env,
				capture_output=True,
				text=True,
				check=False,
			)
			assert result.returncode == 0, (
				f"{case_name} normalization failed: stderr={result.stderr!r}"
			)
			actual_lookups = (
				lookup_log.read_text(encoding="utf-8").splitlines()
				if lookup_log.exists()
				else []
			)
			assert actual_lookups == expected_lookups, case_name


def _base_state(status: str = "in_progress") -> dict:
	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 1,
		"total_waves": 1,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": status,
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "pending"},
				],
			}
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
		"integration_branch": "",
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
	}


def _validation_history_payload(*, integration_sha: str, entries: list[dict]) -> dict:
	return {
		"schema_version": "v1",
		"repository": "owner/repo",
		"integration_sha": integration_sha,
		"entries": entries,
	}


def _operator_bypass_audit_payload(*, tracking_issue_number: int, integration_sha: str, entries: list[dict]) -> dict:
	return {
		"schema_version": "v1",
		"repository": "owner/repo",
		"tracking_issue_number": tracking_issue_number,
		"integration_sha": integration_sha,
		"entries": entries,
	}


def _state_comment(state: dict) -> str:
	# V1 framing is intentionally retained for test SEED data.  The
	# production reader handles the V2 → V1 fallback path, so seeding
	# initial state as V1 still works after the V2 writer change.  The
	# extractor helpers below transparently read either V1 or V2 so the
	# rest of the test suite does not need to change.
	return "<!-- ORCHESTRATOR_STATE_V1\n" + json.dumps(state) + "\nORCHESTRATOR_STATE_V1 -->"


def _extract_latest_standalone_state(comments: list[dict]) -> dict | None:
	for comment in reversed(comments):
		body = str((comment or {}).get("body", ""))
		open_marker = "<!-- AI_STANDALONE_STALL_STATE_V1\n"
		close_marker = "\nAI_STANDALONE_STALL_STATE_V1 -->"
		if not body.startswith(open_marker) or not body.endswith(close_marker):
			continue
		payload = body[len(open_marker):-len(close_marker)]
		return json.loads(payload)
	return None


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


# V2 framing parsing — keep in sync with scripts/orchestrate_state_v2.py.
# Tests cannot import the helper module directly because it lives outside
# `tests/` and the test runner is invoked as a flat script; reimplementing
# the parse loop inline avoids adding a sys.path hack.
_V2_OPENER_RE = re.compile(
	r"^<!-- ORCHESTRATOR_STATE_V2 part=(\d+)/(\d+) manifest=([0-9a-f]{64}) -->$",
	re.MULTILINE,
)
# Mirror MAX_CHUNKS_PER_MANIFEST from scripts/orchestrate_state_v2.py so
# the test parser rejects the same forged-large `total` values that
# production rejects.  Drift here would create a test/prod parity gap
# where oversized fixtures parse green in CI but never see production.
_V2_MAX_CHUNKS_PER_MANIFEST = 1024
_V2_CLOSER = "ORCHESTRATOR_STATE_V2 -->"


_V1_STATE_OPENER_RE = re.compile(r"^<!-- ORCHESTRATOR_STATE_V1$", re.MULTILINE)


def _is_state_comment(body: str) -> bool:
	"""True if the body carries a V1 single-comment state OR a V2 chunk.

	Matches the framing markers anchored to start-of-line (V1: `<!-- ORCHESTRATOR_STATE_V1`,
	V2: the V2 opener regex) instead of plain substring containment so a
	tracking-comment fixture that merely mentions the marker text in prose
	(e.g. an operator quoting the framing in a comment for context) is not
	misclassified as a state comment and filtered out of `tracking_comments`.
	"""
	if _V1_STATE_OPENER_RE.search(body):
		return True
	if _V2_OPENER_RE.search(body):
		return True
	return False


def _parse_v2_chunk(body: str) -> tuple[int, int, str, str] | None:
	m = _V2_OPENER_RE.search(body)
	if m is None:
		return None
	part, total, manifest = int(m.group(1)), int(m.group(2)), m.group(3)
	if part < 1 or total < 1 or part > total:
		return None
	if total > _V2_MAX_CHUNKS_PER_MANIFEST:
		return None
	tail = body[m.end():]
	if tail.startswith("\n"):
		tail = tail[1:]
	closer_marker = "\n" + _V2_CLOSER
	end = tail.rfind(closer_marker)
	if end >= 0:
		chunk = tail[:end]
	elif tail.startswith(_V2_CLOSER):
		chunk = ""
	else:
		return None
	return part, total, manifest, chunk


def _build_v2_state_comment_chain(payload: str, *, chunk_size: int) -> list[dict[str, str]]:
	raw = payload.encode("utf-8")
	encoded = base64.b64encode(raw).decode("ascii")
	manifest = hashlib.sha256(raw).hexdigest()
	total = max(1, (len(encoded) + chunk_size - 1) // chunk_size)
	comments: list[dict[str, str]] = []
	for idx in range(total):
		chunk = encoded[idx * chunk_size : (idx + 1) * chunk_size]
		comments.append({
			"body": (
				f"<!-- ORCHESTRATOR_STATE_V2 part={idx + 1}/{total} manifest={manifest} -->\n"
				f"{chunk}\n{_V2_CLOSER}"
			),
		})
	return comments


def _extract_state_payloads(comments: list[dict]) -> list[str]:
	"""All orchestrator-state JSON payloads in chronological (oldest-first) order.

	Walks V2 comments newest-first to mirror scripts/orchestrate_state_v2.py,
	but records each recovered chain at the index of that chain's newest
	comment so the combined V2 + V1 payload list still stays in
	chronological order and preserves the `payloads[-1]` latest-state
	test contract.
	"""
	payloads: list[tuple[int, str]] = []
	v2_candidates: dict[tuple[str, int], dict[str, object]] = {}
	for idx, comment in reversed(list(enumerate(comments))):
		body = (comment or {}).get("body") or ""
		if "ORCHESTRATOR_STATE_V2" in body:
			parsed = _parse_v2_chunk(body)
			if parsed is not None:
				part, total, manifest, chunk = parsed
				chain_key = (manifest, total)
				candidate = v2_candidates.get(chain_key)
				if part == total:
					candidate = {
						"latest_idx": idx,
						"next_part": total - 1,
						"parts": {part: chunk},
					}
					v2_candidates[chain_key] = candidate
				elif candidate is not None and part == candidate.get("next_part"):
					parts = candidate["parts"]
					assert isinstance(parts, dict)
					parts[part] = chunk
					candidate["next_part"] = part - 1
				else:
					candidate = None
				if candidate is not None:
					parts = candidate["parts"]
					assert isinstance(parts, dict)
					if len(parts) == total and candidate.get("next_part") == 0:
						stitched_b64 = "".join(parts[p] for p in range(1, total + 1))
						compact = re.sub(r"\s+", "", stitched_b64)
						try:
							decoded = base64.b64decode(compact, validate=True)
						except (binascii.Error, ValueError):
							v2_candidates.pop(chain_key, None)
						else:
							if hashlib.sha256(decoded).hexdigest() == manifest:
								payloads.append((int(candidate["latest_idx"]), decoded.decode("utf-8")))
								v2_candidates.pop(chain_key, None)
							else:
								v2_candidates.pop(chain_key, None)
		if "ORCHESTRATOR_STATE_V1" in body:
			match = re.search(r"<!-- ORCHESTRATOR_STATE_V1\n(.*?)\nORCHESTRATOR_STATE_V1 -->", body, flags=re.S)
			if match:
				payloads.append((idx, match.group(1)))
	return [payload for _, payload in sorted(payloads)]


def _extract_latest_state(comments: list[dict]) -> dict:
	# Walk newest-first through the chronologically-ordered payload list
	# returned by `_extract_state_payloads` and return the first one that
	# parses as JSON.  Skipping malformed payloads matches the
	# production reader's "newest VALID state wins" semantic — a
	# truncated state-comment body should not crash callers when an
	# older valid one is also present.
	for raw in reversed(_extract_state_payloads(comments)):
		try:
			return json.loads(raw)
		except json.JSONDecodeError:
			continue
	raise AssertionError("No valid orchestrator state comment (V1 or V2) found")


def _read_task_files(sandbox: Path) -> dict[str, dict]:
	tasks_root = sandbox / ".tasks"
	if not tasks_root.is_dir():
		return {}

	task_files: dict[str, dict] = {}
	for task_file in sorted(tasks_root.glob("*/*.json")):
		relative_path = task_file.relative_to(tasks_root).as_posix()
		task_files[relative_path] = json.loads(task_file.read_text(encoding="utf-8"))
	return task_files


def _run_poller(
	*,
	state: dict,
	enable_validation: str,
	max_validate_cycles: str,
	tracking_labels: list[str] | None = None,
	tracking_comments: list[str | dict] | None = None,
	tracking_body: str | None = None,
	issue_labels: dict[int, list[str]] | None = None,
	issue_comments: dict[int, list[str | dict]] | None = None,
	issue_bodies: dict[int, str] | None = None,
	issue_events: dict[int, list[dict]] | None = None,
	gql_mode: str = "full",
	gql_labels: dict[int, list[str]] | None = None,
	codex_json: dict | None = None,
	fail_validation_dispatch: bool = False,
	fail_release_dispatch: bool = False,
	fail_search_issues: bool = False,
	search_issue_items: list[dict] | None = None,
	prs: list[dict] | None = None,
	pr_api_sequence: dict[int, list[dict]] | None = None,
	existing_branches: list[str] | None = None,
	merge_conflict_on_sync: bool = False,
	blocked_check_shas: list[str] | None = None,
	validation_workflow_runs: list[dict] | None = None,
	issue_closed: dict[int, bool] | None = None,
	issue_linked_prs: dict[int, int] | None = None,
	merge_tree_conflict_paths: list[str] | None = None,
	timeline_fail_for_issues: list[int] | None = None,
	update_branch_fail_for_prs: list[int] | None = None,
	review_dispatch_fail_for_prs: list[int] | None = None,
	active_autofix_runs: list[dict] | None = None,
	label_create_responses: dict[str, dict] | None = None,
	mock_stall_judge_json: dict | None = None,
	fail_issue_comment_get_after: dict[int, int] | None = None,
	fail_issue_get_for: list[int] | None = None,
	fail_issue_edit_for: list[int] | None = None,
	fail_branch_ref_after: dict[str, int] | None = None,
	fail_branch_ref_not_found_after: dict[str, int] | None = None,
	mock_actions_runs_cache_get_json: dict | None = None,
	mock_actions_runs_cache_put_json: dict | None = None,
	actions_runs_workflow_runs: list[dict] | None = None,
	actions_runs_status: int = 200,
	actions_runs_status_sequence: list[int] | None = None,
	actions_runs_etag: str = '"etag-initial"',
	mock_branch_rebuild_audit_payload: dict | None = None,
	mock_branch_rebuild_audit_get_json: dict | None = None,
	mock_branch_rebuild_audit_put_json: dict | None = None,
	branch_protected_branches: dict[str, bool] | None = None,
	branch_ref_shas: dict[str, str] | None = None,
	mock_git_fetch_fail_after: dict[str, int] | None = None,
	branch_rebuild_enabled: str = "false",
	branch_rebuild_threshold_hours: str = "24",
	branch_rebuild_cooldown_hours: str = "48",
	codex_touch_file: str | None = None,
	mock_orch_state_v2_pack_mode: str | None = None,
	mock_git_push_success: bool = False,
	mock_git_checkout_fail: bool = False,
	enable_stall_judge: str = "true",
	stall_judge_trigger_count: str = "2",
	enable_stall_human_terminalization: str = "false",
	enable_clean_wave_judge_skip: str = "true",
	judge_repeat_fingerprint_max: str = "2",
	mock_gh_issue_list_label_filter: bool = False,
	compare_ahead_by: int = 0,
	compare_ahead_by_sequence: list[int] | None = None,
	compare_ahead_by_force_error: bool = False,
	mock_validation_history_payload: dict | None = None,
	mock_validation_history_get_exit_code: int = 0,
	mock_validation_history_get_json: dict | None = None,
	mock_validation_history_append_exit_code: int = 0,
	mock_validation_history_append_json: dict | None = None,
	mock_operator_bypass_audit_payload: dict | None = None,
	mock_operator_bypass_audit_get_exit_code: int = 0,
	mock_operator_bypass_audit_get_json: dict | None = None,
	mock_operator_bypass_audit_append_exit_code: int = 0,
	mock_operator_bypass_audit_append_json: dict | None = None,
	mock_revalidate_events_payload: dict | None = None,
	mock_pr_create_race_pr: dict | None = None,
	mock_pr_ready_exit_code: int = 0,
	env_overrides: dict[str, str] | None = None,
) -> dict:
	tracking_num = 192
	tracking_labels = tracking_labels or []
	tracking_comments = tracking_comments or []
	if issue_labels is None:
		issue_labels = {10: ["ai:merged"]}
	issue_comments = issue_comments or {}
	issue_bodies = issue_bodies or {}
	issue_events = issue_events or {}
	gql_labels = gql_labels or {}
	prs = prs or []
	pr_api_sequence = pr_api_sequence or {}
	existing_branches = existing_branches or ["main"]
	blocked_check_shas = blocked_check_shas or []
	validation_workflow_runs = validation_workflow_runs or []
	issue_closed = issue_closed or {}
	issue_linked_prs = issue_linked_prs or {}
	merge_tree_conflict_paths = merge_tree_conflict_paths or []
	issue_comments = issue_comments or {}
	timeline_fail_for_issues = timeline_fail_for_issues or []
	update_branch_fail_for_prs = update_branch_fail_for_prs or []
	review_dispatch_fail_for_prs = review_dispatch_fail_for_prs or []
	active_autofix_runs = active_autofix_runs or []
	label_create_responses = label_create_responses or {}
	mock_stall_judge_json = mock_stall_judge_json or {}
	fail_issue_comment_get_after = fail_issue_comment_get_after or {}
	fail_issue_get_for = fail_issue_get_for or []
	fail_issue_edit_for = fail_issue_edit_for or []
	fail_branch_ref_after = fail_branch_ref_after or {}
	fail_branch_ref_not_found_after = fail_branch_ref_not_found_after or {}
	mock_actions_runs_cache_get_json = mock_actions_runs_cache_get_json or {}
	mock_actions_runs_cache_put_json = mock_actions_runs_cache_put_json or {}
	actions_runs_workflow_runs = actions_runs_workflow_runs or []
	actions_runs_status_sequence = actions_runs_status_sequence or []
	mock_validation_history_payload = dict(mock_validation_history_payload or {})
	mock_validation_history_get_exit_code = int(mock_validation_history_get_exit_code or 0)
	mock_validation_history_get_json = dict(mock_validation_history_get_json or {})
	mock_validation_history_append_exit_code = int(mock_validation_history_append_exit_code or 0)
	mock_validation_history_append_json = dict(mock_validation_history_append_json or {})
	mock_operator_bypass_audit_payload = dict(mock_operator_bypass_audit_payload or {})
	mock_operator_bypass_audit_get_exit_code = int(mock_operator_bypass_audit_get_exit_code or 0)
	mock_operator_bypass_audit_get_json = dict(mock_operator_bypass_audit_get_json or {})
	mock_operator_bypass_audit_append_exit_code = int(mock_operator_bypass_audit_append_exit_code or 0)
	mock_operator_bypass_audit_append_json = dict(mock_operator_bypass_audit_append_json or {})
	mock_branch_rebuild_audit_get_json = mock_branch_rebuild_audit_get_json or {}
	mock_branch_rebuild_audit_put_json = mock_branch_rebuild_audit_put_json or {}
	branch_protected_branches = branch_protected_branches or {}
	branch_ref_shas = branch_ref_shas or {}
	mock_git_fetch_fail_after = mock_git_fetch_fail_after or {}
	mock_revalidate_events_payload = dict(mock_revalidate_events_payload or {})
	mock_pr_create_race_pr = dict(mock_pr_create_race_pr or {})
	compare_ahead_by_sequence = [int(value) for value in (compare_ahead_by_sequence or [])]
	mock_pr_ready_exit_code = int(mock_pr_ready_exit_code or 0)
	codex_json = codex_json or {
		"status": "complete",
		"justification": "done",
		"assessment": "all work complete",
		"new_issues": [],
		"issues_to_revert": [],
	}

	with tempfile.TemporaryDirectory(prefix="poller-test-") as td:
		tmp = Path(td)
		sandbox = tmp / "sandbox"
		bin_dir = tmp / "bin"
		home_dir = tmp / "home"
		runtime_dir = tmp / "runtime"
		store_file = tmp / "gh_store.json"
		_make_poller_sandbox(sandbox)
		bin_dir.mkdir(parents=True)
		home_dir.mkdir(parents=True)
		runtime_dir.mkdir(parents=True)

		def _comment_entry(raw_comment: str | dict, comment_id: int, issue_num: int) -> dict:
			if isinstance(raw_comment, dict):
				entry = dict(raw_comment)
			else:
				entry = {"body": str(raw_comment)}
			entry.setdefault("id", comment_id)
			entry.setdefault("body", "")
			entry.setdefault("created_at", f"2026-01-01T00:00:{comment_id % 60:02d}Z")
			user = entry.get("user")
			if isinstance(user, dict):
				user_entry = dict(user)
			else:
				user_entry = {"login": str(user) if user else "octocat"}
			user_entry.setdefault("login", "octocat")
			entry["user"] = user_entry
			entry.setdefault(
				"html_url",
				f"https://github.com/owner/repo/issues/{issue_num}#issuecomment-{entry['id']}",
			)
			return entry

		issues: dict[str, dict] = {
			str(tracking_num): {
				"labels": list(tracking_labels),
				"comments": [
					_comment_entry({"body": _state_comment(state), "user": {"login": "github-actions[bot]"}}, 1, tracking_num),
					*[
						_comment_entry(comment_body, idx + 2, tracking_num)
						for idx, comment_body in enumerate(tracking_comments)
					],
					],
				"body": tracking_body if tracking_body is not None else "Tracking issue body",
				"closed": False,
			}
		}
		next_comment_id = 2 + len(tracking_comments)
		for inum, labels in issue_labels.items():
			raw_comments = issue_comments.get(inum, [])
			issue_comment_entries = []
			for comment_body in raw_comments:
				issue_comment_entries.append(_comment_entry(comment_body, next_comment_id, inum))
				next_comment_id += 1
			issues[str(inum)] = {
				"labels": list(labels),
				"comments": issue_comment_entries,
				"body": issue_bodies.get(inum, f"Issue {inum}"),
				"closed": bool(issue_closed.get(inum, False)),
			}

		store = {
			"issues": issues,
			"issue_events": {str(k): list(v) for k, v in issue_events.items()},
			"next_comment_id": next_comment_id,
			"api_calls": [],
			"label_create_calls": [],
			"label_create_responses": {str(k): dict(v) for k, v in label_create_responses.items()},
			"validation_dispatches": [],
			"release_dispatches": [],
			"review_dispatches": [],
			"issue_body_edit_calls": [],
			"commit_status_posts": [],
			"closed_issues": [],
			"graphql_mode": gql_mode,
			"graphql_labels": {str(k): list(v) for k, v in gql_labels.items()},
			"graphql_calls": 0,
			"candidate_details_graphql_calls": 0,
			"label_batch_graphql_calls": 0,
			"issue_label_calls": {},
			"issue_state_calls": {},
			"fail_validation_dispatch": fail_validation_dispatch,
			"fail_release_dispatch": fail_release_dispatch,
			"fail_search_issues": bool(fail_search_issues),
			"search_issue_items": list(search_issue_items or []),
			"default_branch": "main",
			"prs": prs,
			"pr_api_sequence": {str(k): list(v) for k, v in pr_api_sequence.items()},
			"existing_branches": existing_branches,
			"update_branch_calls": [],
			"update_branch_fail_for_prs": [int(x) for x in update_branch_fail_for_prs],
			"review_dispatch_fail_for_prs": [int(x) for x in review_dispatch_fail_for_prs],
			"active_autofix_runs": active_autofix_runs,
			"merge_conflict_on_sync": merge_conflict_on_sync,
			"merge_calls": [],
			"blocked_check_shas": blocked_check_shas,
			"validation_workflow_runs": validation_workflow_runs,
			"issue_linked_prs": {str(k): int(v) for k, v in issue_linked_prs.items()},
			"merge_tree_conflict_paths": list(merge_tree_conflict_paths),
			"timeline_fail_for_issues": [int(x) for x in timeline_fail_for_issues],
			"fail_issue_comment_get_after": {str(k): int(v) for k, v in fail_issue_comment_get_after.items()},
			"fail_issue_get_for": [int(x) for x in fail_issue_get_for],
			"fail_issue_edit_for": [int(x) for x in fail_issue_edit_for],
			"fail_branch_ref_after": {str(k): int(v) for k, v in fail_branch_ref_after.items()},
			"fail_branch_ref_not_found_after": {str(k): int(v) for k, v in fail_branch_ref_not_found_after.items()},
			"mock_actions_runs_cache_get_json": mock_actions_runs_cache_get_json,
			"mock_actions_runs_cache_put_json": mock_actions_runs_cache_put_json,
			"mock_branch_rebuild_audit_payload": mock_branch_rebuild_audit_payload,
			"mock_branch_rebuild_audit_get_json": mock_branch_rebuild_audit_get_json,
			"mock_branch_rebuild_audit_put_json": mock_branch_rebuild_audit_put_json,
			"branch_protected_branches": {str(k): bool(v) for k, v in branch_protected_branches.items()},
			"branch_ref_shas": {str(k): str(v) for k, v in branch_ref_shas.items()},
			"mock_git_fetch_fail_after": {str(k): int(v) for k, v in mock_git_fetch_fail_after.items()},
			"actions_runs_fetch_count": 0,
			"actions_runs_if_none_match_count": 0,
			"actions_runs_etag": actions_runs_etag,
			"actions_runs_workflow_runs": list(actions_runs_workflow_runs),
			"actions_runs_status": int(actions_runs_status),
			"actions_runs_status_sequence": list(actions_runs_status_sequence),
			"mock_gh_issue_list_label_filter": bool(mock_gh_issue_list_label_filter),
			"compare_ahead_by": int(compare_ahead_by),
			"compare_ahead_by_sequence": list(compare_ahead_by_sequence),
			"compare_ahead_by_force_error": bool(compare_ahead_by_force_error),
			"mock_validation_history_payload": mock_validation_history_payload,
			"mock_validation_history_get_exit_code": mock_validation_history_get_exit_code,
			"mock_validation_history_get_json": mock_validation_history_get_json,
			"mock_validation_history_append_exit_code": mock_validation_history_append_exit_code,
			"mock_validation_history_append_json": mock_validation_history_append_json,
			"mock_operator_bypass_audit_payload": mock_operator_bypass_audit_payload,
			"mock_operator_bypass_audit_get_exit_code": mock_operator_bypass_audit_get_exit_code,
			"mock_operator_bypass_audit_get_json": mock_operator_bypass_audit_get_json,
			"mock_operator_bypass_audit_append_exit_code": mock_operator_bypass_audit_append_exit_code,
			"mock_operator_bypass_audit_append_json": mock_operator_bypass_audit_append_json,
			"mock_revalidate_events_payload": mock_revalidate_events_payload,
			"mock_pr_create_race_pr": mock_pr_create_race_pr,
			"mock_pr_ready_exit_code": mock_pr_ready_exit_code,
		}
		store_file.write_text(json.dumps(store), encoding="utf-8")

		(runtime_dir / "tracking_issues.json").write_text(
			json.dumps([{"number": tracking_num, "title": "Test tracking"}]),
			encoding="utf-8",
		)

		gh_mock = r'''#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

store_path = Path(__import__('os').environ['GH_MOCK_STORE'])
store = json.loads(store_path.read_text(encoding='utf-8'))
args = sys.argv[1:]


def save():
	store_path.write_text(json.dumps(store), encoding='utf-8')


def get_issue(num):
	key = str(num)
	if key not in store['issues']:
		store['issues'][key] = {'labels': [], 'comments': [], 'body': '', 'closed': False}
	return store['issues'][key]


def parse_api():
	path = None
	jq = None
	method = 'GET'
	fields = []
	input_file = None
	i = 1
	while i < len(args):
		arg = args[i]
		if arg in ('--paginate', '--slurp'):
			i += 1
			continue
		if arg == '--jq':
			jq = args[i + 1]
			i += 2
			continue
		if arg == '-f':
			fields.append(args[i + 1])
			i += 2
			continue
		if arg == '-X':
			method = args[i + 1]
			i += 2
			continue
		if arg == '--method':
			method = args[i + 1]
			i += 2
			continue
		if arg == '--input':
			input_file = args[i + 1]
			i += 2
			continue
		if arg == '-H':
			i += 2
			continue
		if arg.startswith('-'):
			i += 1
			continue
		if path is None:
			path = arg
		i += 1
	return path, jq, method, fields, input_file


if not args:
	sys.exit(0)

if args[0] == 'label' and len(args) >= 3 and args[1] == 'create':
	label_name = args[2]
	store.setdefault('label_create_calls', []).append(label_name)
	responses = store.get('label_create_responses', {})
	response = responses.get(label_name)
	save()
	if isinstance(response, dict):
		message = str(response.get('stderr', ''))
		exit_code = int(response.get('exit_code', 0))
		if message:
			print(message, file=sys.stderr)
		sys.exit(exit_code)
	sys.exit(0)

if args[0] == 'python3' and len(args) >= 4 and args[1] == 'scripts/ai_memory.py' and args[2] == 'actions-runs-cache':
	cmd = args[3]
	if cmd == 'get':
		repo = ''
		i = 4
		while i < len(args):
			if args[i] == '--repo' and i + 1 < len(args):
				repo = args[i + 1]
				i += 2
				continue
			i += 1
		payload = {
			'ok': True,
			'enabled': True,
			'hit': bool(store.get('mock_actions_runs_cache_hit', False)),
			'cache': store.get('mock_actions_runs_cache_payload'),
		}
		override = store.get('mock_actions_runs_cache_get_json')
		if isinstance(override, dict) and override:
			payload.update(override)
		if repo and isinstance(payload.get('cache'), dict):
			payload['cache'].setdefault('repository', repo)
		print(json.dumps(payload))
		sys.exit(0)
	if cmd == 'put':
		runs_file = ''
		repo = ''
		etag = ''
		ttl_seconds = ''
		i = 4
		while i < len(args):
			if args[i] == '--runs-file' and i + 1 < len(args):
				runs_file = args[i + 1]
				i += 2
				continue
			if args[i] == '--repo' and i + 1 < len(args):
				repo = args[i + 1]
				i += 2
				continue
			if args[i] == '--etag' and i + 1 < len(args):
				etag = args[i + 1]
				i += 2
				continue
			if args[i] == '--ttl-seconds' and i + 1 < len(args):
				ttl_seconds = args[i + 1]
				i += 2
				continue
			i += 1
		runs = []
		if runs_file:
			try:
				raw = Path(runs_file).read_text(encoding='utf-8')
				parsed = json.loads(raw)
				if isinstance(parsed, dict):
					runs = list(parsed.get('workflow_runs', []))
				elif isinstance(parsed, list):
					runs = parsed
			except Exception:
				runs = []
		store['mock_actions_runs_cache_hit'] = True
		store['mock_actions_runs_cache_payload'] = {
			'schema_version': 'v1',
			'repository': repo or 'owner/repo',
			'fetched_at': '2026-01-01T00:00:00Z',
			'ttl_seconds': int(ttl_seconds or 60),
			'etag': etag or None,
			'runs': runs,
		}
		store.setdefault('mock_actions_runs_cache_put_calls', 0)
		store['mock_actions_runs_cache_put_calls'] = int(store['mock_actions_runs_cache_put_calls']) + 1
		save()
		payload = {'ok': True, 'stored': True, 'cache': store['mock_actions_runs_cache_payload']}
		override = store.get('mock_actions_runs_cache_put_json')
		if isinstance(override, dict) and override:
			payload.update(override)
		print(json.dumps(payload))
		sys.exit(0)

if args[0] == 'issue' and len(args) >= 3 and args[1] == 'list':
	# Minimal `gh issue list` filter for the close_merged_issues_sweep
	# path. Gated by store['mock_gh_issue_list_label_filter'] so existing
	# tests that don't exercise the sweep keep their legacy `[]` return
	# value and unchanged closed_issues / label-call expectations. Only
	# new sweep regression tests opt in via the kwarg below; everyone
	# else continues to see no issues from the listing.
	if store.get('mock_gh_issue_list_label_filter') is True:
		il_state = None
		il_label = None
		il_json = False
		il_idx = 2
		while il_idx < len(args):
			il_arg = args[il_idx]
			if il_arg == '--state' and il_idx + 1 < len(args):
				il_state = args[il_idx + 1]
				il_idx += 2
				continue
			if il_arg == '--label' and il_idx + 1 < len(args):
				il_label = args[il_idx + 1]
				il_idx += 2
				continue
			if il_arg == '--json' and il_idx + 1 < len(args):
				il_json = True
				il_idx += 2
				continue
			il_idx += 1
		if il_state == 'open' and il_label is not None and il_json:
			out = []
			for inum, idata in store.get('issues', {}).items():
				if idata.get('closed'):
					continue
				labels = idata.get('labels', []) or []
				if il_label not in labels:
					continue
				try:
					out.append({
						'number': int(inum),
						'labels': [{'name': l} for l in labels],
					})
				except (TypeError, ValueError):
					continue
			print(json.dumps(out))
			sys.exit(0)
	print('[]')
	sys.exit(0)

if args[0] == 'workflow' and len(args) >= 3 and args[1] == 'run':
	wf = args[2]
	if wf in ('ai-validate.yml', 'internal-validate.yml'):
		if store.get('fail_validation_dispatch'):
			print('dispatch failed', file=sys.stderr)
			sys.exit(1)
		tracking = None
		ref = None
		for i, arg in enumerate(args):
			if arg == '--ref' and i + 1 < len(args):
				ref = args[i + 1]
			if arg == '-f' and i + 1 < len(args) and args[i + 1].startswith('tracking_issue='):
				tracking = args[i + 1].split('=', 1)[1]
		store['validation_dispatches'].append({'workflow': wf, 'tracking_issue': tracking, 'ref': ref})
		save()
		sys.exit(0)
	if wf in ('ai-review.yml', 'internal-review.yml', 'review_autofix.yml'):
		pr_number = None
		ref = None
		for i, arg in enumerate(args):
			if arg == '--ref' and i + 1 < len(args):
				ref = args[i + 1]
			if arg == '-f' and i + 1 < len(args) and args[i + 1].startswith('pr_number='):
				pr_number = int(args[i + 1].split('=', 1)[1])
		if pr_number in set(store.get('review_dispatch_fail_for_prs', [])):
			print('dispatch failed', file=sys.stderr)
			sys.exit(1)
		store['review_dispatches'].append({'workflow': wf, 'pr_number': pr_number, 'ref': ref})
		save()
		sys.exit(0)
	if wf == 'test-and-mark-stable.yml':
		if store.get('fail_release_dispatch'):
			print('dispatch failed', file=sys.stderr)
			sys.exit(1)
		dispatch = {'workflow': wf}
		for i, arg in enumerate(args):
			if arg == '--ref' and i + 1 < len(args):
				dispatch['ref'] = args[i + 1]
			if arg == '-f' and i + 1 < len(args):
				field = args[i + 1]
				if '=' in field:
					k, v = field.split('=', 1)
					dispatch[k] = v
		store['release_dispatches'].append(dispatch)
		save()
		sys.exit(0)
	sys.exit(1)

if args[0] == 'run' and len(args) >= 2 and args[1] == 'list':
	workflow = None
	branch = None
	jq_query = None
	for i, arg in enumerate(args):
		if arg == '--workflow' and i + 1 < len(args):
			workflow = args[i + 1]
		if arg == '--branch' and i + 1 < len(args):
			branch = args[i + 1]
		if arg == '--jq' and i + 1 < len(args):
			jq_query = args[i + 1]
	runs = []
	for run in store.get('active_autofix_runs', []):
		if workflow and run.get('workflow') != workflow:
			continue
		if branch and run.get('branch') != branch:
			continue
		runs.append({
			'status': run.get('status', 'queued'),
			'conclusion': run.get('conclusion', ''),
		})
	if jq_query:
		import subprocess as _sp
		p = _sp.run(['jq', '-r', jq_query], input=json.dumps(runs), capture_output=True, text=True)
		print(p.stdout.rstrip())
	else:
		print(json.dumps(runs))
	sys.exit(0)

if args[0] == 'run' and len(args) >= 3 and args[1] == 'view':
	run_id = str(args[2])
	payload = None
	for run in store.get('validation_workflow_runs', []):
		if str((run or {}).get('id', '')) == run_id:
			payload = {
				'jobs': list((run or {}).get('jobs', [])),
				'conclusion': (run or {}).get('conclusion', ''),
				'outputs': dict((run or {}).get('outputs', {})),
			}
			break
	if payload is None:
		print(f'run not found: {run_id}', file=sys.stderr)
		sys.exit(1)
	print(json.dumps(payload))
	sys.exit(0)

if args[0] == 'pr' and len(args) >= 2 and args[1] == 'list':
	base = None
	head = None
	state = None
	search = None
	jq_query = None
	for i, arg in enumerate(args):
		if arg == '--base' and i + 1 < len(args):
			base = args[i + 1]
		if arg == '--head' and i + 1 < len(args):
			head = args[i + 1]
		if arg == '--state' and i + 1 < len(args):
			state = args[i + 1]
		if arg == '--search' and i + 1 < len(args):
			search = args[i + 1]
		if arg == '--jq' and i + 1 < len(args):
			jq_query = args[i + 1]
	prs = []
	for pr in store.get('prs', []):
		merged = bool(pr.get('merged', False) or pr.get('merged_at') is not None)
		pr_state = pr.get('state', 'open')
		if state == 'open' and pr_state != 'open':
			continue
		if state == 'closed' and pr_state != 'closed':
			continue
		if state == 'merged' and not merged:
			continue
		if base and pr.get('baseRefName') != base:
			continue
		if head and pr.get('headRefName') != head:
			continue
		if search:
			m = re.search(r'#(\d+)\s+in:body', search)
			if m and f"#{m.group(1)}" not in str(pr.get('body', '')):
				continue
		payload = dict(pr)
		payload.setdefault('mergedAt', pr.get('merged_at'))
		prs.append(payload)
	if jq_query:
		p = subprocess.run(['jq', '-r', jq_query], input=json.dumps(prs), capture_output=True, text=True)
		print(p.stdout.rstrip() if p.returncode == 0 else '')
	else:
		print(json.dumps(prs))
	sys.exit(0)

if args[0] == 'pr' and len(args) >= 2 and args[1] == 'create':
	base = ''
	head = ''
	title = ''
	body = ''
	draft = False
	i = 2
	while i < len(args):
		if args[i] == '--draft':
			draft = True
			i += 1
			continue
		if args[i] == '--base' and i + 1 < len(args):
			base = args[i + 1]
			i += 2
			continue
		if args[i] == '--head' and i + 1 < len(args):
			head = args[i + 1]
			i += 2
			continue
		if args[i] == '--title' and i + 1 < len(args):
			title = args[i + 1]
			i += 2
			continue
		if args[i] == '--body' and i + 1 < len(args):
			body = args[i + 1]
			i += 2
			continue
		i += 1
	race_pr = store.get('mock_pr_create_race_pr')
	if isinstance(race_pr, dict) and race_pr.get('baseRefName') == base and race_pr.get('headRefName') == head:
		existing = None
		for item in store.get('prs', []):
			if item.get('state', 'open') == 'open' and item.get('baseRefName') == base and item.get('headRefName') == head:
				existing = item
				break
		if existing is None:
			next_num = store.get('next_pr_number', 300)
			store['next_pr_number'] = next_num + 1
			existing = {
				'number': next_num,
				'state': 'open',
				'draft': bool(race_pr.get('draft', draft)),
				'baseRefName': base,
				'headRefName': head,
				'mergeable': race_pr.get('mergeable', True),
				'mergeable_state': race_pr.get('mergeable_state', 'clean'),
				'title': race_pr.get('title', title),
				'body': race_pr.get('body', body),
			}
			store.setdefault('prs', []).append(existing)
			save()
		print('a pull request already exists for this branch pair', file=sys.stderr)
		sys.exit(1)
	next_num = store.get('next_pr_number', 300)
	store['next_pr_number'] = next_num + 1
	pr = {
		'number': next_num,
		'state': 'open',
		'draft': draft,
		'baseRefName': base,
		'headRefName': head,
		'mergeable': True,
		'mergeable_state': 'clean',
		'title': title,
		'body': body,
	}
	store.setdefault('prs', []).append(pr)
	save()
	print(f'https://github.com/owner/repo/pull/{next_num}')
	sys.exit(0)

if args[0] == 'pr' and len(args) >= 3 and args[1] == 'ready':
	pr_num = int(args[2])
	exit_code = int(store.get('mock_pr_ready_exit_code', 0) or 0)
	if exit_code != 0:
		print('mock pr ready failure', file=sys.stderr)
		sys.exit(exit_code)
	for pr in store.get('prs', []):
		if pr.get('number') == pr_num:
			pr['draft'] = False
			store.setdefault('pr_ready_calls', []).append(pr_num)
			save()
			sys.exit(0)
	print('not found', file=sys.stderr)
	sys.exit(1)

if args[0] == 'pr' and len(args) >= 3 and args[1] == 'merge':
	pr_num = int(args[2])
	for pr in store.get('prs', []):
		if pr.get('number') == pr_num:
			if pr.get('mergeable') is False:
				print('conflict', file=sys.stderr)
				sys.exit(1)
			pr['state'] = 'closed'
			pr['merged'] = True
			store.setdefault('merged_prs', []).append(pr_num)
			save()
			sys.exit(0)
	print('not found', file=sys.stderr)
	sys.exit(1)

if args[0] == 'issue' and len(args) >= 3 and args[1] == 'edit':
	num = args[2]
	if int(num) in set(store.get('fail_issue_edit_for', [])):
		save()
		print('forced issue edit failure', file=sys.stderr)
		sys.exit(1)
	issue = get_issue(num)
	body = None
	i = 3
	while i < len(args):
		if args[i] == '--add-label' and i + 1 < len(args):
			label = args[i + 1]
			if label not in issue['labels']:
				issue['labels'].append(label)
			i += 2
			continue
		if args[i] == '--remove-label' and i + 1 < len(args):
			label = args[i + 1]
			issue['labels'] = [x for x in issue['labels'] if x != label]
			i += 2
			continue
		if args[i] in ('--body', '-b') and i + 1 < len(args):
			body = args[i + 1]
			i += 2
			continue
		if args[i] in ('--body-file', '-F') and i + 1 < len(args):
			body_file = args[i + 1]
			if body_file == '-':
				body = sys.stdin.read()
			else:
				body = Path(body_file).read_text(encoding='utf-8')
			i += 2
			continue
		i += 1
	if body is not None:
		issue['body'] = body
		store.setdefault('issue_body_edit_calls', []).append({'issue': int(num), 'body': body})
	save()
	sys.exit(0)

if args[0] == 'issue' and len(args) >= 3 and args[1] == 'close':
	num = args[2]
	issue = get_issue(num)
	issue['closed'] = True
	store['closed_issues'].append(int(num))
	comment = None
	for i, arg in enumerate(args):
		if arg == '--comment' and i + 1 < len(args):
			comment = args[i + 1]
	if comment:
		cid = store['next_comment_id']
		store['next_comment_id'] += 1
		issue['comments'].append({'id': cid, 'body': comment})
	save()
	sys.exit(0)

if args[0] == 'issue' and len(args) >= 3 and args[1] == 'create':
	title = ''
	body = ''
	labels = []
	i = 2
	while i < len(args):
		if args[i] == '--title' and i + 1 < len(args):
			title = args[i + 1]
			i += 2
			continue
		if args[i] == '--body' and i + 1 < len(args):
			body = args[i + 1]
			i += 2
			continue
		if args[i] == '--label' and i + 1 < len(args):
			labels.append(args[i + 1])
			i += 2
			continue
		i += 1
	next_num = store.get('next_issue_number', 900)
	store['next_issue_number'] = next_num + 1
	store['issues'][str(next_num)] = {'labels': list(labels), 'comments': [], 'body': body, 'closed': False, 'title': title}
	store.setdefault('created_issues', []).append({'number': next_num, 'title': title, 'labels': list(labels)})
	save()
	print(f'https://github.com/owner/repo/issues/{next_num}')
	sys.exit(0)

if args[0] == 'api':
	path, jq, method, fields, input_file = parse_api()
	if path is None:
		print('{}')
		sys.exit(0)
	store.setdefault('api_calls', []).append(path)

	if path == 'graphql':
		mode = store.get('graphql_mode', 'full')
		store['graphql_calls'] = int(store.get('graphql_calls', 0)) + 1
		query = ''
		for f in fields:
			if f.startswith('query='):
				query = f.split('=', 1)[1]
		has_issue_aliases = bool(re.search(r'i\d+\s*:\s*issue\(number:\s*\d+\)', query))
		is_candidate_details_batch = (
			has_issue_aliases
			and 'comments(last:' in query
			and 'timelineItems(' in query
		)
		if is_candidate_details_batch:
			store['candidate_details_graphql_calls'] = int(store.get('candidate_details_graphql_calls', 0)) + 1
		# Classify the query so tests can count wave-label-batch attempts
		# independently of unrelated GraphQL callers (standalone-stall
		# marker search, candidate details batch). The wave label batch
		# uses aliased issue(number:N) selectors and requests only labels —
		# no comments(last:) and no search(query:).
		is_label_batch = (
			has_issue_aliases
			and 'labels(first:' in query
			and 'comments(last:' not in query
			and 'timelineItems(' not in query
			and 'search(query:' not in query
		)
		if is_label_batch:
			store['label_batch_graphql_calls'] = int(store.get('label_batch_graphql_calls', 0)) + 1
		save()
		if mode == 'error':
			print('graphql failed', file=sys.stderr)
			sys.exit(1)
		aliases = []
		for alias, issue_num in re.findall(r'i(\d+)\s*:\s*issue\(number:\s*(\d+)\)', query):
			aliases.append((int(alias), int(issue_num)))
		repo = {}
		for alias_num, issue_num in aliases:
			if mode == 'partial' and aliases and (alias_num, issue_num) == aliases[-1]:
				continue
			issue = get_issue(issue_num)
			issue_state = 'CLOSED' if issue.get('closed') else 'OPEN'
			labels = store.get('graphql_labels', {}).get(str(issue_num), issue.get('labels', []))
			issue_payload = {}
			if re.search(r'(?m)^\s*number\s*$', query):
				issue_payload['number'] = issue_num
			if re.search(r'(?m)^\s*state\s*$', query):
				issue_payload['state'] = issue_state
			if 'labels(first:' in query:
				issue_payload['labels'] = {'nodes': [{'name': label} for label in labels]}
			if 'comments(last:' in query:
				comment_nodes = []
				for comment in issue.get('comments', []):
					comment_nodes.append({
						'databaseId': int(comment.get('id', 0) or 0),
						'body': str(comment.get('body', '')),
						'createdAt': '2026-01-01T00:00:00Z',
					})
				issue_payload['comments'] = {'nodes': comment_nodes}
			if 'timelineItems(' in query:
				linked_pr_num = store.get('issue_linked_prs', {}).get(str(issue_num))
				timeline_nodes = []
				if linked_pr_num is not None:
					pr = None
					for entry in store.get('prs', []):
						if int(entry.get('number', 0) or 0) == int(linked_pr_num):
							pr = entry
							break
					if pr is None:
						pr = {
							'number': int(linked_pr_num),
							'state': 'open',
							'merged': False,
							'headRefName': f'ai/issue-{linked_pr_num}',
							'mergeable': True,
							'mergeable_state': 'clean',
						}
					pr_repository = pr.get('repository', {'nameWithOwner': 'owner/repo'})
					if isinstance(pr_repository, dict):
						pr_repository = dict(pr_repository)
						pr_repository['nameWithOwner'] = str(pr_repository.get('nameWithOwner', 'owner/repo'))
					else:
						pr_repository = {'nameWithOwner': str(pr_repository)}
					pr_state = str(pr.get('state', 'open')).upper()
					if pr_state == 'MERGED':
						pr_state = 'CLOSED'
					timeline_nodes.append({
						'willCloseTarget': bool(pr.get('willCloseTarget', True)),
						'source': {
							'__typename': 'PullRequest',
							'number': int(pr.get('number', linked_pr_num)),
							'repository': pr_repository,
							'state': pr_state,
							'merged': bool(pr.get('merged', False)),
							'mergedAt': pr.get('merged_at', None),
							'headRefName': pr.get('headRefName', ''),
							'headRefOid': pr.get('headRefOid', pr.get('headSha', f'mocksha{linked_pr_num}')),
							'mergeable': pr.get('mergeable', None),
							'mergeStateStatus': str(pr.get('mergeStateStatus', pr.get('mergeable_state', ''))).upper(),
							'mergeCommit': {
								'oid': pr.get('merge_commit_sha', None),
							},
							'commits': {
								'nodes': [
									{'commit': {'pushedDate': pr.get('headPushedAt', '2026-01-01T00:00:00Z'), 'committedDate': pr.get('headPushedAt', '2026-01-01T00:00:00Z')}}
								],
							},
						},
					})
				issue_payload['timelineItems'] = {'nodes': timeline_nodes}
			repo[f'i{alias_num}'] = issue_payload
		print(json.dumps({'data': {'repository': repo}}))
		sys.exit(0)

	if path == 'search/issues':
		# Minimal child-issue search: return issues whose stored body contains
		# the queried `Tracking issue: #N` reference (mirrors the real
		# search/issues used by state reconstruction and the deferred-creation
		# duplicate backstop).  Honors an injected failure so fail-closed tests
		# can exercise the "lookup unavailable this cycle" path.  The real
		# endpoint returns both issues and pull requests unless the query adds
		# `is:issue`; explicit `search_issue_items` fixtures can model that.
		if store.get('fail_search_issues'):
			print('search/issues failed', file=sys.stderr)
			sys.exit(1)
		q = ''
		for f in fields:
			if f.startswith('q='):
				q = f.split('=', 1)[1]
		explicit_items = store.get('search_issue_items')
		if explicit_items:
			items = list(explicit_items)
		else:
			items = []
			mt = re.search(r'Tracking issue: #(\d+)', q)
			if mt is not None:
				needle = 'Tracking issue: #' + mt.group(1)
				for inum, idata in store.get('issues', {}).items():
					body = str(idata.get('body', '') or '')
					if needle in body:
						items.append({
							'number': int(inum),
							'title': idata.get('title', ''),
							'body': body,
							'state': 'closed' if idata.get('closed') else 'open',
						})
		if 'is:issue' in q:
			items = [item for item in items if 'pull_request' not in item]
		result = {'total_count': len(items), 'incomplete_results': False, 'items': items}
		if jq:
			import subprocess as _sp
			p = _sp.run(['jq', '-rc', jq], input=json.dumps(result), capture_output=True, text=True)
			if p.returncode != 0:
				print(p.stderr, file=sys.stderr, end='')
				sys.exit(p.returncode)
			print(p.stdout, end='')
		else:
			print(json.dumps(result))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)/comments(?:\?.*)?$', path)
	if m and method == 'GET' and not fields:
		num = m.group(1)
		fail_after = store.get('fail_issue_comment_get_after', {}).get(num)
		if fail_after is not None:
			calls = store.setdefault('issue_comment_get_calls', {})
			calls[num] = int(calls.get(num, 0)) + 1
			if calls[num] > int(fail_after):
				save()
				print('forced comments API failure', file=sys.stderr)
				sys.exit(1)
		issue = get_issue(m.group(1))
		save()
		print(json.dumps(issue['comments']))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)/comments$', path)
	if m and (fields or input_file):
		issue = get_issue(m.group(1))
		body = ''
		for f in fields:
			if f.startswith('body='):
				body = f.split('=', 1)[1]
		if input_file:
			if input_file == '-':
				payload_raw = sys.stdin.read()
			else:
				payload_raw = Path(input_file).read_text(encoding='utf-8')
			try:
				payload_obj = json.loads(payload_raw)
			except Exception:
				payload_obj = {}
			body = payload_obj.get('body', body)
		cid = store['next_comment_id']
		store['next_comment_id'] += 1
		issue['comments'].append({
			'id': cid,
			'body': body,
			'created_at': f'2026-01-01T00:00:{cid % 60:02d}Z',
			'user': {'login': 'github-actions[bot]'},
			'html_url': f'https://github.com/owner/repo/issues/{m.group(1)}#issuecomment-{cid}',
		})
		save()
		print(json.dumps({'id': cid}))
		sys.exit(0)

	m = re.search(r'/issues/comments/(\d+)$', path)
	if m and method == 'PATCH' and (fields or input_file):
		comment_id = int(m.group(1))
		body = ''
		for f in fields:
			if f.startswith('body='):
				body = f.split('=', 1)[1]
		if input_file:
			if input_file == '-':
				payload_raw = sys.stdin.read()
			else:
				payload_raw = Path(input_file).read_text(encoding='utf-8')
			try:
				payload_obj = json.loads(payload_raw)
			except Exception:
				payload_obj = {}
			body = payload_obj.get('body', body)
		updated = False
		for issue in store['issues'].values():
			for comment in issue.get('comments', []):
				if int(comment.get('id', 0) or 0) == comment_id:
					comment['body'] = body
					updated = True
					break
			if updated:
				break
		save()
		print(json.dumps({'id': comment_id, 'updated': updated}))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)/events(?:\?.*)?$', path)
	if m and method == 'GET':
		num = m.group(1)
		calls = store.setdefault('issue_events_get_calls', {})
		calls[num] = int(calls.get(num, 0)) + 1
		save()
		print(json.dumps(store.get('issue_events', {}).get(num, [])))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)/labels$', path)
	if m:
		num = m.group(1)
		issue = get_issue(num)
		counts = store.setdefault('issue_label_calls', {})
		counts[num] = int(counts.get(num, 0)) + 1
		save()
		labels = issue['labels']
		if jq:
			print(json.dumps(labels))
		else:
			print(json.dumps([{'name': l} for l in labels]))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)$', path)
	if m:
		num = int(m.group(1))
		if num in set(store.get('fail_issue_get_for', [])):
			print('forced issue API failure', file=sys.stderr)
			sys.exit(1)
		issue = get_issue(m.group(1))
		issue_state = 'closed' if issue.get('closed') else 'open'
		if jq == '.body':
			print(issue.get('body', ''))
		elif jq == '.state':
			state_counts = store.setdefault('issue_state_calls', {})
			state_counts[m.group(1)] = int(state_counts.get(m.group(1), 0)) + 1
			save()
			print(issue_state)
		elif jq and (jq == '.title' or jq == '.title // ""'):
			print(issue.get('title', ''))
		elif jq:
			# Generic path: build a comprehensive issue-like object and pipe
			# it through real jq so callers can request arbitrary filters
			# (e.g. the consolidated {state, state_reason, labels} fetch used
			# by the validation fix-up loop).
			issue_obj = {
				'body': issue.get('body', ''),
				'title': issue.get('title', ''),
				'state': issue_state,
				'state_reason': issue.get('state_reason', ''),
				'labels': [{'name': l} for l in issue.get('labels', [])],
			}
			import subprocess as _sp
			p = _sp.run(['jq', '-rc', jq], input=json.dumps(issue_obj), capture_output=True, text=True)
			if p.returncode != 0:
				print(p.stderr, file=sys.stderr, end='')
				sys.exit(p.returncode)
			print(p.stdout, end='')
		else:
			print(json.dumps({'body': issue.get('body', ''), 'state': issue_state}))
		sys.exit(0)

	m = re.search(r'/pulls/(\d+)$', path)
	m_files = re.search(r'/pulls/(\d+)/files(\?.*)?$', path)
	m_update = re.search(r'/pulls/(\d+)/update-branch$', path)
	if m_files:
		pr_num = int(m_files.group(1))
		pr = None
		for item in store.get('prs', []):
			if item.get('number') == pr_num:
				pr = item
				break
		files = []
		if pr is not None:
			files = [{'filename': f} for f in pr.get('files', [])]
		if jq:
			import subprocess as _sp
			p = _sp.run(['jq', '-r', jq], input=json.dumps(files), capture_output=True, text=True)
			print(p.stdout.rstrip())
		else:
			print(json.dumps(files))
		sys.exit(0)

	if m_update and method == 'PUT':
		pr_num = int(m_update.group(1))
		store.setdefault('update_branch_calls', []).append(pr_num)
		save()
		if pr_num in set(store.get('update_branch_fail_for_prs', [])):
			print('update-branch failed', file=sys.stderr)
			sys.exit(1)
		print(json.dumps({'message': 'updated'}))
		sys.exit(0)

	if m:
		pr_num = int(m.group(1))
		pr_get_calls = store.setdefault('pr_get_calls', {})
		pr_get_calls[str(pr_num)] = int(pr_get_calls.get(str(pr_num), 0)) + 1
		pr = None
		seq_map = store.get('pr_api_sequence', {})
		seq_key = str(pr_num)
		seq = seq_map.get(seq_key)
		if isinstance(seq, list) and seq:
			idx_map = store.setdefault('pr_api_sequence_index', {})
			idx = int(idx_map.get(seq_key, 0))
			if idx >= len(seq):
				idx = len(seq) - 1
			pr = dict(seq[idx])
			if idx < len(seq) - 1:
				idx_map[seq_key] = idx + 1
			save()
		else:
			for item in store.get('prs', []):
				if item.get('number') == pr_num:
					pr = item
					break
		if pr is None:
			print('{}')
			sys.exit(0)
		if method == 'PATCH':
			payload = {}
			if input_file:
				try:
					payload = json.loads(Path(input_file).read_text(encoding='utf-8'))
				except Exception:
					payload = {}
			if 'body' in payload:
				pr['body'] = payload.get('body', '')
			if 'draft' in payload:
				pr['draft'] = bool(payload.get('draft'))
			store.setdefault('pr_body_update_calls', []).append(pr_num)
			save()
			print(json.dumps(pr))
			sys.exit(0)
		pr_state = pr.get('stateFromApi', pr.get('state', 'open'))
		if jq == '.state':
			print(pr_state)
		elif jq == '.draft':
			print('true' if pr.get('draft', False) else 'false')
		elif jq == '.merged_at != null':
			merged_at = pr.get('merged_at')
			if merged_at is None and pr.get('merged') is True:
				merged_at = 'mock-merged-at'
			print('true' if merged_at is not None else 'false')
		elif jq == '.merged':
			merged = pr.get('merged')
			if merged is None:
				merged = pr.get('state') == 'merged'
			print('true' if merged else 'false')
		elif jq == '.mergeable_state // ""':
			print(pr.get('mergeable_state', ''))
		elif jq == '.mergeable':
			val = pr.get('mergeable', True)
			if val is True:
				print('true')
			elif val is False:
				print('false')
			else:
				print('null')
		elif jq == '.head.sha':
			print(pr.get('headSha', f'mocksha{pr_num}'))
		elif jq == '.head.ref':
			print(pr.get('headRefFromApi', pr.get('headRefName', '')))
		else:
			print(json.dumps({
				'number': pr_num,
				'state': pr_state,
				'draft': pr.get('draft', False),
				'mergeable': pr.get('mergeable', True),
				'mergeable_state': pr.get('mergeable_state', ''),
				'merged': pr.get('merged', False),
				'merged_at': pr.get('merged_at', ('mock-merged-at' if pr.get('merged', False) else None)),
				'title': pr.get('title', ''),
				'body': pr.get('body', ''),
				'base': {
					'ref': pr.get('baseRefName', ''),
				},
				'head': {
					'sha': pr.get('headSha', f'mocksha{pr_num}'),
					'ref': pr.get('headRefFromApi', pr.get('headRefName', '')),
				},
			}))
		sys.exit(0)

	m = re.search(r'/statuses/([^/?]+)$', path)
	if m and method == 'POST':
		sha = m.group(1)
		payload = {'sha': sha}
		for f in fields:
			if '=' in f:
				key, value = f.split('=', 1)
				payload[key] = value
		store.setdefault('commit_status_posts', []).append(payload)
		save()
		print(json.dumps(payload))
		sys.exit(0)

	if re.search(r'/merges$', path) and (method == 'POST' or fields):
		base = ''
		head = ''
		for f in fields:
			if f.startswith('base='):
				base = f.split('=', 1)[1]
			if f.startswith('head='):
				head = f.split('=', 1)[1]
		store.setdefault('merge_calls', []).append({'base': base, 'head': head})
		if store.get('merge_conflict_on_sync'):
			print('conflict', file=sys.stderr)
			sys.exit(1)
		save()
		print(json.dumps({'merged': True}))
		sys.exit(0)

	m = re.search(r'/branches/(.+)$', path)
	if m:
		encoded_branch = m.group(1)
		from urllib.parse import unquote
		branch = unquote(encoded_branch)
		if branch not in store.get('existing_branches', ['main']):
			print('not found', file=sys.stderr)
			sys.exit(1)
		protected = bool(store.get('branch_protected_branches', {}).get(branch, False))
		branch_sha = store.get('branch_ref_shas', {}).get(branch, 'mocksha')
		result = {'name': branch, 'protected': protected, 'commit': {'sha': branch_sha}}
		if jq:
			p = subprocess.run(['jq', '-r', jq], input=json.dumps(result), capture_output=True, text=True)
			if p.returncode != 0:
				sys.stderr.write(p.stderr)
				sys.exit(p.returncode)
			sys.stdout.write(p.stdout)
			save()
			sys.exit(0)
		save()
		print(json.dumps(result))
		sys.exit(0)

	m = re.search(r'/git/refs/heads/(.+)$', path)
	if m and method == 'DELETE':
		encoded_branch = m.group(1)
		from urllib.parse import unquote
		branch = unquote(encoded_branch)
		if bool(store.get('branch_protected_branches', {}).get(branch, False)):
			save()
			print('protected branch', file=sys.stderr)
			sys.exit(1)
		store['existing_branches'] = [str(item) for item in store.get('existing_branches', ['main']) if str(item) != branch]
		store.setdefault('deleted_branches', []).append(branch)
		save()
		print(json.dumps({'ref': f'refs/heads/{branch}'}))
		sys.exit(0)

	if re.search(r'/git/refs$', path) and method == 'POST':
		ref = ''
		sha = ''
		for f in fields:
			if f.startswith('ref='):
				ref = f.split('=', 1)[1]
			if f.startswith('sha='):
				sha = f.split('=', 1)[1]
		branch = ref[len('refs/heads/'):] if ref.startswith('refs/heads/') else ''
		if not branch:
			print('bad ref', file=sys.stderr)
			sys.exit(1)
		known_branches = [str(item) for item in store.get('existing_branches', ['main'])]
		if branch not in known_branches:
			known_branches.append(branch)
		store['existing_branches'] = known_branches
		branch_shas = dict(store.get('branch_ref_shas', {}))
		branch_shas[branch] = sha or 'mocksha'
		store['branch_ref_shas'] = branch_shas
		store.setdefault('created_branches', []).append({'branch': branch, 'sha': sha or 'mocksha'})
		save()
		print(json.dumps({'ref': ref, 'object': {'sha': sha or 'mocksha'}}))
		sys.exit(0)

	m = re.search(r'/git/ref/heads/(.+)$', path)
	if m:
		encoded_branch = m.group(1)
		from urllib.parse import unquote
		branch = unquote(encoded_branch)
		calls = store.setdefault('branch_ref_calls', {})
		calls[branch] = int(calls.get(branch, 0)) + 1
		fail_after = store.get('fail_branch_ref_after', {}).get(branch)
		if fail_after is not None:
			if calls[branch] > int(fail_after):
				save()
				print('forced branch ref API failure', file=sys.stderr)
				sys.exit(1)
		missing_after = store.get('fail_branch_ref_not_found_after', {}).get(branch)
		if missing_after is not None:
			if calls[branch] > int(missing_after):
				nf_calls = store.setdefault('branch_ref_not_found_calls', {})
				nf_calls[branch] = int(nf_calls.get(branch, 0)) + 1
				save()
				print('not found', file=sys.stderr)
				sys.exit(1)
		if branch in store.get('existing_branches', ['main']):
			branch_sha = store.get('branch_ref_shas', {}).get(branch, 'mocksha')
			result = {'ref': f'refs/heads/{branch}', 'object': {'sha': branch_sha}}
			if jq:
				p = subprocess.run(['jq', '-r', jq], input=json.dumps(result), capture_output=True, text=True)
				if p.returncode != 0:
					sys.stderr.write(p.stderr)
					sys.exit(p.returncode)
				sys.stdout.write(p.stdout)
				save()
				sys.exit(0)
			save()
			print(json.dumps(result))
			sys.exit(0)
		save()
		print('not found', file=sys.stderr)
		sys.exit(1)

	if path.endswith('/timeline'):
		m = re.search(r'/issues/(\d+)/timeline$', path)
		events = []
		if m:
			issue_num = m.group(1)
			if int(issue_num) in set(store.get('timeline_fail_for_issues', [])):
				print('timeline lookup failed', file=sys.stderr)
				sys.exit(1)
			linked_pr = store.get('issue_linked_prs', {}).get(str(issue_num))
			if linked_pr is not None:
				events.append({
					'event': 'cross-referenced',
					'source': {
						'issue': {
							'number': int(linked_pr),
							'pull_request': {'url': f'https://api.github.com/repos/owner/repo/pulls/{int(linked_pr)}'},
						}
					},
				})
		if jq:
			import subprocess
			jq_result = subprocess.run(['jq', '-r', jq], input=json.dumps(events), capture_output=True, text=True)
			print(jq_result.stdout.rstrip())
		else:
			print(json.dumps(events))
		sys.exit(0)

	m = re.search(r'/commits/([^/]+)/check-runs(\?.*)?$', path)
	if m:
		sha = m.group(1)
		incomplete = 1 if sha in store.get('blocked_check_shas', []) else 0
		if jq:
			print(str(incomplete))
		else:
			if incomplete:
				print(json.dumps({'check_runs': [{'status': 'in_progress', 'conclusion': None}]}))
			else:
				print(json.dumps({'check_runs': []}))
		sys.exit(0)

	if re.search(r'^repos/[^/]+/[^/]+$', path):
		if jq == '.default_branch':
			print(store.get('default_branch', 'main'))
		else:
			print(json.dumps({'default_branch': store.get('default_branch', 'main')}))
		sys.exit(0)

	# Compare endpoint — added for the integration-ahead-by gate
	# (shubhodeep1/binance-blessings#135). Tests can pin ahead_by via the
	# 'compare_ahead_by' store key (default: 0 = default contains
	# integration tip = no drift, which matches the legacy assumption of
	# tests that predate the gate). Setting compare_ahead_by_force_error
	# simulates the compare API failing so the fail-closed posture can be
	# exercised explicitly.
	m = re.search(r'^repos/[^/]+/[^/]+/compare/.+\.\.\..+$', path)
	if m:
		if store.get('compare_ahead_by_force_error'):
			sys.stderr.write('simulated compare API error\n')
			sys.exit(22)
		sequence = store.get('compare_ahead_by_sequence') or []
		if sequence:
			ahead_by = int(sequence[0])
			if len(sequence) > 1:
				store['compare_ahead_by_sequence'] = list(sequence[1:])
				save()
		else:
			ahead_by = int(store.get('compare_ahead_by', 0))
		if jq == '.ahead_by':
			print(ahead_by)
		else:
			print(json.dumps({'ahead_by': ahead_by, 'behind_by': 0}))
		sys.exit(0)

	m = re.search(r'/actions/runs(?:\?.*)?$', path)
	if m:
		query = path.split('?', 1)[1] if '?' in path else ''
		from urllib.parse import parse_qs
		params = parse_qs(query)
		if_none_match = ''
		i = 1
		while i < len(args):
			if args[i] == '-H' and i + 1 < len(args):
				header = args[i + 1]
				if header.lower().startswith('if-none-match:'):
					if_none_match = header.split(':', 1)[1].strip()
				i += 2
				continue
			i += 1

		store['actions_runs_fetch_count'] = int(store.get('actions_runs_fetch_count', 0)) + 1
		if if_none_match:
			store['actions_runs_if_none_match_count'] = int(store.get('actions_runs_if_none_match_count', 0)) + 1

		status_sequence = store.get('actions_runs_status_sequence', [])
		if isinstance(status_sequence, list) and status_sequence:
			status = int(status_sequence.pop(0))
			store['actions_runs_status_sequence'] = status_sequence
		else:
			status = int(store.get('actions_runs_status', 200))

		etag = store.get('actions_runs_etag', '"etag-initial"')
		workflow_runs = list(store.get('actions_runs_workflow_runs', []))
		# The script calls this endpoint in two distinct shapes:
		#   1. gh api -i ... (no --jq)  -> caller (_load_actions_runs_cached's
		#      primary in_progress fetch) needs the full HTTP response with
		#      headers so it can parse ETag and detect 304.
		#   2. gh api ... --jq '.workflow_runs' (no -i)  -> caller
		#      (_safe_gh_jq for queued/completed) needs just the jq-filtered
		#      body so its `--argjson` merge stays valid.
		# Distinguish via the presence of `jq` (the second case sets it).
		if jq:
			# Persist request bookkeeping even when the caller's jq filter
			# fails: the HTTP round-trip already happened, so later calls in
			# the same mock session should observe the consumed status/counts.
			save()
			if status != 200:
				print(f'actions runs mock failed: HTTP {status}', file=sys.stderr)
				sys.exit(1)
			body_obj = {
				'total_count': len(workflow_runs),
				'workflow_runs': workflow_runs,
			}
			p = subprocess.run(['jq', '-r', jq], input=json.dumps(body_obj), capture_output=True, text=True)
			if p.returncode != 0:
				sys.stderr.write(p.stderr)
				sys.exit(p.returncode)
			sys.stdout.write(p.stdout)
			sys.exit(0)
		status_text = 'OK' if status == 200 else 'Not Modified'
		headers = [
			f'HTTP/1.1 {status} {status_text}',
			'Content-Type: application/json; charset=utf-8',
			f'ETag: {etag}',
			'',
		]
		if status == 304:
			body = ''
		else:
			body_obj = {
				'total_count': len(workflow_runs),
				'workflow_runs': workflow_runs,
			}
			body = json.dumps(body_obj)
		output = '\n'.join(headers) + '\n'
		if body:
			output += body + '\n'
		save()
		sys.stdout.write(output)
		sys.exit(0)

	m = re.search(r'/actions/workflows/[^/]+/runs', path)
	if m:
		runs = store.get('validation_workflow_runs', [])
		result = {'workflow_runs': runs, 'total_count': len(runs)}
		if jq:
			import subprocess as _sp
			p = _sp.run(['jq', '-r', jq], input=json.dumps(result), capture_output=True, text=True)
			print(p.stdout.rstrip())
		else:
			print(json.dumps(result))
		sys.exit(0)

	print('{}')
	sys.exit(0)

print('Unsupported gh call: ' + ' '.join(args), file=sys.stderr)
sys.exit(1)
'''
		_write_exec(bin_dir / "gh", gh_mock)

		real_git = shutil.which("git")
		real_python = shutil.which("python3")
		assert real_git is not None
		assert real_python is not None
		_write_exec(
			bin_dir / "git",
			r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

store_path = Path(os.environ['GH_MOCK_STORE'])
store = json.loads(store_path.read_text(encoding='utf-8'))
args = sys.argv[1:]
real_git = os.environ.get('REAL_GIT_BIN', 'git')

if len(args) >= 2 and args[0] == 'merge-tree' and args[1] == '--write-tree' and '--name-only' in args:
	paths = list(store.get('merge_tree_conflict_paths', []))
	if paths:
		print('f' * 40)
		for path in paths:
			print(path)
		sys.exit(1)
	print('f' * 40)
	sys.exit(0)

if len(args) >= 2 and args[0] == 'push' and os.environ.get('MOCK_GIT_PUSH_SUCCESS', '') == 'true':
	store.setdefault('git_push_calls', []).append(args[1:])
	store_path.write_text(json.dumps(store), encoding='utf-8')
	sys.exit(0)

if args and args[0] == 'checkout' and os.environ.get('MOCK_GIT_CHECKOUT_FAIL', '') == 'true':
	sys.exit(1)

if args and args[0] == 'fetch':
	refspec = None
	if len(args) == 4 and args[1] == '--no-tags' and args[2] == 'origin':
		refspec = args[3]
	elif len(args) == 3 and args[1] == 'origin':
		refspec = args[2]
	if refspec:
		calls = store.setdefault('git_fetch_calls', {})
		calls[refspec] = int(calls.get(refspec, 0)) + 1
		fail_after = store.get('mock_git_fetch_fail_after', {}).get(refspec)
		store_path.write_text(json.dumps(store), encoding='utf-8')
		if fail_after is not None and calls[refspec] > int(fail_after):
			sys.exit(1)
	if refspec and ':' in refspec:
		src_ref, dst_ref = refspec.split(':', 1)
		src_prefix = 'refs/heads/'
		dst_prefix = 'refs/remotes/origin/'
		src_branch = src_ref[len(src_prefix):] if src_ref.startswith(src_prefix) else src_ref
		if dst_ref.startswith(dst_prefix):
			dst_branch = dst_ref[len(dst_prefix):]
			known_branches = set(store.get('existing_branches', []))
			known_branches.update(
				str(pr.get('headRefFromApi', pr.get('headRefName', '')))
				for pr in store.get('prs', [])
				if pr.get('headRefFromApi', pr.get('headRefName', ''))
			)
			if src_branch == dst_branch:
				if src_branch in known_branches:
					head_rev = subprocess.run([real_git, 'rev-parse', 'HEAD'], capture_output=True, text=True)
					if head_rev.returncode != 0:
						sys.exit(head_rev.returncode)
					sha = head_rev.stdout.strip()
					update_ref = subprocess.run([real_git, 'update-ref', dst_ref, sha])
					sys.exit(update_ref.returncode)
				sys.exit(1)

proc = subprocess.run([real_git, *args])
sys.exit(proc.returncode)
''',
		)


		_write_exec(
			bin_dir / "codex",
			"""#!/usr/bin/env python3
import json
import os
import sys

# Drain stdin to avoid SIGPIPE on the upstream cat process
# when the prompt file is larger than the OS pipe buffer.
try:
	sys.stdin.read()
except Exception:
	pass

output = os.environ.get('MOCK_CODEX_JSON', '{}')
parsed = json.loads(output)
touch_file = os.environ.get('MOCK_CODEX_TOUCH_FILE', '')
if touch_file:
	with open(touch_file, 'a', encoding='utf-8') as fh:
		fh.write("mock change\\n")
print(json.dumps(parsed))
""",
		)

		_write_exec(
			bin_dir / "python3",
			r'''#!/usr/bin/python3
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
real_python = os.environ.get("REAL_PYTHON_BIN", "python3")
store_path = Path(os.environ.get("GH_MOCK_STORE", ""))


def _script_matches(arg0: str, rel_path: str) -> bool:
	if not arg0:
		return False
	normalized = arg0.replace("\\", "/")
	return normalized == rel_path or normalized.endswith(f"/{rel_path}")

def _load_store() -> dict:
	if not store_path:
		return {}
	if not store_path.exists():
		return {}
	return json.loads(store_path.read_text(encoding="utf-8"))

def _save_store(store: dict) -> None:
	if not store_path:
		return
	store_path.write_text(json.dumps(store), encoding="utf-8")

if len(args) >= 3 and _script_matches(args[0], "scripts/ai_memory.py") and args[1] == "actions-runs-cache":
	store = _load_store()
	cmd = args[2]
	if cmd == "get":
		repo = ""
		i = 3
		while i < len(args):
			if args[i] == "--repo" and i + 1 < len(args):
				repo = args[i + 1]
				i += 2
				continue
			i += 1
		payload = {
			"ok": True,
			"enabled": True,
			"hit": bool(store.get("mock_actions_runs_cache_hit", False)),
			"cache": store.get("mock_actions_runs_cache_payload"),
		}
		override = store.get("mock_actions_runs_cache_get_json")
		if isinstance(override, dict) and override:
			payload.update(override)
		if repo and isinstance(payload.get("cache"), dict):
			payload["cache"].setdefault("repository", repo)
		print(json.dumps(payload))
		sys.exit(0)
	if cmd == "put":
		runs_file = ""
		repo = ""
		etag = ""
		ttl_seconds = ""
		i = 3
		while i < len(args):
			if args[i] == "--runs-file" and i + 1 < len(args):
				runs_file = args[i + 1]
				i += 2
				continue
			if args[i] == "--repo" and i + 1 < len(args):
				repo = args[i + 1]
				i += 2
				continue
			if args[i] == "--etag" and i + 1 < len(args):
				etag = args[i + 1]
				i += 2
				continue
			if args[i] == "--ttl-seconds" and i + 1 < len(args):
				ttl_seconds = args[i + 1]
				i += 2
				continue
			i += 1
		runs = []
		if runs_file:
			try:
				raw = Path(runs_file).read_text(encoding="utf-8")
				parsed = json.loads(raw)
				if isinstance(parsed, dict):
					runs = list(parsed.get("workflow_runs", []))
				elif isinstance(parsed, list):
					runs = parsed
			except Exception:
				runs = []
		store["mock_actions_runs_cache_hit"] = True
		store["mock_actions_runs_cache_payload"] = {
			"schema_version": "v1",
			"repository": repo or "owner/repo",
			"fetched_at": "2026-01-01T00:00:00Z",
			"ttl_seconds": int(ttl_seconds or 60),
			"etag": etag or None,
			"runs": runs,
		}
		store.setdefault("mock_actions_runs_cache_put_calls", 0)
		store["mock_actions_runs_cache_put_calls"] = int(store["mock_actions_runs_cache_put_calls"]) + 1
		_save_store(store)
		payload = {"ok": True, "stored": True, "cache": store["mock_actions_runs_cache_payload"]}
		override = store.get("mock_actions_runs_cache_put_json")
		if isinstance(override, dict) and override:
			payload.update(override)
		print(json.dumps(payload))
		sys.exit(0)

if len(args) >= 3 and _script_matches(args[0], "scripts/ai_memory.py") and args[1] == "validation-history":
	store = _load_store()
	cmd = args[2]
	repo = ""
	integration_sha = ""
	entry_file = ""
	i = 3
	while i < len(args):
		if args[i] == "--repo" and i + 1 < len(args):
			repo = args[i + 1]
			i += 2
			continue
		if args[i] == "--integration-sha" and i + 1 < len(args):
			integration_sha = args[i + 1].lower()
			i += 2
			continue
		if args[i] == "--entry-file" and i + 1 < len(args):
			entry_file = args[i + 1]
			i += 2
			continue
		i += 1
	normalized_repo = repo.lower()
	payload = store.get("mock_validation_history_payload")
	hit = (
		isinstance(payload, dict)
		and payload.get("repository") == normalized_repo
		and str(payload.get("integration_sha", "")).lower() == integration_sha
	)
	if cmd == "get":
		store.setdefault("mock_validation_history_get_calls", 0)
		store["mock_validation_history_get_calls"] = int(store["mock_validation_history_get_calls"]) + 1
		_save_store(store)
		exit_code = int(store.get("mock_validation_history_get_exit_code", 0) or 0)
		if exit_code != 0:
			sys.exit(exit_code)
		response = {
			"ok": True,
			"enabled": True,
			"hit": hit,
			"validation_history": payload if hit else None,
		}
		override = store.get("mock_validation_history_get_json")
		if isinstance(override, dict) and override:
			response.update(override)
		print(json.dumps(response))
		sys.exit(0)
	if cmd == "append":
		store.setdefault("mock_validation_history_append_calls", 0)
		store["mock_validation_history_append_calls"] = int(store["mock_validation_history_append_calls"]) + 1
		_save_store(store)
		exit_code = int(store.get("mock_validation_history_append_exit_code", 0) or 0)
		if exit_code != 0:
			sys.exit(exit_code)
		entry = {}
		if entry_file:
			try:
				entry = json.loads(Path(entry_file).read_text(encoding="utf-8"))
			except Exception:
				entry = {}
		if not hit:
			payload = {
				"schema_version": "v1",
				"repository": normalized_repo,
				"integration_sha": integration_sha,
				"entries": [],
			}
		payload = dict(payload)
		payload["entries"] = [*(payload.get("entries") or []), entry]
		response = {
			"ok": True,
			"enabled": True,
			"stored": True,
			"validation_history": payload,
		}
		override = store.get("mock_validation_history_append_json")
		if isinstance(override, dict) and override:
			response.update(override)
		if response.get("stored", False):
			stored_payload = response.get("validation_history")
			if isinstance(stored_payload, dict):
				store["mock_validation_history_payload"] = stored_payload
			else:
				store["mock_validation_history_payload"] = payload
		_save_store(store)
		print(json.dumps(response))
		sys.exit(0)

if len(args) >= 3 and _script_matches(args[0], "scripts/ai_memory.py") and args[1] == "operator-bypass-audit":
	store = _load_store()
	cmd = args[2]
	repo = ""
	tracking_issue = 0
	integration_sha = ""
	entry_file = ""
	i = 3
	while i < len(args):
		if args[i] == "--repo" and i + 1 < len(args):
			repo = args[i + 1]
			i += 2
			continue
		if args[i] == "--tracking-issue" and i + 1 < len(args):
			tracking_issue = int(args[i + 1])
			i += 2
			continue
		if args[i] == "--integration-sha" and i + 1 < len(args):
			integration_sha = args[i + 1].lower()
			i += 2
			continue
		if args[i] == "--entry-file" and i + 1 < len(args):
			entry_file = args[i + 1]
			i += 2
			continue
		i += 1
	normalized_repo = repo.lower()
	payload = store.get("mock_operator_bypass_audit_payload")
	hit = (
		isinstance(payload, dict)
		and payload.get("repository") == normalized_repo
		and int(payload.get("tracking_issue_number", 0) or 0) == tracking_issue
		and str(payload.get("integration_sha", "")).lower() == integration_sha
	)
	if cmd == "get":
		store.setdefault("mock_operator_bypass_audit_get_calls", 0)
		store["mock_operator_bypass_audit_get_calls"] = int(store["mock_operator_bypass_audit_get_calls"]) + 1
		_save_store(store)
		exit_code = int(store.get("mock_operator_bypass_audit_get_exit_code", 0) or 0)
		if exit_code != 0:
			sys.exit(exit_code)
		response = {
			"ok": True,
			"enabled": True,
			"hit": hit,
			"audit": payload if hit else None,
		}
		override = store.get("mock_operator_bypass_audit_get_json")
		if isinstance(override, dict) and override:
			response.update(override)
		print(json.dumps(response))
		sys.exit(0)
	if cmd == "append":
		store.setdefault("mock_operator_bypass_audit_append_calls", 0)
		store["mock_operator_bypass_audit_append_calls"] = int(store["mock_operator_bypass_audit_append_calls"]) + 1
		_save_store(store)
		exit_code = int(store.get("mock_operator_bypass_audit_append_exit_code", 0) or 0)
		if exit_code != 0:
			sys.exit(exit_code)
		entry = {}
		if entry_file:
			try:
				entry = json.loads(Path(entry_file).read_text(encoding="utf-8"))
			except Exception:
				entry = {}
		if not hit:
			payload = {
				"schema_version": "v1",
				"repository": normalized_repo,
				"tracking_issue_number": tracking_issue,
				"integration_sha": integration_sha,
				"entries": [],
			}
		payload = dict(payload)
		payload["entries"] = [*(payload.get("entries") or []), entry]
		response = {
			"ok": True,
			"enabled": True,
			"stored": True,
			"audit": payload,
		}
		override = store.get("mock_operator_bypass_audit_append_json")
		if isinstance(override, dict) and override:
			response.update(override)
		if response.get("stored", False):
			stored_payload = response.get("audit")
			if isinstance(stored_payload, dict):
				store["mock_operator_bypass_audit_payload"] = stored_payload
			else:
				store["mock_operator_bypass_audit_payload"] = payload
		_save_store(store)
		print(json.dumps(response))
		sys.exit(0)

if len(args) >= 3 and _script_matches(args[0], "scripts/ai_memory.py") and args[1] == "revalidate-events":
	store = _load_store()
	cmd = args[2]
	repo = ""
	tracking_issue = 0
	integration_sha = ""
	entry_file = ""
	i = 3
	while i < len(args):
		if args[i] == "--repo" and i + 1 < len(args):
			repo = args[i + 1]
			i += 2
			continue
		if args[i] == "--tracking-issue" and i + 1 < len(args):
			tracking_issue = int(args[i + 1])
			i += 2
			continue
		if args[i] == "--integration-sha" and i + 1 < len(args):
			integration_sha = args[i + 1].lower()
			i += 2
			continue
		if args[i] == "--entry-file" and i + 1 < len(args):
			entry_file = args[i + 1]
			i += 2
			continue
		i += 1
	normalized_repo = repo.lower()
	payload = store.get("mock_revalidate_events_payload")
	hit = (
		isinstance(payload, dict)
		and payload.get("repository") == normalized_repo
		and int(payload.get("tracking_issue_number", 0) or 0) == tracking_issue
		and str(payload.get("integration_sha", "")).lower() == integration_sha
	)
	if cmd == "get":
		store.setdefault("mock_revalidate_events_get_calls", 0)
		store["mock_revalidate_events_get_calls"] = int(store["mock_revalidate_events_get_calls"]) + 1
		_save_store(store)
		print(json.dumps({
			"ok": True,
			"enabled": True,
			"hit": hit,
			"events": payload if hit else None,
		}))
		sys.exit(0)
	if cmd == "append":
		entry = {}
		if entry_file:
			try:
				entry = json.loads(Path(entry_file).read_text(encoding="utf-8"))
			except Exception:
				entry = {}
		if not hit:
			payload = {
				"schema_version": "v1",
				"repository": normalized_repo,
				"tracking_issue_number": tracking_issue,
				"integration_sha": integration_sha,
				"entries": [],
			}
		payload = dict(payload)
		payload["entries"] = [*(payload.get("entries") or []), entry]
		store["mock_revalidate_events_payload"] = payload
		store.setdefault("mock_revalidate_events_append_calls", 0)
		store["mock_revalidate_events_append_calls"] = int(store["mock_revalidate_events_append_calls"]) + 1
		_save_store(store)
		print(json.dumps({
			"ok": True,
			"enabled": True,
			"stored": True,
			"events": payload,
		}))
		sys.exit(0)

if len(args) >= 3 and _script_matches(args[0], "scripts/ai_memory.py") and args[1] == "branch-rebuild-audit":
	store = _load_store()
	cmd = args[2]
	if cmd == "get":
		payload = {
			"ok": True,
			"enabled": True,
			"hit": isinstance(store.get("mock_branch_rebuild_audit_payload"), dict),
			"audit": store.get("mock_branch_rebuild_audit_payload"),
		}
		override = store.get("mock_branch_rebuild_audit_get_json")
		if isinstance(override, dict) and override:
			payload.update(override)
		print(json.dumps(payload))
		sys.exit(0)
	if cmd == "put":
		audit_file = ""
		i = 3
		while i < len(args):
			if args[i] == "--audit-file" and i + 1 < len(args):
				audit_file = args[i + 1]
				i += 2
				continue
			i += 1
		audit = None
		if audit_file:
			try:
				audit = json.loads(Path(audit_file).read_text(encoding="utf-8"))
			except Exception:
				audit = None
		if isinstance(audit, dict):
			store["mock_branch_rebuild_audit_payload"] = audit
		store.setdefault("mock_branch_rebuild_audit_put_calls", 0)
		store["mock_branch_rebuild_audit_put_calls"] = int(store["mock_branch_rebuild_audit_put_calls"]) + 1
		_save_store(store)
		payload = {
			"ok": True,
			"enabled": True,
			"stored": True,
			"audit": store.get("mock_branch_rebuild_audit_payload"),
		}
		override = store.get("mock_branch_rebuild_audit_put_json")
		if isinstance(override, dict) and override:
			payload.update(override)
		print(json.dumps(payload))
		sys.exit(0)

if len(args) >= 2 and _script_matches(args[0], "scripts/orchestrate_state_v2.py") and args[1] == "pack":
	mode = os.environ.get("MOCK_ORCH_STATE_V2_PACK_MODE", "")
	if mode == "count_mismatch":
		out_dir = ""
		state_file = ""
		i = 2
		while i < len(args):
			if args[i] == "--out-dir" and i + 1 < len(args):
				out_dir = args[i + 1]
				i += 2
				continue
			if args[i] == "--state-file" and i + 1 < len(args):
				state_file = args[i + 1]
				i += 2
				continue
			i += 1
		raw_bytes = 0
		if state_file:
			try:
				raw_bytes = len(Path(state_file).read_bytes())
			except Exception:
				raw_bytes = 0
		out_path = Path(out_dir)
		out_path.mkdir(parents=True, exist_ok=True)
		chunk_path = out_path / "chunk-0001.txt"
		chunk_path.write_text("mock chunk payload\n", encoding="utf-8")
		print(json.dumps({
			"manifest": "0" * 64,
			"total": 2,
			"files": [str(chunk_path)],
			"raw_bytes": raw_bytes,
			"encoded_bytes": len("mock chunk payload\n"),
			"chunk_size": 65280,
		}))
		sys.exit(0)

proc = subprocess.run([real_python, *args])
sys.exit(proc.returncode)
''',
		)

		env = os.environ.copy()
		env.update(
			{
				"HOME": str(home_dir),
				"RUNTIME_DIR": str(runtime_dir),
				"STATE_FILE": str(runtime_dir / "state.json"),
				"JUDGE_PROMPT_FILE": str(runtime_dir / "judge_prompt.txt"),
				"JUDGE_OUTPUT_FILE": str(runtime_dir / "judge_output.txt"),
				"GH_TOKEN": "test-token",
				"OPENROUTER_API_KEY": "test-openrouter",
				"GITHUB_REPOSITORY": "owner/repo",
				"MODEL_EDITOR": "openai/gpt-5.4",
				"MODEL_REASONING_EFFORT_JUDGE": "xhigh",
				"TG_BOT_SECRET": "",
				"TG_ADMIN_CHAT_ID": "",
				"TOOL_CALL_BUDGET_JUDGE": "60",
				"MAX_REVIEW_BLOCKED_RETRIES": "2",
				"MAX_VALIDATION_RECOVERY_ATTEMPTS": "0",
				"ENABLE_STALL_JUDGE": enable_stall_judge,
				"STALL_JUDGE_TRIGGER_COUNT": stall_judge_trigger_count,
				"ENABLE_STALL_HUMAN_TERMINALIZATION": enable_stall_human_terminalization,
				"ENABLE_CLEAN_WAVE_JUDGE_SKIP": enable_clean_wave_judge_skip,
				"JUDGE_REPEAT_FINGERPRINT_MAX": judge_repeat_fingerprint_max,
				"ENABLE_VALIDATION": enable_validation,
				"MAX_VALIDATE_CYCLES": max_validate_cycles,
				"BRANCH_REBUILD_ENABLED": branch_rebuild_enabled,
				"BRANCH_REBUILD_THRESHOLD_HOURS": branch_rebuild_threshold_hours,
				"BRANCH_REBUILD_COOLDOWN_HOURS": branch_rebuild_cooldown_hours,
				"GH_MOCK_STORE": str(store_file),
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"REAL_GIT_BIN": real_git,
				"REAL_PYTHON_BIN": real_python,
				"MOCK_CODEX_JSON": json.dumps(codex_json),
				"MOCK_GIT_PUSH_SUCCESS": "true" if mock_git_push_success else "false",
				"MOCK_GIT_CHECKOUT_FAIL": "true" if mock_git_checkout_fail else "false",
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			}
		)
		if mock_stall_judge_json:
			env["MOCK_STALL_JUDGE_JSON"] = json.dumps(mock_stall_judge_json)
		if codex_touch_file:
			touch_path = Path(codex_touch_file)
			if not touch_path.is_absolute():
				touch_path = runtime_dir / touch_path
			env["MOCK_CODEX_TOUCH_FILE"] = str(touch_path)
		if mock_orch_state_v2_pack_mode:
			env["MOCK_ORCH_STATE_V2_PACK_MODE"] = mock_orch_state_v2_pack_mode
		if env_overrides:
			env.update({str(k): str(v) for k, v in env_overrides.items()})

		proc = _run_poller_subprocess(
			["bash", str(POLLER_SCRIPT)],
			cwd=str(REPO_ROOT),
			env=env,
			sandbox=sandbox,
		)
		if proc.returncode != 0:
			raise AssertionError(
				"poller exited non-zero\n"
				f"stdout:\n{proc.stdout}\n"
				f"stderr:\n{proc.stderr}"
			)

		result = json.loads(store_file.read_text(encoding="utf-8"))
		tracking_issue = result["issues"][str(tracking_num)]
		state_path = runtime_dir / "state.json"
		result["latest_state"] = _extract_latest_state(tracking_issue["comments"])
		result["state_on_disk"] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
		result["task_files"] = _read_task_files(sandbox)
		result["tracking_labels"] = tracking_issue["labels"]
		result["tracking_closed"] = tracking_issue.get("closed", False)
		result["merge_calls"] = result.get("merge_calls", [])
		result["review_dispatches"] = result.get("review_dispatches", [])
		result["update_branch_calls"] = result.get("update_branch_calls", [])
		result["label_create_calls"] = result.get("label_create_calls", [])
		result["api_calls"] = result.get("api_calls", [])
		result["release_dispatches"] = result.get("release_dispatches", [])
		result["issue_body_edit_calls"] = result.get("issue_body_edit_calls", [])
		result["commit_status_posts"] = result.get("commit_status_posts", [])
		result["pr_ready_calls"] = result.get("pr_ready_calls", [])
		result["pr_body_update_calls"] = result.get("pr_body_update_calls", [])
		result["mock_operator_bypass_audit_get_calls"] = int(result.get("mock_operator_bypass_audit_get_calls", 0))
		result["mock_operator_bypass_audit_append_calls"] = int(result.get("mock_operator_bypass_audit_append_calls", 0))
		result["stdout"] = proc.stdout
		result["stderr"] = proc.stderr
		judge_prompt_path = runtime_dir / "judge_prompt.txt"
		result["judge_prompt"] = judge_prompt_path.read_text(encoding="utf-8") if judge_prompt_path.exists() else ""
		result["actions_runs_fetch_count"] = int(result.get("actions_runs_fetch_count", 0))
		result["actions_runs_if_none_match_count"] = int(result.get("actions_runs_if_none_match_count", 0))
		return result


def test_task_state_mirror_disabled_writes_no_task_files():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["depends_on"] = ["issue-0"]
	state["waves"][0]["issues"][0]["reissue_depends_on"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)
	assert result["task_files"] == {}


def test_task_state_mirror_enabled_writes_latest_wave_issue_payloads():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["depends_on"] = ["issue-0"]
	state["waves"][0]["issues"][0]["reissue_depends_on"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		env_overrides={"ORCH_TASK_FILES_ENABLED": "true"},
	)
	latest_issue_state = result["state_on_disk"]["waves"][0]["issues"][0]
	assert result["latest_state"]["waves"][0]["issues"][0] == latest_issue_state
	assert result["task_files"] == {
		"1/issue-1.json": {
			**latest_issue_state,
			"schema_version": "task_state.v1.json",
		}
	}


def test_task_state_mirror_enabled_accepts_uppercase_flag_value():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["depends_on"] = ["issue-0"]
	state["waves"][0]["issues"][0]["reissue_depends_on"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		env_overrides={"ORCH_TASK_FILES_ENABLED": "TRUE"},
	)
	latest_issue_state = result["state_on_disk"]["waves"][0]["issues"][0]
	assert result["task_files"] == {
		"1/issue-1.json": {
			**latest_issue_state,
			"schema_version": "task_state.v1.json",
		}
	}


def test_task_state_mirror_enabled_unblocks_dependents_after_checkpoint_mirror():
	state = _base_state(status="in_progress")
	state["total_issues"] = 2
	state["issue_number_map"]["issue-2"] = 11
	state["waves"][0]["issues"] = [
		{"id": "issue-1", "github_issue": 10, "status": "merged"},
		{
			"id": "issue-2",
			"github_issue": 11,
			"status": "pending",
			"depends_on": ["issue-1"],
			"reissue_depends_on": [10, 999],
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 11: []},
		env_overrides={"ORCH_TASK_FILES_ENABLED": "true"},
	)
	latest_issue_one_state, latest_issue_two_state = result["state_on_disk"]["waves"][0]["issues"]
	assert result["task_files"] == {
		"1/issue-1.json": {
			**latest_issue_one_state,
			"schema_version": "task_state.v1.json",
		},
		"1/issue-2.json": {
			**latest_issue_two_state,
			"depends_on": [],
			"reissue_depends_on": [999],
			"schema_version": "task_state.v1.json",
		},
	}


# ---------------------------------------------------------------------------
# Tests: orchestrate poll validation lifecycle
# ---------------------------------------------------------------------------


def test_label_batch_graphql_error_falls_back_to_rest():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		gql_mode="error",
	)
	assert result["label_batch_graphql_calls"] == 1
	assert result["issue_label_calls"].get("10", 0) > 0


def test_label_batch_graphql_partial_falls_back_to_rest():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		gql_mode="partial",
	)
	assert result["label_batch_graphql_calls"] == 1
	assert result["issue_label_calls"].get("10", 0) > 0


def test_label_batch_graphql_full_skips_rest_fallback():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		gql_mode="full",
	)
	assert result["label_batch_graphql_calls"] == 1
	assert result["issue_label_calls"].get("10", 0) == 0


def test_current_wave_state_graphql_full_skips_rest_fallback():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		gql_mode="full",
	)
	assert result["candidate_details_graphql_calls"] > 0
	assert result["issue_state_calls"].get("10", 0) == 0


def test_current_wave_state_graphql_partial_falls_back_to_rest():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		gql_mode="partial",
	)
	assert result["candidate_details_graphql_calls"] > 0
	assert result["issue_state_calls"].get("10", 0) > 0


def test_current_wave_state_graphql_error_falls_back_to_rest():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		gql_mode="error",
	)
	assert result["candidate_details_graphql_calls"] > 0
	assert result["issue_state_calls"].get("10", 0) > 0


def test_ensure_label_exists_avoids_repo_label_get_probe_and_accepts_already_exists():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		label_create_responses={
			"ai:merged": {
				"exit_code": 1,
				"stderr": "HTTP 422 Unprocessable Entity: already_exists",
			},
		},
	)
	assert result["latest_state"]["status"] == "complete"
	assert "ai:merged" in result["label_create_calls"]
	assert not any("/labels/" in path for path in result["api_calls"])
	assert "label already exists, skipping 'ai:merged'" in result["stderr"]


def test_complete_verdict_enters_validation_mode_when_enabled():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "validating"
	assert result["latest_state"]["judge_cycle"] == 1
	assert result["latest_state"]["validation_cycle"] == 1
	assert "ai:validating" in result["tracking_labels"]
	assert result["tracking_closed"] is False
	assert len(result["validation_dispatches"]) == 1
	assert result["validation_dispatches"][0]["ref"] == "orchestrator/project-192"
	assert result["merge_calls"]
	assert result["merge_calls"][0]["base"] == "orchestrator/project-192"
	assert result["merge_calls"][0]["head"] == "main"


def test_complete_verdict_dispatches_validation_with_closed_wave_issue_no_pr():
	"""Regression for the validate-dispatch deadlock (hylifegroup.com#3).

	A final wave whose issues are all ``ai:merged`` except one legitimately
	closed without a merged PR (``ai:closed`` — e.g. a judge-fix-up that needed
	no code change) reconciles to ``wave_complete=true`` AND ``any_failed=true``.
	The judge still declares the project complete (the WAVE_COMPLETE hard guard
	does not block on ANY_FAILED), so the poller transitions to ``ai:validating``
	and reaches ``dispatch_validation_if_needed``.

	Before the fix, that helper's preflight also gated on ``ANY_FAILED=true`` and
	deferred dispatch every cycle, wedging the project in ``ai:validating``
	indefinitely (validation never dispatched -> never earned ``ai:validated`` ->
	integration never merged -> tracking issue never closed). The gate now keys
	on ``WAVE_COMPLETE`` only, so validation must dispatch on this tick."""
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["total_issues"] = 2
	state["waves"][0]["issues"].append(
		{"id": "issue-2", "github_issue": 11, "status": "pending"}
	)
	state["issue_number_map"]["issue-2"] = 11
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		# Force the judge invocation (default verdict "complete") rather than
		# the clean-wave skip, so the test drives the judge-complete ->
		# dispatch_validation_if_needed path that the deadlock lived on.
		enable_clean_wave_judge_skip="false",
		issue_labels={10: ["ai:merged"], 11: ["ai:closed"]},
		issue_closed={11: True},
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "validating", result["stdout"]
	assert "ai:validating" in result["tracking_labels"]
	assert result["tracking_closed"] is False
	# The fix: validation dispatches despite ANY_FAILED=true from the closed
	# wave issue. Pre-fix this list was empty and the run deferred.
	assert len(result["validation_dispatches"]) == 1, result["stdout"]
	assert result["validation_dispatches"][0]["ref"] == "orchestrator/project-192"
	assert (
		"deferring validate dispatch" not in result["stdout"]
	), result["stdout"]


def test_complete_verdict_still_defers_validation_for_failed_wave_phase():
	"""Keep the deadlock fix from widening into failed-wave misdispatch.

	``ANY_FAILED`` is broader than the judge-cleared closed-without-merge case:
	an explicit failed terminal phase such as ``ai:plan-failed`` still produces
	``any_failed=true`` and must continue to block validation dispatch, even if
	the wave reconciles to ``wave_complete=true``. Without that narrower gate,
	the poller dispatches runtime validation against a project the current wave
	still marks as failed.
	"""
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["total_issues"] = 2
	state["waves"][0]["issues"].append(
		{"id": "issue-2", "github_issue": 11, "status": "pending"}
	)
	state["issue_number_map"]["issue-2"] = 11
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		issue_labels={10: ["ai:plan-failed"], 11: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "validating", result["stdout"]
	assert result["validation_dispatches"] == [], result["stdout"]
	assert (
		"wave includes failed issue statuses other than adjudicated closed-without-merge"
		in result["stdout"]
	), result["stdout"]


def test_complete_verdict_still_defers_validation_for_closed_plus_failed_labels():
	"""A contradictory ``ai:closed`` label must not hide a failure label.

	``determine_phase`` prioritises ``ai:closed`` over failure labels, so the
	validate-dispatch safety signal must inspect the full label set rather than
	rely on the single derived phase. Otherwise a wave issue carrying both
	``ai:closed`` and ``ai:plan-failed`` wrongly looks like the intended
	closed-without-merge bypass and validation dispatches against a failed wave.
	"""
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["total_issues"] = 2
	state["waves"][0]["issues"].append(
		{"id": "issue-2", "github_issue": 11, "status": "pending"}
	)
	state["issue_number_map"]["issue-2"] = 11
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		issue_labels={10: ["ai:closed", "ai:plan-failed"], 11: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "validating", result["stdout"]
	assert result["validation_dispatches"] == [], result["stdout"]
	assert (
		"wave includes failed issue statuses other than adjudicated closed-without-merge"
		in result["stdout"]
	), result["stdout"]


def test_complete_verdict_enters_validation_mode_when_enable_validation_is_mixed_case_truthy():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="TrUe",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "validating"
	assert "ai:validating" in result["tracking_labels"]
	assert result["tracking_closed"] is False
	assert len(result["validation_dispatches"]) == 1



def test_complete_verdict_redispatches_validation_when_previous_dispatch_cycle_exists():
	state = _base_state(status="in_progress")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_cycle"] = 1
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "validating"
	assert result["latest_state"]["validation_last_dispatch_cycle"] == 1
	assert len(result["validation_dispatches"]) == 1



def test_complete_verdict_keeps_open_when_validation_disabled():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 350,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["tracking_closed"] is False
	assert result["validation_dispatches"] == []
	assert "ai:merged" in result["tracking_labels"]
	assert result["latest_state"]["final_merge_pr"] == 350
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["release_dispatches"] == []


def test_complete_verdict_override_reports_pending_wave_not_final_wave():
	state = _base_state(status="in_progress")
	state["total_issues"] = 2
	state["total_waves"] = 2
	state["waves"].append(
		{
			"wave": 2,
			"issues": [
				{"id": "issue-2", "github_issue": 11, "status": "pending"},
			],
		}
	)
	state["issue_number_map"]["issue-2"] = 11
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		issue_labels={10: ["ai:merged"], 11: []},
	)
	assert result["latest_state"]["status"] == "in_progress"
	assert result["latest_state"]["current_wave"] == 2
	tracking_comments = [
		str((c or {}).get("body", ""))
		for c in result["issues"][str(192)]["comments"]
	]
	override_comments = [
		body for body in tracking_comments
		if "Judge verdict overridden" in body
	]
	assert override_comments, "Expected override comment when future wave work remains."
	assert "pending wave state" in override_comments[-1]
	assert "wave_complete=true" in override_comments[-1]
	assert "wave=1/2" in override_comments[-1]
	assert "not the final wave" not in override_comments[-1]
	assert "project still has pending wave state" in result["stdout"]
	assert "not the final wave" not in result["stdout"]


def test_complete_verdict_falls_through_to_finalize_on_integration_drift():
	"""Regression for the deadlock surfaced by ``shubhodeep1/bitsafe.io#325``.

	PR #2778 folded ``integration_contained_in_default`` into the
	``project_complete`` flag computed by ``cmd_check_wave_status``. That
	collided with the pre-existing ``Hard guard: judge cannot declare
	"complete" while waves remain`` override in
	``scripts/orchestrate_poll_process.sh``, which
	consumed ``PROJECT_COMPLETE`` and so began overriding ``complete`` →
	``in_progress`` whenever the integration branch had drifted ahead of
	default (``ahead_by > 0``) — even when every wave issue was merged.
	The override blocked the only per-tick caller of
	``finalize_integration_merge_if_needed`` (the ``complete)`` arm in the
	main judge-verdict handler), while PR #2778 had extended
	``finalize_integration_merge_if_needed`` itself to clear the stale
	``merged`` pin and reopen a fresh final PR. The
	two mechanisms gated each other and the orchestrator looped forever.

	This test pins ``compare_ahead_by=5`` (integration drift) with every
	wave issue merged. The fix narrows the override to fire only when
	waves themselves are pending, so the ``complete)`` arm reaches
	``finalize_integration_merge_if_needed`` and merges the final PR."""
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 350,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert result["latest_state"]["status"] == "complete", (
		"complete) arm must run despite integration drift; got status="
		f"{result['latest_state'].get('status')!r}, stderr=\n{result['stderr']}"
	)
	assert result["latest_state"]["final_merge_pr"] == 350
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert "ai:merged" in result["tracking_labels"]
	tracking_comments = [
		str((c or {}).get("body", ""))
		for c in result["issues"][str(192)]["comments"]
	]
	override_comments = [
		body for body in tracking_comments
		if "Judge verdict overridden" in body
	]
	assert not override_comments, (
		"Override guard must not fire when only integration drift remains; "
		f"got override comment(s): {override_comments!r}"
	)
	assert "Overriding to 'in_progress'" not in (result["stdout"] + result["stderr"]), (
		"Override warning must not be logged when only integration drift "
		f"remains; stdout=\n{result['stdout']}\nstderr=\n{result['stderr']}"
	)


def test_complete_verdict_enters_validation_and_finishes_after_integration_drift():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_cycle"] = 2
	prs = [
		{
			"number": 350,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	first = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert first["latest_state"]["status"] == "validating"
	assert first["latest_state"]["validation_cycle"] == 2
	assert first["latest_state"]["final_merge_status"] == "pending"
	assert first["latest_state"].get("validation_completed_cycle") is None
	assert "ai:validating" in first["tracking_labels"]
	# Validation must actually dispatch against the integration branch even
	# though it is ahead of default (ahead_by=5). Asserting the dispatch —
	# not just the status flip — is what guards against the
	# validate-needs-merge / merge-needs-validate deadlock: an earlier
	# revision gated dispatch on PROJECT_COMPLETE (which folds in
	# ahead_by==0) and so silently never dispatched here.
	assert len(first["validation_dispatches"]) == 1, (
		"validation must dispatch under integration drift; got "
		f"{first['validation_dispatches']!r}, stderr=\n{first['stderr']}"
	)
	assert first["validation_dispatches"][0]["ref"] == "orchestrator/project-192"
	assert first["latest_state"]["validation_last_dispatch_cycle"] == 2
	first_tracking_comments = [
		str((c or {}).get("body", ""))
		for c in first["issues"][str(192)]["comments"]
	]
	assert not any("Judge verdict overridden" in body for body in first_tracking_comments), (
		"Validation-enabled drift must not trip the pending-wave override; "
		f"got tracking comments: {first_tracking_comments!r}"
	)
	assert "Overriding to 'in_progress'" not in (first["stdout"] + first["stderr"]), (
		"Validation-enabled drift must reach the complete verdict handler; "
		f"stdout=\n{first['stdout']}\nstderr=\n{first['stderr']}"
	)

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert second["latest_state"]["status"] == "complete"
	assert second["latest_state"]["validation_cycle"] == 2
	assert second["latest_state"]["validation_completed_cycle"] == 2
	assert second["latest_state"]["final_merge_pr"] == 350
	assert second["latest_state"]["final_merge_status"] == "merged"
	assert "ai:validated" in second["tracking_labels"]
	assert "Overriding to 'in_progress'" not in (second["stdout"] + second["stderr"]), (
		"Validation completion after drift must not re-trigger the pending-wave override; "
		f"stdout=\n{second['stdout']}\nstderr=\n{second['stderr']}"
	)


def test_validation_dispatches_under_integration_drift_when_validation_enabled():
	"""Regression for the integration-branch validation-dispatch deadlock
	(reported from consumer ``shubhodeep1/radateeree-resort.com#3`` against
	``coding-workflows@stable``).

	For a project that uses a separate integration branch with validation
	enabled, the judge ``complete)`` arm transitions to ``status=validating``
	without first merging integration→default (that merge is intentionally
	deferred until ``ai:validated``). ``dispatch_validation_if_needed`` then
	gated its preflight on ``PROJECT_COMPLETE``, which folds in
	``integration_contained_in_default`` (``ahead_by==0``). While the
	integration branch is ahead of default (``ahead_by>0``), that gate is
	never satisfied, so validation never dispatches; but the
	integration→default merge that would clear it only runs after
	``ai:validated`` — which can only be earned by a validation run that
	never dispatches. Permanent stall.

	The fix gates the preflight on ``WAVE_COMPLETE`` / ``ANY_FAILED`` (wave
	PRs merged into the integration branch) instead of ``PROJECT_COMPLETE``,
	because validation runs against ``ref=integration_branch``. This test
	pins ``compare_ahead_by=5`` and asserts validation dispatches against the
	integration branch on the judge-complete entry AND again on a subsequent
	poll that re-enters the function from the steady-state ``validating``
	loop (where ``PROJECT_COMPLETE`` is unset and the live gate is recomputed
	by ``refresh_validation_dispatch_wave_gate``)."""
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	kw = dict(
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)

	# First poll: judge declares complete; the validation-enabled complete)
	# arm must reach a dispatch despite ahead_by=5.
	first = _run_poller(state=state, **kw)
	assert first["latest_state"]["status"] == "validating"
	assert len(first["validation_dispatches"]) == 1, (
		"validation must dispatch from the judge-complete arm under "
		f"integration drift; got {first['validation_dispatches']!r}, "
		f"stderr=\n{first['stderr']}"
	)
	assert first["validation_dispatches"][0]["ref"] == "orchestrator/project-192"
	assert first["latest_state"]["validation_last_dispatch_cycle"] == 1
	# The old PROJECT_COMPLETE-gated deferral line must be gone.
	assert "Preflight: PROJECT_COMPLETE=" not in (first["stdout"] + first["stderr"]), (
		"dispatch must not defer on PROJECT_COMPLETE while integration is "
		f"ahead of default; stdout=\n{first['stdout']}\nstderr=\n{first['stderr']}"
	)

	# Steady-state validating loop: reset the dispatch marker (as a stale /
	# redispatch tick would) and confirm the function re-enters from the
	# validating arm — where PROJECT_COMPLETE is unset on entry — and still
	# dispatches once the live wave gate is recomputed. ahead_by stays 5.
	resumed = dict(first["latest_state"])
	resumed["validation_last_dispatch_cycle"] = 0
	resumed["validation_last_dispatch_ts"] = 0
	second = _run_poller(
		state=resumed,
		tracking_labels=["ai:validating"],
		**kw,
	)
	assert second["latest_state"]["status"] == "validating"
	assert len(second["validation_dispatches"]) == 1, (
		"validation must re-dispatch from the steady-state validating loop "
		f"under integration drift; got {second['validation_dispatches']!r}, "
		f"stderr=\n{second['stderr']}"
	)
	assert second["validation_dispatches"][0]["ref"] == "orchestrator/project-192"
	assert second["latest_state"]["final_merge_status"] == "pending"


def test_integration_ahead_creates_and_reuses_eager_draft_pr_before_validation_complete():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	first = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert first["latest_state"]["status"] == "validating"
	assert first["latest_state"]["final_merge_status"] == "pending"
	assert len(first["prs"]) == 1
	pr = first["prs"][0]
	assert first["latest_state"]["final_merge_pr"] == pr["number"]
	assert pr["headRefName"] == "orchestrator/project-192"
	assert pr["baseRefName"] == "main"
	assert pr.get("draft") is True
	assert "<!-- VALIDATION_STATUS_V1 -->" in pr.get("body", "")
	assert "Awaiting validation." in pr.get("body", "")
	assert first["pr_ready_calls"] == []
	assert "EAGER_DRAFT_PR_CREATED" in (first["stdout"] + first["stderr"])

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=first["tracking_labels"],
		issue_labels={10: ["ai:merged"]},
		prs=first["prs"],
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert second["latest_state"]["final_merge_pr"] == pr["number"]
	assert len(second["prs"]) == 1
	assert second["prs"][0]["number"] == pr["number"]
	assert second["prs"][0].get("draft") is True
	assert second["pr_ready_calls"] == []
	assert second["pr_body_update_calls"] == []


def test_integration_ahead_recovers_when_eager_pr_create_races_existing_open_pr():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		mock_pr_create_race_pr={
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"draft": True,
		},
	)
	assert result["latest_state"]["status"] == "validating"
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert len(result["prs"]) == 1
	pr = result["prs"][0]
	assert result["latest_state"]["final_merge_pr"] == pr["number"]
	assert pr["headRefName"] == "orchestrator/project-192"
	assert pr["baseRefName"] == "main"
	assert pr.get("draft") is True
	assert "<!-- VALIDATION_STATUS_V1 -->" in pr.get("body", "")
	assert result["pr_body_update_calls"] == [pr["number"]]


def test_missing_integration_branch_marks_failed():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		existing_branches=["main"],
	)
	assert result["latest_state"]["status"] == "failed"
	assert result["latest_state"]["final_merge_status"] == "failed"
	assert "final_merge_error" in result["latest_state"]
	assert result["release_dispatches"] == []

def test_comprehensive_pending_complete_dispatches_release_with_metadata():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 351,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:comprehensive-test-pending"],
		tracking_comments=[
			"<!-- COMPREHENSIVE_RELEASE_METADATA_V1 -->\nversion_tag: v9.9.9\ntest_repo: owner/release-tests\n<!-- /COMPREHENSIVE_RELEASE_METADATA_V1 -->",
			"version_tag: v0.0.1\ntest_repo: attacker/repo",
		],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert len(result["release_dispatches"]) == 1
	dispatch = result["release_dispatches"][0]
	assert dispatch["workflow"] == "test-and-mark-stable.yml"
	assert dispatch["ref"] == "stable"
	assert dispatch["dry_run"] == "false"
	assert dispatch["version_tag"] == "v9.9.9"
	assert dispatch["test_repo"] == "owner/release-tests"
	assert "ai:comprehensive-test-pending" not in result["tracking_labels"]


def test_comprehensive_pending_complete_dispatches_release_without_optional_metadata():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 352,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:comprehensive-test-pending"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert len(result["release_dispatches"]) == 1
	dispatch = result["release_dispatches"][0]
	assert dispatch["workflow"] == "test-and-mark-stable.yml"
	assert dispatch["ref"] == "stable"
	assert dispatch["dry_run"] == "false"
	assert "version_tag" not in dispatch
	assert "test_repo" not in dispatch
	assert "ai:comprehensive-test-pending" not in result["tracking_labels"]


def test_comprehensive_pending_already_complete_dispatches_release():
	state = _base_state(status="complete")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 353,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:comprehensive-test-pending"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert len(result["release_dispatches"]) == 1
	dispatch = result["release_dispatches"][0]
	assert dispatch["workflow"] == "test-and-mark-stable.yml"
	assert dispatch["ref"] == "stable"
	assert dispatch["dry_run"] == "false"
	assert "ai:comprehensive-test-pending" not in result["tracking_labels"]


def test_comprehensive_pending_failed_does_not_dispatch_release():
	state = _base_state(status="failed")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:comprehensive-test-pending"],
		issue_labels={10: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "failed"
	assert result["release_dispatches"] == []
	assert "ai:comprehensive-test-pending" not in result["tracking_labels"]


def test_comprehensive_pending_complete_dispatch_failure_is_retryable():
	state = _base_state(status="complete")
	state["integration_branch"] = "orchestrator/project-192"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:comprehensive-test-pending"],
		issue_labels={10: ["ai:merged"]},
		fail_release_dispatch=True,
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["release_dispatches"] == []
	assert "ai:comprehensive-test-pending" in result["tracking_labels"]


def test_wave_judge_uses_integration_branch_context_when_available():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		existing_branches=["main", "orchestrator/project-192"],
		enable_clean_wave_judge_skip="false",
	)
	assert "Judge execution context for tracking #192: source=integration_branch" in result["stdout"]
	assert "sentinel_present=true" in result["stdout"]
	assert "Judge context sentinel for tracking #192: integration-branch-only-symbol" in result["stdout"]


def test_wave_judge_uses_default_branch_context_without_integration_metadata():
	state = _base_state(status="in_progress")
	state["integration_branch"] = ""
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		enable_clean_wave_judge_skip="false",
	)
	assert "Judge execution context for tracking #192: source=default_branch" in result["stdout"]
	assert "sentinel_present=false" in result["stdout"]
	assert "Judge context sentinel for tracking #192:" not in result["stdout"]


def test_review_blocked_merged_followup_retargets_to_integration_branch():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"][0]["status"] = "review-blocked"

	# The pr_api_sequence entries are consumed sequentially by every
	# gh api /pulls/N call.  The reconciliation loop calls
	# _issue_cross_ref_pr_number_last (which triggers the REST timeline
	# enrichment path — one extra /pulls/N call) PLUS _fetch_pr_json,
	# consuming 2 entries before the review-blocked handler even starts.
	# Provide enough "open" entries so the reconciliation sees the PR as
	# open (keeping the issue review-blocked) and the review-blocked
	# handler's own flow sees the open→merged transition.
	_open_pr = {
		"number": 901,
		"state": "open",
		"merged": False,
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	_merged_pr = {
		"number": 901,
		"state": "closed",
		"merged": True,
		"merged_at": "2026-04-15T00:00:00Z",
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		# 4 open entries absorb: sync-superseded timeline enrichment,
		# sync-superseded _fetch_pr_json, reconciliation timeline
		# enrichment, reconciliation _fetch_pr_json.  The merged entry
		# is first seen by the review-blocked handler.
		pr_api_sequence={
			901: [dict(_open_pr) for _ in range(4)] + [dict(_merged_pr)],
		},
		prs=[
			{
				"number": 901,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-15T00:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"mergeable": True,
				"mergeable_state": "clean",
				"title": "Test PR",
				"body": "Body",
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		codex_json={
			"action": "fix",
			"justification": "apply fixes",
			"fix_description": "patched",
			"remaining_issues_summary": "none",
		},
		codex_touch_file="sandbox_fix.txt",
		mock_git_push_success=True,
	)


	followup_prs = [pr for pr in result["prs"] if int(pr.get("number", 0)) != 901]
	assert any(pr.get("baseRefName") == "orchestrator/project-192" for pr in followup_prs)
	assert not any(
		pr.get("headRefName", "").startswith("fix/10-followup-") and pr.get("baseRefName") == "main"
		for pr in followup_prs
	)


def test_review_blocked_merged_followup_refuses_default_base_when_active_integration_branch_unavailable():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"][0]["status"] = "review-blocked"

	# Extra open entries for reconciliation timeline enrichment (see
	# test_review_blocked_merged_followup_retargets_to_integration_branch).
	_open_pr = {
		"number": 901,
		"state": "open",
		"merged": False,
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	_merged_pr = {
		"number": 901,
		"state": "closed",
		"merged": True,
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		# 4 open entries absorb pre-handler PR API calls (sync timeline
		# enrichment, sync _fetch_pr_json, reconciliation timeline
		# enrichment, reconciliation _fetch_pr_json).
		pr_api_sequence={
			901: [dict(_open_pr) for _ in range(4)] + [dict(_merged_pr)],
		},
		prs=[
			{
				"number": 901,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-15T00:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"mergeable": True,
				"mergeable_state": "clean",
				"title": "Test PR",
				"body": "Body",
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		fail_branch_ref_not_found_after={"orchestrator/project-192": 1},
		codex_json={
			"action": "fix",
			"justification": "apply fixes",
			"fix_description": "patched",
			"remaining_issues_summary": "none",
		},
		codex_touch_file="sandbox_fix.txt",
		mock_git_push_success=True,
	)


	assert len(result["prs"]) == 1
	assert "Aborting follow-up PR creation to avoid targeting main" in (result["stdout"] + result["stderr"])


def test_review_blocked_merged_followup_keeps_default_base_when_no_integration_context():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "review-blocked"

	# Extra open entries for reconciliation timeline enrichment (see
	# test_review_blocked_merged_followup_retargets_to_integration_branch).
	_open_pr = {
		"number": 901,
		"state": "open",
		"merged": False,
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	_merged_pr = {
		"number": 901,
		"state": "closed",
		"merged": True,
		"merged_at": "2026-04-15T00:00:00Z",
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		# 2 open entries absorb: reconciliation timeline enrichment and
		# reconciliation _fetch_pr_json (no sync calls — no integration
		# branch configured).
		pr_api_sequence={
			901: [dict(_open_pr) for _ in range(2)] + [dict(_merged_pr), dict(_merged_pr)],
		},
		prs=[
			{
				"number": 901,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-15T00:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"mergeable": True,
				"mergeable_state": "clean",
				"title": "Test PR",
				"body": "Body",
			},
		],
		existing_branches=["main"],
		fail_issue_comment_get_after={192: 1},
		codex_json={
			"action": "fix",
			"justification": "apply fixes",
			"fix_description": "patched",
			"remaining_issues_summary": "none",
		},
		codex_touch_file="sandbox_fix.txt",
		mock_git_push_success=True,
	)


	followup_prs = [pr for pr in result["prs"] if int(pr.get("number", 0)) != 901]
	assert any(pr.get("baseRefName") == "main" for pr in followup_prs)


def test_review_blocked_followup_refusal_increments_retry_counter():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"][0]["status"] = "review-blocked"

	# Extra open entries for reconciliation timeline enrichment (see
	# test_review_blocked_merged_followup_retargets_to_integration_branch).
	_open_pr = {
		"number": 901,
		"state": "open",
		"merged": False,
		"merged_at": None,
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	_merged_pr = {
		"number": 901,
		"state": "closed",
		"merged": True,
		"merged_at": "2026-04-15T00:00:00Z",
		"baseRefName": "main",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
		"title": "Test PR",
		"body": "Body",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		# 4 open entries absorb pre-handler PR API calls (sync timeline
		# enrichment, sync _fetch_pr_json, reconciliation timeline
		# enrichment, reconciliation _fetch_pr_json).
		pr_api_sequence={
			901: [dict(_open_pr) for _ in range(4)] + [dict(_merged_pr)],
		},
		prs=[
			{
				"number": 901,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-15T00:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"mergeable": True,
				"mergeable_state": "clean",
				"title": "Test PR",
				"body": "Body",
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		fail_branch_ref_not_found_after={"orchestrator/project-192": 1},
		codex_json={
			"action": "fix",
			"justification": "apply fixes",
			"fix_description": "patched",
			"remaining_issues_summary": "none",
		},
		codex_touch_file="sandbox_fix.txt",
		mock_git_push_success=True,
	)


	assert len(result["prs"]) == 1
	assert result["latest_state"]["review_blocked_retries"].get("10") == 1


def test_sync_superseded_sets_state_once_and_skips_future_sync_attempts():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "main"
	prs = [
		{
			"number": 901,
			"state": "closed",
			"merged": False,
			"baseRefName": "main",
			"headRefName": "ai/issue-10",
			"mergeable": None,
			"mergeable_state": "unknown",
			"files": ["scripts/orchestrate_poll_process.sh"],
		},
	]
	first = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		issue_linked_prs={10: 901},
		prs=prs,
		existing_branches=["main"],
	)
	assert first["latest_state"]["sync"]["status"] == "superseded-by-main"
	assert first["latest_state"]["sync"]["superseded_notified"] is True
	assert first["latest_state"]["final_merge_status"] == "superseded-by-main"
	assert first["merge_calls"] == []

	first_comment_bodies = [c.get("body", "") for c in first["issues"]["192"]["comments"]]
	first_superseded_comments = [
		body
		for body in first_comment_bodies
		if "Integration branch superseded by" in body
	]
	assert len(first_superseded_comments) == 1

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=[
			body for body in first_comment_bodies if not _is_state_comment(body)
		],
		issue_labels={10: ["ai:merged"]},
		issue_linked_prs={10: 901},
		prs=prs,
		existing_branches=["main"],
	)
	second_comment_bodies = [c.get("body", "") for c in second["issues"]["192"]["comments"]]
	second_superseded_comments = [
		body
		for body in second_comment_bodies
		if "Integration branch superseded by" in body
	]
	assert len(second_superseded_comments) == 1
	assert second["merge_calls"] == []


def test_superseded_state_reactivates_when_timeline_lookup_fails_for_other_issue():
	state = _base_state(status="in_progress")
	state["status"] = "done"
	state["total_issues"] = 2
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_status"] = "superseded-by-main"
	state["final_merge_pr"] = 300
	state["sync"] = {
		"status": "superseded-by-main",
		"superseded_notified": True,
		"last_sync_outcome": "superseded-skip",
		"superseded_at": "2026-04-14T00:00:00Z",
	}
	state["waves"][0]["issues"].append({"id": "issue-2", "github_issue": 11, "status": "pending"})
	state["issue_number_map"]["issue-2"] = 11
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 11: ["ai:implementing"]},
		issue_linked_prs={11: 902},
		prs=[
			{
				"number": 902,
				"state": "open",
				"merged": False,
				"baseRefName": "main",
				"headRefName": "ai/issue-11",
				"mergeable": None,
				"mergeable_state": "unknown",
				"files": ["scripts/orchestrate_poll_process.sh"],
			},
		],
		timeline_fail_for_issues=[10],
		existing_branches=["main", "orchestrator/project-192"],
		merge_conflict_on_sync=True,
	)
	assert result["latest_state"]["sync"]["status"] == "conflict"
	assert "Integration sync conflict" in "\n".join(c.get("body", "") for c in result["issues"]["192"]["comments"])
	assert "keeping sync paused for now" not in (result["stdout"] + result["stderr"])


def test_sync_conflict_comment_includes_paths_and_runbook_link():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "main"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main"],
		merge_conflict_on_sync=True,
		merge_tree_conflict_paths=["src/a.py", "src/b.py"],
	)
	assert result["latest_state"]["sync"]["status"] == "conflict"
	assert result["latest_state"]["sync"]["last_conflict_paths"] == ["src/a.py", "src/b.py"]

	conflict_comments = [
		c.get("body", "")
		for c in result["issues"]["192"]["comments"]
		if "## ⚠️ Integration sync conflict" in c.get("body", "")
	]
	assert len(conflict_comments) == 1
	assert "- `src/a.py`" in conflict_comments[0]
	assert "- `src/b.py`" in conflict_comments[0]
	assert "orchestrator-integration-branch-rebuild-runbook.md" in conflict_comments[0]


def test_sync_conflict_dedupe_skips_identical_warnings():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "main"
	first = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main"],
		merge_conflict_on_sync=True,
		merge_tree_conflict_paths=["src/a.py"],
	)
	first_comment_bodies = [c.get("body", "") for c in first["issues"]["192"]["comments"]]

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=[
			body for body in first_comment_bodies if not _is_state_comment(body)
		],
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main"],
		merge_conflict_on_sync=True,
		merge_tree_conflict_paths=["src/a.py"],
	)
	conflict_comments = [
		c.get("body", "")
		for c in second["issues"]["192"]["comments"]
		if "## ⚠️ Integration sync conflict" in c.get("body", "")
	]
	assert len(conflict_comments) == 1


def test_sync_conflict_posts_again_when_conflict_set_changes():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "main"
	first = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main"],
		merge_conflict_on_sync=True,
		merge_tree_conflict_paths=["src/a.py"],
	)
	first_comment_bodies = [c.get("body", "") for c in first["issues"]["192"]["comments"]]

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=[
			body for body in first_comment_bodies if not _is_state_comment(body)
		],
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main"],
		merge_conflict_on_sync=True,
		merge_tree_conflict_paths=["src/b.py"],
	)
	conflict_comments = [
		c.get("body", "")
		for c in second["issues"]["192"]["comments"]
		if "## ⚠️ Integration sync conflict" in c.get("body", "")
	]
	assert len(conflict_comments) == 2
	assert "- `src/b.py`" in conflict_comments[-1]


def test_sync_conflict_escalates_to_judge_immediately_after_retry_budget_exhausted():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 3
	# Keep the dispatch timestamp inside cooldown so this test verifies that
	# retry-budget exhaustion takes priority over cooldown deferral.
	state["integration_conflict_dispatch_ts"] = 9999999999
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main", "orchestrator/project-192"],
		merge_conflict_on_sync=True,
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Integration judge invoked" in body for body in tracking_bodies)
	assert result["review_dispatches"] == []

def test_final_merge_conflict_sets_merge_conflict_status():
	# Regression coverage for the self-healing flow introduced in PR #918
	# (issue #832). When the final integration->default PR is unmergeable,
	# finalize_integration_merge_if_needed must NOT halt the project with
	# status=merge_conflict (the legacy stall behavior). Instead it must
	# route the PR through heal_integration_branch_conflict, which
	# dispatches the review/autofix workflow against the final PR and
	# leaves the project status=in_progress so the next poll tick can
	# retry the merge after automated conflict resolution. The historical
	# test name is preserved per CLAUDE.md §6 (Naming Immutability).
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 351,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": False,
			"mergeable_state": "dirty",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	# Project must NOT halt with status=merge_conflict anymore — the
	# self-healing flow keeps the project in_progress so finalize can
	# retry on the next poll tick.
	assert result["latest_state"]["status"] == "in_progress"
	# The final PR is still recorded (eager final-PR handling) but its
	# merge is deferred to the next tick (final_merge_status=pending),
	# not flagged as a terminal "conflict".
	assert result["latest_state"]["final_merge_pr"] == 351
	assert result["latest_state"]["final_merge_status"] == "pending"
	# finalize_integration_merge_if_needed must report that it routed
	# the unmergeable PR through the self-healing flow rather than
	# halting the project. This stdout marker is the contract that the
	# new mergeability gate fired (scripts/orchestrate_poll_process.sh
	# ~L1377).
	assert "[final-merge] PR #351 is not mergeable; invoking self-healing flow." in result["stdout"], (
		f"expected self-healing flow log line in poller stdout; got tail:\n"
		f"{result['stdout'][-2000:]}"
	)
	# A review/autofix workflow_dispatch was issued against final PR #351
	# for automated conflict resolution. The standalone PR conflict sweep
	# attempts update-branch first (which the mock returns success for)
	# and only falls back to dispatch on update-branch failure, so the
	# only path that can produce a dispatch entry for #351 in this test
	# is heal_integration_branch_conflict -> _dispatch_review_for_conflicts.
	dispatched_for_final = [
		d for d in result["review_dispatches"] if d.get("pr_number") == 351
	]
	assert dispatched_for_final, (
		f"expected a review workflow dispatch for final PR #351 "
		f"(via heal_integration_branch_conflict), "
		f"got: {result['review_dispatches']}"
	)


def test_final_merge_waits_for_required_checks_before_merging():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 352,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
			"headSha": "blockedsha352",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		blocked_check_shas=["blockedsha352"],
	)
	assert result["latest_state"]["status"] == "in_progress"
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert result["latest_state"]["final_merge_pr"] == 352
	assert result.get("merged_prs", []) == []


def test_final_merge_promotes_eager_draft_pr_when_tracking_issue_ready_to_merge():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 361,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:ready-to-merge"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_pr"] == 361
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["pr_ready_calls"] == [361]
	assert 361 in result.get("merged_prs", [])
	assert result["prs"][0].get("draft") is False
	assert "EAGER_DRAFT_PR_PROMOTED pr=361 gate=tracking-ready-to-merge" in (result["stdout"] + result["stderr"])


def test_tracking_body_reconcile_self_heals_stale_tracker_and_refreshes_readiness_status():
	tracking_body = """## Project: Test Project

Summary text.

---

**Total issues:** 1 | **Waves:** 1
**Integration branch:** `orchestrator/project-192`

### Wave 1

- [ ] **issue-1**: First task (priority 1)

---
*This issue is managed by the AI orchestrator. Do not edit manually.*
`ai:orchestrator-tracking`
"""
	expected_body = tracking_body.replace("- [ ] **issue-1**", "- [x] **issue-1**")
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	state["project_body_snapshot"] = tracking_body
	state["tracking_body_sync_hash"] = hashlib.sha256(tracking_body.encode("utf-8")).hexdigest()
	state["final_merge_pr"] = 472
	state["waves"][0]["issues"][0]["status"] = "merged"
	prs = [
		{
			"number": 472,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"headSha": "headsha472",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]

	first = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-tracking"],
		tracking_body=tracking_body,
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert first["issues"]["192"]["body"] == expected_body
	assert first["issue_body_edit_calls"] == [{"issue": 192, "body": expected_body}]
	assert first["latest_state"]["tracking_body_sync_hash"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
	assert first["latest_state"]["tracking_body_last_readiness_refresh_hash"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
	assert first["commit_status_posts"] == [
		{
			"sha": "headsha472",
			"state": "success",
			"context": "orchestrator/integration-pr-not-ready",
			"description": "all 1 sub-issue(s) on #192 are ticked — integration PR is ready",
			"target_url": "https://github.com/owner/repo/issues/192",
		}
	]

	first_tracking_comments = [
		dict(comment)
		for comment in first["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	second = _run_poller(
		state=first["latest_state"],
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=first["tracking_labels"],
		tracking_comments=first_tracking_comments,
		tracking_body=first["issues"]["192"]["body"],
		issue_labels={10: ["ai:merged"]},
		prs=first["prs"],
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert second["issues"]["192"]["body"] == expected_body
	assert second["issue_body_edit_calls"] == []
	assert second["commit_status_posts"] == []


def test_tracking_body_reconcile_runs_during_normal_poll_cycle():
	tracking_body = """## Project: Test Project

Summary text.

---

**Total issues:** 1 | **Waves:** 1
**Integration branch:** `orchestrator/project-192`

### Wave 1

- [ ] **issue-1**: First task (priority 1)

---
*This issue is managed by the AI orchestrator. Do not edit manually.*
`ai:orchestrator-tracking`
"""
	expected_body = tracking_body.replace("- [ ] **issue-1**", "- [x] **issue-1**")
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["project_body_snapshot"] = tracking_body
	state["tracking_body_sync_hash"] = hashlib.sha256(tracking_body.encode("utf-8")).hexdigest()
	state["final_merge_pr"] = 472
	state["waves"][0]["issues"][0]["status"] = "merged"
	prs = [
		{
			"number": 472,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"headSha": "headsha472",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-tracking"],
		tracking_body=tracking_body,
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		blocked_check_shas=["headsha472"],
	)

	assert result["issues"]["192"]["body"] == expected_body
	assert result["issue_body_edit_calls"] == [{"issue": 192, "body": expected_body}]
	assert result["latest_state"]["tracking_body_sync_hash"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
	assert result["latest_state"]["tracking_body_last_readiness_refresh_hash"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
	assert result["commit_status_posts"] == [
		{
			"sha": "headsha472",
			"state": "success",
			"context": "orchestrator/integration-pr-not-ready",
			"description": "all 1 sub-issue(s) on #192 are ticked — integration PR is ready",
			"target_url": "https://github.com/owner/repo/issues/192",
		}
	]
	assert result["latest_state"]["status"] == "in_progress"
	assert result["latest_state"]["final_merge_status"] == "pending"


def test_tracking_body_reconcile_skips_readiness_refresh_when_issue_body_edit_fails():
	tracking_body = """## Project: Test Project

Summary text.

---

**Total issues:** 1 | **Waves:** 1
**Integration branch:** `orchestrator/project-192`

### Wave 1

- [ ] **issue-1**: First task (priority 1)

---
*This issue is managed by the AI orchestrator. Do not edit manually.*
`ai:orchestrator-tracking`
"""
	stale_hash = hashlib.sha256(tracking_body.encode("utf-8")).hexdigest()
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["project_body_snapshot"] = tracking_body
	state["tracking_body_sync_hash"] = stale_hash
	state["tracking_body_last_readiness_refresh_hash"] = stale_hash
	state["final_merge_pr"] = 472
	state["waves"][0]["issues"][0]["status"] = "merged"
	prs = [
		{
			"number": 472,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"headSha": "headsha472",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-tracking"],
		tracking_body=tracking_body,
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		blocked_check_shas=["headsha472"],
		fail_issue_edit_for=[192],
	)

	assert result["issues"]["192"]["body"] == tracking_body
	assert result["issue_body_edit_calls"] == []
	assert result["commit_status_posts"] == []
	assert result["latest_state"]["tracking_body_sync_hash"] == stale_hash
	assert result["latest_state"]["tracking_body_last_readiness_refresh_hash"] == stale_hash
	assert result["latest_state"]["status"] == "in_progress"
	assert result["latest_state"]["final_merge_status"] == "pending"


def test_force_merge_bypass_promotes_eager_pr_once_per_sha_and_records_audit():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 460,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	issue_events = {
		192: [
			{
				"id": 991,
				"event": "labeled",
				"created_at": "2026-05-23T16:15:00Z",
				"label": {"name": "ai:force-merge"},
				"actor": {"login": "octocat"},
			},
		],
	}
	first = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:force-merge"],
		issue_labels={10: ["ai:implementing"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		issue_events=issue_events,
	)
	assert first["latest_state"]["final_merge_pr"] == 460
	assert first["latest_state"]["force_merge_last_bypassed_integration_sha"] == "abcdef1234"
	assert first["pr_ready_calls"] == [460]
	assert first["prs"][0].get("draft") is False
	assert first["mock_operator_bypass_audit_append_calls"] == 1
	stored_audit = first["mock_operator_bypass_audit_payload"]
	assert stored_audit["tracking_issue_number"] == 192
	assert stored_audit["integration_sha"] == "abcdef1234"
	assert len(stored_audit["entries"]) == 1
	assert stored_audit["entries"][0]["actor"] == "octocat"
	assert stored_audit["entries"][0]["bypass_kind"] == "force-merge"
	assert stored_audit["entries"][0]["source_comment_id"] == first["latest_state"]["force_merge_last_bypass_tracking_comment_id"]
	assert any("/issues/192/events?per_page=100" in path for path in first["api_calls"])
	assert "FORCE_MERGE_BYPASS tracking_issue=192 pr=460 integration_branch=orchestrator/project-192 integration_sha=abcdef1234 actor=octocat ahead_by=5" in (first["stdout"] + first["stderr"])
	first_tracking_comments = [
		dict(comment)
		for comment in first["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	first_tracking_bodies = [str(comment.get("body", "")) for comment in first_tracking_comments]
	force_merge_tracking_comments = [
		body for body in first_tracking_bodies if body.startswith("## ⚠️ Operator bypass applied: ai:force-merge")
	]
	assert len(force_merge_tracking_comments) == 1
	assert "@octocat" in force_merge_tracking_comments[0]
	assert "abcdef1234" in force_merge_tracking_comments[0]
	first_pr_comments = [dict(comment) for comment in first["issues"]["460"]["comments"]]
	first_pr_bodies = [str(comment.get("body", "")) for comment in first_pr_comments]
	force_merge_pr_comments = [
		body for body in first_pr_bodies if body.startswith("## ⚠️ Operator bypass recorded")
	]
	assert len(force_merge_pr_comments) == 1
	assert "Tracking issue audit:" in force_merge_pr_comments[0]

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=first["tracking_labels"],
		tracking_comments=first_tracking_comments,
		issue_labels={10: ["ai:implementing"], 460: []},
		issue_comments={460: first_pr_comments},
		prs=first["prs"],
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		issue_events=issue_events,
		mock_operator_bypass_audit_payload=stored_audit,
	)
	assert second["pr_ready_calls"] == []
	assert second["mock_operator_bypass_audit_append_calls"] == 0
	second_tracking_bodies = [
		str(comment.get("body", ""))
		for comment in second["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	assert len([body for body in second_tracking_bodies if body.startswith("## ⚠️ Operator bypass applied: ai:force-merge")]) == 1
	second_pr_bodies = [str(comment.get("body", "")) for comment in second["issues"]["460"]["comments"]]
	assert len([body for body in second_pr_bodies if body.startswith("## ⚠️ Operator bypass recorded")]) == 1

	third_tracking_comments = [
		dict(comment)
		for comment in second["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	third_pr_comments = [dict(comment) for comment in second["issues"]["460"]["comments"]]
	third = _run_poller(
		state=second["latest_state"],
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=second["tracking_labels"],
		tracking_comments=third_tracking_comments,
		issue_labels={10: ["ai:implementing"], 460: []},
		issue_comments={460: third_pr_comments},
		prs=second["prs"],
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=6,
		branch_ref_shas={"orchestrator/project-192": "fedcba9876"},
		issue_events=issue_events,
	)
	assert third["pr_ready_calls"] == []
	assert third["latest_state"]["force_merge_last_bypassed_integration_sha"] == "fedcba9876"
	assert third["mock_operator_bypass_audit_append_calls"] == 1
	assert third["mock_operator_bypass_audit_payload"]["integration_sha"] == "fedcba9876"
	third_tracking_bodies = [
		str(comment.get("body", ""))
		for comment in third["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	third_force_merge_tracking_comments = [
		body for body in third_tracking_bodies if body.startswith("## ⚠️ Operator bypass applied: ai:force-merge")
	]
	assert len(third_force_merge_tracking_comments) == 2
	assert any("fedcba9876" in body for body in third_force_merge_tracking_comments)
	third_pr_bodies = [str(comment.get("body", "")) for comment in third["issues"]["460"]["comments"]]
	third_force_merge_pr_comments = [
		body for body in third_pr_bodies if body.startswith("## ⚠️ Operator bypass recorded")
	]
	assert len(third_force_merge_pr_comments) == 2
	assert any("fedcba9876" in body for body in third_force_merge_pr_comments)


def test_force_merge_bypass_promotion_failure_posts_retry_audit_once_per_sha():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 461,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	issue_events = {
		192: [
			{
				"id": 992,
				"event": "labeled",
				"created_at": "2026-05-23T16:20:00Z",
				"label": {"name": "ai:force-merge"},
				"actor": {"login": "octocat"},
			},
		],
	}
	first = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:force-merge"],
		issue_labels={10: ["ai:implementing"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		issue_events=issue_events,
		mock_pr_ready_exit_code=1,
	)
	assert first["pr_ready_calls"] == []
	assert first["prs"][0].get("draft") is True
	assert first["latest_state"].get("force_merge_last_bypassed_integration_sha") in (None, "")
	assert first["mock_operator_bypass_audit_append_calls"] == 0
	assert 461 in first["pr_body_update_calls"]
	assert "failed this cycle" in first["prs"][0].get("body", "")
	first_tracking_comments = [
		dict(comment)
		for comment in first["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	first_failure_comments = [
		str(comment.get("body", ""))
		for comment in first_tracking_comments
		if str(comment.get("body", "")).startswith("## ⚠️ Operator bypass requested but not yet applied: ai:force-merge")
	]
	assert len(first_failure_comments) == 1
	assert "@octocat" in first_failure_comments[0]
	assert "will retry the promotion" in first_failure_comments[0]

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=first["tracking_labels"],
		tracking_comments=first_tracking_comments,
		issue_labels={10: ["ai:implementing"]},
		prs=first["prs"],
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		issue_events=issue_events,
		mock_pr_ready_exit_code=1,
	)
	second_failure_comments = [
		str(comment.get("body", ""))
		for comment in second["issues"]["192"]["comments"]
		if str(comment.get("body", "")).startswith("## ⚠️ Operator bypass requested but not yet applied: ai:force-merge")
	]
	assert len(second_failure_comments) == 1
	assert second["mock_operator_bypass_audit_append_calls"] == 0


def test_force_merge_bypass_promotes_eager_pr_for_validation_origin_terminal_failure():
	state = _base_state(status="failed")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 462
	prs = [
		{
			"number": 462,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	issue_events = {
		192: [
			{
				"id": 993,
				"event": "labeled",
				"created_at": "2026-05-23T16:25:00Z",
				"label": {"name": "ai:force-merge"},
				"actor": {"login": "octocat"},
			},
		],
	}
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed", "ai:harness-broken", "ai:force-merge"],
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		issue_events=issue_events,
	)
	assert result["latest_state"]["status"] == "failed"
	assert result["latest_state"]["final_merge_pr"] == 462
	assert result["latest_state"]["force_merge_last_bypassed_integration_sha"] == "abcdef1234"
	assert result["pr_ready_calls"] == [462]
	assert result["prs"][0].get("draft") is False
	assert result["validation_dispatches"] == []
	assert result["mock_operator_bypass_audit_append_calls"] == 1
	stored_audit = result["mock_operator_bypass_audit_payload"]
	assert stored_audit["tracking_issue_number"] == 192
	assert stored_audit["integration_sha"] == "abcdef1234"
	assert len(stored_audit["entries"]) == 1
	assert stored_audit["entries"][0]["actor"] == "octocat"
	tracking_bodies = [
		str(comment.get("body", ""))
		for comment in result["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	assert len([
		body for body in tracking_bodies if body.startswith("## ⚠️ Operator bypass applied: ai:force-merge")
	]) == 1
	pr_bodies = [str(comment.get("body", "")) for comment in result["issues"]["462"]["comments"]]
	assert len([
		body for body in pr_bodies if body.startswith("## ⚠️ Operator bypass recorded")
	]) == 1


def test_final_merge_keeps_legacy_open_non_draft_pr_behind_readiness_gate():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 362,
			"state": "open",
			"draft": False,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "merge_conflict"
	assert result["latest_state"]["final_merge_pr"] == 362
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert result.get("merged_prs", []) == []
	assert result["pr_ready_calls"] == []
	assert "<!-- VALIDATION_STATUS_V1 -->" in result["prs"][0].get("body", "")
	assert "waiting for the tracking issue readiness gate" in (result["stdout"] + result["stderr"])


def test_final_merge_legacy_validated_gate_requires_current_sha_pass_history():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 363,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_payload=_validation_history_payload(
			integration_sha="deadbeef1234",
			entries=[
				{
					"outcome": "passed",
					"raw_status": "pass",
					"raw_conclusion": "success",
					"run_id": 9001,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9001",
					"recorded_at": "2026-05-23T10:00:00Z",
					"cycle": 1,
					"context": "validation passed",
					"source": "test",
				}
			],
		),
	)
	assert result["latest_state"]["status"] == "merge_conflict"
	assert result["latest_state"]["final_merge_pr"] == 363
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert result["pr_ready_calls"] == []
	assert result.get("merged_prs", []) == []
	assert result.get("mock_validation_history_get_calls", 0) == 1
	assert "Validation label present, but no passing validation-history entry exists yet for integration SHA `abcdef1234`." in result["prs"][0]["body"]
	assert "blocked by validation history" in (result["stdout"] + result["stderr"])


def test_final_merge_legacy_validated_gate_allows_current_sha_pass_history():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 364,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_payload=_validation_history_payload(
			integration_sha="abcdef1234",
			entries=[
				{
					"outcome": "passed",
					"raw_status": "pass",
					"raw_conclusion": "success",
					"run_id": 9002,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9002",
					"recorded_at": "2026-05-23T10:00:00Z",
					"cycle": 1,
					"context": "validation passed",
					"source": "test",
				}
			],
		),
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert 364 in result["pr_ready_calls"]
	assert 364 in result.get("merged_prs", [])
	assert result.get("mock_validation_history_get_calls", 0) >= 1
	assert "EAGER_DRAFT_PR_PROMOTED pr=364 gate=tracking-validated-legacy" in (result["stdout"] + result["stderr"])


def test_final_merge_legacy_validated_gate_blocks_later_non_harness_failure():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 365,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_payload=_validation_history_payload(
			integration_sha="abcdef1234",
			entries=[
				{
					"outcome": "passed",
					"raw_status": "pass",
					"raw_conclusion": "success",
					"run_id": 9003,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9003",
					"recorded_at": "2026-05-23T10:00:00Z",
					"cycle": 1,
					"context": "validation passed",
					"source": "test",
				},
				{
					"outcome": "failed",
					"raw_status": "needs_fixes",
					"raw_conclusion": "failure",
					"run_id": 9004,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9004",
					"recorded_at": "2026-05-23T11:00:00Z",
					"cycle": 1,
					"context": "validation failed",
					"source": "test",
				},
			],
		),
	)
	assert result["latest_state"]["status"] == "merge_conflict"
	assert result["latest_state"]["final_merge_pr"] == 365
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert result["pr_ready_calls"] == []
	assert result.get("merged_prs", []) == []
	assert "Validation label present, but a later non-harness validation failure is recorded for integration SHA `abcdef1234`; rerun validation before promoting." in result["prs"][0]["body"]


def test_final_merge_legacy_validated_gate_blocks_later_error_outcome():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 365,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_payload=_validation_history_payload(
			integration_sha="abcdef1234",
			entries=[
				{
					"outcome": "passed",
					"raw_status": "pass",
					"raw_conclusion": "success",
					"run_id": 9003,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9003",
					"recorded_at": "2026-05-23T10:00:00Z",
					"cycle": 1,
					"context": "validation passed",
					"source": "test",
				},
				{
					"outcome": "error",
					"raw_status": "error",
					"raw_conclusion": "failure",
					"run_id": 9004,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9004",
					"recorded_at": "2026-05-23T11:00:00Z",
					"cycle": 1,
					"context": "validation errored",
					"source": "test",
				},
			],
		),
	)
	assert result["latest_state"]["status"] == "merge_conflict"
	assert result["latest_state"]["final_merge_pr"] == 365
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert result["pr_ready_calls"] == []
	assert result.get("merged_prs", []) == []
	assert "Validation label present, but a later non-harness validation failure is recorded for integration SHA `abcdef1234`; rerun validation before promoting." in result["prs"][0]["body"]


def test_final_merge_legacy_validated_gate_ignores_later_harness_failure():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 366,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_payload=_validation_history_payload(
			integration_sha="abcdef1234",
			entries=[
				{
					"outcome": "passed",
					"raw_status": "pass",
					"raw_conclusion": "success",
					"run_id": 9005,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9005",
					"recorded_at": "2026-05-23T10:00:00Z",
					"cycle": 1,
					"context": "validation passed",
					"source": "test",
				},
				{
					"outcome": "failed",
					"raw_status": "harness_error",
					"raw_conclusion": "failure",
					"run_id": 9006,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9006",
					"recorded_at": "2026-05-23T11:00:00Z",
					"cycle": 1,
					"context": "harness failed",
					"source": "test",
				},
			],
		),
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert 366 in result["pr_ready_calls"]
	assert 366 in result.get("merged_prs", [])


def test_final_merge_legacy_validated_gate_fails_open_on_history_read_error():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 367,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_get_json={
			"warning_code": "history_read_failed",
			"warning": "mock git read failure",
		},
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert 367 in result["pr_ready_calls"]
	assert 367 in result.get("merged_prs", [])
	assert "Validation history unavailable for integration SHA abcdef1234; falling back to legacy ai:validated gate (reason=history_read_failed)." in (result["stdout"] + result["stderr"])


def test_final_merge_legacy_validated_gate_fails_open_on_shell_wrapper_history_read_error():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 369,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_get_exit_code=1,
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result.get("mock_validation_history_get_calls", 0) >= 1
	assert 369 in result["pr_ready_calls"]
	assert 369 in result.get("merged_prs", [])
	assert "Validation history unavailable for integration SHA abcdef1234; falling back to legacy ai:validated gate (reason=history_read_failed)." in (result["stdout"] + result["stderr"])


def test_final_merge_legacy_validated_gate_blocks_later_failure_without_raw_status():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	prs = [
		{
			"number": 370,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_payload=_validation_history_payload(
			integration_sha="abcdef1234",
			entries=[
				{
					"outcome": "passed",
					"raw_status": "pass",
					"raw_conclusion": "success",
					"run_id": 9007,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9007",
					"recorded_at": "2026-05-23T10:00:00Z",
					"cycle": 1,
					"context": "validation passed",
					"source": "test",
				},
				{
					"outcome": "failed",
					"raw_status": None,
					"raw_conclusion": "failure",
					"run_id": 9008,
					"run_attempt": 1,
					"run_url": "https://example.invalid/runs/9008",
					"recorded_at": "2026-05-23T11:00:00Z",
					"cycle": 1,
					"context": "validation failed",
					"source": "test",
				},
			],
		),
	)
	assert result["latest_state"]["status"] == "merge_conflict"
	assert result["latest_state"]["final_merge_pr"] == 370
	assert result["latest_state"]["final_merge_status"] == "pending"
	assert result["pr_ready_calls"] == []
	assert result.get("merged_prs", []) == []
	assert "Validation label present, but a later non-harness validation failure is recorded for integration SHA `abcdef1234`; rerun validation before promoting." in result["prs"][0]["body"]


def test_mark_validation_complete_fails_open_on_history_write_error():
	state = _base_state(status="validating")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_cycle"] = 2
	prs = [
		{
			"number": 368,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_append_json={
			"stored": False,
			"warning": "mock git push failure",
		},
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert result["latest_state"]["final_merge_pr"] == 368
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["pr_ready_calls"] == [368]
	assert 368 in result.get("merged_prs", [])
	assert result.get("mock_validation_history_append_calls", 0) == 1
	assert result.get("mock_validation_history_get_calls", 0) == 0
	assert "Validation history unavailable for integration SHA abcdef1234; falling back to legacy ai:validated gate (reason=history_write_failed_current_tick)." in (result["stdout"] + result["stderr"])


def test_mark_validation_complete_fails_open_on_shell_wrapper_history_write_error():
	state = _base_state(status="validating")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_cycle"] = 2
	prs = [
		{
			"number": 371,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_validation_history_append_exit_code=1,
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert result["latest_state"]["final_merge_pr"] == 371
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["pr_ready_calls"] == [371]
	assert 371 in result.get("merged_prs", [])
	assert result.get("mock_validation_history_append_calls", 0) == 1
	assert result.get("mock_validation_history_get_calls", 0) == 0
	assert "Validation history unavailable for integration SHA abcdef1234; falling back to legacy ai:validated gate (reason=history_write_failed_current_tick)." in (result["stdout"] + result["stderr"])


def test_integration_stale_alert_fires_once_after_threshold():
	now_epoch = int(time.time())
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["last_main_squash_at_utc"] = now_epoch - (7 * 3600)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert "INTEGRATION_STALE_ALERT_SENT tracking_issue=192 integration_branch=orchestrator/project-192 default_branch=main ahead_by=5" in (result["stdout"] + result["stderr"])
	assert result["state_on_disk"]["last_main_squash_at_utc"] == state["last_main_squash_at_utc"]
	assert isinstance(result["state_on_disk"]["integration_stale_last_alerted_at_utc"], int)
	assert result["state_on_disk"]["integration_stale_last_alerted_at_utc"] >= now_epoch - 5


def test_integration_stale_alert_dedupes_before_realert_window():
	now_epoch = int(time.time())
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["last_main_squash_at_utc"] = now_epoch - (7 * 3600)
	state["integration_stale_last_alerted_at_utc"] = now_epoch - 3600
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert "INTEGRATION_STALE_ALERT_SENT" not in (result["stdout"] + result["stderr"])
	assert result["state_on_disk"]["integration_stale_last_alerted_at_utc"] == state["integration_stale_last_alerted_at_utc"]


def test_integration_stale_alert_repeats_after_realert_window():
	now_epoch = int(time.time())
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["last_main_squash_at_utc"] = now_epoch - (20 * 3600)
	state["integration_stale_last_alerted_at_utc"] = now_epoch - (13 * 3600)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert "INTEGRATION_STALE_ALERT_SENT tracking_issue=192 integration_branch=orchestrator/project-192 default_branch=main ahead_by=5" in (result["stdout"] + result["stderr"])
	assert isinstance(result["state_on_disk"]["integration_stale_last_alerted_at_utc"], int)
	assert result["state_on_disk"]["integration_stale_last_alerted_at_utc"] > state["integration_stale_last_alerted_at_utc"]


def test_integration_stale_alert_window_clears_when_branch_catches_up():
	now_epoch = int(time.time())
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["last_main_squash_at_utc"] = now_epoch - (20 * 3600)
	state["integration_stale_last_alerted_at_utc"] = now_epoch - 3600
	cleared = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=0,
	)
	assert "INTEGRATION_STALE_ALERT_SENT" not in (cleared["stdout"] + cleared["stderr"])
	assert cleared["state_on_disk"]["integration_stale_last_alerted_at_utc"] is None
	assert isinstance(cleared["state_on_disk"]["last_main_squash_at_utc"], int)
	assert cleared["state_on_disk"]["last_main_squash_at_utc"] >= now_epoch - 5

	second_state = dict(cleared["state_on_disk"])
	second_state["status"] = "in_progress"
	second_state["last_main_squash_at_utc"] = int(time.time()) - (7 * 3600)
	second_state["integration_stale_last_alerted_at_utc"] = None
	second_state["final_merge_pr"] = None
	second_state["final_merge_status"] = "pending"
	second = _run_poller(
		state=second_state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
	)
	assert "INTEGRATION_STALE_ALERT_SENT tracking_issue=192 integration_branch=orchestrator/project-192 default_branch=main ahead_by=5" in (second["stdout"] + second["stderr"])


def test_integration_stale_alert_disabled_when_hours_zero():
	now_epoch = int(time.time())
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	# 20h ahead — well past the 6h default; would alert if the path were enabled.
	state["last_main_squash_at_utc"] = now_epoch - (20 * 3600)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=5,
		env_overrides={"ORCH_INTEGRATION_STALE_ALERT_HOURS": "0"},
	)
	assert "INTEGRATION_STALE_ALERT_SENT" not in (result["stdout"] + result["stderr"])
	# Disabled path is a true no-op: it neither starts the dedup window nor
	# rewrites the squash anchor.
	assert result["state_on_disk"].get("integration_stale_last_alerted_at_utc") is None
	assert result["state_on_disk"]["last_main_squash_at_utc"] == state["last_main_squash_at_utc"]


def test_integration_backpressure_blocks_merges_at_threshold_and_clears_below_it():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 470
	prs = [
		{
			"number": 470,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
		{
			"number": 910,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-10",
			"headRefFromApi": "ai/issue-10",
			"headSha": "sha910",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	first = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 910},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=10,
	)
	assert first["latest_state"]["final_merge_pr"] == 470
	assert "ai:integration-backpressure" in first["tracking_labels"]
	assert first.get("merged_prs", []) == []
	assert "BACKPRESSURE_TRIGGERED tracking_issue=192 integration_branch=orchestrator/project-192 default_branch=main ahead_by=10 threshold=10 effective_threshold=10 final_pr=470" in (first["stdout"] + first["stderr"])
	first_tracking_comments = [
		dict(comment)
		for comment in first["issues"]["192"]["comments"]
		if not _is_state_comment(str((comment or {}).get("body", "")))
	]
	first_completion_comment = next(
		str(comment.get("body", ""))
		for comment in first_tracking_comments
		if "<!-- orchestrator:completion-status -->" in str(comment.get("body", ""))
	)
	assert "ai:integration-backpressure" in first_completion_comment
	assert "open integration PR #470" in first_completion_comment
	assert "pull/470" in first_completion_comment

	second = _run_poller(
		state=first["latest_state"],
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=first["tracking_labels"],
		tracking_comments=first_tracking_comments,
		issue_labels={10: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 910},
		prs=first["prs"],
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=9,
	)
	assert "ai:integration-backpressure" not in second["tracking_labels"]
	assert 910 in second.get("merged_prs", [])
	assert "BACKPRESSURE_CLEARED tracking_issue=192 integration_branch=orchestrator/project-192 default_branch=main ahead_by=9 threshold=10 effective_threshold=10 final_pr=470" in (second["stdout"] + second["stderr"])
	second_completion_comment = next(
		str(comment.get("body", ""))
		for comment in second["issues"]["192"]["comments"]
		if "<!-- orchestrator:completion-status -->" in str(comment.get("body", ""))
	)
	assert "ai:integration-backpressure" not in second_completion_comment


def test_integration_backpressure_refreshes_after_first_merge_within_same_cycle():
	state = _base_state(status="in_progress")
	state["total_issues"] = 2
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"] = [
		{"id": "issue-1", "github_issue": 10, "status": "pending"},
		{"id": "issue-2", "github_issue": 11, "status": "pending"},
	]
	state["issue_number_map"] = {"issue-1": 10, "issue-2": 11}
	prs = [
		{
			"number": 910,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-10",
			"headRefFromApi": "ai/issue-10",
			"headSha": "sha910",
			"mergeable": True,
			"mergeable_state": "clean",
		},
		{
			"number": 911,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-11",
			"headRefFromApi": "ai/issue-11",
			"headSha": "sha911",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"], 11: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 910, 11: 911},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by_sequence=[9, 10],
	)
	assert 910 in result.get("merged_prs", [])
	assert 911 not in result.get("merged_prs", [])
	assert "[backpressure] Deferring merge of PR #911 for issue #11: integration branch ahead_by=10 meets effective threshold 10 (configured floor ORCH_INTEGRATION_MAX_AHEAD_COMMITS=10)." in (result["stdout"] + result["stderr"])


def test_integration_backpressure_size_aware_floor_does_not_self_deadlock_large_project():
	# Regression for the project-#2974 self-deadlock: a project with more
	# planned sub-issue commits than the flat ORCH_INTEGRATION_MAX_AHEAD_COMMITS
	# (10) would otherwise trip backpressure on its own merges before the
	# integration->default PR can drain (the eager final PR only merges at
	# completion), so the very merge needed to reach completion is paused
	# forever. With the size-aware floor the effective threshold becomes
	# max(10, planned_issue_count + margin) = max(10, 10 + 5) = 15, so an
	# integration branch 12 commits ahead must NOT trip backpressure and the
	# ready-to-merge sub-issue PR must still merge.
	state = _base_state(status="in_progress")
	state["total_issues"] = 10
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 470
	prs = [
		{
			"number": 470,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
		{
			"number": 910,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-10",
			"headRefFromApi": "ai/issue-10",
			"headSha": "sha910",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 910},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=12,
	)
	# ahead_by=12 < effective threshold 15 -> backpressure inactive.
	assert "ai:integration-backpressure" not in result["tracking_labels"]
	assert 910 in result.get("merged_prs", [])
	assert "BACKPRESSURE_TRIGGERED" not in (result["stdout"] + result["stderr"])
	completion_comment = next(
		str(comment.get("body", ""))
		for comment in result["issues"]["192"]["comments"]
		if "<!-- orchestrator:completion-status -->" in str(comment.get("body", ""))
	)
	assert "ai:integration-backpressure" not in completion_comment


def test_integration_backpressure_uses_wave_issue_count_when_total_issues_missing():
	state = _base_state(status="in_progress")
	state.pop("total_issues", None)
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 470
	state["waves"][0]["issues"] = [
		{"id": "issue-1", "github_issue": 10, "status": "pending"},
		{"id": "issue-2", "github_issue": 11, "status": "merged"},
		{"id": "issue-3", "github_issue": 12, "status": "merged"},
	]
	state["issue_number_map"] = {"issue-1": 10, "issue-2": 11, "issue-3": 12}
	prs = [
		{
			"number": 470,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
		{
			"number": 910,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-10",
			"headRefFromApi": "ai/issue-10",
			"headSha": "sha910",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"], 11: ["ai:merged"], 12: ["ai:merged"]},
		issue_linked_prs={10: 910},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=3,
		env_overrides={
			"ORCH_INTEGRATION_MAX_AHEAD_COMMITS": "2",
			"ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN": "1",
		},
	)
	assert "ai:integration-backpressure" not in result["tracking_labels"]
	assert 910 in result.get("merged_prs", [])
	assert "BACKPRESSURE_TRIGGERED" not in (result["stdout"] + result["stderr"])


def test_integration_backpressure_falls_back_to_floor_on_non_numeric_total_issues():
	state = _base_state(status="in_progress")
	state["total_issues"] = "oops"
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 470
	prs = [
		{
			"number": 470,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
		{
			"number": 910,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-10",
			"headRefFromApi": "ai/issue-10",
			"headSha": "sha910",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 910},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=2,
		env_overrides={
			"ORCH_INTEGRATION_MAX_AHEAD_COMMITS": "2",
			"ORCH_INTEGRATION_BACKPRESSURE_PROJECT_MARGIN": "5",
		},
	)
	assert "ai:integration-backpressure" in result["tracking_labels"]
	assert result.get("merged_prs", []) == []
	assert "BACKPRESSURE_TRIGGERED tracking_issue=192 integration_branch=orchestrator/project-192 default_branch=main ahead_by=2 threshold=2 effective_threshold=2 final_pr=470" in (result["stdout"] + result["stderr"])


def test_backward_scan_backpressure_log_reports_floor_and_effective_threshold():
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 2,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 35, "status": "pending"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": 20, "status": "pending"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 35, "issue-2": 20},
		"pending_issue_defs": {},
		"integration_branch": "orchestrator/project-192",
		"final_merge_strategy": "squash",
		"final_merge_pr": 470,
		"final_merge_status": "pending",
	}
	prs = [
		{
			"number": 470,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"headRefFromApi": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
		},
		{
			"number": 935,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-35",
			"headRefFromApi": "ai/issue-35",
			"headSha": "sha935",
			"mergeable": True,
			"mergeable_state": "clean",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={35: ["ai:ready-to-merge"], 20: ["ai:implementing"]},
		issue_linked_prs={35: 935},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		compare_ahead_by=10,
	)
	assert 935 not in result.get("merged_prs", [])
	assert "[backward-scan] Backpressure active (ahead_by=10, threshold=10, effective_threshold=10); deferring auto-merge of PR #935 for prior-wave issue #35." in (result["stdout"] + result["stderr"])


def test_integration_backpressure_threshold_cache_avoids_command_substitution_subshells():
	# Static contract: the size-aware threshold helper caches its computed
	# value in `_INTEGRATION_BACKPRESSURE_EFFECTIVE_THRESHOLD_CACHE`, but
	# bash command substitution runs in a subshell. Calling the helper as
	# `foo="$(_integration_backpressure_effective_threshold)"` therefore
	# re-runs jq on every use because the cache write dies with the subshell.
	# The call sites must pass an output variable directly instead.
	poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")

	assert "$(_integration_backpressure_effective_threshold" not in poller_body, (
		"_integration_backpressure_effective_threshold must not be invoked via "
		"command substitution; that drops the cached threshold in a subshell "
		"and respawns jq on every call."
	)
	direct_calls = re.findall(
		r"^\s*_integration_backpressure_effective_threshold\s+[A-Za-z_][A-Za-z0-9_]*\s*$",
		poller_body,
		re.MULTILINE,
	)
	assert len(direct_calls) >= 5, (
		"Backpressure threshold call sites must pass an output variable "
		"directly so the per-tracking-issue jq result stays cached in the "
		"current shell."
	)


def test_final_merge_treats_closed_merged_pr_as_success():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_status"] = "conflict"
	state["final_merge_pr"] = 353
	prs = [
		{
			"number": 353,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_pr"] == 353
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["release_dispatches"] == []


def test_merge_conflict_state_completes_when_final_pr_already_merged_and_branch_deleted():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_status"] = "conflict"
	state["final_merge_pr"] = 354
	prs = [
		{
			"number": 354,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert "ai:merged" in result["tracking_labels"]
	assert result["latest_state"]["final_merge_pr"] == 354
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["release_dispatches"] == []


def test_external_finalize_detect_leaves_merge_conflict_validation_path_intact():
	state = _base_state(status="merge_conflict")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 356
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 356,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_pr"] == 356
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["latest_state"]["validation_completed_cycle"] == 1
	assert "ai:validated" in result["tracking_labels"]
	assert "ai:merged" not in result["tracking_labels"]


def test_external_finalize_detect_leaves_validation_completion_path_intact():
	state = _base_state(status="validating")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_cycle"] = 2
	state["final_merge_pr"] = 357
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 357,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_pr"] == 357
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert "ai:validated" in result["tracking_labels"]
	assert "ai:merged" not in result["tracking_labels"]


def test_validation_completion_preempts_sync_branch_missing_failure_when_final_pr_already_merged():
	state = _base_state(status="validating")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_cycle"] = 2
	state["final_merge_pr"] = 359
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 359,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main"],
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert result["latest_state"]["final_merge_pr"] == 359
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert "ai:validated" in result["tracking_labels"]
	assert "ai:merged" not in result["tracking_labels"]
	assert not any("Integration branch missing" in body for body in tracking_bodies)


def test_validation_fixing_completion_preempts_sync_branch_missing_failure_when_final_pr_already_merged():
	state = _base_state(status="validation-fixing")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_cycle"] = 2
	state["validation_active_fix_issues"] = []
	state["final_merge_pr"] = 360
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 360,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main"],
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert result["latest_state"]["final_merge_pr"] == 360
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert "ai:validated" in result["tracking_labels"]
	assert "ai:merged" not in result["tracking_labels"]
	assert not any("Integration branch missing" in body for body in tracking_bodies)


def test_external_finalize_detect_skips_terminal_fallthrough():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 355
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 355,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_pr"] == 355
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert "Project already complete, skipping." not in result["stdout"]


def test_external_finalize_detect_preempts_sync_branch_missing_failure():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 358
	state["final_merge_status"] = "pending"
	prs = [
		{
			"number": 358,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main"],
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["final_merge_pr"] == 358
	assert result["latest_state"]["final_merge_status"] == "merged"
	assert "ai:merged" in result["tracking_labels"]
	assert not any("Integration branch missing" in body for body in tracking_bodies)


def test_external_finalize_refuses_complete_when_subissues_never_created():
	# Behavioural regression test for project #2734 (postmortem layer 6).
	# State models the actual incident: a multi-wave project where Wave 1
	# sub-issues were merged but Waves 2-7 were never dispatched — their
	# entries are present in the state with status="not_created" and
	# github_issue=null because the orchestrator never created them.  An
	# operator (or self-heal pipeline) squash-merges the integration PR
	# externally.  The external-finalize block sees a merged final PR and
	# would, pre-fix, mark the project complete and broadcast "✅ Project
	# complete" via Telegram while 7 of 9 sub-issues had shipped no code.
	#
	# Post-fix the gate must:
	#   - leave status as in_progress (NOT transition to complete)
	#   - leave final_merge_status as pending (NOT transition to merged)
	#   - NOT apply the ai:merged label
	#   - emit a [external-finalize-partial] warning to stdout/stderr
	#   - persist external_finalize_partial_alert_sig on the state file
	#     so the Telegram alert deduplicates on subsequent ticks
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 2750
	state["final_merge_status"] = "pending"
	# Replace the default 1-issue Wave 1 with a 2-wave project: Wave 1
	# merged, Wave 2 never created (status="not_created", github_issue=null).
	# This mirrors project #2734's actual `.waves[]` shape from comment #38.
	state["total_issues"] = 2
	state["total_waves"] = 2
	state["waves"] = [
		{
			"wave": 1,
			"issues": [
				{"id": "phase1-verifier-baseline-delta", "github_issue": 10, "status": "merged"},
			],
		},
		{
			"wave": 2,
			"issues": [
				{"id": "phase1-resolver-bootstrap-wiring", "github_issue": None, "status": "not_created"},
			],
		},
	]
	state["issue_number_map"] = {"phase1-verifier-baseline-delta": 10}
	prs = [
		{
			"number": 2750,
			"state": "closed",
			"merged": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": None,
			"mergeable_state": "unknown",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	# The gate must refuse to transition state.
	assert result["latest_state"]["status"] != "complete", (
		"completeness gate failed: status transitioned to `complete` despite "
		"Wave 2's sub-issue still being `not_created` — this is the exact "
		"failure mode the gate is meant to prevent (project #2734 scenario)."
	)
	assert result["latest_state"]["final_merge_status"] != "merged", (
		"completeness gate failed: final_merge_status transitioned to `merged` "
		"despite incomplete sub-issues."
	)
	# The ai:merged label must NOT be applied while sub-issues remain
	# uncreated; the label is the orchestrator's primary downstream signal
	# for release callbacks and label-repair sweeps.
	assert "ai:merged" not in result["tracking_labels"], (
		"completeness gate failed: ai:merged label was applied to tracking "
		"issue despite Wave 2 sub-issue never being created. Downstream "
		"automation would treat this project as done and skip remediation."
	)
	# The warning marker must surface so log-analysis tooling can detect
	# this failure mode in workflow runs.
	combined_log = result.get("stdout", "") + "\n" + result.get("stderr", "")
	assert "[external-finalize-partial]" in combined_log, (
		"completeness gate failed: `[external-finalize-partial]` warning "
		"marker missing from poller stdout/stderr — without it, log-analysis "
		"tooling cannot detect this failure mode at scale."
	)
	# Note: the alert-dedup signature
	# (`external_finalize_partial_alert_sig` on the state file) is verified
	# by the companion static guard test
	# `test_external_finalize_gates_on_subissue_completeness_before_marking_complete`,
	# which pins both the persistence and the dedup condition.  This
	# behavioural test does not re-verify it because `_run_poller` only
	# exposes state via the comment trail, and the gate intentionally does
	# NOT post_state_comment (the project is genuinely still in_progress
	# from the orchestrator's POV — posting a state comment on every gate
	# fire would re-introduce the alert-fatigue failure mode at the
	# tracking-issue level).


def test_standalone_conflict_sweep_skips_integration_base_prs():
	state = _base_state(status="complete")
	prs = [
		{
			"number": 410,
			"state": "open",
			"baseRefName": "orchestrator/project-192",
			"headRefName": "ai/issue-10",
			"mergeable": False,
			"mergeable_state": "dirty",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
	)
	assert result["update_branch_calls"] == []
	assert result["review_dispatches"] == []


def test_standalone_conflict_sweep_handles_non_ai_branch_conflicts():
	state = _base_state(status="complete")
	prs = [
		{
			"number": 411,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "claude/issue-10",
			"mergeable": False,
			"mergeable_state": "dirty",
			"headSha": "sha411",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		prs=prs,
		update_branch_fail_for_prs=[411],
	)
	assert result["update_branch_calls"] == [411]
	assert len(result["review_dispatches"]) == 1
	assert result["review_dispatches"][0]["pr_number"] == 411
	assert result["review_dispatches"][0]["ref"] == "claude/issue-10"


def test_standalone_conflict_sweep_keeps_ai_issue_branch_behavior():
	state = _base_state(status="complete")
	prs = [
		{
			"number": 412,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "ai/issue-10",
			"mergeable": False,
			"mergeable_state": "dirty",
			"headSha": "sha412",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		prs=prs,
	)
	assert result["update_branch_calls"] == [412]
	assert result["review_dispatches"] == []


def test_standalone_conflict_sweep_skips_closed_pr_on_detail_fetch():
	state = _base_state(status="complete")
	prs = [
		{
			"number": 413,
			"state": "open",
			"stateFromApi": "closed",
			"baseRefName": "main",
			"headRefName": "claude/issue-13",
			"mergeable": False,
			"mergeable_state": "dirty",
			"headSha": "sha413",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		prs=prs,
	)
	assert result["update_branch_calls"] == []
	assert result["review_dispatches"] == []


def test_standalone_conflict_sweep_active_run_guard_skips_dispatch():
	state = _base_state(status="complete")
	prs = [
		{
			"number": 414,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "claude/issue-14",
			"mergeable": False,
			"mergeable_state": "dirty",
			"headSha": "sha414",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		prs=prs,
		update_branch_fail_for_prs=[414],
		active_autofix_runs=[
			{"workflow": "ai-review.yml", "branch": "claude/issue-14", "status": "in_progress"},
		],
	)
	assert result["update_branch_calls"] == [414]
	assert result["review_dispatches"] == []


def test_standalone_conflict_sweep_missing_head_ref_logs_warning_and_continues():
	state = _base_state(status="complete")
	prs = [
		{
			"number": 415,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "claude/issue-15",
			"headRefFromApi": "",
			"mergeable": False,
			"mergeable_state": "dirty",
			"headSha": "sha415",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		prs=prs,
	)
	assert result["update_branch_calls"] == []
	assert result["review_dispatches"] == []
	assert "unavailable head ref" in (result["stdout"] + result["stderr"])


def test_standalone_conflict_sweep_consumes_budget_after_override_cap():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:done",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
			"conflict_override_count": {"sha416": 2},
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:done"]},
		issue_comments={501: [standalone_state_comment]},
		issue_linked_prs={501: 416},
		mock_gh_issue_list_label_filter=True,
		prs=[
			{
				"number": 416,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "claude/issue-501",
				"headRefFromApi": "claude/issue-501",
				"headSha": "sha416",
				"mergeable": False,
				"mergeable_state": "dirty",
			},
		],
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["501"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 1
	assert standalone_state["phase_attempts"]["ai:done"] == 1
	assert standalone_state["conflict_override_count"]["sha416"] == 3
	assert len([d for d in result["review_dispatches"] if str(d.get("pr_number")) == "416"]) == 1


def test_standalone_retrigger_review_skips_empty_commit_when_review_run_has_blank_head_branch_but_matching_sha():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:done",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	head_ref = "claude/issue-501"
	head_sha = "a" * 40
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:done"]},
		issue_comments={501: [standalone_state_comment]},
		issue_linked_prs={501: 416},
		mock_gh_issue_list_label_filter=True,
		prs=[
			{
				"number": 416,
				"state": "open",
				"baseRefName": "main",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": head_sha,
				"mergeable": True,
				"mergeable_state": "clean",
			},
		],
		actions_runs_workflow_runs=[
			{
				"id": 26088864017,
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"status": "queued",
				"head_branch": "",
				"head_sha": head_sha,
				"created_at": "2999-01-01T00:00:00Z",
			},
		],
		mock_git_push_success=True,
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["501"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 0
	assert result.get("git_push_calls", []) == [], (
		f"expected no standalone empty-commit push when a blank-head_branch run matches "
		f"the PR head_sha; got push calls {result.get('git_push_calls', [])}"
	)


def test_standalone_retrigger_review_skips_empty_commit_for_review_run_past_stall_threshold_but_within_budget():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:done",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	head_ref = "claude/issue-501"
	started_169m_ago = time.strftime(
		"%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 169 * 60)
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:done"]},
		issue_comments={501: [standalone_state_comment]},
		issue_linked_prs={501: 416},
		mock_gh_issue_list_label_filter=True,
		prs=[
			{
				"number": 416,
				"state": "open",
				"baseRefName": "main",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha416",
				"mergeable": True,
				"mergeable_state": "clean",
			},
		],
		# Seed only the cached runs blob: the branch-scoped `gh run list`
		# fallback is backed by `active_autofix_runs` in this harness, so leaving
		# that empty proves the standalone inline guard itself uses the longer
		# review window and blocks the destructive empty-commit push.
		actions_runs_workflow_runs=[
			{
				"id": 26944643043,
				"name": "Internal: AI Review & Autofix",
				"path": ".github/workflows/internal-review.yml",
				"status": "in_progress",
				"head_branch": head_ref,
				"run_started_at": started_169m_ago,
			},
		],
		mock_git_push_success=True,
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["501"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 0, (
		f"expected standalone stall_recovery_count to stay at 0 when a review run past "
		f"the stall threshold but within its budget blocks the empty-commit push; "
		f"got {standalone_state['stall_recovery_count']}"
	)
	assert result.get("git_push_calls", []) == [], (
		f"expected no standalone empty-commit push when an in-budget review run blocks recovery; "
		f"got push calls {result.get('git_push_calls', [])}"
	)


def test_standalone_stall_recovery_skips_when_phase_attempts_exhausted():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:review-blocked",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
			"phase_attempts": {"ai:review-blocked": 5},
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:review-blocked"]},
		issue_comments={501: [standalone_state_comment]},
		mock_gh_issue_list_label_filter=True,
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["501"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 0
	assert standalone_state["phase_attempts"]["ai:review-blocked"] == 5
	assert "ai:closed" in result["issues"]["501"]["labels"]
	assert result["review_dispatches"] == []


def test_standalone_stall_recovery_honors_ai_done_phase_attempt_override():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:done",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
			"phase_attempts": {"ai:done": 5},
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:done"]},
		issue_comments={501: [standalone_state_comment]},
		mock_gh_issue_list_label_filter=True,
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["501"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 1
	assert standalone_state["phase_attempts"]["ai:done"] == 6
	assert "ai:closed" not in result["issues"]["501"]["labels"]


def test_standalone_merged_guard_ignores_refs_only_cross_reference():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:planning",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:planning"]},
		issue_comments={501: [standalone_state_comment]},
		issue_linked_prs={501: 416},
		mock_gh_issue_list_label_filter=True,
		prs=[
			{
				"number": 416,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-15T00:00:00Z",
				"body": "Refs #501",
				"willCloseTarget": False,
			},
		],
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["501"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 1
	assert "ai:merged" not in result["issues"]["501"]["labels"]


def test_standalone_retrigger_review_does_not_increment_when_empty_commit_checkout_fails():
	state = _base_state(status="complete")
	standalone_state_comment = (
		"<!-- AI_STANDALONE_STALL_STATE_V1\n"
		+ json.dumps({
			"schema_version": 1,
			"last_seen_phase": "ai:done",
			"status_since_ts": 1,
			"stall_recovery_count": 0,
		})
		+ "\nAI_STANDALONE_STALL_STATE_V1 -->"
	)
	head_ref = "claude/issue-502"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 502: ["ai:done"]},
		issue_comments={502: [standalone_state_comment]},
		issue_linked_prs={502: 417},
		mock_gh_issue_list_label_filter=True,
		prs=[
			{
				"number": 417,
				"state": "open",
				"baseRefName": "main",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha417",
				"mergeable": True,
				"mergeable_state": "clean",
			},
		],
		mock_git_push_success=True,
		mock_git_checkout_fail=True,
	)
	standalone_state = _extract_latest_standalone_state(result["issues"]["502"]["comments"])
	assert standalone_state is not None
	assert standalone_state["stall_recovery_count"] == 0
	assert result.get("git_push_calls", []) == [], (
		f"expected checkout failure to skip the standalone empty-commit push; "
		f"got push calls {result.get('git_push_calls', [])}"
	)



def test_validation_fixing_redispatches_when_fix_issues_merged():
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_cycle"] = 1
	state["validation_active_fix_issues"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "validating"
	assert result["latest_state"]["validation_cycle"] == 2
	assert result["latest_state"]["validation_active_fix_issues"] == []
	assert len(result["validation_dispatches"]) == 1


def test_implementation_failed_post_codex_open_blockers_defers_reissue_and_persists_dependency_metadata():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"], 501: ["ai:planning"]},
		issue_comments={
			10: [
				"## Post-Codex validation diagnosed follow-up fixes\n\n"
				"Investigate captured diagnostics.\n\n"
				"Created fix-up issues:\n"
				"- #501"
			],
		},
	)
	assert result.get("created_issues", []) == []
	assert result["closed_issues"] == []
	latest_issue_state = result["latest_state"]["waves"][0]["issues"][0]
	assert latest_issue_state["github_issue"] == 10
	assert latest_issue_state.get("reissue_depends_on") == [501]
	assert "Deferring implementation-failed reissue for #10" in result["stdout"]


def test_implementation_failed_post_codex_closed_blockers_reissues_with_post_codex_guidance_and_uses_depends_on():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	state["waves"][0]["issues"][0]["depends_on"] = [777]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"], 501: ["ai:closed"], 777: ["ai:closed"]},
		issue_closed={501: True, 777: True},
		issue_comments={
			10: [
				"## Post-Codex validation diagnosed follow-up fixes\n\n"
				"Investigate captured diagnostics.\n\n"
				"Created fix-up issues:\n"
				"- #501"
			],
		},
		issue_bodies={10: "Source body"},
	)
	assert result.get("created_issues", [])
	new_issue_num = result["created_issues"][0]["number"]
	assert result["closed_issues"] == [10]
	new_issue_body = result["issues"][str(new_issue_num)]["body"]
	assert "failed during post-Codex syntax/validation checks" in new_issue_body
	assert "Post-Codex blocker context" in new_issue_body
	assert "- #501" in new_issue_body
	latest_issue_state = result["latest_state"]["waves"][0]["issues"][0]
	assert latest_issue_state["github_issue"] == str(new_issue_num)
	assert latest_issue_state.get("depends_on") == [501, 777]
	assert latest_issue_state.get("reissue_depends_on") is None


def test_implementation_failed_noop_path_keeps_existing_guidance_without_post_codex_context():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"]},
		issue_bodies={10: "Source body"},
	)
	assert result.get("created_issues", [])
	new_issue_num = result["created_issues"][0]["number"]
	new_issue_body = result["issues"][str(new_issue_num)]["body"]
	assert "previous implementation attempt produced no repository changes" in new_issue_body
	assert "Post-Codex blocker context" not in new_issue_body
	assert "ALLOW_WORKFLOW_EDITS=true" in new_issue_body


def test_implementation_failed_post_codex_unknown_blocker_state_defers_reissue():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"], 501: ["ai:closed"]},
		issue_comments={
			10: [
				"## Post-Codex validation diagnosed follow-up fixes\n\n"
				"Investigate captured diagnostics.\n\n"
				"Created fix-up issues:\n"
				"- #501"
			],
		},
		fail_issue_get_for=[501],
	)
	assert result.get("created_issues", []) == []
	assert result["closed_issues"] == []
	assert "blocker status lookup incomplete" in result["stdout"]


def test_implementation_failed_comment_lookup_failure_defers_reissue():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"]},
		fail_issue_comment_get_after={10: 0},
	)
	assert result.get("created_issues", []) == []
	assert result["closed_issues"] == []
	assert "unable to fetch issue comments for post-codex blocker detection" in result["stdout"]


def test_review_blocked_merged_fix_followup_retargets_base_to_integration_branch():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "resolve_active_orchestrator_context_for_issue \"${rb_issue}\" \"${TRACKING_NUM:-}\"" in script
	assert "BASE_REF=\"${ORCH_FOLLOWUP_INTEGRATION_BRANCH}\"" in script
	assert "Retargeting base to ${BASE_REF}." in script


def test_review_blocked_merged_fix_followup_refuses_when_integration_branch_invalid():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "RB_FOLLOWUP_REFUSED=\"true\"" in script
	assert "integration branch '${ORCH_FOLLOWUP_INTEGRATION_BRANCH:-<missing>}' is unavailable. Aborting follow-up PR creation to avoid targeting ${DEFAULT_BRANCH:-main}." in script
	assert "Refused merged follow-up PR creation for review-blocked issue #${rb_issue}" in script


def test_review_blocked_merged_fix_followup_keeps_default_base_without_integration_context():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert ": \"${BASE_REF:=${DEFAULT_BRANCH:-main}}\"" in script
	assert "if [ \"${RB_INTEGRATION_BRANCH_VALID}\" = \"true\" ]" in script
	assert "&& { [ \"${BASE_REF}\" = \"${DEFAULT_BRANCH:-main}\" ] || [ \"${BASE_REF}\" = \"main\" ]; }; then" in script


def test_post_issue_comment_json_validates_numeric_body_size_before_limit_check():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	post_issue_comment_json = script.split("post_issue_comment_json() {", 1)[1].split('payload_file="$(mktemp', 1)[0]
	guard = 'if ! [[ "${body_bytes}" =~ ^[0-9]+$ ]]; then'
	limit_check = 'if [ "${body_bytes}" -gt 65536 ]; then'
	assert guard in post_issue_comment_json
	assert 'Failed to capture numeric body size for #${issue_num}; skipping post.' in post_issue_comment_json
	assert post_issue_comment_json.index(guard) < post_issue_comment_json.index(limit_check)


def test_post_tracking_comment_validates_numeric_body_size_before_limit_check():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	post_tracking_comment = script.split("post_tracking_comment() {", 1)[1].split('payload_file="$(mktemp', 1)[0]
	guard = 'if ! [[ "${body_bytes}" =~ ^[0-9]+$ ]]; then'
	limit_check = 'if [ "${body_bytes}" -gt 65536 ]; then'
	assert guard in post_tracking_comment
	assert 'Failed to capture numeric body size for tracking issue #${TRACKING_NUM}; skipping post.' in post_tracking_comment
	assert post_tracking_comment.index(guard) < post_tracking_comment.index(limit_check)


def test_validation_fixing_backfills_ai_merged_from_linked_merged_pr_evidence():
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_cycle"] = 1
	state["validation_active_fix_issues"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:closed"]},
		issue_linked_prs={501: 901},
		prs=[
			{
				"number": 901,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-12T08:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-501",
				"mergeable": None,
				"mergeable_state": "unknown",
			},
		],
	)
	assert result["latest_state"]["status"] == "validating"
	assert result["latest_state"]["validation_cycle"] == 2
	assert len(result["validation_dispatches"]) == 1
	assert "ai:merged" in result["issues"]["501"]["labels"]
	assert "ai:closed" not in result["issues"]["501"]["labels"]


def test_validation_fixing_closed_without_merged_evidence_still_fails():
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_cycle"] = 1
	state["validation_active_fix_issues"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:closed"]},
	)
	assert result["latest_state"]["status"] == "failed"
	assert "closed without merge" in result["latest_state"].get("validation_failure_reason", "")
	assert "ai:merged" not in result["issues"]["501"]["labels"]


def test_validation_fixing_lookup_failure_is_fail_safe_and_no_backfill():
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_cycle"] = 1
	state["validation_active_fix_issues"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:closed"]},
		timeline_fail_for_issues=[501],
	)
	assert result["latest_state"]["status"] == "validation-fixing"
	assert result["latest_state"].get("validation_fix_issues_batch_cycles") == 1
	assert "validation_failure_reason" not in result["latest_state"]
	assert "ai:merged" not in result["issues"]["501"]["labels"]
	assert "merged PR lookup failed; leaving issue pending for retry" in (result["stdout"] + result["stderr"])



def test_validation_cycle_limit_marks_failed():
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 3
	state["validation_last_dispatch_cycle"] = 3
	state["validation_active_fix_issues"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 501: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "failed"
	assert "MAX_VALIDATE_CYCLES=3" in result["latest_state"].get("validation_failure_reason", "")
	assert "ai:validation-failed" in result["tracking_labels"]
	assert result["validation_dispatches"] == []



def test_validation_dispatch_failure_marks_failed():
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_cycle"] = 0
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		fail_validation_dispatch=True,
	)
	assert result["latest_state"]["status"] == "failed"
	assert "Unable to dispatch ai-validate.yml" in result["latest_state"].get("validation_failure_reason", "")
	assert "ai:validation-failed" in result["tracking_labels"]


def test_validation_harness_error_raw_status_preserves_budget_and_sets_additive_label():
	state = _base_state(status="validating")
	state["validation_cycle"] = 2
	state["validation_recovery_count"] = 2
	state["validation_last_dispatch_cycle"] = 2
	state["judge_last_fingerprint"] = "fingerprint-before-harness-error"
	state["judge_fingerprint_repeat_count"] = 3
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		validation_workflow_runs=[{
			"id": 7001,
			"run_attempt": 1,
			"status": "completed",
			"conclusion": "failure",
			"created_at": "2026-01-01T00:00:00Z",
			"outputs": {"raw_status": "harness_error"},
		}],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed"
	assert ls["validation_cycle"] == 2
	assert ls["validation_recovery_count"] == 2
	assert ls["validation_last_raw_status"] == "harness_error"
	assert (ls["judge_last_fingerprint"], ls["judge_fingerprint_repeat_count"]) == ("", 0)
	assert "ai:validation-failed" in result["tracking_labels"]
	assert "ai:harness-broken" in result["tracking_labels"]
	assert "HARNESS_ERROR_DETECTED" in (result["stdout"] + result["stderr"])



def test_validation_harness_error_comment_fallback_preserves_budget_when_outputs_missing():
	state = _base_state(status="validating")
	state["validation_cycle"] = 2
	state["validation_recovery_count"] = 2
	state["validation_last_dispatch_cycle"] = 2
	state["judge_last_fingerprint"] = "fingerprint-before-harness-error"
	state["judge_fingerprint_repeat_count"] = 3
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=["## ⚠️ Runtime validation harness generation failed\n\nTemplate renderer failed while generating validation assets."],
		validation_workflow_runs=[{
			"id": 7002,
			"run_attempt": 1,
			"status": "completed",
			"conclusion": "failure",
			"created_at": "2026-01-01T00:00:00Z",
		}],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed"
	assert ls["validation_cycle"] == 2
	assert ls["validation_recovery_count"] == 2
	assert ls["validation_last_raw_status"] == "harness_error"
	assert (ls["judge_last_fingerprint"], ls["judge_fingerprint_repeat_count"]) == ("", 0)
	assert "ai:harness-broken" in result["tracking_labels"]
	assert "validation_raw_status_fallback" in (result["stdout"] + result["stderr"])



def test_validation_harness_error_validate_failure_comment_accepts_bare_token():
	state = _base_state(status="validating")
	state["validation_cycle"] = 2
	state["validation_recovery_count"] = 2
	state["validation_last_dispatch_cycle"] = 2
	state["judge_last_fingerprint"] = "fingerprint-before-harness-error"
	state["judge_fingerprint_repeat_count"] = 3
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=["## ❌ Validate workflow failure\n\nharness_error"],
		validation_workflow_runs=[{
			"id": 7003,
			"run_attempt": 1,
			"status": "completed",
			"conclusion": "failure",
			"created_at": "2026-01-01T00:00:00Z",
		}],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed"
	assert ls["validation_cycle"] == 2
	assert ls["validation_recovery_count"] == 2
	assert ls["validation_last_raw_status"] == "harness_error"
	assert (ls["judge_last_fingerprint"], ls["judge_fingerprint_repeat_count"]) == ("", 0)
	assert "ai:harness-broken" in result["tracking_labels"]
	assert "HARNESS_ERROR_DETECTED" in (result["stdout"] + result["stderr"])




def test_validation_fixing_label_collects_active_fix_issue_ids_from_comment():
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	comment_body = """## 🧪 Runtime validation found fixable issues

- #501: Fix API validation issue
- #502: Fix migration edge case
"""
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-fixing"],
		tracking_comments=[comment_body],
	)
	assert result["latest_state"]["status"] == "validation-fixing"
	assert result["latest_state"]["validation_active_fix_issues"] == [501, 502]


def test_validation_fixing_extracts_issues_from_literal_backslash_n_comment():
	"""post_tracking_comment produces literal \\n (not real newlines).
	extract_fix_issues_from_comment must handle both formats."""
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	# Simulate what gh api stores when post_tracking_comment sends literal \n
	comment_body = (
		"## 🧪 Runtime validation found fixable issues\\n\\n"
		"Diagnosis text here\\n\\nCreated fix-up issues:\\n"
		"- #601: Fix first issue\\n- #602: Fix second issue"
	)
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-fixing"],
		tracking_comments=[comment_body],
	)
	assert result["latest_state"]["status"] == "validation-fixing"
	assert result["latest_state"]["validation_active_fix_issues"] == [601, 602]


def test_validation_fixing_extracts_single_issue_from_literal_backslash_n():
	"""Single fix issue after literal \\n — the exact scenario that caused
	issue #2269 to fail before the extraction fix."""
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	comment_body = (
		"## 🧪 Runtime validation found fixable issues\\n\\n"
		"Diagnosis text\\n\\nCreated fix-up issues:\\n- #701: Only fix"
	)
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-fixing"],
		tracking_comments=[comment_body],
	)
	assert result["latest_state"]["status"] == "validation-fixing"
	assert result["latest_state"]["validation_active_fix_issues"] == [701]


def test_invalid_max_validate_cycles_defaults_to_three():
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 3
	state["validation_last_dispatch_cycle"] = 3
	state["validation_active_fix_issues"] = [501]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="0",
		issue_labels={10: ["ai:merged"], 501: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "failed"
	assert "MAX_VALIDATE_CYCLES=3" in result["latest_state"].get("validation_failure_reason", "")

def test_validated_label_marks_complete_and_keeps_open():
	state = _base_state(status="validating")
	state["validation_cycle"] = 2
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert result["tracking_closed"] is False
	assert "ai:validated" in result["tracking_labels"]
	assert "ai:harness-broken" not in result["tracking_labels"]


def test_validated_clears_harness_broken_label_and_records_pass_raw_status():
	state = _base_state(status="validating")
	state["validation_cycle"] = 2
	state["validation_last_raw_status"] = "harness_error"
	state["validation_failure_reason"] = "## ❌ Runtime validation harness error\n\nHarness failed"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validated", "ai:harness-broken"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_last_raw_status"] == "pass"
	assert "ai:harness-broken" not in result["tracking_labels"]


def test_validated_removes_stale_validating_and_validation_fixing_labels():
	"""Both ai:validating and ai:validation-fixing must be removed when ai:validated is present.

	The set_tracking_phase_label function previously built --remove-label args
	for ALL sibling phase labels.  If gh issue edit failed for any of those
	(e.g. the label was not on the issue) the || true silently swallowed the
	error, leaving stale ai:validating / ai:validation-fixing on the issue
	even after the project had been marked as validated.  The fix fetches
	current issue labels first and only removes labels that are present.
	"""
	state = _base_state(status="validating")
	state["validation_cycle"] = 2
	# Issue already has ai:validated (set by validate_process.sh in cycle 2)
	# but still carries the stale labels from earlier phases.
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validating", "ai:validation-fixing", "ai:validated"],
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["latest_state"]["validation_completed_cycle"] == 2
	assert result["tracking_closed"] is False
	final_labels = result["tracking_labels"]
	assert "ai:validated" in final_labels, f"ai:validated missing from {final_labels}"
	assert "ai:validating" not in final_labels, f"ai:validating still present in {final_labels}"
	assert "ai:validation-fixing" not in final_labels, f"ai:validation-fixing still present in {final_labels}"


def test_validated_from_validation_fixing_state_removes_all_stale_labels():
	"""Stale labels are removed when the state is validation-fixing at poll time."""
	state = _base_state(status="validation-fixing")
	state["validation_cycle"] = 2
	state["validation_active_fix_issues"] = []
	# Issue has both stale labels along with ai:validated (cycle 2 passed).
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validating", "ai:validation-fixing", "ai:validated"],
	)
	assert result["latest_state"]["status"] == "complete"
	final_labels = result["tracking_labels"]
	assert "ai:validated" in final_labels, f"ai:validated missing from {final_labels}"
	assert "ai:validating" not in final_labels, f"ai:validating still present in {final_labels}"
	assert "ai:validation-fixing" not in final_labels, f"ai:validation-fixing still present in {final_labels}"


def test_validation_run_fallback_completes_when_label_missing():
	"""When in validating state but ai:validated label is missing, the fallback
	should detect a successful workflow run conclusion and mark complete."""
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_ts"] = 0
	state["validation_last_dispatch_cycle"] = 1
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validating"],
		validation_workflow_runs=[{
			"status": "completed",
			"conclusion": "success",
			"created_at": "2026-01-01T00:00:00Z",
		}],
	)
	assert result["latest_state"]["status"] == "complete"
	assert "ai:validated" in result["tracking_labels"]
	# No new validation dispatch should have been made
	assert len(result["validation_dispatches"]) == 0


def test_validation_run_fallback_does_not_trigger_on_failure():
	"""When the last validation run concluded with failure, the fallback should
	not trigger and the poller should redispatch."""
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_ts"] = 0
	state["validation_last_dispatch_cycle"] = 0
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validating"],
		validation_workflow_runs=[{
			"status": "completed",
			"conclusion": "failure",
			"created_at": "2026-01-01T00:00:00Z",
		}],
	)
	# Should NOT complete — fallback only triggers on success
	assert result["latest_state"]["status"] == "validating"
	assert len(result["validation_dispatches"]) == 1


def test_validation_active_run_prevents_redispatch():
	"""When a validation run is still in progress and the stale threshold has
	been exceeded, the poller should NOT redispatch."""
	state = _base_state(status="validating")
	state["validation_cycle"] = 1
	state["validation_last_dispatch_ts"] = 1  # Very old timestamp to trigger stale check
	state["validation_last_dispatch_cycle"] = 1
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validating"],
		validation_workflow_runs=[{
			"status": "in_progress",
			"conclusion": None,
			"created_at": "2026-01-01T00:00:00Z",
		}],
	)
	# Should not have dispatched a new validation run
	assert len(result["validation_dispatches"]) == 0
	# Should still be in validating state
	assert result["latest_state"]["status"] == "validating"


# ---------------------------------------------------------------------------
# Tests: judge advancement logic (fix-up issue handling)
# ---------------------------------------------------------------------------


def test_in_progress_judge_does_not_advance_when_fixups_added_to_current_wave():
	"""When the judge returns in_progress with new issues, those issues are
	added to the current wave. The poller must NOT advance current_wave
	because the newly-added issues are still pending (non-terminal)."""
	state = _base_state(status="in_progress")
	state["total_waves"] = 2
	state["waves"].append({
		"wave": 2,
		"issues": [
			{"id": "issue-2", "github_issue": None, "status": "not_created"},
		],
	})
	state["pending_issue_defs"] = {
		"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
	}
	codex_json = {
		"status": "in_progress",
		"justification": "need fix-up",
		"assessment": "Wave 1 merged but needs a fix",
		"new_issues": [
			{"id": "fixup-1", "title": "Fix-up 1", "body": "Fix the thing"},
		],
		"issues_to_revert": [],
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		issue_labels={10: ["ai:merged"]},
		codex_json=codex_json,
	)
	ls = result["latest_state"]
	# Must NOT have advanced to wave 2 — fix-up is still pending in wave 1
	assert ls["current_wave"] == 1, f"Expected current_wave=1, got {ls['current_wave']}"
	# The fix-up issue should be in wave 1's issues
	wave1_ids = [i["id"] for i in ls["waves"][0]["issues"]]
	assert "fixup-1" in wave1_ids, f"fixup-1 not found in wave 1 issues: {wave1_ids}"


def test_in_progress_judge_advances_when_no_new_issues():
	"""When the judge returns in_progress with NO new issues, the poller
	should advance to the next wave normally."""
	state = _base_state(status="in_progress")
	state["total_waves"] = 2
	state["waves"].append({
		"wave": 2,
		"issues": [
			{"id": "issue-2", "github_issue": None, "status": "not_created"},
		],
	})
	state["pending_issue_defs"] = {
		"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
	}
	codex_json = {
		"status": "in_progress",
		"justification": "on track",
		"assessment": "Wave 1 done, proceed to wave 2",
		"new_issues": [],
		"issues_to_revert": [],
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		codex_json=codex_json,
	)
	ls = result["latest_state"]
	# Should have advanced to wave 2
	assert ls["current_wave"] == 2, f"Expected current_wave=2, got {ls['current_wave']}"


def test_clean_wave_skip_advances_without_judge_when_future_defs_remain():
	state = _base_state(status="in_progress")
	state["total_waves"] = 2
	state["waves"].append({
		"wave": 2,
		"issues": [
			{"id": "issue-2", "github_issue": None, "status": "not_created"},
		],
	})
	state["pending_issue_defs"] = {
		"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="true",
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "in_progress",
			"justification": "unused",
			"assessment": "unused",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	ls = result["latest_state"]
	assert ls["current_wave"] == 2
	assert ls["judge_cycle"] == 1
	assert ls.get("judge_stall_cycles", 0) == 0
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert not any("Judge Evaluation" in body for body in tracking_bodies)


def test_clean_wave_skip_blocked_when_disabled():
	state = _base_state(status="in_progress")
	state["total_waves"] = 2
	state["waves"].append({
		"wave": 2,
		"issues": [
			{"id": "issue-2", "github_issue": None, "status": "not_created"},
		],
	})
	state["pending_issue_defs"] = {
		"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "in_progress",
			"justification": "on track",
			"assessment": "advance",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Judge Evaluation" in body for body in tracking_bodies)


def test_clean_wave_skip_blocked_when_wave_has_failed_issue():
	state = _base_state(status="in_progress")
	state["total_waves"] = 2
	state["waves"].append({
		"wave": 2,
		"issues": [
			{"id": "issue-2", "github_issue": None, "status": "not_created"},
		],
	})
	state["pending_issue_defs"] = {
		"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="true",
		issue_labels={10: ["ai:closed"]},
		codex_json={
			"status": "in_progress",
			"justification": "needs decision",
			"assessment": "failed issue present",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Judge Evaluation" in body for body in tracking_bodies)


def test_clean_wave_skip_blocked_when_stuck_wave_forces_judge():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	state["waves"][0]["issues"].append({
		"id": "missing-1",
		"github_issue": None,
		"status": "not_created",
	})
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="true",
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "in_progress",
			"justification": "stuck handling",
			"assessment": "judge required",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Judge Evaluation" in body for body in tracking_bodies)


def test_judge_repeat_fingerprint_breaker_escalates_after_limit():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	state.pop("judge_last_fingerprint", None)
	state.pop("judge_fingerprint_repeat_count", None)
	judge_json = {
		"status": "failed",
		"justification": "npm run audit:ci failed in scripts/lint.sh:123 at 2026-01-01T12:34:56Z cycle 5/10",
		"assessment": "audit gate still fails",
		"new_issues": [],
		"issues_to_revert": [],
	}

	first = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		judge_repeat_fingerprint_max="5",
		issue_labels={10: ["ai:merged"]},
		codex_json=judge_json,
	)
	first_state = first["latest_state"]
	assert first_state["recovery_count"] == 1
	assert first_state["judge_fingerprint_repeat_count"] == 1
	assert first_state["judge_last_fingerprint"]
	assert first_state["status"] == "in_progress"
	assert len(first.get("created_issues", [])) == 0

	second = _run_poller(
		state=first_state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		judge_repeat_fingerprint_max="5",
		issue_labels={10: ["ai:merged"]},
		codex_json=judge_json,
	)
	second_state = second["latest_state"]
	assert second_state["recovery_count"] == 2
	assert second_state["judge_fingerprint_repeat_count"] == 2
	assert second_state["status"] == "in_progress"
	assert len(second.get("created_issues", [])) == 0

	third = _run_poller(
		state=second_state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		judge_repeat_fingerprint_max="2",
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "failed",
			"justification": "npm run audit:ci failed in scripts/lint.sh:987 at 2026-01-01T12:39:56Z cycle 7/10",
			"assessment": "same gate failure with cosmetic drift",
			"new_issues": [{"id": "fixup-1", "title": "Fix-up 1", "body": "Fix gating failure"}],
			"issues_to_revert": [],
		},
	)
	third_state = third["latest_state"]
	assert third_state["status"] == "failed"
	assert third_state["judge_fingerprint_repeat_count"] == 3
	assert third_state["recovery_count"] == 2
	assert len(third.get("created_issues", [])) == 0
	assert "ai:blocked" in third["tracking_labels"]
	tracking_bodies = [c.get("body", "") for c in third["issues"]["192"]["comments"]]
	completion_comment = next(body for body in tracking_bodies if "<!-- orchestrator:completion-status -->" in body)
	assert "<!-- status:failed -->" in completion_comment
	assert "Judge repeat-fingerprint breaker triggered" in completion_comment
	assert any("Judge repeat-fingerprint breaker triggered" in body for body in tracking_bodies)
	breaker_comment = next(
		body for body in tracking_bodies
		if body.startswith("## ❌ Judge repeat-fingerprint breaker triggered")
	)
	assert "scripts/lint.sh" in breaker_comment
	assert ":987" not in breaker_comment
	assert "2026-01-01T12:39:56Z" not in breaker_comment
	assert "cycle 7/10" not in breaker_comment


def test_judge_repeat_fingerprint_normalization_resets_on_material_change():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	first = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		judge_repeat_fingerprint_max="5",
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "failed",
			"justification": "npm run audit:ci failed in scripts/lint.sh:44 at 2026-01-01T12:34:56Z cycle 1/10",
			"assessment": "first failure",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	second = _run_poller(
		state=first["latest_state"],
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		judge_repeat_fingerprint_max="5",
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "failed",
			"justification": "tests failed in scripts/tests.sh:88 at 2026-01-01T12:40:00Z cycle 2/10",
			"assessment": "materially different failure",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	ls = second["latest_state"]
	assert ls["judge_fingerprint_repeat_count"] == 1
	assert ls["recovery_count"] == 2
	assert ls["status"] == "in_progress"
	assert len(second.get("created_issues", [])) == 0


def test_judge_repeat_fingerprint_empty_justification_fail_open():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	state.update({"judge_last_fingerprint": "abc123", "judge_fingerprint_repeat_count": 2})
	result = _run_poller(state=state, enable_validation="false", max_validate_cycles="3", enable_clean_wave_judge_skip="false", judge_repeat_fingerprint_max="2", issue_labels={10: ["ai:merged"]}, codex_json={"status": "failed", "justification": "", "assessment": "missing details", "new_issues": [], "issues_to_revert": []})
	ls = result["latest_state"]
	assert (ls["judge_last_fingerprint"], ls["judge_fingerprint_repeat_count"]) == ("", 0)
	assert ls["status"] == "in_progress"
	assert ls["recovery_count"] == 1
	assert "ai:blocked" not in result["tracking_labels"]


def test_judge_prompt_includes_harness_validation_context():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	state["validation_last_raw_status"] = "harness_error"
	state["validation_failure_reason"] = "## ❌ Runtime validation harness error\n\nHarness timed out"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		tracking_labels=["ai:harness-broken"],
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "in_progress",
			"justification": "harness issue still under investigation",
			"assessment": "waiting for harness repair",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	prompt = result["judge_prompt"]
	assert "Latest validation raw status: harness_error" in prompt
	assert "Harness-broken label present: true" in prompt
	assert "Judge note: the latest validation failure is classified as a harness/infrastructure defect" in prompt


def test_judge_repeat_fingerprint_penalty_is_suppressed_after_harness_error():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	state["validation_last_raw_status"] = "harness_error"
	state["judge_last_fingerprint"] = "existing-fingerprint"
	state["judge_fingerprint_repeat_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_clean_wave_judge_skip="false",
		judge_repeat_fingerprint_max="2",
		tracking_labels=["ai:harness-broken"],
		issue_labels={10: ["ai:merged"]},
		codex_json={
			"status": "failed",
			"justification": "same harness issue again",
			"assessment": "still waiting on validation harness repair",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	ls = result["latest_state"]
	assert (ls["judge_last_fingerprint"], ls["judge_fingerprint_repeat_count"]) == ("", 0)
	assert ls["status"] == "in_progress"
	assert "ai:blocked" not in result["tracking_labels"]


def test_backward_scan_updates_prior_wave_merged_issue():
	"""When a prior wave has a non-terminal issue that is now ai:merged,
	the backward scan should update its status in state."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 2,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "fixup-1", "github_issue": 35, "status": "pending"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": 20, "status": "pending"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10, "fixup-1": 35, "issue-2": 20},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 35: ["ai:merged"], 20: ["ai:implementing"]},
	)
	ls = result["latest_state"]
	# The backward scan should have updated fixup-1 in wave 1 to "merged"
	wave1_issues = {i["id"]: i["status"] for i in ls["waves"][0]["issues"]}
	assert wave1_issues.get("fixup-1") == "merged", \
		f"Expected fixup-1 status=merged, got {wave1_issues.get('fixup-1')}"
	assert result["issue_label_calls"].get("35", 0) == 0


def test_backward_scan_updates_prior_wave_closed_issue():
	"""When a prior wave has a non-terminal issue that is now ai:closed,
	the backward scan should update its status in state."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 2,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "fixup-1", "github_issue": 35, "status": "pending"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": 20, "status": "pending"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10, "fixup-1": 35, "issue-2": 20},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 35: ["ai:closed"], 20: ["ai:implementing"]},
	)
	ls = result["latest_state"]
	wave1_issues = {i["id"]: i["status"] for i in ls["waves"][0]["issues"]}
	assert wave1_issues.get("fixup-1") == "closed", \
		f"Expected fixup-1 status=closed, got {wave1_issues.get('fixup-1')}"
	assert result["issue_label_calls"].get("35", 0) == 0


def test_backward_scan_label_batch_partial_falls_back_to_rest():
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 2,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "fixup-1", "github_issue": 35, "status": "pending"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": 20, "status": "pending"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10, "fixup-1": 35, "issue-2": 20},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 35: ["ai:merged"], 20: ["ai:implementing"]},
		gql_mode="partial",
	)
	assert result["issue_label_calls"].get("35", 0) > 0


def test_backward_scan_label_batch_error_falls_back_to_rest():
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 2,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "fixup-1", "github_issue": 35, "status": "pending"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": 20, "status": "pending"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10, "fixup-1": 35, "issue-2": 20},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 35: ["ai:merged"], 20: ["ai:implementing"]},
		gql_mode="error",
	)
	assert result["issue_label_calls"].get("35", 0) > 0


def test_backward_scan_promotes_ready_to_merge_with_merged_pr_to_merged():
	"""Backward-scan defensive reconcile (added 2026-04-27): when a prior-wave
	child issue carries ai:ready-to-merge but its linked PR is already MERGED,
	the backward-scan must promote the issue to ai:merged (and update wave
	state to merged) so close_merged_issues_sweep can finalize it. Previously
	the backward-scan only attempted gh pr merge against open PRs and silently
	left already-merged prior-wave children stranded.

	Integration branch intentionally unset: the fingerprint-capture branch
	is gated on .integration_branch being non-empty and the test fixture
	does not provide a real integration branch, so we keep the assertion
	surface scoped to the label promotion + wave-state mutation that the
	defensive reconcile is supposed to guarantee.
	"""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 2,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "fixup-1", "github_issue": 35, "status": "pending"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": 20, "status": "pending"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10, "fixup-1": 35, "issue-2": 20},
		"pending_issue_defs": {},
	}
	merged_pr = {
		"number": 935,
		"state": "closed",
		"merged": True,
		"merged_at": "2026-04-27T12:00:00Z",
		"baseRefName": "orchestrator/project-192",
		"headRefName": "ai/issue-35",
		"headRefFromApi": "ai/issue-35",
		"mergeable": True,
		"mergeable_state": "clean",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={
			10: ["ai:merged"],
			35: ["ai:ready-to-merge"],
			20: ["ai:implementing"],
		},
		issue_linked_prs={35: 935},
		prs=[merged_pr],
	)
	# Issue 35 must transition to ai:merged in the live label store and to
	# status=merged in wave-1 state. The label edit also strips
	# ai:ready-to-merge so re-runs are idempotent.
	final_labels = result["issues"]["35"]["labels"]
	assert "ai:merged" in final_labels, f"Expected ai:merged on #35; got {final_labels}"
	assert "ai:ready-to-merge" not in final_labels, f"Expected ai:ready-to-merge stripped from #35; got {final_labels}"
	wave1_issues = {i["id"]: i["status"] for i in result["latest_state"]["waves"][0]["issues"]}
	assert wave1_issues.get("fixup-1") == "merged", f"Expected fixup-1 status=merged, got {wave1_issues.get('fixup-1')}"
	# Public log-prefix audit line (renames are breaking per CLAUDE.md §6).
	assert "[backward-scan] #35 ai:ready-to-merge but linked PR #935 is already merged" in result["stdout"], (
		"Missing backward-scan promotion log line in poller stdout"
	)


def test_close_merged_issues_sweep_closes_ready_to_merge_with_verified_merged_pr():
	"""close_merged_issues_sweep defensive backstop (added 2026-04-27):
	open issues carrying ai:ready-to-merge whose linked PR is verified
	merged via the issue timeline must be closed AND have ai:merged
	backfilled before close, so concurrent readers (wave-status resolver,
	validation fix-up loop) see the same terminal label as the
	merged_label-origin path always produced.
	"""
	# Project is already complete so the wave loop is benign and the only
	# late side-effect on the orchestrator-managed child issue (#10) is the
	# sweep's ready_label-origin branch.
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 1,
		"total_waves": 1,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "complete",
		"integration_branch": "orchestrator/project-192",
		"final_merge_pr": 0,
		"final_merge_status": "merged",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
	}
	merged_pr = {
		"number": 901,
		"state": "closed",
		"merged": True,
		"merged_at": "2026-04-27T12:00:00Z",
		"baseRefName": "orchestrator/project-192",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 901},
		prs=[merged_pr],
		mock_gh_issue_list_label_filter=True,
	)
	# Sweep must (a) backfill ai:merged + strip ai:ready-to-merge before
	# close, (b) close the issue, (c) emit the public log prefix carrying
	# origin=ready_label so the new ready_label branch is observable.
	final_labels = result["issues"]["10"]["labels"]
	assert "ai:merged" in final_labels, f"Expected ai:merged backfilled on #10; got {final_labels}"
	assert "ai:ready-to-merge" not in final_labels, f"Expected ai:ready-to-merge stripped from #10; got {final_labels}"
	assert 10 in result.get("closed_issues", []), (
		f"Expected #10 closed by sweep; closed_issues={result.get('closed_issues')}"
	)
	assert "CLOSE_MERGED_SWEEP issue=10 pr=901 origin=ready_label status=closed" in result["stdout"], (
		"Missing CLOSE_MERGED_SWEEP ready_label closure log line in poller stdout"
	)


def test_close_merged_issues_sweep_pending_ready_to_merge_does_not_alert():
	"""When an ai:ready-to-merge issue has NO merged PR yet (the normal
	pending state for an in-flight auto-merge), the sweep must NOT close
	the issue and must NOT emit a stale-label Telegram alert. The label is
	a request to auto-merge, not a contract that a merged PR exists.
	"""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 1,
		"total_waves": 1,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "complete",
		"integration_branch": "orchestrator/project-192",
		"final_merge_pr": 0,
		"final_merge_status": "merged",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
	}
	open_pr = {
		"number": 901,
		"state": "open",
		"merged": False,
		"baseRefName": "orchestrator/project-192",
		"headRefName": "ai/issue-10",
		"headRefFromApi": "ai/issue-10",
		"mergeable": True,
		"mergeable_state": "clean",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:ready-to-merge"]},
		issue_linked_prs={10: 901},
		prs=[open_pr],
		mock_gh_issue_list_label_filter=True,
	)
	assert 10 not in result.get("closed_issues", []), (
		f"Issue #10 should remain open while PR is unmerged; closed_issues={result.get('closed_issues')}"
	)
	# The pending log prefix carries origin=ready_label so the no-alert
	# branch is observable; the merged_label-origin alert prefix must NOT
	# appear for this issue (would indicate a stale-label false positive).
	assert "CLOSE_MERGED_SWEEP issue=10 origin=ready_label no_merged_pr_found" in result["stdout"], (
		"Missing pending-state log line for ai:ready-to-merge sweep entry"
	)
	assert "CLOSE_MERGED_SWEEP issue=10 origin=merged_label no_merged_pr_found" not in result["stdout"], (
		"Pending ai:ready-to-merge issue must not trip the merged_label stale-label alert path"
	)
	# Labels must remain unchanged (no spurious ai:merged backfill).
	final_labels = result["issues"]["10"]["labels"]
	assert "ai:merged" not in final_labels, f"Did not expect ai:merged backfill; got {final_labels}"
	assert "ai:ready-to-merge" in final_labels, f"Expected ai:ready-to-merge preserved; got {final_labels}"


def test_in_progress_judge_recreates_closed_fixup_id_stays_on_current_wave():
	"""If judge reuses a local ID whose previous issue is closed, the recreated
	issue should replace tracking for that ID and the poller must stay on the
	current wave until the recreated issue is resolved."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "fixup-1", "github_issue": 35, "status": "closed"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10, "fixup-1": 35},
		"pending_issue_defs": {
			"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
		},
	}
	codex_json = {
		"status": "in_progress",
		"justification": "retry fix-up",
		"assessment": "Need another attempt for fixup-1",
		"new_issues": [
			{"id": "fixup-1", "title": "Fix-up 1", "body": "Retry fix"},
		],
		"issues_to_revert": [],
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"], 35: ["ai:closed"]},
		codex_json=codex_json,
	)
	ls = result["latest_state"]
	assert ls["current_wave"] == 1, f"Expected current_wave=1, got {ls['current_wave']}"
	wave1 = {i["id"]: i for i in ls["waves"][0]["issues"]}
	assert wave1["fixup-1"]["status"] == "pending", f"Expected recreated fixup-1 status=pending, got {wave1['fixup-1']['status']}"
	assert str(wave1["fixup-1"]["github_issue"]) != "35", f"Expected fixup-1 github_issue to be replaced, got {wave1['fixup-1']['github_issue']}"
	created_issue = next((item for item in result.get("created_issues", []) if item.get("number") == 900), None)
	assert created_issue is not None, f"Expected created issue #900 in mock store, got {result.get('created_issues', [])}"
	assert "ai:clarification" in created_issue.get("labels", [])
	assert "ai:orchestrator-managed" in created_issue.get("labels", [])


def test_standalone_close_and_reissue_keeps_clarification_only_label():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	anchor = "This issue was re-created by standalone stall recovery."
	assert anchor in script, "Could not locate standalone close_and_reissue guidance block"
	window = script[script.index(anchor):script.index(anchor) + 1200]
	assert '--label "ai:clarification"' in window
	assert '--label "ai:orchestrator-managed"' not in window


def test_fresh_push_suppress_window_pinned_to_50_minutes():
	"""Lock the ``_FRESH_PUSH_SUPPRESS_SECS`` window at 50 minutes (3000s).

	The window was bumped from 30 min (1800s) to 50 min (3000s) because
	typical review_autofix cycles run 35-45 minutes end-to-end on busy
	consumer repos, so a single cycle outlasted the original window and
	the guard never fired between cycles (observed on
	tele-funtoken-msg-scoring PRs #3057 and #3062). The window is
	deliberately not configurable (per original
	investigate-stall-recovery-dx7zm Q2=B decision); this test pins the
	value so any future bump-or-shrink is an intentional, reviewed
	change rather than a silent regression.
	"""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "_FRESH_PUSH_SUPPRESS_SECS=3000" in script, (
		"_FRESH_PUSH_SUPPRESS_SECS must equal 3000 (50 min); see "
		"scripts/orchestrate_poll_process.sh _check_fresh_push_guard"
	)
	assert "_FRESH_PUSH_SUPPRESS_SECS=1800" not in script, (
		"obsolete 1800s (30 min) fresh-push window literal still present"
	)


def test_close_linked_pr_uses_multi_source_lookup_and_iterates():
	"""Gap 1 regression guard: ``close_linked_pr`` MUST enumerate every
	linked PR (cross-ref + branch-name + body-parse) and iterate, not
	just consult the timeline ``last`` cross-ref. Enforced here as a
	structural check so the prod miss that orphaned PR #2568 (issue
	#2552) cannot silently regress."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	start = script.index("close_linked_pr() {")
	end = script.index("\n}\n", start) + 2
	body = script[start:end]
	assert "_find_all_linked_prs" in body, (
		"close_linked_pr must call _find_all_linked_prs (multi-source lookup)"
	)
	# Single-PR `_issue_cross_ref_pr_number_last` must NOT be used here;
	# that's what caused the prod miss.
	assert "_issue_cross_ref_pr_number_last" not in body, (
		"close_linked_pr must not rely on the single-PR timeline lookup"
	)
	# Must iterate (while loop over lookup output) and record diagnostics.
	assert "while IFS= read -r pr_num" in body
	assert "close_linked_pr: closing linked PR #" in body
	assert "close_linked_pr: skipping PR #" in body
	assert "close_linked_pr: no linked PRs found for issue" in body


def test_multi_source_lookup_helpers_present():
	"""The three lookup strategies must exist as individually callable
	helpers so future callers can reuse them without duplicating the
	search logic."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "_linked_prs_by_branch_name()" in script
	assert 'gh pr list --repo "${GITHUB_REPOSITORY}"' in script
	assert '--head "ai/issue-${issue_num}"' in script
	assert "_linked_prs_by_body_reference()" in script
	body_ref_start = script.index("_linked_prs_by_body_reference()")
	body_ref_end = script.index("\n}\n", body_ref_start) + 2
	body_ref = script[body_ref_start:body_ref_end]
	# Body-parse regex must include all three GitHub close keywords and
	# a trailing word-boundary guard so "#25528" does not match for #2552.
	assert "close[sd]?|fix(es|ed)?|resolve[sd]?" in body_ref
	assert "\\\\b" in body_ref
	assert "_find_all_linked_prs()" in script
	# Dedup contract: sort -u so the same PR surfaced by multiple
	# strategies is only acted on once.
	find_all_start = script.index("_find_all_linked_prs()")
	find_all_end = script.index("\n}\n", find_all_start) + 2
	assert "sort -u" in script[find_all_start:find_all_end]


def test_close_and_reissue_sites_surface_reissue_without_pr():
	"""Gap 2 regression guard: BOTH close_and_reissue paths must call
	``surface_reissue_closed_without_pr`` BEFORE closing the PR/issue,
	so the surfacing lands on an open issue with the re-issue body
	still accessible."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	# Main-poll path. The case label is indented 4 spaces (the standalone
	# path uses 6 spaces, so this anchor is unambiguous). The case body
	# may contain pre-checks (e.g. the ancestor-chain no-op cap added in
	# PR #1452) before the legacy close+re-issue flow; what matters for
	# this regression guard is that *within* the legacy flow,
	# surface_reissue_closed_without_pr fires before close_linked_pr.
	main_case_open = '\n    close_and_reissue)\n'
	assert main_case_open in script, "Could not locate main close_and_reissue"
	main_start = script.index(main_case_open) + len(main_case_open)
	main_case_tail = script[main_start:]
	main_end_match = re.search(r'\n[ \t]+;;\n', main_case_tail)
	assert main_end_match is not None, (
		"Could not bound main close_and_reissue case block: missing terminating ';;'"
	)
	main_end = main_start + main_end_match.start()
	main_case_body = script[main_start:main_end]
	legacy_flow_match = re.search(
		r'echo "  Closing and re-issuing stalled issue #\$\{issue_num\}\.\.\."\n[ \t]+surface_reissue_closed_without_pr "\$\{issue_num\}"',
		main_case_body,
	)
	assert legacy_flow_match is not None, (
		"main close_and_reissue legacy flow anchor missing"
	)
	main_window = main_case_body[legacy_flow_match.start():]
	assert 'surface_reissue_closed_without_pr "${issue_num}"' in main_window
	assert 'close_linked_pr "${issue_num}"' in main_window, (
		"close_linked_pr call missing in main_window"
	)
	assert main_window.index('surface_reissue_closed_without_pr "${issue_num}"') < main_window.index('close_linked_pr "${issue_num}"'), (
		"surface must run before close_linked_pr so the comment lands on an open issue"
	)
	assert '"main"' in main_window
	# Standalone path. Use the legacy-flow close message (which mentions
	# the issue being stuck) so we don't collide with the ancestor-chain
	# no-op cap short-circuit — that short-circuit intentionally does
	# *not* re-issue, and therefore must not call
	# surface_reissue_closed_without_pr.
	standalone_anchor = 'close_linked_pr "${issue_num}" "Closed by standalone stall recovery — issue #${issue_num} was stuck'
	assert standalone_anchor in script, (
		"Could not locate standalone close_and_reissue legacy close call"
	)
	idx = script.index(standalone_anchor)
	pre = script[max(0, idx - 400):idx]
	assert 'surface_reissue_closed_without_pr "${issue_num}"' in pre, (
		"standalone close_and_reissue must also call surface_reissue_closed_without_pr"
	)
	assert '"standalone"' in pre


def test_surface_reissue_closed_without_pr_emits_stable_signals():
	"""The log prefix is a public contract (documented in agents.md)
	because downstream alerting greps for it. Guard against accidental
	rename."""
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	start = script.index("surface_reissue_closed_without_pr()")
	end = script.index("\n}\n", start) + 2
	body = script[start:end]
	# Stable log prefix (documented contract).
	assert 'echo "REISSUE_CLOSED_WITHOUT_PR issue=' in body
	# GHA warning annotation.
	assert "::warning title=Re-issue closed without PR::" in body
	# Issue comment with the re-issue context.
	assert "Re-issue closed without producing a PR" in body
	# ai-memory ledger surface via the blessed helper — gated on
	# helper availability so the script works in environments where
	# memory_helpers.sh is not sourced.
	assert "declare -F memory_record_run_event" in body
	assert "reissue_closed_without_pr" in body
	# Must no-op when body lacks the Re-issued marker.
	assert "Re-issued from #" in body


def test_implementation_failed_reissue_keeps_orchestrator_labels_and_updates_mapping():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	state["waves"][0]["issues"][0]["impl_noop_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-managed"],
		issue_labels={10: ["ai:implementation-failed"]},
	)
	created = result.get("created_issues", [])
	assert len(created) == 1
	assert created[0]["number"] == 900
	assert "ai:clarification" in created[0].get("labels", [])
	assert "ai:orchestrator-managed" in created[0].get("labels", [])
	latest = result["latest_state"]
	assert str(latest["issue_number_map"]["issue-1"]) == "900"
	assert latest["waves"][0]["issues"][0]["impl_noop_count"] == 1


def test_implementation_failed_reissue_hits_cap_and_closes_without_creating_new_issue():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "implementation-failed"
	state["waves"][0]["issues"][0]["impl_noop_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-managed"],
		issue_labels={10: ["ai:implementation-failed"]},
	)
	assert result.get("created_issues", []) == []
	assert 10 in result.get("closed_issues", [])
	latest = result["latest_state"]
	assert str(latest["waves"][0]["issues"][0]["github_issue"]) == "10"
	assert latest["waves"][0]["issues"][0]["impl_noop_count"] == 3


def test_implementation_failed_reissue_preserves_dependency_gates_and_pending_defs():
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 1,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "implementation-failed", "impl_noop_count": 0},
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			}
		],
		"dependency_edges": [{"from": "issue-1", "to": "issue-2"}],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {
			"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 2},
		},
		"integration_branch": "",
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-managed"],
		issue_labels={10: ["ai:implementation-failed"]},
	)
	latest = result["latest_state"]
	# issue-1 was implementation-failed and is reissued as a fresh issue.
	assert str(latest["issue_number_map"]["issue-1"]) == "900"
	# issue-2 depends on issue-1, which is non-terminal this tick
	# (implementation-failed, being reissued). The U2 runtime blocker gate
	# (scripts/blocker_check.py, added in #3048) defers issue-2's deferred
	# creation until its blocker terminalizes, so it stays pending rather than
	# being created alongside the reissue — preserving both the dependency
	# edges and the pending definition.
	assert "issue-2" not in latest["issue_number_map"]
	assert latest["pending_issue_defs"] == {
		"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 2},
	}
	created = result.get("created_issues", [])
	assert not any(item.get("title") == "Issue 2" for item in created)
	assert (
		"dispatch_deferred_blocker local_id=issue-2 wave=1 reason=blocked_by_dependency"
		in result["stdout"]
	)
	assert latest["dependency_edges"] == [{"from": "issue-1", "to": "issue-2"}]


def test_clean_wave_skip_advances_without_judge_call():
	"""A clean wave with no pending definitions should advance without invoking judge."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		enable_clean_wave_judge_skip="true",
	)
	ls = result["latest_state"]
	assert ls["current_wave"] == 2
	assert ls["judge_cycle"] == 1
	assert ls["judge_stall_cycles"] == 0
	assert "Running judge evaluation" not in result["stdout"]


def test_clean_wave_skip_disabled_keeps_judge_invocation():
	"""Disabling the skip flag should keep the existing judge invocation path."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		enable_clean_wave_judge_skip="false",
		codex_json={
			"status": "in_progress",
			"justification": "advance",
			"assessment": "Proceed",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	assert "Running judge evaluation" in result["stdout"]
	ls = result["latest_state"]
	assert ls["judge_cycle"] == 1



def test_clean_wave_skip_advances_when_pending_issue_defs_exist():
	"""Deferred later-wave definitions should not block clean-wave judge skip."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {
			"issue-2": {"title": "Issue 2", "body": "Body 2", "priority": 5},
		},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		enable_clean_wave_judge_skip="true",
	)
	ls = result["latest_state"]
	assert ls["current_wave"] == 2
	assert ls["judge_cycle"] == 1
	assert "Running judge evaluation" not in result["stdout"]


def test_clean_wave_skip_does_not_run_when_wave_has_failures():
	"""Failed issues in a completed wave must still invoke judge."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "closed"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:closed"]},
		enable_clean_wave_judge_skip="true",
		codex_json={
			"status": "in_progress",
			"justification": "needs attention",
			"assessment": "Wave has failures",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	assert "Running judge evaluation" in result["stdout"]


def test_clean_wave_skip_does_not_run_on_stuck_wave():
	"""Stuck-wave judge invocations must not be bypassed by clean-wave skip."""
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "issue-2", "github_issue": None, "status": "not_created"},
				],
			},
			{
				"wave": 2,
				"issues": [],
			},
		],
		"dependency_edges": [],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		enable_clean_wave_judge_skip="true",
		codex_json={
			"status": "in_progress",
			"justification": "stuck",
			"assessment": "Need intervention",
			"new_issues": [],
			"issues_to_revert": [],
		},
	)
	assert "Wave 1 is stuck" in result["stdout"]
	assert "Running judge evaluation" in result["stdout"]


def test_implementation_failed_reissue_persists_fixup_blocker_metadata_from_comment():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "implementation-failed"
	issue["github_issue"] = 10
	issue["id"] = "issue-1"

	comment_body = (
		"## Post-Codex validation diagnosed follow-up fixes\n\n"
		"Created fix-up issues:\n- #901\n- #902\n\n"
		"<!-- IMPLEMENT_FIXUP_BLOCKERS_V1\n"
		"{\"fixup_issue_numbers\":[901,902,902],\"blocks_source_issue\":10}\n"
		"IMPLEMENT_FIXUP_BLOCKERS_V1 -->"
	)

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"], 901: ["ai:closed"], 902: ["ai:closed"]},
		issue_closed={901: True, 902: True},
		issue_comments={10: [comment_body]},
	)

	entry = result["latest_state"]["waves"][0]["issues"][0]
	assert entry.get("blocks_source_issue") == 10
	assert entry.get("fixup_issue_numbers") == [901, 902]
 
	state_comments = _extract_state_payloads(result["issues"]["192"]["comments"])
	assert state_comments, "expected state comments to be present"
	latest_payload = json.loads(state_comments[-1])
	latest_entry = latest_payload["waves"][0]["issues"][0]
	assert latest_entry.get("fixup_issue_numbers") == [901, 902]

	new_issue_num = str(entry.get("github_issue"))
	assert "ai:clarification" in result["issues"][new_issue_num]["labels"]


def test_implementation_failed_reissue_without_blocker_metadata_keeps_state_compatible():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "implementation-failed"
	issue["github_issue"] = 10

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementation-failed"]},
	)

	entry = result["latest_state"]["waves"][0]["issues"][0]
	assert "blocks_source_issue" not in entry
	assert "fixup_issue_numbers" not in entry


# ---------------------------------------------------------------------------
# Tests: /revalidate reset from validation-failed
# ---------------------------------------------------------------------------


def test_revalidate_resets_validation_failed_and_dispatches():
	"""A /revalidate comment posted after the state comment should reset a
	validation-failed project back to validating and dispatch validation."""
	state = _base_state(status="failed")
	state["validation_cycle"] = 3
	state["validation_recovery_count"] = 2
	state["validation_failure_reason"] = "Exceeded MAX_VALIDATE_CYCLES"
	state["validation_active_fix_issues"] = [501]
	state["validation_last_dispatch_cycle"] = 3
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed", "ai:harness-broken"],
		tracking_comments=["/revalidate"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "validating", f"Expected status=validating, got {ls['status']}"
	assert ls["validation_cycle"] == 1, f"Expected validation_cycle=1, got {ls['validation_cycle']}"
	assert ls["validation_recovery_count"] == 0, f"Expected validation_recovery_count=0, got {ls['validation_recovery_count']}"
	assert ls["validation_active_fix_issues"] == [], f"Expected empty fix issues, got {ls['validation_active_fix_issues']}"
	assert ls["validation_last_dispatch_cycle"] == 1
	assert "validation_failure_reason" not in ls, f"Expected validation_failure_reason to be removed, got {ls.get('validation_failure_reason')}"
	assert "ai:validating" in result["tracking_labels"]
	assert "ai:validation-failed" not in result["tracking_labels"]
	assert "ai:harness-broken" not in result["tracking_labels"]
	assert len(result["validation_dispatches"]) == 1


def test_revalidate_clears_harness_broken_refreshes_draft_pr_and_records_memory_event():
	state = _base_state(status="failed")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_pr"] = 301
	state["final_merge_status"] = "pending"
	state["validation_failure_reason"] = "Harness failed to boot"
	prs = [
		{
			"number": 301,
			"state": "open",
			"draft": True,
			"baseRefName": "main",
			"headRefName": "orchestrator/project-192",
			"mergeable": True,
			"mergeable_state": "clean",
			"body": "Squash merge of orchestrator project #192.\n\nRefs #192",
		},
	]
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed", "ai:harness-broken"],
		tracking_comments=[
			{
				"id": 7,
				"body": "/revalidate fixed config",
				"user": {"login": "octocat"},
				"created_at": "2026-05-23T16:20:00Z",
				"html_url": "https://github.com/owner/repo/issues/192#issuecomment-7",
			}
		],
		prs=prs,
		existing_branches=["main", "orchestrator/project-192"],
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
	)
	latest = result["latest_state"]
	assert latest["status"] == "validating"
	assert latest["validation_cycle"] == 1
	assert "ai:validating" in result["tracking_labels"]
	assert "ai:validation-failed" not in result["tracking_labels"]
	assert "ai:harness-broken" not in result["tracking_labels"]
	assert len(result["validation_dispatches"]) == 1
	assert result["mock_revalidate_events_get_calls"] == 1
	assert result["mock_revalidate_events_append_calls"] == 1
	assert result["pr_body_update_calls"] == [301]
	assert "<!-- VALIDATION_STATUS_V1 -->" in result["prs"][0]["body"]
	assert "Revalidating after operator reset." in result["prs"][0]["body"]
	stored_events = result["mock_revalidate_events_payload"]
	assert stored_events["repository"] == "owner/repo"
	assert stored_events["tracking_issue_number"] == 192
	assert stored_events["integration_sha"] == "abcdef1234"
	entry = stored_events["entries"][0]
	assert entry["actor"] == "octocat"
	assert entry["timestamp_utc"] == "2026-05-23T16:20:00Z"
	assert entry["prior_outcome"] == "harness_error"
	assert entry["prior_context"] == "Harness failed to boot"
	assert entry["reason"] == "fixed config"
	assert entry["source_comment_id"] == 7
	assert entry["source_comment_url"] == "https://github.com/owner/repo/issues/192#issuecomment-7"


def test_revalidate_dedupes_same_actor_and_sha_within_five_minutes():
	state = _base_state(status="failed")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_failure_reason"] = "Harness failed to boot"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed", "ai:harness-broken"],
		tracking_comments=[
			{
				"id": 8,
				"body": "/revalidate retry please",
				"user": {"login": "octocat"},
				"created_at": "2026-05-23T16:20:00Z",
				"html_url": "https://github.com/owner/repo/issues/192#issuecomment-8",
			}
		],
		existing_branches=["main", "orchestrator/project-192"],
		branch_ref_shas={"orchestrator/project-192": "abcdef1234"},
		mock_revalidate_events_payload={
			"schema_version": "v1",
			"repository": "owner/repo",
			"tracking_issue_number": 192,
			"integration_sha": "abcdef1234",
			"entries": [
				{
					"actor": "octocat",
					"timestamp_utc": "2026-05-23T16:16:30Z",
					"prior_outcome": "harness_error",
				}
			],
		},
	)
	assert result["latest_state"]["status"] == "failed"
	assert result["validation_dispatches"] == []
	assert result["mock_revalidate_events_get_calls"] == 1
	assert result.get("mock_revalidate_events_append_calls", 0) == 0
	assert "ai:validation-failed" in result["tracking_labels"]
	assert "ai:harness-broken" in result["tracking_labels"]
	tracking_bodies = [comment.get("body", "") for comment in result["issues"]["192"]["comments"]]
	assert any(body.startswith("<!-- revalidate-dedup:8:abcdef1234 -->") for body in tracking_bodies)
	assert any("Already processed /revalidate from @octocat at 2026-05-23T16:16:30Z." in body for body in tracking_bodies)


def test_revalidate_allows_same_actor_after_integration_sha_changes():
	state = _base_state(status="failed")
	state["integration_branch"] = "orchestrator/project-192"
	state["validation_failure_reason"] = "Harness failed to boot"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed", "ai:harness-broken"],
		tracking_comments=[
			{
				"id": 9,
				"body": "/revalidate retry after new push",
				"user": {"login": "octocat"},
				"created_at": "2026-05-23T16:20:00Z",
				"html_url": "https://github.com/owner/repo/issues/192#issuecomment-9",
			}
		],
		existing_branches=["main", "orchestrator/project-192"],
		branch_ref_shas={"orchestrator/project-192": "fedcba9876"},
		mock_revalidate_events_payload={
			"schema_version": "v1",
			"repository": "owner/repo",
			"tracking_issue_number": 192,
			"integration_sha": "abcdef1234",
			"entries": [
				{
					"actor": "octocat",
					"timestamp_utc": "2026-05-23T16:16:30Z",
					"prior_outcome": "harness_error",
				}
			],
		},
	)
	assert result["latest_state"]["status"] == "validating"
	assert len(result["validation_dispatches"]) == 1
	assert result["mock_revalidate_events_get_calls"] == 1
	assert result["mock_revalidate_events_append_calls"] == 1
	stored_events = result["mock_revalidate_events_payload"]
	assert stored_events["integration_sha"] == "fedcba9876"
	assert stored_events["entries"][0]["reason"] == "retry after new push"


def test_revalidate_not_blocked_by_prose_marker_comment_after_command():
	state = _base_state(status="failed")
	state["validation_failure_reason"] = "Exceeded cycles"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=[
			"/revalidate",
			"Operator note: I reviewed the ORCHESTRATOR_STATE_V2 framing above.",
		],
	)
	ls = result["latest_state"]
	assert ls["status"] == "validating", f"Expected status=validating, got {ls['status']}"
	assert len(result["validation_dispatches"]) == 1


def test_revalidate_not_blocked_by_torn_v2_chunk_after_command():
	state = _base_state(status="failed")
	state["validation_failure_reason"] = "Exceeded cycles"
	payload = json.dumps(state)
	encoded_len = len(base64.b64encode(payload.encode("utf-8")))
	partial_chain = _build_v2_state_comment_chain(payload, chunk_size=max(1, encoded_len // 2))
	assert len(partial_chain) > 1
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=["/revalidate", partial_chain[0]["body"]],
	)
	ls = result["latest_state"]
	assert ls["status"] == "validating", f"Expected status=validating, got {ls['status']}"
	assert len(result["validation_dispatches"]) == 1


def test_v2_extract_accepts_older_complete_chain_when_newer_same_manifest_uses_different_total():
	state = _base_state(status="in_progress")
	payload = json.dumps(state)
	encoded_len = len(base64.b64encode(payload.encode("utf-8")))
	older_complete = _build_v2_state_comment_chain(payload, chunk_size=encoded_len + 1)
	newer_partial = _build_v2_state_comment_chain(
		payload,
		chunk_size=max(1, encoded_len // 2),
	)[:1]
	comments = older_complete + newer_partial

	with tempfile.TemporaryDirectory() as td:
		comments_json = Path(td) / "comments.json"
		comments_json.write_text(json.dumps(comments), encoding="utf-8")
		proc = subprocess.run(
			[
				"python3",
				str(REPO_ROOT / "scripts" / "orchestrate_state_v2.py"),
				"extract",
				"--comments-json",
				str(comments_json),
			],
			capture_output=True,
			text=True,
			timeout=30,
		)

	assert proc.returncode == 0, proc.stderr
	assert json.loads(proc.stdout) == state
	assert _extract_latest_state(comments) == state


def test_v2_extract_accepts_older_complete_chain_when_newer_same_manifest_same_total_uses_different_chunking():
	state = _base_state(status="in_progress")
	payload = json.dumps(state)
	encoded_len = len(base64.b64encode(payload.encode("utf-8")))
	older_chunk_size = (encoded_len // 2) + 1
	newer_chunk_size = encoded_len - 1
	assert max(1, (encoded_len + older_chunk_size - 1) // older_chunk_size) == 2
	assert max(1, (encoded_len + newer_chunk_size - 1) // newer_chunk_size) == 2
	older_complete = _build_v2_state_comment_chain(payload, chunk_size=older_chunk_size)
	newer_partial = _build_v2_state_comment_chain(payload, chunk_size=newer_chunk_size)[:1]
	comments = older_complete + newer_partial

	with tempfile.TemporaryDirectory() as td:
		comments_json = Path(td) / "comments.json"
		comments_json.write_text(json.dumps(comments), encoding="utf-8")
		proc = subprocess.run(
			[
				"python3",
				str(REPO_ROOT / "scripts" / "orchestrate_state_v2.py"),
				"extract",
				"--comments-json",
				str(comments_json),
			],
			capture_output=True,
			text=True,
			timeout=30,
		)

	assert proc.returncode == 0, proc.stderr
	assert json.loads(proc.stdout) == state
	assert _extract_latest_state(comments) == state


def test_v2_extract_helper_matches_production_for_interleaved_older_complete_and_newer_prefix_same_total():
	state = _base_state(status="in_progress")
	payload = json.dumps(state)
	encoded_len = len(base64.b64encode(payload.encode("utf-8")))
	chunk_size = max(1, encoded_len // 4)
	older_complete = _build_v2_state_comment_chain(payload, chunk_size=chunk_size)
	assert len(older_complete) >= 4
	newer_prefix = _build_v2_state_comment_chain(payload, chunk_size=chunk_size)[:1]
	comments = older_complete[:-1] + newer_prefix + older_complete[-1:]

	with tempfile.TemporaryDirectory() as td:
		comments_json = Path(td) / "comments.json"
		comments_json.write_text(json.dumps(comments), encoding="utf-8")
		proc = subprocess.run(
			[
				"python3",
				str(REPO_ROOT / "scripts" / "orchestrate_state_v2.py"),
				"extract",
				"--comments-json",
				str(comments_json),
			],
			capture_output=True,
			text=True,
			timeout=30,
		)

	assert proc.returncode == 0, proc.stderr
	assert json.loads(proc.stdout) == state
	assert _extract_latest_state(comments) == state


def test_revalidate_ignored_when_no_comment():
	"""Without a /revalidate comment, a validation-failed project stays skipped."""
	state = _base_state(status="failed")
	state["validation_failure_reason"] = "Some failure"
	stale_completion_comment = (
		"<!-- orchestrator:completion-status -->\n"
		"<!-- status:in-progress -->\n"
		"## Completion status\n\n"
		"**State:** `in-progress`\n\n"
		"Waiting on wave merges.\n"
	)
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=[stale_completion_comment],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed", f"Expected status=failed, got {ls['status']}"
	assert "ai:validation-failed" in result["tracking_labels"]
	assert result["validation_dispatches"] == []
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	completion_comment = next(body for body in tracking_bodies if "<!-- orchestrator:completion-status -->" in body)
	assert "<!-- status:failed -->" in completion_comment
	assert "terminal `failed` state" in completion_comment


def test_revalidate_with_extra_text_after_command():
	"""A /revalidate comment with additional text (reason) should still trigger."""
	state = _base_state(status="failed")
	state["validation_failure_reason"] = "Exceeded cycles"
	state["validation_recovery_count"] = 1
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=["/revalidate fixed the Docker config manually"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "validating", f"Expected status=validating, got {ls['status']}"
	assert ls["validation_cycle"] == 1
	assert ls["validation_recovery_count"] == 0
	assert len(result["validation_dispatches"]) == 1


def test_revalidate_not_triggered_for_non_validation_failure():
	"""A project in failed state without ai:validation-failed label should not
	be affected by /revalidate (e.g. judge-level failure)."""
	state = _base_state(status="failed")
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:closed"],
		tracking_comments=["/revalidate"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed", f"Expected status=failed, got {ls['status']}"
	assert result["validation_dispatches"] == []


# ---------------------------------------------------------------------------
# Tests: /judge_resume reset controls for non-validation failed projects
# ---------------------------------------------------------------------------


def test_judge_resume_plain_preserves_counters():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 7
	state["recovery_count"] = 3
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		tracking_comments=["/judge_resume"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "in_progress"
	assert ls["judge_stall_cycles"] == 7
	assert ls["recovery_count"] == 3
	assert "judge_stall_cycles: preserved (7); recovery_count: preserved (3)" in result["stdout"]
	tracking_comments = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any(
		"Counter handling: judge_stall_cycles: preserved (7); recovery_count: preserved (3)" in body
		for body in tracking_comments
	)


def test_judge_resume_not_blocked_by_prose_marker_comment_after_command():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 9
	state["recovery_count"] = 6
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		tracking_comments=[
			"/judge_resume --force",
			"FYI: the ORCHESTRATOR_STATE_V1 marker above came from the previous run.",
		],
	)
	ls = result["latest_state"]
	assert ls["status"] == "in_progress"
	assert ls["judge_stall_cycles"] == 0
	assert ls["recovery_count"] == 0


def test_judge_resume_reset_recovery_only():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 8
	state["recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		tracking_comments=["/judge_resume --reset-recovery"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "in_progress"
	assert ls["judge_stall_cycles"] == 8
	assert ls["recovery_count"] == 0
	assert "judge_stall_cycles: preserved (8); recovery_count: reset (2 -> 0)" in result["stdout"]


def test_judge_resume_reset_stall_only():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 4
	state["recovery_count"] = 5
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		tracking_comments=["/judge_resume --reset-stall"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "in_progress"
	assert ls["judge_stall_cycles"] == 0
	assert ls["recovery_count"] == 5
	assert "judge_stall_cycles: reset (4 -> 0); recovery_count: preserved (5)" in result["stdout"]


def test_judge_resume_force_resets_both_counters():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 9
	state["recovery_count"] = 6
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		tracking_comments=["/judge_resume --force"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "in_progress"
	assert ls["judge_stall_cycles"] == 0
	assert ls["recovery_count"] == 0
	assert "judge_stall_cycles: reset (9 -> 0); recovery_count: reset (6 -> 0)" in result["stdout"]


def test_judge_resume_unknown_flags_preserve_counters():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 5
	state["recovery_count"] = 4
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		tracking_comments=["/judge_resume --unknown-flag extra words"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "in_progress"
	assert ls["judge_stall_cycles"] == 5
	assert ls["recovery_count"] == 4
	assert "judge_stall_cycles: preserved (5); recovery_count: preserved (4)" in result["stdout"]


def test_judge_resume_ignored_for_validation_failed_project():
	state = _base_state(status="failed")
	state["judge_stall_cycles"] = 10
	state["recovery_count"] = 3
	state["validation_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
		tracking_comments=["/judge_resume --force"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed"
	assert ls["judge_stall_cycles"] == 10
	assert ls["recovery_count"] == 3
	assert ls["validation_recovery_count"] == 2


def test_missing_labels_closed_issue_healed_to_terminal_without_retrigger():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		issue_closed={10: True},
	)
	issue_status = result["latest_state"]["waves"][0]["issues"][0]["status"]
	assert issue_status == "closed"
	assert "ai:closed" in result["issues"]["10"]["labels"]
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert not any("/reclarify" in body or "/approved" in body for body in issue_comments)


def test_stale_pending_terminal_truth_from_merged_pr_is_auto_corrected():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "pending"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		issue_linked_prs={10: 900},
		prs=[
			{
				"number": 900,
				"state": "closed",
				"merged": True,
				"baseRefName": "main",
				"headRefName": "ai/issue-10",
				"mergeable": None,
				"mergeable_state": "unknown",
			},
		],
	)
	issue_status = result["latest_state"]["waves"][0]["issues"][0]["status"]
	assert issue_status == "merged"
	assert "ai:merged" in result["issues"]["10"]["labels"]


def test_forced_terminal_merged_phase_repair_removes_single_existing_phase_label():
	"""Regression: forcing ai:merged must evict an existing single phase label."""
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 900},
		prs=[
			{
				"number": 900,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-04-12T08:00:00Z",
				"baseRefName": "main",
				"headRefName": "ai/issue-10",
				"mergeable": None,
				"mergeable_state": "unknown",
			},
		],
	)
	final_labels = result["issues"]["10"]["labels"]
	assert final_labels == ["ai:merged"]


def test_conflicting_phase_labels_are_repaired_to_single_phase():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:planning", "ai:implementing"]},
	)
	final_labels = result["issues"]["10"]["labels"]
	assert "ai:implementing" in final_labels
	assert "ai:planning" not in final_labels


def test_contract_helper_guard_in_poller_tests():
	contract_path = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
	helper_path = REPO_ROOT / "scripts" / "label_helpers.sh"
	contract = json.loads(contract_path.read_text(encoding="utf-8"))
	helper_text = helper_path.read_text(encoding="utf-8")
	match = re.search(r"declare -A _AI_LABEL_COLORS=\((.*?)\n\)", helper_text, flags=re.S)
	assert match, "Could not parse label helper catalog"
	helper_labels = set(re.findall(r'\["([^"]+)"\]=', match.group(1)))
	assert helper_labels == set(contract["labels"].keys())



def test_stall_judge_resolve_merge_conflict_dispatches_review_and_increments_once():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 77},
		prs=[
			{
				"number": 77,
				"state": "open",
				"mergeable": False,
				"headRefName": "feature/stall-judge",
				"headRefFromApi": "feature/stall-judge",
				"headSha": "deadbeef",
				"baseRefName": "main",
			},
		],
		mock_stall_judge_json={
			"action": "resolve_merge_conflict",
			"justification": "conflicted",
			"target_pr": 77,
			"head_ref": "feature/stall-judge",
		},
	)
	assert 77 in result.get("update_branch_calls", [])
	assert result.get("review_dispatches", []), result
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3



def test_stall_judge_escalate_human_adds_needs_human_label_and_increments_counter():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:implementing"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		enable_stall_human_terminalization="true",
		mock_stall_judge_json={
			"action": "escalate_human",
			"justification": "needs operator",
			"target_pr": None,
			"head_ref": None,
		},
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3
	assert "ai:needs-human" in result["issues"]["10"]["labels"]


def test_stall_judge_escalate_human_is_downgraded_when_human_terminalization_disabled():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:implementing"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		enable_stall_human_terminalization="false",
		mock_stall_judge_json={
			"action": "escalate_human",
			"justification": "needs operator",
			"target_pr": None,
			"head_ref": None,
		},
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3
	assert "ai:needs-human" not in result["issues"]["10"]["labels"]
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	tracking_comments = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("/approved" in body for body in issue_comments)
	assert any("**Decision (judge):** escalate_human" in body and "**Decision (effective):** retrigger_implement" in body for body in tracking_comments)


def test_stall_judge_escalate_human_allowed_when_human_terminalization_enabled():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:implementing"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		enable_stall_human_terminalization="true",
		mock_stall_judge_json={
			"action": "escalate_human",
			"justification": "needs operator",
			"target_pr": None,
			"head_ref": None,
		},
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3
	assert "ai:needs-human" in result["issues"]["10"]["labels"]
	tracking_comments = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("**Decision (judge):** escalate_human" in body and "**Decision (effective):** escalate_human" in body for body in tracking_comments)


def test_stall_judge_escalate_human_issue_not_redetected_after_needs_human():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:implementing"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing", "ai:needs-human"]},
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	tracking_comments = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert not any("/approved" in body or "/reclarify" in body for body in issue_comments)
	assert not any("Stall Judge — Issue #10" in body for body in tracking_comments)



def test_retrigger_review_redispatches_when_last_autofix_concluded_failure():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 77},
		prs=[
			{
				"number": 77,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": "claude/retrigger-review-failed-autofix",
				"headRefFromApi": "claude/retrigger-review-failed-autofix",
				"headSha": "sha77",
				"baseRefName": "main",
			},
		],
		active_autofix_runs=[
			{
				"workflow": "review_autofix.yml",
				"branch": "claude/retrigger-review-failed-autofix",
				"status": "completed",
				"conclusion": "failure",
			},
		],
		mock_git_push_success=True,
	)
	dispatches_for_pr = [d for d in result.get("review_dispatches", []) if str(d.get("pr_number")) == "77"]
	assert dispatches_for_pr, (
		f"expected review_autofix redispatch for PR 77 after last run concluded failure; "
		f"got: {result.get('review_dispatches')}"
	)


def test_retrigger_review_keeps_empty_commit_path_when_no_prior_autofix_failure():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 78},
		prs=[
			{
				"number": 78,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": "claude/retrigger-review-no-failed-autofix",
				"headRefFromApi": "claude/retrigger-review-no-failed-autofix",
				"headSha": "sha78",
				"baseRefName": "main",
			},
		],
		mock_git_push_success=True,
	)
	dispatches_for_pr = [d for d in result.get("review_dispatches", []) if str(d.get("pr_number")) == "78"]
	assert dispatches_for_pr == [], (
		f"expected no review_autofix redispatch when no prior failed autofix run exists; "
		f"got: {dispatches_for_pr}"
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1


def test_retrigger_review_does_not_increment_when_redispatch_skipped():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 79},
		prs=[
			{
				"number": 79,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": "claude/retrigger-review-active-autofix",
				"headRefFromApi": "claude/retrigger-review-active-autofix",
				"headSha": "sha79",
				"baseRefName": "main",
			},
		],
		active_autofix_runs=[
			{
				"workflow": "review_autofix.yml",
				"branch": "claude/retrigger-review-active-autofix",
				"status": "queued",
				"conclusion": "",
			},
			{
				"workflow": "review_autofix.yml",
				"branch": "claude/retrigger-review-active-autofix",
				"status": "completed",
				"conclusion": "failure",
			},
		],
		mock_git_push_success=True,
	)
	dispatches_for_pr = [d for d in result.get("review_dispatches", []) if str(d.get("pr_number")) == "79"]
	assert dispatches_for_pr == [], (
		f"expected no redispatch when _dispatch_review_for_conflicts returns rc=2; got: {dispatches_for_pr}"
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0


def test_retrigger_review_skips_empty_commit_when_review_run_inflight():
	# When a fresh review_autofix run is already in_progress on the PR's
	# head branch, retrigger_review must NOT push an empty commit — that
	# push would flip the in-flight run's stale-base gate
	# (check_external_branch_advance.sh classifies the orchestrator's
	# commit subject as ADVANCE=external because it doesn't match the
	# [ai-autofix] / [ai-merge-resolve] prefixes), causing the editor
	# commit and downstream push/mark-ready steps to be skipped, which
	# is exactly the 6h cycle observed on shubhodeep1/tele-funtoken-msg-scoring#3057.
	# Recovery counter must NOT be incremented (no work was done).
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "claude/retrigger-review-inflight-run"
	# run_started_at uses a far-future sentinel so the run is guaranteed
	# to stay under the STALL_THRESHOLD_MINUTES zombie cutoff.
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 80},
		prs=[
			{
				"number": 80,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha80",
				"baseRefName": "main",
			},
		],
		actions_runs_workflow_runs=[
			{
				"id": 26088864015,
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"status": "in_progress",
				"head_branch": head_ref,
				"run_started_at": "2999-01-01T00:00:00Z",
			},
		],
		mock_git_push_success=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0, (
		f"expected stall_recovery_count to stay at 0 when an in-flight "
		f"review_autofix run blocks the empty-commit push; "
		f"got {issue_entry['stall_recovery_count']}"
	)
	dispatches_for_pr = [d for d in result.get("review_dispatches", []) if str(d.get("pr_number")) == "80"]
	assert dispatches_for_pr == [], (
		f"expected no redispatch when in-flight run blocks recovery; "
		f"got: {dispatches_for_pr}"
	)


def test_retrigger_review_skips_empty_commit_when_review_run_has_blank_head_branch_but_matching_sha():
	# workflow_dispatch review runs can report a blank/null head_branch in
	# /actions/runs even though they still target the PR head SHA. Those
	# runs must still block the empty-commit push; otherwise the new guard
	# misses the same head_branch=null case called out in the PR summary.
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "claude/retrigger-review-blank-branch"
	head_sha = "a" * 40
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 84},
		prs=[
			{
				"number": 84,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": head_sha,
				"baseRefName": "main",
			},
		],
		actions_runs_workflow_runs=[
			{
				"id": 26088864016,
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"status": "in_progress",
				"head_branch": "",
				"head_sha": head_sha,
				"run_started_at": "2999-01-01T00:00:00Z",
			},
		],
		mock_git_push_success=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0, (
		f"expected blank-head_branch review run with matching head_sha to "
		f"block the empty-commit push; got stall_recovery_count="
		f"{issue_entry['stall_recovery_count']}"
	)
	assert result.get("git_push_calls", []) == [], (
		f"expected no empty-commit push when a blank-head_branch run matches "
		f"the PR head_sha; got push calls {result.get('git_push_calls', [])}"
	)


def test_retrigger_review_ignores_inflight_run_on_unrelated_branch():
	# Defense-in-depth: an in-flight review run on a DIFFERENT branch
	# must NOT block the empty-commit push for this PR.  Without the
	# head_branch filter, the guard would deadlock recovery whenever
	# any other PR has a live review.
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "claude/retrigger-review-this-branch"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 81},
		prs=[
			{
				"number": 81,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha81",
				"baseRefName": "main",
			},
		],
		actions_runs_workflow_runs=[
			{
				"id": 99999999,
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"status": "in_progress",
				"head_branch": "claude/some-other-branch",
				"run_started_at": "2999-01-01T00:00:00Z",
			},
			{
				"id": 99999998,
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"status": "queued",
				"head_branch": "",
				"head_sha": "b" * 40,
				"created_at": "2999-01-01T00:00:00Z",
			},
		],
		mock_git_push_success=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1, (
		f"expected empty-commit push to proceed when in-flight run is on a "
		f"different branch; got stall_recovery_count="
		f"{issue_entry['stall_recovery_count']}"
	)


def test_retrigger_review_inflight_guard_treats_zombie_run_as_eligible():
	# A run older than STALL_THRESHOLD_MINUTES is a zombie and must NOT
	# block recovery — otherwise a stuck Actions runner could deadlock
	# the autofix loop forever.  Mirrors the zombie cutoff in
	# build_active_issue_set (line 4944-4954).
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "claude/retrigger-review-zombie-run"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 82},
		prs=[
			{
				"number": 82,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha82",
				"baseRefName": "main",
			},
		],
		actions_runs_workflow_runs=[
			{
				"id": 11111111,
				"name": "Review Autofix",
				"path": ".github/workflows/review_autofix.yml",
				"status": "in_progress",
				"head_branch": head_ref,
				# Far in the past — past any reasonable STALL_THRESHOLD_MINUTES.
				"run_started_at": "1970-01-02T00:00:00Z",
			},
		],
		mock_git_push_success=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1, (
		f"expected empty-commit push to proceed past zombie in-flight run; "
		f"got stall_recovery_count={issue_entry['stall_recovery_count']}"
	)


def test_retrigger_review_skips_empty_commit_for_review_run_past_stall_threshold_but_within_budget():
	# Regression for the PR #3082 / issue #3081 "stuck 169m, attempt 2" loop:
	# a review_autofix run that has been in_progress longer than
	# STALL_THRESHOLD_MINUTES (120) but is still within its legitimate budget
	# (REVIEW_RUN_MAX_RUNTIME_MINUTES, default 250 ≈ the 240-min codex-agent
	# job timeout) is STILL EDITING and must NOT be clobbered by an empty
	# commit.  Before the review-aware window it was misclassified as a zombie
	# (>120m), dropped from the active set, and re-triggered — discarding the
	# whole review pass via AUTOFIX_PRE_EDITOR_STALE_BASE -> soft_exit.
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "ai/issue-3081"
	# 169 minutes ago: past the 120m stall threshold, inside the 250m review
	# window — exactly the window where the old code re-triggered destructively.
	started_169m_ago = time.strftime(
		"%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 169 * 60)
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 3082},
		prs=[
			{
				"number": 3082,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha3082",
				"baseRefName": "main",
			},
		],
		actions_runs_workflow_runs=[
			{
				"id": 26944643043,
				"name": "Internal: AI Review & Autofix",
				"path": ".github/workflows/internal-review.yml",
				"status": "in_progress",
				"head_branch": head_ref,
				"run_started_at": started_169m_ago,
			},
		],
		mock_git_push_success=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0, (
		f"expected stall_recovery_count to stay at 0 when a review run past the "
		f"stall threshold but within its budget blocks the empty-commit push; "
		f"got {issue_entry['stall_recovery_count']}"
	)
	dispatches_for_pr = [d for d in result.get("review_dispatches", []) if str(d.get("pr_number")) == "3082"]
	assert dispatches_for_pr == [], (
		f"expected no redispatch when an in-budget review run blocks recovery; "
		f"got: {dispatches_for_pr}"
	)


def test_retrigger_review_skips_empty_commit_when_pr_head_advanced_after_snapshot():
	# Defense-in-depth for the narrower PR-fetch→git-fetch race: if the
	# PR head SHA changed after `_fetch_pr_json` captured `.head.sha`, the
	# empty-commit retrigger should bail out instead of racing newer work.
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "claude/retrigger-review-advanced-head"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 83},
		prs=[
			{
				"number": 83,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "0" * 40,
				"baseRefName": "main",
			},
		],
		mock_git_push_success=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0, (
		f"expected stale PR head snapshot to leave stall_recovery_count at 0; "
		f"got {issue_entry['stall_recovery_count']}"
	)
	assert result.get("git_push_calls", []) == [], (
		f"expected no empty-commit push when the PR head advanced; "
		f"got push calls {result.get('git_push_calls', [])}"
	)


def test_retrigger_review_does_not_increment_when_empty_commit_checkout_fails():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:done"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	head_ref = "claude/retrigger-review-checkout-fails"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:done"]},
		issue_linked_prs={10: 85},
		prs=[
			{
				"number": 85,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": head_ref,
				"headRefFromApi": head_ref,
				"headSha": "sha85",
				"baseRefName": "main",
			},
		],
		mock_git_push_success=True,
		mock_git_checkout_fail=True,
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0, (
		f"expected checkout failure to leave stall_recovery_count at 0; "
		f"got {issue_entry['stall_recovery_count']}"
	)
	assert f"Issue #10 PR #85 checkout origin/{head_ref} failed after fetch; skipping empty-commit push." in (
		result["stdout"] + result["stderr"]
	)
	assert result.get("git_push_calls", []) == [], (
		f"expected checkout failure to skip the empty-commit push; "
		f"got push calls {result.get('git_push_calls', [])}"
	)


def test_stall_judge_unknown_action_falls_back_to_declarative_recovery():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:awaiting-approval"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:awaiting-approval"]},
		mock_stall_judge_json={
			"action": "nonsense",
			"justification": "bad output",
			"target_pr": None,
			"head_ref": None,
		},
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	tracking_comments = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Stall Judge — Issue #10" in body for body in tracking_comments)
	# With human terminalization disabled by default, fallback stays autonomous
	# and downgrades the terminal human escalation to auto_approve.
	assert "ai:needs-human" not in result["issues"]["10"]["labels"]
	assert any("/approved" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3


def test_stall_judge_unknown_action_falls_back_to_human_when_gate_enabled():
	state = _base_state(status="in_progress")
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["last_seen_phase"] = "ai:awaiting-approval"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 2
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		enable_stall_human_terminalization="true",
		issue_labels={10: ["ai:awaiting-approval"]},
		mock_stall_judge_json={
			"action": "nonsense",
			"justification": "bad output",
			"target_pr": None,
			"head_ref": None,
		},
	)
	assert "ai:needs-human" in result["issues"]["10"]["labels"]
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert not any("/approved" in body for body in issue_comments)



def test_no_labels_open_issue_uses_bounded_recovery_policy():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert any("/reclarify" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1
	assert issue_entry["status"] == "in_progress"


def test_no_labels_with_open_linked_pr_skips_retrigger_pipeline():
	# Regression for #923: when an orchestrator-managed issue ends up with
	# empty labels but already has an open linked PR, the stall detector must
	# NOT fire /reclarify (retrigger_pipeline) — that action assumes the
	# issue never entered the pipeline, which is incorrect here.
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		issue_linked_prs={10: 930},
		prs=[{
			"number": 930,
			"state": "open",
			"baseRefName": "main",
			"headRefName": "ai/issue-10",
			"mergeable": True,
			"mergeable_state": "clean",
		}],
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert not any("/reclarify" in body for body in issue_comments)
	assert not any("/answer" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	# Counter must NOT increment when the guard skips the action.
	assert issue_entry["stall_recovery_count"] == 0
	assert issue_entry["status"] == "in_progress"


def test_no_labels_with_cross_repo_linked_pr_still_retriggers_pipeline():
	# Same-repo linked-PR filtering must ignore cross-repo timeline refs so a
	# foreign PR cannot suppress repo-local early-phase recovery.
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		issue_linked_prs={10: 936},
		prs=[{
			"number": 936,
			"state": "open",
			"repository": "other-owner/other-repo",
			"baseRefName": "main",
			"headRefName": "ai/issue-10",
			"mergeable": True,
			"mergeable_state": "clean",
		}],
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert any("/reclarify" in body for body in issue_comments)
	assert not any("/answer" in body for body in issue_comments)
	assert "STALL_SKIP issue=10 reason=open_linked_pr pr=936" not in result["stdout"]
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1
	assert issue_entry["status"] == "in_progress"


def test_no_labels_with_rest_fallback_closing_linked_pr_skips_retrigger_pipeline():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		gql_mode="error",
		issue_labels={10: []},
		issue_linked_prs={10: 932},
		prs=[{
			"number": 932,
			"state": "open",
			"body": "Fixes #10",
			"baseRefName": "main",
			"headRefName": "ai/issue-10",
			"mergeable": True,
			"mergeable_state": "clean",
		}],
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert not any("/reclarify" in body for body in issue_comments)
	assert not any("/answer" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 0
	assert issue_entry["status"] == "in_progress"


def test_no_labels_with_rest_fallback_keyword_substring_still_retriggers_pipeline():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		gql_mode="error",
		issue_labels={10: []},
		issue_linked_prs={10: 933},
		prs=[{
			"number": 933,
			"state": "open",
			"body": "We need to prefix #10 before rollout",
			"baseRefName": "main",
			"headRefName": "infra/fix-933",
			"mergeable": True,
			"mergeable_state": "clean",
		}],
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert any("/reclarify" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1
	assert issue_entry["status"] == "in_progress"


def test_no_labels_with_rest_fallback_colon_form_still_retriggers_pipeline():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		gql_mode="error",
		issue_labels={10: []},
		issue_linked_prs={10: 934},
		prs=[{
			"number": 934,
			"state": "open",
			"body": "Closes: #10",
			"baseRefName": "main",
			"headRefName": "infra/fix-934",
			"mergeable": True,
			"mergeable_state": "clean",
		}],
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert any("/reclarify" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1
	assert issue_entry["status"] == "in_progress"


def test_no_labels_with_refs_only_linked_pr_still_retriggers_pipeline():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "no_labels"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: []},
		issue_linked_prs={10: 931},
		prs=[{
			"number": 931,
			"state": "open",
			"body": "Refs #10",
			"willCloseTarget": False,
			"baseRefName": "main",
			"headRefName": "infra/fix-931",
			"mergeable": True,
			"mergeable_state": "clean",
		}],
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert any("/reclarify" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 1
	assert issue_entry["status"] == "in_progress"


def test_managed_stall_recovery_skips_needs_human_phase():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["last_seen_phase"] = "ai:needs-human"
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 2

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:planning", "ai:needs-human"]},
	)

	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 2
	assert "ai:needs-human" in result["issues"]["10"]["labels"]
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert not any("Stall recovery" in body or "/approved" in body or "/answer" in body for body in issue_comments)


def test_standalone_stall_recovery_skips_needs_human_candidates():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:needs-human"]},
	)

	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert not any("Standalone stall recovery" in body for body in issue_comments)
	assert "ai:needs-human" in result["issues"]["10"]["labels"]
	assert result["issues"]["10"].get("closed", False) is False


def test_standalone_stall_recovery_uses_shared_gate_aware_action_resolver():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	function_match = re.search(
		r'(^|\n)run_standalone_stall_recovery\(\) \{\n(?P<body>.*?)(?=^\w+\(\) \{|\Z)',
		script,
		re.MULTILINE | re.DOTALL,
	)
	assert function_match is not None
	function_body = function_match.group("body")
	assert 'action="$(recovery_action_for_phase "${phase}" "${recovery_count}")"' in function_body
	assert 'ENABLE_STALL_HUMAN_TERMINALIZATION' in script


def test_state_extraction_with_special_chars_in_comment_bodies():
	"""State extraction succeeds when surrounding comments have backticks/quotes/markdown."""
	state = _base_state(status="in_progress")
	# Add comments with bodies that contain backticks, quotes, and multiline markdown.
	# These should not confuse the jq state-extraction filter.
	tricky_comments = [
		"## Some `code` block\n\n```python\nprint('hello \"world\"')\n```",
		'Issue body with "double quotes" and `backticks` and\nmultiline\ncontent',
		"```json\n{\"key\": \"value with <!-- comment --> markers\"}\n```",
	]
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=tricky_comments,
		issue_labels={10: ["ai:implementing"]},
	)
	# State should still be extracted and the poller should run normally.
	final_state = result["latest_state"]
	assert final_state["schema_version"] == "orchestrate_state.v1"
	assert final_state["status"] == "in_progress"


def test_malformed_latest_state_falls_back_to_older_valid_and_posts_healed_state():
	state = _base_state(status="in_progress")
	malformed_latest = '<!-- ORCHESTRATOR_STATE_V1\n{"schema_version":"orchestrate_state.v1",\nORCHESTRATOR_STATE_V1 -->'
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=[malformed_latest],
		issue_labels={10: ["ai:implementing"]},
	)
	assert "restored from older valid state and posted healed canonical state" in result["stdout"]
	assert result["latest_state"]["schema_version"] == "orchestrate_state.v1"
	assert result["latest_state"]["status"] == "in_progress"
	state_payloads = _extract_state_payloads(result["issues"]["192"]["comments"])
	valid_payloads = []
	for payload in state_payloads:
		try:
			valid_payloads.append(json.loads(payload))
		except json.JSONDecodeError:
			continue
	assert any(payload.get("schema_version") == "orchestrate_state.v1" for payload in valid_payloads)
	comments = result["issues"]["192"]["comments"]
	malformed_idx = next(i for i, c in enumerate(comments) if c.get("body") == malformed_latest)
	following_payloads = _extract_state_payloads(comments[malformed_idx + 1 :])
	assert following_payloads
	following_valid_payloads = []
	for raw_payload in following_payloads:
		try:
			following_valid_payloads.append(json.loads(raw_payload))
		except json.JSONDecodeError:
			continue
	assert any(payload.get("schema_version") == "orchestrate_state.v1" for payload in following_valid_payloads)


def test_state_comment_pack_manifest_count_mismatch_skips_partial_v2_write():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		mock_orch_state_v2_pack_mode="count_mismatch",
	)
	state_comment_bodies = [
		c.get("body", "")
		for c in result["issues"]["192"]["comments"]
		if _is_state_comment(c.get("body", ""))
	]
	assert not any("ORCHESTRATOR_STATE_V2" in body for body in state_comment_bodies), (
		"a pack manifest whose files count does not match its declared total must not post a partial "
		f"V2 state chain. Saw state comments={state_comment_bodies!r}"
	)
	assert "pack returned 1 chunk file(s) but declared total=2" in result["stderr"]


def test_all_invalid_state_comments_trigger_reconstruction_path_without_heal():
	invalid_state = {"schema_version": "orchestrate_state.v1"}
	malformed_latest = '<!-- ORCHESTRATOR_STATE_V1\n{"schema_version":"orchestrate_state.v1",\nORCHESTRATOR_STATE_V1 -->'
	result = _run_poller(
		state=invalid_state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=[malformed_latest],
		issue_labels={10: ["ai:implementing"]},
	)
	assert "No valid ORCHESTRATOR_STATE_V1 comment found for tracking issue #192. Attempting state reconstruction..." in result["stdout"]
	assert "restored from older valid state and posted healed canonical state" not in result["stdout"]
	assert "State reconstructed and posted for tracking issue #192." in result["stdout"]
	assert result["latest_state"]["schema_version"] == "orchestrate_state.v1"


def test_failed_comments_fetch_skips_state_reconstruction():
	"""A failed comments fetch must not be misread as 'state missing'.

	Regression for the project #3627 incident (poll run 29539323907): the
	tracking issue had 665 comments, the paginated
	``gh api --paginate .../issues/3627/comments`` fetch exhausted its retries
	("gh command failed after 5 attempts"), and the poller treated the
	resulting empty COMMENTS as "No state found ... Attempting state
	reconstruction".  Reconstruction reset current_wave to 1 and — with the
	child-issue search also returning an empty map — re-created the
	already-completed wave-1 issue as duplicates (#3674, then #3676 on the
	next cycle).

	When COMMENTS_FETCH_OK != "true" the poller must instead skip
	reconstruction this cycle and retry on the next poll, matching how the
	rest of the file treats an unreadable comments fetch as "fail open".
	"""
	rewindable_body = (
		"## Project: Demo project\n\n"
		"**Integration branch:** `orchestrator/project-192`\n\n"
		"### Wave 1\n\n"
		"- [x] **phase-a-done**: Phase A — already completed (priority 1)\n\n"
		"### Wave 2\n\n"
		"- [ ] **phase-b-next**: Phase B — not started (priority 2)\n"
	)
	result = _run_poller(
		state={"schema_version": "orchestrate_state.v1"},
		enable_validation="false",
		max_validate_cycles="3",
		tracking_body=rewindable_body,
		# Force the tracking-issue comments GET to fail on the first call,
		# reproducing the exhausted-retries fetch from run 29539323907.
		fail_issue_comment_get_after={192: 0},
	)
	assert (
		"Comments fetch failed for tracking issue #192; cannot confirm "
		"orchestrator state is missing" in result["stdout"]
	), result["stdout"]
	# The destructive reconstruction path must NOT run when the fetch failed.
	assert "Attempting state reconstruction" not in result["stdout"], result["stdout"]
	assert (
		"State reconstructed and posted for tracking issue #192."
		not in result["stdout"]
	), result["stdout"]
	# And no duplicate wave-1 issue may be spawned for already-done work.
	assert "Deferred issue creation for wave" not in result["stdout"], result["stdout"]


def test_reconstruction_refused_when_body_has_completed_unmapped_issue():
	"""Defense-in-depth for #3627: refuse a from-scratch rebuild that would
	duplicate finished work.

	Even when the comments fetch succeeds but carries no valid state comment,
	the poller must not rebuild a project whose tracking body shows completed
	([x] or [X]) sub-issues the child-issue search cannot map — a from-scratch rebuild
	resets current_wave to 1 and re-creates those finished issues as
	duplicates.  rebuild_tracking_state raises ReconstructionUnsafeError, the
	helper exits non-zero, and the poller skips this cycle instead of creating
	duplicates.
	"""
	rewindable_body = (
		"## Project: Demo project\n\n"
		"**Integration branch:** `orchestrator/project-192`\n\n"
		"### Wave 1\n\n"
		"- [X] **phase-h-done**: Phase H — already merged (priority 1)\n\n"
		"### Wave 2\n\n"
		"- [ ] **phase-d-next**: Phase D — not started (priority 2)\n"
	)
	invalid_state = {"schema_version": "orchestrate_state.v1"}
	malformed_latest = '<!-- ORCHESTRATOR_STATE_V1\n{"schema_version":"orchestrate_state.v1",\nORCHESTRATOR_STATE_V1 -->'
	result = _run_poller(
		state=invalid_state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_comments=[malformed_latest],
		tracking_body=rewindable_body,
		issue_labels={10: ["ai:implementing"]},
	)
	# The rebuild is refused (and the reason is surfaced), not performed.
	assert (
		"State reconstruction failed for tracking issue #192, skipping."
		in result["stdout"]
	), result["stdout"]
	assert "refusing to reconstruct state" in result["stdout"], result["stdout"]
	assert (
		"State reconstructed and posted for tracking issue #192."
		not in result["stdout"]
	), result["stdout"]
	# No duplicate GitHub issue may be created for the already-completed work.
	assert result.get("created_issues", []) == [], result.get("created_issues")


def test_deferred_creation_adopts_existing_github_issue_instead_of_duplicating():
	"""Backstop for the project #3542 duplicate-Phase-1 failure mode.

	When the loaded state presents a current-wave issue as uncreated
	(github_issue == null) AND absent from issue_number_map — e.g. the poller
	acted on a stale/rewound snapshot whose write recording the original issue
	never durably landed — the deferred-creation path must NOT mint a duplicate
	if an issue already exists on GitHub carrying that Local ID.  It adopts the
	existing issue (here a closed/merged one, mirroring the original #3543)
	instead of creating a second one.
	"""
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"] = [
		{"id": "phase-1-backend", "github_issue": None, "status": "not_created"},
	]
	state["issue_number_map"] = {}
	state["pending_issue_defs"] = {
		"phase-1-backend": {
			"title": "Phase 1: backend",
			"body": "Implement phase 1 backend.",
			"priority": 1,
		},
	}
	existing_body = (
		"Implement phase 1 backend.\n\n---\n"
		"**Orchestrator metadata** (do not edit)\n"
		"- Tracking issue: #192\n"
		"- Local ID: `phase-1-backend`\n"
		"- Priority: 1\n"
		"- Managed by: AI Orchestrator"
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={3543: ["ai:merged"]},
		issue_closed={3543: True},
		issue_bodies={3543: existing_body},
	)
	# No duplicate issue may be minted for the already-created work.
	assert result.get("created_issues", []) == [], result.get("created_issues")
	# The existing issue is adopted into the wave entry + issue_number_map and
	# dropped from pending_issue_defs.
	ls = result["latest_state"]
	w1 = ls["waves"][0]["issues"][0]
	assert w1["github_issue"] == 3543, w1
	assert ls["issue_number_map"].get("phase-1-backend") == 3543
	assert "phase-1-backend" not in ls["pending_issue_defs"]
	# The adoption is logged and NOT announced as a fresh creation.
	assert "already exists on GitHub as #3543" in result["stdout"], result["stdout"]
	tracking_bodies = "".join(
		str(c.get("body", "")) for c in result["issues"]["192"]["comments"]
	)
	assert "Deferred Issue Creation" not in tracking_bodies


def test_deferred_creation_scopes_lookup_to_issues_and_adopts_existing_issue():
	"""Mixed issue/PR search results must not degrade the duplicate backstop.

	The real `search/issues` endpoint returns pull requests too unless the query
	adds `is:issue`. A PR-like result with `body: null` used to make the jq
	reduce fail, which the shell fallback then misread as a successful empty
	lookup and allowed duplicate creation. Scope the query to issues and keep
	the real child issue adoptable.
	"""
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"] = [
		{"id": "phase-1-backend", "github_issue": None, "status": "not_created"},
	]
	state["issue_number_map"] = {}
	state["pending_issue_defs"] = {
		"phase-1-backend": {
			"title": "Phase 1: backend",
			"body": "Implement phase 1 backend.",
			"priority": 1,
		},
	}
	existing_body = (
		"Implement phase 1 backend.\n\n---\n"
		"**Orchestrator metadata** (do not edit)\n"
		"- Tracking issue: #192\n"
		"- Local ID: `phase-1-backend`\n"
		"- Priority: 1\n"
		"- Managed by: AI Orchestrator"
	)
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		search_issue_items=[
			{"number": 9001, "title": "Integration PR", "body": None, "pull_request": {}, "state": "open"},
			{"number": 3543, "title": "Phase 1: backend", "body": existing_body, "state": "closed"},
		],
	)
	assert result.get("created_issues", []) == [], result.get("created_issues")
	assert "phase-1-backend" not in result["latest_state"]["pending_issue_defs"]
	w1 = result["latest_state"]["waves"][0]["issues"][0]
	assert w1["github_issue"] == 3543, w1


def test_deferred_creation_skips_when_existence_lookup_fails():
	"""Fail-closed: when the child-issue existence lookup itself fails this
	cycle, deferred creation must skip (and retry on the next poll) rather than
	risk a duplicate — an unavailable lookup is not evidence the issue is
	absent.
	"""
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"] = [
		{"id": "phase-1-backend", "github_issue": None, "status": "not_created"},
	]
	state["issue_number_map"] = {}
	state["pending_issue_defs"] = {
		"phase-1-backend": {
			"title": "Phase 1: backend",
			"body": "Implement phase 1 backend.",
			"priority": 1,
		},
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		fail_search_issues=True,
		# Keep gh_retry from sleeping through its backoff ladder during the test.
		env_overrides={"GH_RETRY_MAX_ATTEMPTS": "1"},
	)
	# No issue created, and the wave entry stays uncreated for a later retry.
	assert result.get("created_issues", []) == [], result.get("created_issues")
	assert "skipping creation to avoid a duplicate" in result["stdout"], result["stdout"]
	w1 = result["latest_state"]["waves"][0]["issues"][0]
	assert w1.get("github_issue") is None, w1


def test_deferred_creation_relinks_issue_number_map_entry_without_creation_comment():
	"""A zero-creation relink cycle must not claim that issues were created.

	When an uncreated wave entry already has a Local ID -> GitHub issue mapping
	in-state, the deferred-creation loop heals the wave entry from
	issue_number_map instead of minting a new issue.  That relink path is not a
	creation and must not feed the "Created them now" tracking comment or
	Telegram notification.
	"""
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"] = [
		{"id": "issue-1", "github_issue": None, "status": "not_created"},
	]
	state["pending_issue_defs"] = {}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:clarification"]},
	)
	assert result.get("created_issues", []) == [], result.get("created_issues")
	assert "already mapped to #10" in result["stdout"], result["stdout"]
	ls = result["latest_state"]
	w1 = ls["waves"][0]["issues"][0]
	assert w1["github_issue"] == 10, w1
	tracking_bodies = "".join(
		str(c.get("body", "")) for c in result["issues"]["192"]["comments"]
	)
	assert "Deferred Issue Creation" not in tracking_bodies


def test_truncated_comments_json_is_handled_gracefully():
	"""Poller exits cleanly when the comments API returns invalid/truncated JSON."""
	with tempfile.TemporaryDirectory(prefix="poller-test-truncated-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		home_dir = tmp / "home"
		runtime_dir = tmp / "runtime"
		bin_dir.mkdir(parents=True)
		home_dir.mkdir(parents=True)
		runtime_dir.mkdir(parents=True)

		tracking_num = 192

		# Minimal gh mock: returns truncated JSON for comments endpoint,
		# empty body for issue body (causes reconstruction to skip gracefully).
		gh_mock = """\
#!/usr/bin/env python3
import json, re, sys
args = sys.argv[1:]
if not args:
    sys.exit(0)
if args[0] == 'api':
    path = next((a for a in args if not a.startswith('-') and a != 'api'), '')
    jq = None
    i = 0
    while i < len(args):
        if args[i] == '--jq' and i + 1 < len(args):
            jq = args[i + 1]
        i += 1
    # Comments endpoint: return truncated (invalid) JSON to simulate network cut
    if re.search(r'/issues/\\d+/comments', path):
        sys.stdout.write('[{"id":1,"body":"fragment...')
        sys.exit(0)
    # Issue body for state reconstruction
    if re.search(r'/issues/\\d+$', path):
        if jq == '.body':
            print('')
        elif jq == '.state':
            print('open')
        elif jq == '.title':
            print('Test tracking')
        else:
            print(json.dumps({'body': '', 'state': 'open'}))
        sys.exit(0)
    # Repo default_branch
    if re.match(r'repos/[^/]+/[^/]+$', path):
        if jq == '.default_branch':
            print('main')
        else:
            print(json.dumps({'default_branch': 'main'}))
        sys.exit(0)
    # Labels endpoint
    if re.search(r'/issues/\\d+/labels', path):
        if jq:
            print('[]')
        else:
            print('[]')
        sys.exit(0)
    print('{}')
    sys.exit(0)
if args[0] == 'issue' and len(args) >= 2 and args[1] == 'list':
    print('[]')
    sys.exit(0)
print('Unsupported: ' + ' '.join(args), file=sys.stderr)
sys.exit(1)
"""
		(bin_dir / "gh").write_text(gh_mock)
		(bin_dir / "gh").chmod(0o755)

		# Minimal codex mock (should not be reached in this test)
		(bin_dir / "codex").write_text(
			"#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({'status':'complete','justification':'','assessment':'','new_issues':[],'issues_to_revert':[]}))\n"
		)
		(bin_dir / "codex").chmod(0o755)

		(runtime_dir / "tracking_issues.json").write_text(
			json.dumps([{"number": tracking_num, "title": "Truncated JSON test"}]),
			encoding="utf-8",
		)

		env = os.environ.copy()
		env.update(
			{
				"HOME": str(home_dir),
				"RUNTIME_DIR": str(runtime_dir),
				"STATE_FILE": str(runtime_dir / "state.json"),
				"JUDGE_PROMPT_FILE": str(runtime_dir / "judge_prompt.txt"),
				"JUDGE_OUTPUT_FILE": str(runtime_dir / "judge_output.txt"),
				"GH_TOKEN": "test-token",
				"OPENROUTER_API_KEY": "test-key",
				"GITHUB_REPOSITORY": "owner/repo",
				"MODEL_EDITOR": "openai/gpt-5.4",
				"MODEL_REASONING_EFFORT_JUDGE": "xhigh",
				"TG_BOT_SECRET": "",
				"TG_ADMIN_CHAT_ID": "",
				"TOOL_CALL_BUDGET_JUDGE": "60",
				"MAX_REVIEW_BLOCKED_RETRIES": "2",
				"MAX_VALIDATION_RECOVERY_ATTEMPTS": "0",
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"ENABLE_VALIDATION": "false",
				"MAX_VALIDATE_CYCLES": "3",
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			}
		)

		proc = _run_poller_subprocess(
			["bash", str(POLLER_SCRIPT)],
			cwd=str(REPO_ROOT),
			env=env,
		)
		# Poller must exit cleanly — not crash with a jq parse error
		assert proc.returncode == 0, (
			"Poller should handle truncated comments JSON gracefully (exit 0)\n"
			f"stdout:\n{proc.stdout}\n"
			f"stderr:\n{proc.stderr}"
		)
		# A warning about JSON validation failure should appear
		combined = proc.stdout + proc.stderr
		assert "failed validation" in combined or "warning" in combined.lower(), (
			"Expected a warning about invalid JSON in poller output\n"
			f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
		)



# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


# ============================================================
# #1057 auto-heal hardening — additive coverage for:
#   - INTEGRATION_SYNC_CONFLICT_MAX_RETRIES env var (#3)
#   - merged_issue_fingerprints state field + capture helper (#2 capture)
#   - prompts/integration-sync-conflict-resolver.txt placeholders (#4)
#   - scripts/verify_integration_fingerprints.py (#2 verify + #5)
# ============================================================


def _resolver_retry_state_block_for_test(
	*,
	head_sha: str,
	consecutive_failure_count: int,
	escalated: bool = True,
	failure_signature_sha256: str = "signature-1",
) -> str:
	payload = {
		"schema_version": 1,
		"head_sha": head_sha,
		"failure_signature_sha256": failure_signature_sha256,
		"last_failure_signature": failure_signature_sha256,
		"consecutive_failure_count": consecutive_failure_count,
		"threshold": 5,
		"regressed_by_resolver_count": 1,
		"pre_existing_drift_count": 0,
		"last_regressed_by_resolver": [{"fp_key": ["scripts/example.py", "EXPECTED_LINE"], "path": "scripts/example.py", "kind": "must_contain", "issue": 1500, "pr": 2600}],
		"last_pre_existing_drift": [],
		"escalated": escalated,
		"escalated_at": "2026-05-20T00:00:00Z" if escalated else "",
		"updated_at": "2026-05-20T00:00:00Z",
	}
	return "<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1\n" + json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n-->"


def test_integration_sync_conflict_uses_sync_specific_retry_budget_default_one():
	# With the new INTEGRATION_SYNC_CONFLICT_MAX_RETRIES=1 default, an
	# orchestrator/project-* head ref should escalate to the integration
	# judge after exactly ONE unresolved tick — not three. This test sets
	# unresolved_ticks=1 (one prior dispatch) and asserts the judge is
	# invoked on this tick instead of dispatching a second resolver.
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 1
	# Keep the dispatch timestamp inside cooldown so this test verifies that
	# retry-budget exhaustion takes priority over cooldown deferral.
	state["integration_conflict_dispatch_ts"] = 9999999999
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main", "orchestrator/project-192"],
		merge_conflict_on_sync=True,
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Integration judge invoked" in body for body in tracking_bodies), (
		"expected integration judge invocation comment after a single unresolved tick "
		"on an orchestrator/project-* branch (INTEGRATION_SYNC_CONFLICT_MAX_RETRIES=1)"
	)
	assert result["review_dispatches"] == [], (
		"expected NO additional resolver dispatch when the sync-specific retry "
		"budget is exhausted; got: " + str(result["review_dispatches"])
	)


def test_integration_sync_conflict_non_orchestrator_branch_keeps_global_budget():
	# A non-orchestrator integration branch (e.g. a manually-named
	# integration ref) should NOT trip the new tighter budget; it must
	# continue to honour the historical INTEGRATION_CONFLICT_MAX_RETRIES=3
	# default. unresolved_ticks=1 should NOT escalate; the resolver
	# should still be dispatched.
	state = _base_state(status="in_progress")
	state["integration_branch"] = "feature/manual-integration"
	state["integration_conflict_unresolved_ticks"] = 1
	# Allow a fresh dispatch (no cooldown gating).
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main", "feature/manual-integration"],
		merge_conflict_on_sync=True,
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert not any("Integration judge invoked" in body for body in tracking_bodies), (
		"expected NO integration judge invocation for non-orchestrator/project-* "
		"branch with unresolved_ticks=1 (global budget INTEGRATION_CONFLICT_MAX_RETRIES=3 still applies)"
	)


def test_integration_sync_conflict_existing_three_tick_test_still_escalates():
	# Belt-and-braces: the historical
	# test_sync_conflict_escalates_to_judge_immediately_after_retry_budget_exhausted
	# scenario (unresolved_ticks=3 on an orchestrator/project-* branch)
	# must continue to escalate after this change — 3 >= effective max
	# (1) so the judge is still invoked. This guards against accidental
	# regressions in the budget gate ordering.
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 3
	state["integration_conflict_dispatch_ts"] = 9999999999
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main", "orchestrator/project-192"],
		merge_conflict_on_sync=True,
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Integration judge invoked" in body for body in tracking_bodies)


def test_integration_conflict_redispatch_stops_when_current_final_pr_head_is_resolver_escalated():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=[
			{
				"number": 353,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "escalatedsha353",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="escalatedsha353",
					consecutive_failure_count=5,
				),
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
	)
	dispatches_for_final = [d for d in result["review_dispatches"] if d.get("pr_number") == 353]
	assert dispatches_for_final == [], (
		"expected no resolver redispatch once AUTOFIX_RESOLVER_RETRY_STATE_V1 "
		"marks the current final-PR head as escalated; got: "
		+ str(dispatches_for_final)
	)
	latest_state = result["latest_state"]
	assert latest_state["integration_sync_status"] == "escalated"
	assert latest_state["integration_conflict_unresolved_ticks"] == 2
	assert latest_state["integration_conflict_dispatch_count"] == 4


def test_integration_conflict_redispatch_resumes_when_retry_state_head_sha_is_stale():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=[
			{
				"number": 354,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "freshsha354",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="stalesha354",
					consecutive_failure_count=5,
				),
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
	)
	dispatches_for_final = [d for d in result["review_dispatches"] if d.get("pr_number") == 354]
	assert dispatches_for_final, (
		"expected resolver redispatch to resume when the persisted retry-state "
		"head SHA no longer matches the current final-PR head"
	)
	assert result["latest_state"]["integration_sync_status"] == "healing"
	assert result["latest_state"]["integration_conflict_unresolved_ticks"] == 1
	assert result["latest_state"]["integration_conflict_dispatch_count"] == 1


def test_integration_conflict_branch_rebuild_waits_for_threshold():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=[
			{
				"number": 355,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "escalatedsha355",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="escalatedsha355",
					consecutive_failure_count=5,
				),
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
		branch_rebuild_enabled="true",
		branch_rebuild_threshold_hours="999999",
	)
	assert result["latest_state"]["integration_sync_status"] == "escalated"
	assert result.get("mock_branch_rebuild_audit_put_calls", 0) == 0
	assert not any("/git/refs/heads/orchestrator%2Fproject-192" in path for path in result["api_calls"]), (
		"branch rebuild threshold gate should skip delete/recreate API calls"
	)
	assert not any(path.endswith("/git/refs") for path in result["api_calls"]), (
		"branch rebuild threshold gate should skip recreate API calls"
	)


def test_integration_conflict_branch_rebuild_respects_cooldown():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=[
			{
				"number": 356,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "escalatedsha356",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="escalatedsha356",
					consecutive_failure_count=5,
				),
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
		branch_rebuild_enabled="true",
		branch_rebuild_threshold_hours="1",
		branch_rebuild_cooldown_hours="999999",
		mock_branch_rebuild_audit_payload={
			"last_rebuild_at": "2999-01-01T00:00:00Z",
		},
	)
	assert result["latest_state"]["integration_sync_status"] == "escalated"
	assert result.get("mock_branch_rebuild_audit_put_calls", 0) == 0
	assert not any("/git/refs/heads/orchestrator%2Fproject-192" in path for path in result["api_calls"]), (
		"branch rebuild cooldown gate should skip delete API calls"
	)
	assert not any(path.endswith("/git/refs") for path in result["api_calls"]), (
		"branch rebuild cooldown gate should skip recreate API calls"
	)


def test_integration_conflict_branch_rebuild_refuses_audit_warnings():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		prs=[
			{
				"number": 358,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "escalatedsha358",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="escalatedsha358",
					consecutive_failure_count=5,
				),
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
		branch_rebuild_enabled="true",
		branch_rebuild_threshold_hours="1",
		branch_rebuild_cooldown_hours="1",
		mock_branch_rebuild_audit_get_json={
			"warning": "audit_corrupt",
			"hit": False,
			"audit": None,
		},
	)
	assert result["latest_state"]["integration_sync_status"] == "escalated"
	assert "audit storage is unavailable, disabled, or warning-bearing" in result["stderr"]
	assert result.get("mock_branch_rebuild_audit_put_calls", 0) == 0
	assert not any("/git/refs/heads/orchestrator%2Fproject-192" in path for path in result["api_calls"]), (
		"branch rebuild audit warnings should block delete API calls"
	)
	assert not any(path.endswith("/git/refs") for path in result["api_calls"]), (
		"branch rebuild audit warnings should block recreate API calls"
	)


def test_integration_conflict_branch_rebuild_replay_failure_marks_terminal_failure():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"][0]["status"] = "merged"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		issue_linked_prs={10: 901},
		prs=[
			{
				"number": 357,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "escalatedsha357",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="escalatedsha357",
					consecutive_failure_count=5,
				),
			},
			{
				"number": 901,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-05-19T12:00:00Z",
				"merge_commit_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
				"baseRefName": "orchestrator/project-192",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"headSha": "childprsha901",
				"mergeable": True,
				"mergeable_state": "clean",
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
		branch_rebuild_enabled="true",
		branch_rebuild_threshold_hours="1",
		branch_rebuild_cooldown_hours="1",
		branch_ref_shas={
			"main": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"orchestrator/project-192": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		},
	)
	assert result["latest_state"]["status"] == "failed"
	assert result["latest_state"]["final_merge_status"] == "failed"
	assert result["latest_state"]["integration_sync_status"] == "branch_rebuild_failed"
	assert result.get("mock_branch_rebuild_audit_put_calls", 0) >= 2, result
	latest_audit = result.get("mock_branch_rebuild_audit_payload") or {}
	assert latest_audit.get("outcome") == "failed", latest_audit
	assert latest_audit.get("pre_rebuild_branch_head_sha") == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", latest_audit
	assert "deadbeef" in str(latest_audit.get("failure_detail", "")) or "missing commit object" in str(latest_audit.get("failure_detail", "")), latest_audit
	assert any("/git/refs/heads/orchestrator%2Fproject-192" in path for path in result["api_calls"]), (
		"expected delete-ref API call during rebuild attempt"
	)
	assert any(path.endswith("/git/refs") for path in result["api_calls"]), (
		"expected recreate-ref API call during rebuild attempt"
	)
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert any("Integration branch rebuild failed" in body for body in tracking_bodies), tracking_bodies


def test_integration_conflict_branch_rebuild_fetch_retry_stays_escalated_and_ignores_skipped_preflight_cooldown():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"][0]["status"] = "merged"
	state["integration_conflict_unresolved_ticks"] = 2
	state["integration_conflict_dispatch_count"] = 4
	state["integration_conflict_dispatch_ts"] = 0
	branch_refspec = "refs/heads/orchestrator/project-192:refs/remotes/origin/orchestrator/project-192"
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
		issue_linked_prs={10: 902},
		prs=[
			{
				"number": 359,
				"state": "open",
				"baseRefName": "main",
				"headRefName": "orchestrator/project-192",
				"headSha": "escalatedsha359",
				"mergeable": False,
				"mergeable_state": "dirty",
				"body": _resolver_retry_state_block_for_test(
					head_sha="escalatedsha359",
					consecutive_failure_count=5,
				),
			},
			{
				"number": 902,
				"state": "closed",
				"merged": True,
				"merged_at": "2026-05-19T12:00:00Z",
				"merge_commit_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
				"baseRefName": "orchestrator/project-192",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"headSha": "childprsha902",
				"mergeable": True,
				"mergeable_state": "clean",
			},
		],
		existing_branches=["main", "orchestrator/project-192"],
		merge_tree_conflict_paths=["scripts/example.py"],
		branch_rebuild_enabled="true",
		branch_rebuild_threshold_hours="1",
		branch_rebuild_cooldown_hours="999999",
		mock_branch_rebuild_audit_payload={
			"last_rebuild_at": "2999-01-01T00:00:00Z",
			"outcome": "skipped_preflight",
		},
		branch_ref_shas={
			"main": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"orchestrator/project-192": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		},
		mock_git_fetch_fail_after={branch_refspec: 1},
	)
	assert result["latest_state"]["status"] == "in_progress"
	assert result["latest_state"]["integration_sync_status"] == "escalated"
	assert result.get("mock_branch_rebuild_audit_put_calls", 0) >= 2, result
	latest_audit = result.get("mock_branch_rebuild_audit_payload") or {}
	assert latest_audit.get("outcome") == "skipped_preflight", latest_audit
	assert "could not fetch the new remote branch ref locally" in str(latest_audit.get("failure_detail", "")), latest_audit
	assert any("/git/refs/heads/orchestrator%2Fproject-192" in path for path in result["api_calls"]), result["api_calls"]
	assert any(path.endswith("/git/refs") for path in result["api_calls"]), result["api_calls"]
	tracking_bodies = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert not any("Integration branch rebuild failed" in body for body in tracking_bodies), tracking_bodies


def test_branch_rebuild_replay_configures_git_identity():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert re.search(
		r'cd "\$\{worktree_dir\}" \|\| exit 20\s+git config user.name "codex-bot" >>"\$\{replay_log\}" 2>&1 \|\| exit 26\s+git config user.email "codex@users\.noreply\.github\.com" >>"\$\{replay_log\}" 2>&1 \|\| exit 26\s+while IFS= read -r replay_item; do',
		script,
	), "expected branch rebuild replay subshell to configure git identity before cherry-pick"


def test_worktree_registry_is_wired_around_poller_worktree_lifecycles():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "worktree_registry_enabled()" in script
	assert 'bash scripts/worktree_registry.sh register' in script
	assert 'bash scripts/worktree_registry.sh deregister' in script
	assert 'worktree_registry_register "$(basename -- "${wt}")" "${wt}" "${branch}" "project-${project}" "orchestrate-poll"' in script
	assert 'worktree_registry_register "$(basename -- "${_ws}")" "${_ws}" "${int_sha}" "pr-${pr_num}" "orchestrate-poll"' in script
	assert 'worktree_registry_register "$(basename -- "${_wh}")" "${_wh}" "${_tmp_branch}" "pr-${pr_num}" "orchestrate-poll"' in script
	assert 'worktree_registry_register "$(basename -- "${wt}")" "${wt}" "refs/remotes/origin/${integration_branch}" "tracking-${TRACKING_NUM:-0}" "orchestrate-poll"' in script
	assert 'worktree_registry_register "$(basename -- "${worktree_dir}")" "${worktree_dir}" "refs/remotes/origin/${integration_branch}" "pr-${final_pr}" "orchestrate-poll"' in script
	assert script.count("write_state_snapshot_actions_runs_export || true") >= 2
	assert "subshell returns intentional rc values used by the retry logic" in script
	assert ") || subshell_rc=$?" in script
	assert ") || replay_rc=$?" in script


def test_integration_conflict_mergeable_payload_reuse_preserves_false_values():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "if .mergeable != null then .mergeable else empty end" in script


def test_orchestrator_state_seeds_merged_issue_fingerprints_field():
	# ensure_integration_conflict_state_fields must seed the new
	# merged_issue_fingerprints field so downstream jq arithmetic and
	# the verifier renderer have a stable shape to read.
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	# No conflict, no fingerprints — just exercise a single poll tick
	# and confirm the field is present in the final state comment.
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:implementing"]},
		existing_branches=["main", "orchestrator/project-192"],
	)
	final_state = result["latest_state"]
	assert "merged_issue_fingerprints" in final_state, (
		"expected merged_issue_fingerprints field to be seeded by "
		"ensure_integration_conflict_state_fields on every poll tick"
	)
	assert isinstance(final_state["merged_issue_fingerprints"], dict)


def test_capture_intent_fingerprints_helper_is_defined_and_idempotent():
	# Static check that the helper function exists in the poll script
	# and the env-var defaults are wired through.
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "capture_intent_fingerprints_for_merged_subissue()" in script
	assert "FINGERPRINT_PER_FILE_CAP" in script
	assert "FINGERPRINT_MIN_PATTERN_CHARS" in script
	assert "FINGERPRINT_POST_MERGE_REF" in script
	assert "git rev-parse --verify FETCH_HEAD" in script
	assert "command -v timeout >/dev/null 2>&1" in script
	assert 'GIT_COMMAND_TIMEOUT_SECS="${integration_fetch_timeout_secs}"' in script
	assert 'GIT_TERMINAL_PROMPT=0 timeout "${integration_fetch_timeout_secs}s"' in script
	assert "skipping post-merge presence filter" in script
	assert 'elif git rev-parse --verify --quiet "refs/remotes/origin/${integration_branch_for_capture}"' not in script
	# Idempotency guard — must short-circuit when fingerprints are
	# already recorded for that issue.
	assert ".merged_issue_fingerprints[$k]" in script


def test_integration_sync_conflict_max_retries_env_var_is_documented_and_defaulted():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert 'INTEGRATION_SYNC_CONFLICT_MAX_RETRIES="${INTEGRATION_SYNC_CONFLICT_MAX_RETRIES:-1}"' in script
	# The select case in heal_integration_branch_conflict must select
	# the new var only for orchestrator/project-* branches.
	assert "orchestrator/project-*" in script
	assert "effective_max_retries=" in script


def test_integration_sync_conflict_resolver_template_has_required_placeholders():
	tpl_path = REPO_ROOT / "prompts" / "integration-sync-conflict-resolver.txt"
	assert tpl_path.exists(), (
		"integration-sync-conflict-resolver.txt template must exist for "
		"the review_autofix.yml prompt-rendering branch"
	)
	body = tpl_path.read_text(encoding="utf-8")
	for placeholder in (
		"{{CONFLICTED_FILES_COUNT}}",
		"{{CONFLICTED_FILES_LIST}}",
		"{{INTEGRATION_BRANCH}}",
		"{{TRACKING_ISSUE_NUMBER}}",
		"{{TRACKING_ISSUE_TITLE}}",
		"{{TRACKING_ISSUE_BODY}}",
		"{{MERGED_SUB_ISSUES_LIST}}",
		"{{MERGED_SUB_ISSUE_COUNT}}",
		"{{INTENT_FINGERPRINTS_JSON}}",
	):
		assert placeholder in body, f"missing placeholder {placeholder} in template"
	# Hard-rule sanity check: the template must instruct the model to
	# treat fingerprints as load-bearing and to synthesize when
	# necessary.
	assert "must_contain" in body
	assert "must_not_contain" in body
	assert "synthesize" in body.lower()


def _verifier_module():
	import importlib.util

	spec = importlib.util.spec_from_file_location(
		"verify_integration_fingerprints",
		REPO_ROOT / "scripts" / "verify_integration_fingerprints.py",
	)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


def _verifier_sandbox(files: dict[str, str], fingerprints: dict) -> tuple[Path, Path]:
	td = Path(tempfile.mkdtemp(prefix="verifier-test-"))
	for rel, content in files.items():
		p = td / rel
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text(content, encoding="utf-8")
	fp_path = td / "fingerprints.json"
	fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
	return td, fp_path


def test_verify_integration_fingerprints_baseline_regressions():
	# CI/release workflows run explicit `python3 tests/<file>.py` allowlists,
	# so execute the dedicated baseline verifier suite from this already-
	# allowlisted harness too.
	import importlib.util

	spec = importlib.util.spec_from_file_location(
		"test_verify_integration_fingerprints_baseline",
		REPO_ROOT / "tests" / "test_verify_integration_fingerprints_baseline.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	for name, value in sorted(vars(mod).items()):
		if name.startswith("test_") and callable(value):
			value()


def test_verify_integration_fingerprints_partial_removal_regressions():
	# CI/release workflows run explicit `python3 tests/<file>.py` allowlists,
	# so execute the dedicated partial-removal verifier suite from this
	# already-allowlisted harness too.
	import importlib.util

	spec = importlib.util.spec_from_file_location(
		"test_verify_integration_fingerprints_partial_removal",
		REPO_ROOT / "tests" / "test_verify_integration_fingerprints_partial_removal.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	for name, value in sorted(vars(mod).items()):
		if name.startswith("test_") and callable(value):
			value()


def test_verify_integration_fingerprints_post_capture_reintroduction_regressions():
	# CI/release workflows run explicit `python3 tests/<file>.py` allowlists,
	# so execute the dedicated post-capture-reintroduction verifier suite
	# from this already-allowlisted harness too.
	import importlib.util

	spec = importlib.util.spec_from_file_location(
		"test_verify_integration_fingerprints_post_capture_reintroduction",
		REPO_ROOT / "tests" / "test_verify_integration_fingerprints_post_capture_reintroduction.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	for name, value in sorted(vars(mod).items()):
		if name.startswith("test_") and callable(value):
			value()


def test_branch_rebuild_audit_regressions():
	# CI/release workflows run explicit `python3 tests/<file>.py` allowlists,
	# so execute the dedicated branch-rebuild audit suite from this already-
	# allowlisted harness too.
	import importlib.util

	spec = importlib.util.spec_from_file_location(
		"test_branch_rebuild",
		REPO_ROOT / "tests" / "test_branch_rebuild.py",
	)
	assert spec is not None and spec.loader is not None
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	for name, value in sorted(vars(mod).items()):
		if name.startswith("test_") and callable(value):
			value()


def test_verify_integration_fingerprints_passes_when_intent_preserved():
	mod = _verifier_module()
	files = {
		".github/workflows/implement.yml": (
			'install -m 0755 "${src}" "${health_script}"\n'
			'bash "${health_script}" repair\n'
		),
	}
	fingerprints = {
		"1059": {
			"issue": 1059,
			"pr": 1066,
			"must_contain": [
				{"file": ".github/workflows/implement.yml", "regex": r"install\ \-m\ 0755"},
			],
			"must_not_contain": [
				{"file": ".github/workflows/implement.yml", "regex": r"gh\ api\ \-H"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 0
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_rejects_missing_must_contain():
	mod = _verifier_module()
	files = {
		# Resolver took main's verbatim, dropping H1's install line.
		".github/workflows/implement.yml": (
			'gh api -H "Accept: x" "stuff"\n'
			'chmod +x "${health_script}"\n'
		),
	}
	fingerprints = {
		"1059": {
			"issue": 1059,
			"pr": 1066,
			"must_contain": [
				{"file": ".github/workflows/implement.yml", "regex": r"install\ \-m\ 0755"},
			],
			"must_not_contain": [],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 1
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_rejects_reappeared_must_not_contain():
	mod = _verifier_module()
	files = {
		".github/workflows/implement.yml": (
			'install -m 0755 "${src}" "${health_script}"\n'
			'gh api -H "Accept: x" "/repos/foo/contents/scripts/g.sh"\n'
		),
	}
	fingerprints = {
		"1059": {
			"issue": 1059,
			"pr": 1066,
			"must_contain": [
				{"file": ".github/workflows/implement.yml", "regex": r"install\ \-m\ 0755"},
			],
			"must_not_contain": [
				{"file": ".github/workflows/implement.yml", "regex": r"gh\ api\ \-H"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 1
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_fails_open_on_missing_file():
	mod = _verifier_module()
	# Pass a path that does not exist anywhere — must return exit 2
	# (plumbing failure) rather than 1 (violation).
	with tempfile.TemporaryDirectory() as td:
		missing = Path(td) / "nope.json"
		assert mod.main([str(missing)]) == 2


def test_verify_integration_fingerprints_fails_open_on_unparseable_json():
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td:
		bad = Path(td) / "bad.json"
		bad.write_text("not json at all", encoding="utf-8")
		assert mod.main([str(bad)]) == 2


def test_verify_integration_fingerprints_skips_empty_object():
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td:
		empty = Path(td) / "empty.json"
		empty.write_text("{}", encoding="utf-8")
		assert mod.main([str(empty)]) == 0


def _run_verifier_list_mode(mod, fp_path: "Path") -> tuple[int, str, str]:
	"""Invoke `mod.main(['--list-violated-files', str(fp_path)])` with
	stdout+stderr captured via io.StringIO, returning (rc, stdout, stderr).

	The in-tree custom test harness at the bottom of this file calls each
	`test_*` function with zero args (see `main()` below), so pytest
	fixtures like `capsys` are not available — use this helper instead.
	"""
	import io
	import contextlib

	out_buf = io.StringIO()
	err_buf = io.StringIO()
	with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
		rc = mod.main(["--list-violated-files", str(fp_path)])
	return rc, out_buf.getvalue(), err_buf.getvalue()


def test_verify_integration_fingerprints_list_mode_returns_violated_files():
	# --list-violated-files must print the violated file paths to stdout
	# and always exit 0 when the JSON parses — the prepare-step
	# expansion logic in scripts/review_conflict_prepare.sh relies on
	# this contract to collect files Codex is allowed to edit.
	mod = _verifier_module()
	files = {
		# must_contain missing → file-a violated.
		"scripts/file_a.py": "different content",
		# must_not_contain matches → file-b violated.
		"scripts/file_b.py": "BANNED_LINE\n",
		# both fingerprints satisfied → file-c NOT violated.
		"scripts/file_c.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/file_a.py", "regex": r"NEEDED_PATTERN"},
				{"file": "scripts/file_c.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [
				{"file": "scripts/file_b.py", "regex": r"BANNED_LINE"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		rc, out, _err = _run_verifier_list_mode(mod, fp)
		assert rc == 0
		printed = out.strip().splitlines()
		assert printed == ["scripts/file_a.py", "scripts/file_b.py"], (
			f"list-violated-files must emit sorted unique violated paths, got {printed!r}"
		)
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_list_mode_emits_nothing_when_clean():
	mod = _verifier_module()
	files = {
		"scripts/clean.py": "EXPECTED_LINE\n",
	}
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/clean.py", "regex": r"EXPECTED_LINE"},
			],
			"must_not_contain": [],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		rc, out, _err = _run_verifier_list_mode(mod, fp)
		assert rc == 0
		assert out == ""
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_list_mode_fails_open_on_missing_file():
	# Plumbing failure (missing fingerprints path) must still exit 2 in
	# list mode so the caller can distinguish "no violations" (exit 0,
	# empty stdout) from "could not determine".  Also assert the
	# ::warning:: stays off stdout — the whole point of routing plumbing
	# warnings to stderr is so list-mode stdout is paths-only even on
	# fail-open paths.
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td:
		missing = Path(td) / "nope.json"
		rc, out, err = _run_verifier_list_mode(mod, missing)
		assert rc == 2
		assert out == "", f"plumbing warning leaked to stdout in list mode: {out!r}"
		assert "::warning::" in err, (
			f"expected ::warning:: annotation on stderr so operators still see it, got stderr={err!r}"
		)


def test_verify_integration_fingerprints_list_mode_missing_path_arg_keeps_stdout_clean():
	# No positional path + no env var → main() returns 2 with a
	# ::warning:: on stderr and nothing on stdout.
	mod = _verifier_module()
	# Scrub env so the verifier's env-fallback path is exercised.
	prev_env = os.environ.pop("INTEGRATION_FINGERPRINTS_FILE", None)
	try:
		import io
		import contextlib

		out_buf = io.StringIO()
		err_buf = io.StringIO()
		with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
			rc = mod.main(["--list-violated-files"])
		assert rc == 2
		assert out_buf.getvalue() == "", (
			f"no-path-supplied warning leaked to stdout in list mode: {out_buf.getvalue()!r}"
		)
		assert "::warning::" in err_buf.getvalue()
	finally:
		if prev_env is not None:
			os.environ["INTEGRATION_FINGERPRINTS_FILE"] = prev_env


def test_verify_integration_fingerprints_list_mode_unparseable_json_keeps_stdout_clean():
	# JSON parse failure in list mode → exit 2, empty stdout, warning on stderr.
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td:
		bad = Path(td) / "bad.json"
		bad.write_text("not json at all", encoding="utf-8")
		rc, out, err = _run_verifier_list_mode(mod, bad)
		assert rc == 2
		assert out == "", f"JSON-parse warning leaked to stdout: {out!r}"
		assert "::warning::" in err


def test_verify_integration_fingerprints_list_mode_never_prints_annotations_to_stdout():
	# stdout contract: --list-violated-files emits file paths only.
	# Any diagnostic output (::warning::, ::error::) MUST go to stderr.
	# Regression guard for the PR #1581 review thread: a ::warning::
	# leaked onto stdout would be captured as a phantom file path by
	# scripts/review_conflict_prepare.sh and crash check_resolver_diff.sh
	# downstream.  Exercise the _read_file fallback path (os.open
	# raising an unexpected OSError on a real path) by pointing a
	# must_contain fingerprint at the sandbox root — open(dir) raises
	# IsADirectoryError on read, which is the non-FileNotFoundError
	# branch that emits the stderr warning.
	mod = _verifier_module()
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				# Point at the sandbox root (a directory) — open() will
				# succeed but fh.read() raises IsADirectoryError in the
				# generic except branch, exercising the warning path.
				{"file": ".", "regex": r"ANY"},
			],
			"must_not_contain": [],
		}
	}
	sandbox, fp = _verifier_sandbox({}, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		rc, out, _err = _run_verifier_list_mode(mod, fp)
		assert rc == 0
		# stdout must contain ONLY file paths (one per line), never a
		# GitHub Actions annotation line.
		for line in out.splitlines():
			assert not line.startswith("::"), (
				f"stdout contract violation: annotation leaked into list-violated-files output: {line!r}"
			)
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_list_mode_lists_missing_must_contain_file():
	# A must_contain entry whose target file does not exist on disk at
	# all is a violation — the resolver needs the path in its working
	# set so it can (re-)create the file.
	mod = _verifier_module()
	fingerprints = {
		"1500": {
			"issue": 1500,
			"pr": 1501,
			"must_contain": [
				{"file": "scripts/vanished.py", "regex": r"ANY"},
			],
			"must_not_contain": [],
		}
	}
	# Sandbox has no files at all except the fingerprints.
	sandbox, fp = _verifier_sandbox({}, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		import io
		import contextlib

		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			rc = mod.main(["--list-violated-files", str(fp)])
		assert rc == 0
		assert buf.getvalue().strip().splitlines() == ["scripts/vanished.py"]
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def _extract_intent_fingerprint_extractor_py() -> str:
	# Pull the embedded Python heredoc body out of the bash function
	# capture_intent_fingerprints_for_merged_subissue so the extractor
	# can be exercised directly from a test. Anchors on the function
	# name to locate the right heredoc (the poller script has several
	# other python3 <<'PY' blocks); if the anchor or heredoc shape
	# changes the test fails loudly rather than silently skipping
	# coverage.
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	anchor = "capture_intent_fingerprints_for_merged_subissue()"
	anchor_idx = script.index(anchor)
	open_marker = "<<'PY'"
	close_marker = "\nPY\n"
	open_idx = script.index(open_marker, anchor_idx)
	# Skip past the opening marker line (to the start of the body).
	body_start = script.index("\n", open_idx) + 1
	body_end = script.index(close_marker, body_start)
	return script[body_start:body_end]


def test_capture_intent_fingerprints_cross_dedups_net_no_op_lines():
	# Regression guard for the false-positive that blocked
	# orchestrator/project-1479: PR #1491/#1487/#1505/#1526 all wrap a
	# bare call in an if/else fallback, so the same stripped line
	# appears on BOTH sides of the unified diff (removed at the old
	# indent, re-added inside the new conditional at a deeper indent).
	# Without cross-set dedup the extractor captures the line as
	# must_not_contain, which guarantees a false must_not_contain
	# violation on every downstream commit that preserves the PR's
	# intent. The net-change filter must drop such lines from both
	# contracts.
	py_code = _extract_intent_fingerprint_extractor_py()

	diff_text = (
		"diff --git a/scripts/foo.sh b/scripts/foo.sh\n"
		"--- a/scripts/foo.sh\n"
		"+++ b/scripts/foo.sh\n"
		"@@ -1,1 +1,6 @@\n"
		"-        PW_LABELS=\"$(gh_retry _safe_gh_jq \"repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels\" --jq '[.[].name]' || echo '[]')\"\n"
		"+        if echo \"${PRIOR_LABELS_JSON}\" | jq -e --arg key \"${pw_inum}\" 'has($key)' >/dev/null 2>&1; then\n"
		"+          PW_LABELS=\"$(echo \"${PRIOR_LABELS_JSON}\" | jq -c --arg key \"${pw_inum}\" '.[$key] // []')\"\n"
		"+        else\n"
		"+          PW_LABELS=\"$(gh_retry _safe_gh_jq \"repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels\" --jq '[.[].name]' || echo '[]')\"\n"
		"+        fi\n"
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")

	shared_line = (
		"PW_LABELS=\"$(gh_retry _safe_gh_jq \"repos/${GITHUB_REPOSITORY}"
		"/issues/${pw_inum}/labels\" --jq '[.[].name]' || echo '[]')\""
	)
	shared_regex = re.escape(shared_line)
	mc_regexes = [p.get("regex", "") for p in result.get("must_contain", [])]
	mnc_regexes = [p.get("regex", "") for p in result.get("must_not_contain", [])]
	assert shared_regex not in mnc_regexes, (
		"shared line still present in must_not_contain — extractor did not cross-dedup. "
		f"Got mnc_regexes={mnc_regexes!r}"
	)
	assert shared_regex not in mc_regexes, (
		"shared line still present in must_contain — extractor did not cross-dedup. "
		f"Got mc_regexes={mc_regexes!r}"
	)
	# Genuine additions (the new if/then/else/fi wrapper lines that are
	# NOT also on the minus side) must still survive as must_contain so
	# true regressions are still detected.
	if_line = "if echo \"${PRIOR_LABELS_JSON}\" | jq -e --arg key \"${pw_inum}\" 'has($key)' >/dev/null 2>&1; then"
	assert any(re.search(r, if_line) for r in mc_regexes), (
		"true net-added lines must still produce must_contain entries; "
		f"mc_regexes={mc_regexes!r}"
	)


def test_capture_intent_fingerprints_preserves_net_duplicate_line_additions():
	# If a diff adds N copies and removes M copies of the same stripped
	# line, only min(N, M) shared occurrences should be canceled. Any
	# residual net addition must survive as must_contain.
	py_code = _extract_intent_fingerprint_extractor_py()

	diff_text = (
		"diff --git a/scripts/foo.sh b/scripts/foo.sh\n"
		"--- a/scripts/foo.sh\n"
		"+++ b/scripts/foo.sh\n"
		"@@ -1,2 +1,3 @@\n"
		"-        echo \"duplicate-line\"\n"
		"-        echo \"duplicate-line\"\n"
		"+        echo \"duplicate-line\"\n"
		"+        echo \"duplicate-line\"\n"
		"+        echo \"duplicate-line\"\n"
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")

	dup_regex = re.escape('echo "duplicate-line"')
	mc_regexes = [p.get("regex", "") for p in result.get("must_contain", [])]
	mnc_regexes = [p.get("regex", "") for p in result.get("must_not_contain", [])]
	assert dup_regex in mc_regexes, (
		"net-added duplicate line should survive as must_contain fingerprint; "
		f"mc_regexes={mc_regexes!r}"
	)
	assert dup_regex not in mnc_regexes, (
		"net-added duplicate line must not remain in must_not_contain; "
		f"mnc_regexes={mnc_regexes!r}"
	)


def test_capture_intent_fingerprints_substring_dedups_extended_lines():
	# Regression guard for tele-funtoken-msg-scoring PR #2852 (runs
	# 25612161581 / 25614444875): a sub-issue extended an existing line
	# by appending text — the unified diff has the SHORTER stripped line
	# on the minus side and the LONGER stripped line (which textually
	# contains the shorter) on the plus side, on the SAME file. Without
	# the substring-overlap filter, both survive (`Counter`-based exact
	# dedup only catches identical strings); the captured pair becomes a
	# structurally unsatisfiable must_contain ⊃ must_not_contain
	# constraint under re.search (the longer match guarantees the
	# shorter substring matches), and the resolver burns its 3-attempt
	# retry budget on a hunk it cannot make pass. The substring filter
	# must drop the removed-line pattern from must_not_contain while
	# preserving the added-line pattern in must_contain.
	py_code = _extract_intent_fingerprint_extractor_py()

	short_line = "    description: critic-driven cohort-mix rollouts."
	long_line = (
		"    description: critic-driven cohort-mix rollouts. "
		"When critic authority is enabled, accepted by orchestrator."
	)
	diff_text = (
		"diff --git a/db/contracts/flash_offer_cohort_config.yml "
		"b/db/contracts/flash_offer_cohort_config.yml\n"
		"--- a/db/contracts/flash_offer_cohort_config.yml\n"
		"+++ b/db/contracts/flash_offer_cohort_config.yml\n"
		"@@ -1,3 +1,3 @@\n"
		" prefix\n"
		f"-{short_line}\n"
		f"+{long_line}\n"
		" suffix\n"
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")

	short_regex = re.escape(short_line.strip())
	long_regex = re.escape(long_line.strip())
	mc_regexes = [p.get("regex", "") for p in result.get("must_contain", [])]
	mnc_regexes = [p.get("regex", "") for p in result.get("must_not_contain", [])]
	assert short_regex not in mnc_regexes, (
		"removed-line pattern that is a substring of an added-line pattern on the same file "
		f"must be dropped from must_not_contain. Got mnc_regexes={mnc_regexes!r}"
	)
	assert long_regex in mc_regexes, (
		"net-added longer line must still survive as must_contain so the stronger intent is "
		f"enforced downstream. Got mc_regexes={mc_regexes!r}"
	)


def test_capture_intent_fingerprints_substring_dedup_preserves_unrelated_pairs():
	# Negative control for the substring-overlap filter: when a removed
	# line's stripped text is NOT a substring of any added line on the
	# same file, both sides must survive — otherwise the filter would
	# silently drop legitimate must_not_contain patterns and let real
	# regressions through.
	py_code = _extract_intent_fingerprint_extractor_py()

	removed_line = "    description: old standalone configuration directive"
	added_line = "    description: entirely unrelated new configuration directive"
	diff_text = (
		"diff --git a/db/contracts/example.yml b/db/contracts/example.yml\n"
		"--- a/db/contracts/example.yml\n"
		"+++ b/db/contracts/example.yml\n"
		"@@ -1,2 +1,2 @@\n"
		f"-{removed_line}\n"
		f"+{added_line}\n"
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")

	removed_regex = re.escape(removed_line.strip())
	added_regex = re.escape(added_line.strip())
	mc_regexes = [p.get("regex", "") for p in result.get("must_contain", [])]
	mnc_regexes = [p.get("regex", "") for p in result.get("must_not_contain", [])]
	assert added_regex in mc_regexes, (
		"unrelated added line must survive as must_contain; "
		f"mc_regexes={mc_regexes!r}"
	)
	assert removed_regex in mnc_regexes, (
		"unrelated removed line must survive as must_not_contain; the substring filter "
		f"must only drop pairs where one is a literal substring of the other. "
		f"mnc_regexes={mnc_regexes!r}"
	)


def test_verify_integration_fingerprints_cross_dedups_self_contradictory_pairs():
	# Defensive path in the verifier: if the orchestrator state file
	# still holds legacy bad fingerprints (capture is idempotent per
	# issue, so an already-stored entry survives the extractor fix),
	# the verifier must treat any (file, regex) pair present in BOTH
	# must_contain and must_not_contain as self-contradictory and
	# skip it rather than emit a guaranteed-impossible violation.
	mod = _verifier_module()
	files = {
		"scripts/foo.sh": (
			'        PW_LABELS="$(gh_retry _safe_gh_jq '
			'"repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels" '
			'--jq \'[.[].name]\' || echo \'[]\')"\n'
		),
	}
	shared_regex = re.escape(
		"PW_LABELS=\"$(gh_retry _safe_gh_jq "
		"\"repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels\" "
		"--jq '[.[].name]' || echo '[]')\""
	)
	fingerprints = {
		"1483": {
			"issue": 1483,
			"pr": 1491,
			"must_contain": [
				{"file": "scripts/foo.sh", "regex": shared_regex},
			],
			"must_not_contain": [
				{"file": "scripts/foo.sh", "regex": shared_regex},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 0
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_still_fails_on_real_violation_when_mixed_with_shared():
	# Belt-and-braces: the cross-dedup must ONLY skip patterns present
	# in both sets; genuine must_not_contain entries (not also in
	# must_contain) must still trigger a violation. Otherwise the
	# defensive path would silently neuter the whole verifier.
	mod = _verifier_module()
	files = {
		"scripts/foo.sh": (
			'        PW_LABELS="$(gh_retry _safe_gh_jq '
			'"repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels" '
			'--jq \'[.[].name]\' || echo \'[]\')"\n'
			'        gh api -H "Accept: x" "/repos/foo/bar"\n'
		),
	}
	shared_regex = re.escape(
		"PW_LABELS=\"$(gh_retry _safe_gh_jq "
		"\"repos/${GITHUB_REPOSITORY}/issues/${pw_inum}/labels\" "
		"--jq '[.[].name]' || echo '[]')\""
	)
	fingerprints = {
		"1483": {
			"issue": 1483,
			"pr": 1491,
			"must_contain": [
				{"file": "scripts/foo.sh", "regex": shared_regex},
			],
			"must_not_contain": [
				{"file": "scripts/foo.sh", "regex": shared_regex},
				# Genuine must_not_contain — NOT in must_contain, so
				# the dedup leaves it alone.
				{"file": "scripts/foo.sh", "regex": r"gh\ api\ \-H"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 1
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_cross_issue_exact_conflicts_prefer_newer_capture():
	# Historic merged-subissue state can contain the exact same (file, regex)
	# pair across DIFFERENT issues with opposite intent: an older issue kept
	# a line under must_contain, then a newer issue intentionally deleted that
	# same line under must_not_contain. The verifier should prefer the newer
	# captured_at intent rather than report an impossible contradiction.
	import contextlib
	import io

	mod = _verifier_module()
	legacy_line = "# Serena tool-usage guidance appears only when the workflow has bootstrapped"
	files = {
		"scripts/render_prompt.sh": (
			"# Serena tool-usage guidance stays prompt-local and renders to an empty block\n"
			"# when Serena is unavailable.\n"
		),
	}
	fingerprints = {
		"2523": {
			"issue": 2523,
			"pr": 2524,
			"captured_at": "2026-05-11T18:16:20Z",
			"must_contain": [
				{"file": "scripts/render_prompt.sh", "regex": re.escape(legacy_line)},
			],
			"must_not_contain": [],
		},
		"2525": {
			"issue": 2525,
			"pr": 2527,
			"captured_at": "2026-05-11T22:09:20Z",
			"must_contain": [],
			"must_not_contain": [
				{"file": "scripts/render_prompt.sh", "regex": re.escape(legacy_line)},
			],
		},
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	stdout_buf = io.StringIO()
	try:
		os.chdir(sandbox)
		with contextlib.redirect_stdout(stdout_buf):
			rc = mod.main([str(fp)])
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)
	assert rc == 0, f"verify must PASS when newer exact conflicting capture wins; got rc={rc}"
	captured = stdout_buf.getvalue()
	assert "::warning::Fingerprint cross-issue exact-conflict dedup" in captured, (
		"verifier must emit a ::warning:: naming the cross-issue exact-conflict dedup so "
		f"operators can see the stale-state cause; got stdout={captured!r}"
	)
	assert "#2525 (PR #2527)" in captured, (
		"warning should name the newer winning capture so operators can trace provenance; "
		f"got stdout={captured!r}"
	)



def test_verify_integration_fingerprints_cross_issue_exact_conflicts_require_strictly_newer_capture():
	# Conservative guardrail: if opposite-intent exact conflicts tie on
	# captured_at, the verifier must NOT guess. Leave the contradiction live
	# so verify mode fails instead of silently preferring one side.
	mod = _verifier_module()
	legacy_line = "# Serena tool-usage guidance appears only when the workflow has bootstrapped"
	files = {
		"scripts/render_prompt.sh": (
			"# Serena tool-usage guidance stays prompt-local and renders to an empty block\n"
			"# when Serena is unavailable.\n"
		),
	}
	fingerprints = {
		"2523": {
			"issue": 2523,
			"pr": 2524,
			"captured_at": "2026-05-11T22:09:20Z",
			"must_contain": [
				{"file": "scripts/render_prompt.sh", "regex": re.escape(legacy_line)},
			],
			"must_not_contain": [],
		},
		"2525": {
			"issue": 2525,
			"pr": 2527,
			"captured_at": "2026-05-11T22:09:20Z",
			"must_contain": [],
			"must_not_contain": [
				{"file": "scripts/render_prompt.sh", "regex": re.escape(legacy_line)},
			],
		},
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 1
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)



def test_verify_integration_fingerprints_list_mode_skips_cross_issue_exact_conflicts():
	# list-violated-files shares the verify-mode dedup logic, but must stay
	# silent on stdout except for genuinely violated file paths. A stale older
	# exact conflict superseded by a newer capture must therefore produce no
	# file-path output at all.
	mod = _verifier_module()
	legacy_line = "# Serena tool-usage guidance appears only when the workflow has bootstrapped"
	files = {
		"scripts/render_prompt.sh": (
			"# Serena tool-usage guidance stays prompt-local and renders to an empty block\n"
			"# when Serena is unavailable.\n"
		),
	}
	fingerprints = {
		"2523": {
			"issue": 2523,
			"pr": 2524,
			"captured_at": "2026-05-11T18:16:20Z",
			"must_contain": [
				{"file": "scripts/render_prompt.sh", "regex": re.escape(legacy_line)},
			],
			"must_not_contain": [],
		},
		"2525": {
			"issue": 2525,
			"pr": 2527,
			"captured_at": "2026-05-11T22:09:20Z",
			"must_contain": [],
			"must_not_contain": [
				{"file": "scripts/render_prompt.sh", "regex": re.escape(legacy_line)},
			],
		},
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		rc, out, err = _run_verifier_list_mode(mod, fp)
		assert rc == 0
		assert out == ""
		assert err == ""
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_substring_dedups_self_contradictory_pairs():
	# Companion path to test_verify_integration_fingerprints_cross_dedups_self_
	# contradictory_pairs for state files captured before the capture-side
	# substring filter landed (capture is idempotent per issue, so an
	# already-stored bad entry survives the extractor fix). When the state
	# file holds a must_not_contain regex whose source is a literal
	# substring of a must_contain regex on the SAME file, the verifier
	# must drop the must_not_contain side (any tree satisfying the longer
	# must_contain trivially matches the shorter must_not_contain under
	# re.search, making the pair structurally unsatisfiable) and emit a
	# ::warning:: that names the dedup so the upstream cause stays visible.
	import contextlib
	import io
	mod = _verifier_module()
	short_text = "critic-driven cohort-mix rollouts."
	long_text = (
		"critic-driven cohort-mix rollouts. "
		"When critic authority is enabled, accepted by orchestrator."
	)
	files = {
		"db/contracts/flash_offer_cohort_config.yml": (
			f"  description: |\n    {long_text}\n"
		),
	}
	fingerprints = {
		"2849": {
			"issue": 2849,
			"pr": 2851,
			"must_contain": [
				{"file": "db/contracts/flash_offer_cohort_config.yml", "regex": re.escape(long_text)},
			],
			"must_not_contain": [
				{"file": "db/contracts/flash_offer_cohort_config.yml", "regex": re.escape(short_text)},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	stdout_buf = io.StringIO()
	try:
		os.chdir(sandbox)
		with contextlib.redirect_stdout(stdout_buf):
			rc = mod.main([str(fp)])
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)
	assert rc == 0, f"verify must PASS after substring dedup; got rc={rc}"
	captured = stdout_buf.getvalue()
	assert "::warning::Fingerprint substring-overlap dedup" in captured, (
		"verifier must emit a ::warning:: naming the substring-overlap dedup so the "
		f"capture-side cause stays visible in the run log; got stdout={captured!r}"
	)


def test_verify_integration_fingerprints_substring_dedup_still_fails_on_unrelated_must_not_contain():
	# Belt-and-braces parallel to test_verify_integration_fingerprints_still_
	# fails_on_real_violation_when_mixed_with_shared: the substring-overlap
	# dedup must ONLY drop must_not_contain regexes that are literal
	# substrings of a must_contain regex on the SAME file. Genuine
	# must_not_contain entries (unrelated to any must_contain) must still
	# trigger a violation when present in the post-resolve tree —
	# otherwise the substring path would silently neuter the verifier
	# whenever any pattern happened to overlap.
	mod = _verifier_module()
	short_text = "critic-driven cohort-mix rollouts."
	long_text = (
		"critic-driven cohort-mix rollouts. "
		"When critic authority is enabled, accepted by orchestrator."
	)
	files = {
		"db/contracts/flash_offer_cohort_config.yml": (
			f"  description: |\n    {long_text}\n"
			"    forbidden_marker_that_should_not_be_here\n"
		),
	}
	fingerprints = {
		"2849": {
			"issue": 2849,
			"pr": 2851,
			"must_contain": [
				{"file": "db/contracts/flash_offer_cohort_config.yml", "regex": re.escape(long_text)},
			],
			"must_not_contain": [
				# Will be dropped by the substring filter (substring of the must_contain regex above).
				{"file": "db/contracts/flash_offer_cohort_config.yml", "regex": re.escape(short_text)},
				# Genuine forbidden pattern, NOT a substring of any must_contain — must still fire.
				{"file": "db/contracts/flash_offer_cohort_config.yml", "regex": r"forbidden_marker_that_should_not_be_here"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 1
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


# ===========================================================================
# must_not_exist constraint: path-agnostic capture of merged sub-PR file
# deletions and the verifier hard-reject when those paths reappear on the
# integration tree. Covers the upstream regression where a back-merge
# resolver silently reintroduced files an earlier sub-issue had deleted
# because the text-regex contract did not (and structurally cannot) catch
# deletions of files outside the resolver-safe ALLOWED_PREFIXES allowlist.
# ===========================================================================


def test_capture_intent_fingerprints_records_must_not_exist_on_file_deletion():
	# A unified diff containing ``+++ /dev/null`` against ``--- a/<path>``
	# must produce a ``must_not_exist`` entry for that path. Covers the
	# core capture behavior new in this revision.
	py_code = _extract_intent_fingerprint_extractor_py()

	diff_text = (
		"diff --git a/tests/test_canonicalize_autobet_profit.py b/tests/test_canonicalize_autobet_profit.py\n"
		"deleted file mode 100644\n"
		"--- a/tests/test_canonicalize_autobet_profit.py\n"
		"+++ /dev/null\n"
		"@@ -1,3 +0,0 @@\n"
		"-import unittest\n"
		"-\n"
		"-class CanonicalizeProfitTest(unittest.TestCase): pass\n"
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")
	mne_paths = [e.get("file") for e in result.get("must_not_exist", [])]
	assert "tests/test_canonicalize_autobet_profit.py" in mne_paths, (
		f"deleted file must appear in must_not_exist; got {mne_paths!r}"
	)


def test_capture_intent_fingerprints_must_not_exist_skips_allowed_prefixes_filter():
	# Path-agnostic contract: a deletion of a file OUTSIDE the
	# resolver-safe ALLOWED_PREFIXES (e.g. consumer ``backend/``) must
	# still be recorded under ``must_not_exist`` even though the
	# text-regex side of capture would skip the same path. This is the
	# direct fix for the upstream failure mode the Phase 5 planner BLOCK
	# surfaced: ``backend/canonicalize_autobet_profit.py`` was deleted by
	# the original sub-issue but never fingerprinted because ``backend/``
	# is not on the regex allowlist, so the resolver was free to
	# resurrect it on a back-merge with no contract violation.
	py_code = _extract_intent_fingerprint_extractor_py()

	diff_text = (
		"diff --git a/backend/canonicalize_autobet_profit.py b/backend/canonicalize_autobet_profit.py\n"
		"deleted file mode 100644\n"
		"--- a/backend/canonicalize_autobet_profit.py\n"
		"+++ /dev/null\n"
		"@@ -1,2 +0,0 @@\n"
		'-PROFIT_MIRROR_FIELD = "profit_scaled"\n'
		"-def run(): pass\n"
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")
	mne_paths = [e.get("file") for e in result.get("must_not_exist", [])]
	mnc_paths = {p.get("file") for p in result.get("must_not_contain", [])}
	assert "backend/canonicalize_autobet_profit.py" in mne_paths, (
		"must_not_exist must capture deletions path-agnostically, NOT honour the "
		"text-regex ALLOWED_PREFIXES allowlist (else the very upstream regression "
		f"this revision exists to fix is reintroduced). Got mne={mne_paths!r}"
	)
	# Sanity: the text-regex side correctly filters backend/ out — only
	# must_not_exist provides the deletion guarantee for that path.
	assert "backend/canonicalize_autobet_profit.py" not in mnc_paths, (
		f"must_not_contain should still honour ALLOWED_PREFIXES; got mnc_paths={mnc_paths!r}"
	)


def test_capture_intent_fingerprints_must_not_exist_ignores_pure_additions():
	# A diff that adds a new file (``--- /dev/null`` ⇒ ``+++ b/<path>``)
	# must NOT produce a must_not_exist entry — only deletions do. The
	# parser must distinguish the two transitions correctly.
	py_code = _extract_intent_fingerprint_extractor_py()

	diff_text = (
		"diff --git a/scripts/new_helper.sh b/scripts/new_helper.sh\n"
		"new file mode 100755\n"
		"--- /dev/null\n"
		"+++ b/scripts/new_helper.sh\n"
		"@@ -0,0 +1,2 @@\n"
		"+#!/usr/bin/env bash\n"
		'+echo "newly added helper script"\n'
	)

	with tempfile.TemporaryDirectory() as td:
		diff_file = Path(td) / "pr.diff"
		diff_file.write_text(diff_text, encoding="utf-8")
		env = {
			**os.environ,
			"FINGERPRINT_PER_FILE_CAP": "12",
			"FINGERPRINT_MIN_PATTERN_CHARS": "12",
		}
		proc = subprocess.run(
			["python3", "-c", py_code, str(diff_file)],
			capture_output=True,
			text=True,
			env=env,
			timeout=30,
		)
	assert proc.returncode == 0, f"extractor exited nonzero: stderr={proc.stderr!r}"
	result = json.loads(proc.stdout or "{}")
	assert result.get("must_not_exist", []) == [], (
		"adding a file must not register a must_not_exist entry; "
		f"got {result.get('must_not_exist')!r}"
	)


def test_capture_intent_fingerprints_storage_writes_must_not_exist_field():
	# Static check: the bash storage jq writes ``must_not_exist`` to the
	# state alongside must_contain / must_not_contain, and the diagnostic
	# log line surfaces the must_not_exist count so operators can see
	# capture coverage at a glance.
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert "must_not_exist: ($fp.must_not_exist // [])" in script, (
		"capture function must persist must_not_exist into "
		"merged_issue_fingerprints state entries"
	)
	assert "must_not_exist=${mne_count}" in script, (
		"capture function must surface must_not_exist count in its log line"
	)


def test_verify_integration_fingerprints_passes_when_must_not_exist_path_absent():
	# Working-tree mode: a must_not_exist entry whose path is not present
	# on disk is the satisfied case — no violation, exit 0.
	mod = _verifier_module()
	files = {
		"tests/unrelated_test.py": "import unittest\n",
	}
	fingerprints = {
		"2969": {
			"issue": 2969,
			"pr": 2970,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [
				{"file": "backend/canonicalize_autobet_profit.py"},
				{"file": "tests/test_canonicalize_autobet_profit.py"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 0
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_rejects_must_not_exist_path_present():
	# Working-tree mode: a path the sub-issue deleted is back on disk —
	# the verifier must reject with exit 1 so the [ai-merge-resolve]
	# commit is aborted (resolver path) or the wave dispatch is blocked
	# (orchestrator path).
	mod = _verifier_module()
	files = {
		# Same path the sub-issue removed has reappeared (back-merge from
		# main brought it back). Even with reformatted contents, the
		# verifier must catch it via must_not_exist alone.
		"backend/canonicalize_autobet_profit.py": (
			'PROFIT_MIRROR_FIELD = "profit_scaled"\n'
			"def run():\n"
			"    pass\n"
		),
	}
	fingerprints = {
		"2969": {
			"issue": 2969,
			"pr": 2970,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [
				{"file": "backend/canonicalize_autobet_profit.py"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 1
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_must_not_exist_with_empty_or_malformed_entries():
	# Robustness: entries missing the ``file`` key, non-dict entries, and
	# an absent ``must_not_exist`` field must all be tolerated as
	# fail-open no-ops — never crash the verifier and never produce a
	# violation.
	mod = _verifier_module()
	files = {
		"unrelated.txt": "content\n",
	}
	fingerprints = {
		"1": {
			"issue": 1,
			"pr": 2,
			"must_contain": [],
			"must_not_contain": [],
			# Mixed shapes: missing file key, non-dict, empty file value,
			# none of which should violate.
			"must_not_exist": [
				{},
				{"file": ""},
				"not-a-dict",
				{"other_key": "ignored"},
			],
		},
		"3": {
			"issue": 3,
			"pr": 4,
			"must_contain": [],
			"must_not_contain": [],
			# Absent must_not_exist field — handled via default.
		},
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		assert mod.main([str(fp)]) == 0
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def test_verify_integration_fingerprints_list_mode_includes_must_not_exist_violations():
	# --list-violated-files must surface must_not_exist regressions
	# alongside must_contain / must_not_contain regressions so
	# review_conflict_prepare.sh adds the resurrected path to the
	# resolver's working set.
	mod = _verifier_module()
	files = {
		"backend/canonicalize_autobet_profit.py": "any content\n",
		"tests/clean.py": "content\n",
	}
	fingerprints = {
		"2969": {
			"issue": 2969,
			"pr": 2970,
			"must_contain": [],
			"must_not_contain": [],
			"must_not_exist": [
				{"file": "backend/canonicalize_autobet_profit.py"},
				# This path is not on disk — must NOT appear in output.
				{"file": "backend/already_gone.py"},
			],
		}
	}
	sandbox, fp = _verifier_sandbox(files, fingerprints)
	prev_cwd = os.getcwd()
	try:
		os.chdir(sandbox)
		rc, out, _err = _run_verifier_list_mode(mod, fp)
		assert rc == 0
		printed = out.strip().splitlines()
		assert "backend/canonicalize_autobet_profit.py" in printed, (
			f"resurrected path must appear in list-violated-files output; got {printed!r}"
		)
		assert "backend/already_gone.py" not in printed, (
			f"path that is genuinely absent must not appear; got {printed!r}"
		)
	finally:
		os.chdir(prev_cwd)
		shutil.rmtree(sandbox, ignore_errors=True)


def _make_git_sandbox_with_blob(tmp_root: Path, paths: dict[str, str]) -> tuple[Path, str]:
	"""Initialize a tiny throwaway git repo containing ``paths`` and return
	(repo_root, commit_sha). Helper for the verifier's --ref mode tests."""
	repo = tmp_root / "repo"
	repo.mkdir()
	subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=repo, check=True)
	subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
	subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
	subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
	for rel, content in paths.items():
		p = repo / rel
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text(content, encoding="utf-8")
	subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
	subprocess.run(
		["git", "commit", "--quiet", "-m", "seed"], cwd=repo, check=True
	)
	commit = subprocess.run(
		["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
	).stdout.strip()
	return repo, commit


def test_verify_integration_fingerprints_ref_mode_rejects_resurrected_file():
	# --ref mode: verify against a git ref's tree (no checkout). The
	# wave-dispatch gate uses this to assert the integration branch HEAD
	# still honours the merged sub-issue fingerprint contract without
	# disturbing the cwd. A path captured in must_not_exist that is
	# present at the ref must produce exit 1.
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td_root:
		repo, _commit = _make_git_sandbox_with_blob(
			Path(td_root),
			{
				# This file was supposed to be deleted by the merged sub-issue
				# but a back-merge brought it back at the ref's HEAD.
				"backend/canonicalize_autobet_profit.py": (
					'PROFIT_MIRROR_FIELD = "profit_scaled"\n'
				),
			},
		)
		fingerprints = {
			"2969": {
				"issue": 2969,
				"pr": 2970,
				"must_contain": [],
				"must_not_contain": [],
				"must_not_exist": [
					{"file": "backend/canonicalize_autobet_profit.py"},
				],
			}
		}
		fp_path = Path(td_root) / "fp.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		prev_cwd = os.getcwd()
		try:
			# Run the verifier from inside the temp repo so git resolves
			# the ref against this throwaway sandbox.
			os.chdir(repo)
			assert mod.main(["--ref", "HEAD", str(fp_path)]) == 1
		finally:
			os.chdir(prev_cwd)


def test_verify_integration_fingerprints_ref_mode_passes_when_deletion_held():
	# --ref mode: the same fingerprint, but the ref's tree honours the
	# deletion (file absent) — exit 0.
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td_root:
		repo, _commit = _make_git_sandbox_with_blob(
			Path(td_root),
			{
				# Some unrelated file so the commit isn't empty; the
				# deleted path is intentionally not staged.
				"README.md": "placeholder\n",
			},
		)
		fingerprints = {
			"2969": {
				"issue": 2969,
				"pr": 2970,
				"must_contain": [],
				"must_not_contain": [],
				"must_not_exist": [
					{"file": "backend/canonicalize_autobet_profit.py"},
				],
			}
		}
		fp_path = Path(td_root) / "fp.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			assert mod.main(["--ref", "HEAD", str(fp_path)]) == 0
		finally:
			os.chdir(prev_cwd)


def test_verify_integration_fingerprints_ref_mode_must_contain_via_git_show():
	# --ref mode also reads file contents through git show for the
	# must_contain / must_not_contain regex checks. Cover the contract
	# so future refactors don't accidentally break ref-mode regex
	# reading (e.g. by routing only the existence check through git).
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td_root:
		repo, _commit = _make_git_sandbox_with_blob(
			Path(td_root),
			{
				".github/workflows/implement.yml": (
					'install -m 0755 "${src}" "${health_script}"\n'
				),
			},
		)
		fingerprints = {
			"1059": {
				"issue": 1059,
				"pr": 1066,
				"must_contain": [
					{"file": ".github/workflows/implement.yml", "regex": r"install\ \-m\ 0755"},
				],
				"must_not_contain": [
					{"file": ".github/workflows/implement.yml", "regex": r"BANNED_PATTERN"},
				],
			}
		}
		fp_path = Path(td_root) / "fp.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			# Use --ref=HEAD form to also exercise that flag-parse branch.
			assert mod.main(["--ref=HEAD", str(fp_path)]) == 0
		finally:
			os.chdir(prev_cwd)


def test_verify_integration_fingerprints_ref_mode_fails_open_on_unknown_ref():
	# A ref the local git repo cannot resolve (typo, not fetched) must
	# not produce phantom violations. _read_file and _path_exists both
	# return None / False on git failure, which surfaces as
	# (a) must_contain entries reporting "file does not exist" but
	# (b) must_not_exist / must_not_contain entries silently passing.
	# In a wave-dispatch gate context callers should treat ref-mode
	# inability to resolve the branch as a plumbing failure (handled
	# upstream by the rev-parse guard before invoking the verifier).
	# Here we just guarantee no crash and a deterministic exit shape.
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td_root:
		repo, _commit = _make_git_sandbox_with_blob(
			Path(td_root),
			{"README.md": "x\n"},
		)
		fingerprints = {
			"1": {
				"issue": 1,
				"pr": 2,
				"must_contain": [],
				"must_not_contain": [],
				"must_not_exist": [
					{"file": "some/path.py"},
				],
			}
		}
		fp_path = Path(td_root) / "fp.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		prev_cwd = os.getcwd()
		try:
			os.chdir(repo)
			# Nonexistent ref: must_not_exist check returns False (path
			# absent at unknown ref ⇒ contract satisfied) → exit 0.
			rc = mod.main(["--ref", "refs/heads/does-not-exist", str(fp_path)])
			assert rc == 0, f"unknown ref must not crash or fault; got rc={rc}"
		finally:
			os.chdir(prev_cwd)


def test_verify_integration_fingerprints_ref_mode_honoured_via_env_var():
	# INTEGRATION_VERIFY_REF env var is the fallback for callers that
	# can't pass --ref through their argv plumbing.
	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td_root:
		repo, _commit = _make_git_sandbox_with_blob(
			Path(td_root),
			{
				"backend/file_back.py": "content\n",
			},
		)
		fingerprints = {
			"2969": {
				"issue": 2969,
				"pr": 2970,
				"must_contain": [],
				"must_not_contain": [],
				"must_not_exist": [{"file": "backend/file_back.py"}],
			}
		}
		fp_path = Path(td_root) / "fp.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		prev_cwd = os.getcwd()
		prev_env = os.environ.get("INTEGRATION_VERIFY_REF")
		try:
			os.chdir(repo)
			os.environ["INTEGRATION_VERIFY_REF"] = "HEAD"
			rc = mod.main([str(fp_path)])
			assert rc == 1, "env-supplied ref must trigger ref mode and detect the violation"
		finally:
			os.chdir(prev_cwd)
			if prev_env is None:
				os.environ.pop("INTEGRATION_VERIFY_REF", None)
			else:
				os.environ["INTEGRATION_VERIFY_REF"] = prev_env


def test_verify_integration_fingerprints_blank_cli_ref_falls_back_to_working_tree():
	# ``--ref=`` used to route reads through ``git show :path`` /
	# ``git cat-file -e :path`` (the index), which is wrong when the
	# caller really supplied a blank ref by mistake. Normalize blank CLI
	# refs back to working-tree mode instead, even when the env-var
	# fallback is set.
	import contextlib
	import io

	mod = _verifier_module()
	with tempfile.TemporaryDirectory() as td_root:
		repo, _commit = _make_git_sandbox_with_blob(
			Path(td_root),
			{
				"backend/file_back.py": "content from index\n",
			},
		)
		(repo / "backend" / "file_back.py").unlink()
		fingerprints = {
			"2969": {
				"issue": 2969,
				"pr": 2970,
				"must_contain": [],
				"must_not_contain": [],
				"must_not_exist": [{"file": "backend/file_back.py"}],
			}
		}
		fp_path = Path(td_root) / "fp.json"
		fp_path.write_text(json.dumps(fingerprints), encoding="utf-8")
		prev_cwd = os.getcwd()
		prev_env = os.environ.get("INTEGRATION_VERIFY_REF")
		stderr_buf = io.StringIO()
		try:
			os.chdir(repo)
			os.environ["INTEGRATION_VERIFY_REF"] = "HEAD"
			with contextlib.redirect_stderr(stderr_buf):
				rc = mod.main(["--ref=", str(fp_path)])
			assert rc == 0, (
				"blank CLI ref must keep working-tree mode even when the env fallback is set"
			)
		finally:
			os.chdir(prev_cwd)
			if prev_env is None:
				os.environ.pop("INTEGRATION_VERIFY_REF", None)
			else:
				os.environ["INTEGRATION_VERIFY_REF"] = prev_env
		assert "blank --ref value supplied" in stderr_buf.getvalue()


def test_verify_integration_fingerprints_unknown_flag_returns_exit_2():
	import contextlib
	import io

	mod = _verifier_module()
	stderr_buf = io.StringIO()
	with contextlib.redirect_stderr(stderr_buf):
		rc = mod.main(["--unknown-flag", "fingerprints.json"])
	assert rc == 2
	assert "unknown option '--unknown-flag'" in stderr_buf.getvalue()


def test_verify_integration_fingerprints_trailing_unknown_flag_returns_exit_2():
	import contextlib
	import io

	mod = _verifier_module()
	stderr_buf = io.StringIO()
	with contextlib.redirect_stderr(stderr_buf):
		rc = mod.main(["fingerprints.json", "--unknown-flag"])
	assert rc == 2
	assert "unknown option '--unknown-flag'" in stderr_buf.getvalue()


def test_wave_dispatch_gate_invokes_verifier_against_integration_ref():
	# Static contract: the wave-dispatch gate block must invoke the
	# verifier with --ref pointing at the integration branch before
	# advancing the wave, and must skip dispatch (no Wave Dispatched
	# comment) when the verifier returns exit 1.
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	# Gate block markers.
	assert "Wave-dispatch integration-state gate" in script, (
		"poller must contain the wave-dispatch integration-state gate block"
	)
	assert "WAVE_GATE_BLOCKED" in script, (
		"poller must use the WAVE_GATE_BLOCKED flag to skip dispatch on violation"
	)
	# Verifier invocation with --ref against the integration branch.
	assert "python3 scripts/verify_integration_fingerprints.py" in script
	assert '--ref "${_gate_ref}"' in script, (
		"wave-dispatch gate must run the verifier in --ref mode against the "
		"integration branch HEAD (not the cwd working tree)"
	)
	assert 'git rev-parse --verify FETCH_HEAD' in script, (
		"wave-dispatch gate must pin the freshly fetched integration ref to a commit SHA before verification"
	)
	assert '_gate_ref="${_gate_integration_branch}"' not in script, (
		"wave-dispatch gate must not fall back to a potentially stale local integration branch when origin is unavailable"
	)
	assert "fetch of integration branch" in script, (
		"wave-dispatch gate must warn and fail open when the integration-branch fetch fails"
	)
	assert 'elif [ "${_gate_exit}" -eq 2 ]; then' in script, (
		"wave-dispatch gate must surface verifier plumbing failures with an explicit warning"
	)
	assert "verifier exited 2 (plumbing failure)" in script, (
		"wave-dispatch gate warning must explain the fail-open verifier exit-2 path"
	)
	assert 'elif [ "${_gate_exit}" -ne 0 ]; then' in script, (
		"wave-dispatch gate must warn on unexpected verifier exits instead of silently treating them as passes"
	)
	assert "verifier exited ${_gate_exit} (unexpected)" in script, (
		"wave-dispatch gate warning must surface unexpected verifier exit codes while preserving fail-open dispatch"
	)
	assert "trap 'rm -f \"${_gate_fp_file:-}\" \"${_gate_log_file:-}\" 2>/dev/null || true' EXIT" in script, (
		"wave-dispatch gate must protect temp-file cleanup with an EXIT trap"
	)
	assert script.index(
		"trap 'rm -f \"${_gate_fp_file:-}\" \"${_gate_log_file:-}\" 2>/dev/null || true' EXIT"
	) < script.index('_gate_fp_file="$(mktemp'), (
		"wave-dispatch gate must register cleanup before allocating temp files"
	)
	assert 'trap - EXIT' in script, (
		"wave-dispatch gate must clear its temporary EXIT trap after explicit cleanup"
	)
	# Failure-path side effects: stall cycle bump, tracking comment,
	# telegram alert.
	assert "Wave ${NEXT_WAVE} dispatch BLOCKED" in script
	assert "wave ${NEXT_WAVE} dispatch blocked" in script
	# The dispatch block must be gated on WAVE_GATE_BLOCKED so the
	# CREATED_NUMS / ACTUALLY_CREATED_COUNT bookkeeping only runs when
	# the gate passes.
	assert 'if [ "${WAVE_GATE_BLOCKED}" = "true" ]; then' in script


def test_actions_runs_shared_loader_reuses_single_fetch_per_tick() -> None:
	state = _base_state()
	state["project_status"] = "in_progress"
	state["issues"] = [
		{
			"id": "issue-one",
			"title": "Issue one",
			"github_issue": 10,
			"status": "in_progress",
			"phase": "implementing",
			"last_updated": "2026-01-01T00:00:00Z",
		}
	]
	run = {
		"id": 1001,
		"status": "in_progress",
		"conclusion": None,
		"name": "Internal Review",
		"workflow_id": 11,
		"created_at": "2026-01-01T00:00:00Z",
		"updated_at": "2026-01-01T00:01:00Z",
		"run_started_at": "2026-01-01T00:00:30Z",
		"head_branch": "ai/issue-10",
		"event": "pull_request",
		"run_attempt": 1,
		"html_url": "https://example.invalid/runs/1001",
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-managed", "ai:in-progress"],
		issue_labels={10: ["ai:managed", "ai:implementing"]},
		issue_linked_prs={10: 351},
		prs=[
			{
				"number": 351,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"baseRefName": "main",
				"headSha": "sha-10",
			}
		],
		validation_workflow_runs=[run],
	)
	assert result["actions_runs_fetch_count"] == 3


def test_actions_runs_cached_loader_uses_if_none_match_when_stale() -> None:
	state = _base_state()
	state["project_status"] = "in_progress"
	state["issues"] = [
		{
			"id": "issue-one",
			"title": "Issue one",
			"github_issue": 10,
			"status": "in_progress",
			"phase": "implementing",
			"last_updated": "2026-01-01T00:00:00Z",
		}
	]
	run = {
		"id": 1001,
		"status": "in_progress",
		"conclusion": None,
		"name": "Internal Review",
		"workflow_id": 11,
		"created_at": "2026-01-01T00:00:00Z",
		"updated_at": "2026-01-01T00:01:00Z",
		"run_started_at": "2026-01-01T00:00:30Z",
		"head_branch": "ai/issue-10",
		"event": "pull_request",
		"run_attempt": 1,
		"html_url": "https://example.invalid/runs/1001",
	}
	cached_payload = {
		"schema_version": "v1",
		"repository": "owner/repo",
		"fetched_at": "2024-01-01T00:00:00Z",
		"ttl_seconds": 60,
		"etag": '"etag-old"',
		"runs": [
			run,
			{
				"id": 1002,
				"status": "completed",
				"conclusion": "success",
				"name": "Internal Review",
				"workflow_id": 11,
				"created_at": "2026-01-01T00:00:10Z",
				"updated_at": "2026-01-01T00:01:10Z",
				"run_started_at": "2026-01-01T00:00:40Z",
				"head_branch": "ai/issue-10",
				"event": "pull_request",
				"run_attempt": 1,
				"html_url": "https://example.invalid/runs/1002",
			},
		],
	}
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		tracking_labels=["ai:orchestrator-managed", "ai:in-progress"],
		issue_labels={10: ["ai:managed", "ai:implementing"]},
		issue_linked_prs={10: 351},
		prs=[
			{
				"number": 351,
				"state": "open",
				"mergeable": True,
				"mergeable_state": "clean",
				"headRefName": "ai/issue-10",
				"headRefFromApi": "ai/issue-10",
				"baseRefName": "main",
				"headSha": "sha-10",
			}
		],
		validation_workflow_runs=[run],
		mock_actions_runs_cache_get_json={"ok": True, "enabled": True, "hit": True, "cache": cached_payload},
		actions_runs_workflow_runs=[run],
		actions_runs_status=304,
	)
	assert result["actions_runs_fetch_count"] == 1
	assert result["actions_runs_if_none_match_count"] >= 1


def test_resolver_tooling_refresh_allowlist_includes_both_retry_preludes():
	# scripts/orchestrate_poll_process.sh has a resolver-tooling refresh
	# allowlist (the `refresh_files=( ... )` array) that controls which
	# files are re-pulled from default_branch onto the orchestrator's
	# integration-sync working copy.  Both the standard reflexion prelude
	# and the timeout-aware reflexion prelude must be in this list — if
	# either is missing, consumer repos pinning to @stable would not pick
	# up the prelude after a release and the resolver retry loop would
	# silently fall back to "retry with original prompt verbatim" on the
	# specific failure class that the missing prelude was supposed to
	# handle.  Added after PR #2453's claude-branch-review flagged that
	# the new timeout prelude needed an explicit pin against future
	# refactors silently dropping it.
	poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
	# Narrow the assertion to the substring between `refresh_files=(`
	# and its matching closing `)` so the test actually enforces array
	# membership — not "path appears anywhere in the file" (which would
	# false-positive if a future refactor moved the path to a comment
	# or echo while dropping it from the allowlist).  There is only one
	# `refresh_files=(` declaration in the file, so a simple
	# split-from-the-marker / slice-to-next-`)` works without a full
	# bash parser.
	open_marker = "refresh_files=("
	open_idx = poller_body.find(open_marker)
	assert open_idx != -1, (
		f"{open_marker} ... ) array missing from "
		"scripts/orchestrate_poll_process.sh; the resolver-tooling "
		"refresh path has been removed or renamed."
	)
	# Closing `)` of the array is the first `)` that appears on its
	# own line (possibly indented).  This matches the existing array
	# style and avoids matching `)` characters that occur inside
	# comments or quoted paths within the array body.
	close_re = re.compile(r"^\s*\)\s*$", re.MULTILINE)
	close_match = close_re.search(poller_body, pos=open_idx + len(open_marker))
	assert close_match is not None, (
		f"could not find closing `)` for the {open_marker} array in "
		"scripts/orchestrate_poll_process.sh; the array literal is "
		"either malformed or its closing brace style changed."
	)
	array_body = poller_body[open_idx + len(open_marker): close_match.start()]
	for tpl in (
		"prompts/integration-sync-conflict-resolver-retry-prelude.txt",
		"prompts/integration-sync-conflict-resolver-retry-timeout-prelude.txt",
	):
		assert tpl in array_body, (
			f"{tpl} missing from the refresh_files=( ... ) allowlist "
			"body in scripts/orchestrate_poll_process.sh (not just "
			"absent from the file as a whole — the path must be a "
			"member of the array). Consumer repos pinning @stable "
			"would not pick up this prelude after a release, "
			"silently disabling the corresponding retry-reflexion path."
		)
	# Defence-in-depth: the matching workflow-side bootstrap must also
	# stage the timeout-prelude file so the script_ref pin path mirrors
	# the orchestrator refresh path.  PR #3191 extracted the inline
	# support-staging block out of review_autofix.yml into
	# scripts/stage_workflow_support.sh (to stay under GitHub Actions'
	# per-step expression-template limit), so the `install -m 0644` line
	# now lives in that script — but review_autofix.yml must still invoke
	# it.  Anchor the staging assertion on the actual `install -m 0644`
	# line in the helper script (matching the bare filename anywhere
	# would false-positive if the string remained only in an
	# `echo "::warning::..."` line while the `install` line was removed
	# or altered) AND verify the workflow wires the helper, so neither
	# half of the mirror can be silently dropped.  The signature
	# `install -m 0644 ... ${SUPPORT_PROMPTS_DIR}/<file>` is the exact
	# shape used by every prompt-staging block in the helper.
	wf_body = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	stage_helper_body = (REPO_ROOT / "scripts" / "stage_workflow_support.sh").read_text(encoding="utf-8")
	wf_lines = wf_body.splitlines()
	timeout_prelude_install_re = re.compile(
		r"install -m 0644 [^\n]*\$\{SUPPORT_PROMPTS_DIR\}/integration-sync-conflict-resolver-retry-timeout-prelude\.txt",
	)
	helper_line_idx = next(
		(
			idx
			for idx, line in enumerate(wf_lines)
			if re.search(r'helper="[^"\n]*stage_workflow_support\.sh"', line)
		),
		None,
	)
	assert timeout_prelude_install_re.search(stage_helper_body) is not None, (
		"scripts/stage_workflow_support.sh does not stage the timeout-prelude "
		"template via the expected `install -m 0644 ... ${SUPPORT_PROMPTS_DIR}/"
		"integration-sync-conflict-resolver-retry-timeout-prelude.txt` "
		"signature; consumer-repo runs whose pinned script_ref includes "
		"the new prelude file would still hit a missing-template "
		"::warning:: at runtime."
	)
	assert helper_line_idx is not None, (
		"review_autofix.yml does not define a helper pointing at "
		"stage_workflow_support.sh; the workflow-side bootstrap that stages "
		"the resolver retry preludes is unwired."
	)
	next_step_idx = next(
		(
			idx
			for idx, line in enumerate(wf_lines[helper_line_idx + 1 :], start=helper_line_idx + 1)
			if re.match(r"^\s*-\s+name:", line)
		),
		len(wf_lines),
	)
	assert any(
		re.search(r'^\s*bash "\$\{helper\}"$', line)
		for line in wf_lines[helper_line_idx + 1 : next_step_idx]
	), (
		"review_autofix.yml no longer wires scripts/stage_workflow_support.sh via "
		"the expected helper-assignment + same-step `bash \"${helper}\"` sequence; "
		"the workflow-side bootstrap that stages the resolver retry preludes "
		"(including the timeout prelude) is unwired, so the script_ref pin "
		"path no longer mirrors the orchestrator refresh path."
	)


def test_external_finalize_detect_marks_project_complete_when_final_pr_already_merged():
	# Regression guard for the orchestrator/project-2734 wave-2 dispatch
	# loop (issue #2734): the orchestrator created the final integration
	# PR (`final_merge_pr=2750`) eagerly via the self-healing pipeline,
	# an operator squash-merged it at 2026-05-18T21:31:50Z, but
	# `final_merge_status` stayed `pending` because
	# `finalize_integration_merge_if_needed` is only invoked from the
	# `merge_conflict` and judge-`complete` arms — never from the plain
	# `in_progress` arm.  The poller kept cycling on the wave-dispatch
	# gate (which fired Telegram `Wave 2 dispatch BLOCKED` alerts every
	# ~30 min) and never noticed PR #2750 had merged.
	#
	# The fix is an external-finalize detect block placed AFTER
	# `TRACKING_LABELS` is fetched and BEFORE
	# `sync_default_into_integration_branch` in the orchestrator's
	# per-tracking-issue loop: read `final_merge_pr` +
	# `final_merge_status` from state, fetch the PR once via the shared
	# `_fetch_pr_json` + `_jq_field` helper path, and on confirmed
	# closed-and-merged, transition status to `complete` before the sync
	# path can mark a deleted integration branch as failed. The dedicated
	# `merge_conflict`, `validating`, and `validation-fixing` paths still
	# own their validation/finalize bookkeeping. This test pins the
	# placement, the gate condition, AND the state mutation shape
	# (status=complete + final_merge_status=merged) so a future refactor
	# cannot silently drop any of the three.
	poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
	sync_marker = '    if ! sync_default_into_integration_branch "${INTEGRATION_BRANCH_TRACKING}" "${DEFAULT_BRANCH_TRACKING}"; then'
	sync_idx = poller_body.find(sync_marker)
	assert sync_idx != -1, (
		"sync_default_into_integration_branch call is missing from the "
		"per-tracking-issue loop; the external-finalize block must precede "
		"that sync path so deleted integration branches cannot preempt the "
		"merged-PR recovery."
	)
	# Anchor on the `TRACKING_LABELS=` fetch that precedes the
	# external-finalize block. Several helpers reuse the same assignment,
	# so search backward from the sync call and take the nearest preceding
	# occurrence — that is the per-tracking-issue-loop landmark this
	# regression is pinning.
	anchor = '  TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"'
	anchor_idx = poller_body.rfind(anchor, 0, sync_idx)
	assert anchor_idx != -1, (
		"TRACKING_LABELS=... fetch (the documented insertion landmark for "
		"the external-finalize detect block) is missing or renamed in "
		"scripts/orchestrate_poll_process.sh; update this test to match "
		"the new landmark."
	)
	# Bound the search window to the block between the TRACKING_LABELS
	# fetch and the sync call so the assertion enforces *placement*, not
	# just "appears anywhere in the file" (which would false-positive if
	# a refactor moved the block back below the sync path and reintroduced
	# the deleted-branch failure).
	merge_conflict_marker = 'if [ "${PROJECT_STATUS}" = "merge_conflict" ]; then'
	merge_conflict_idx = poller_body.find(merge_conflict_marker, sync_idx)
	assert merge_conflict_idx != -1, (
		'merge_conflict switch `if [ "${PROJECT_STATUS}" = "merge_conflict" ]` '
		"is missing from the per-tracking-issue loop; the sync block must "
		"still flow into the later merge_conflict arm after the external-"
		"finalize pre-check."
	)
	window = poller_body[anchor_idx:sync_idx]
	# Gate condition: only fire when state is non-terminal, not already
	# on the dedicated `merge_conflict` / validation-completion paths,
	# a final PR is pinned, and `final_merge_status` is still `pending`.
	# Each guard protects a different failure mode (terminal states
	# already handled, `merge_conflict`/validated states need their own
	# finalize path, no PR pinned yet means orchestrator hasn't even
	# created the integration PR, non-pending status means another code
	# path already finalized).
	for needle, why in (
		('.final_merge_pr // empty', "external-finalize block no longer reads `.final_merge_pr` from state; without it the block has no PR to inspect."),
		('.final_merge_status // "pending"', "external-finalize block no longer reads `.final_merge_status` from state; without it the block cannot tell pending from already-finalized projects and would re-finalize on every tick."),
		('!= "merge_conflict"', "external-finalize block no longer excludes `merge_conflict`; without that guard the block steals work from the dedicated finalize/validation path and duplicates final-PR reads on every merge_conflict poll tick."),
		('!= "validating"', "external-finalize block no longer excludes `validating`; without that guard an externally merged final PR bypasses `mark_validation_complete` and drops `validation_completed_cycle` / `ai:validated` on the final validation-complete tick."),
		('!= "validation-fixing"', "external-finalize block no longer excludes `validation-fixing`; without that guard the shortcut can bypass the validation-completion path that owns the final validated-state transition."),
		('= "pending"', "external-finalize block no longer guards on `final_merge_status = pending`; without this guard the block would re-fire after the orchestrator's own finalize path already ran."),
		("'.state'", "external-finalize block no longer reads the pinned PR's `.state` field; without it the block cannot tell open from closed PRs."),
		("'.merged_at != null'", "external-finalize block no longer reads the pinned PR's `.merged_at` field; without it the block would mis-treat a closed-without-merge PR as a successful finalize."),
	):
		assert needle in window, (
			f"external-finalize detect block in scripts/orchestrate_poll_process.sh "
			f"no longer contains `{needle}` between the TRACKING_LABELS fetch and "
			f"the sync_default_into_integration_branch call — {why}"
		)
	assert window.count('_fetch_pr_json "${_orch_extfin_pr}"') == 1, (
		"external-finalize detect block no longer uses a single PR fetch before "
		"the sync_default_into_integration_branch call; duplicate "
		"PR fetches reintroduce avoidable API churn and a narrow "
		"state/merged-at race."
	)
	# State mutation shape: must set BOTH status=complete AND
	# final_merge_status=merged in the same jq pass.  Setting only one
	# would leave the other path inconsistent: status=complete without
	# final_merge_status=merged would re-trigger finalize on the next
	# tick (wasting API budget); final_merge_status=merged without
	# status=complete would leave the project stuck in `in_progress`
	# and the wave-dispatch loop running.
	for needle, why in (
		('.status = "complete"', "external-finalize block no longer transitions project status to `complete`; without this the project stays in_progress and the wave-dispatch loop keeps firing on every tick."),
		('.final_merge_status = "merged"', "external-finalize block no longer marks `final_merge_status = merged`; finalize_integration_merge_if_needed's early-return check would re-enter the merge flow on the next tick if it were ever called."),
	):
		assert needle in window, (
			f"external-finalize detect block no longer performs the state "
			f"mutation `{needle}` before the sync_default_into_integration_branch "
			f"call — {why}"
		)
	# Side-effect surface: tracking comment + Telegram cleanup must
	# both fire so the operator's open `Wave dispatch BLOCKED` alerts
	# on the tracking issue + Telegram channel are explicitly
	# superseded.  Without these the user sees stale BLOCKED comments
	# at the top of the tracking issue indefinitely.
	for needle, why in (
		('post_tracking_comment', "external-finalize block no longer posts a tracking comment explaining the external-merge transition; operators would have to guess why the project suddenly went quiet."),
		('tg_cleanup_msgs', "external-finalize block no longer calls tg_cleanup_msgs so prior `Wave dispatch BLOCKED` Telegram alerts stay pinned in the channel after the project completes — defeats half of the user-visible silence."),
		('set_tracking_phase_label "ai:merged"', "external-finalize block no longer sets the `ai:merged` phase label, so downstream label-driven automation (release callbacks, label-repair sweeps) cannot detect that this project is done."),
	):
		assert needle in window, (
			f"external-finalize detect block no longer triggers `{needle}` "
			f"in its side-effect block — {why}"
		)
	assert re.search(r'PROJECT_STATUS="complete"\s+continue', window) is not None, (
		"external-finalize detect block no longer short-circuits after setting "
		"`PROJECT_STATUS=complete`; without the `continue`, the same tick falls "
		"through to the terminal-state skip block and logs a misleading "
		"`Project already complete, skipping.` line."
	)


def test_external_finalize_gates_on_subissue_completeness_before_marking_complete():
	# Regression guard for the project #2734 false-completion broadcast
	# (docs/postmortems/2026-05-18-project-2734-stall.md, layer 6).  The
	# external-finalize block added by PR #2777 transitioned status=complete
	# whenever the pinned integration PR was closed+merged, without checking
	# whether the sub-issues in `.waves[].issues[]` had actually shipped.
	# Project #2734's integration PR (#2750) was squash-merged externally
	# with only Wave 1 (2 of 9 sub-issues) merged; on the next poll tick
	# the unconditional transition broadcast "✅ Project complete" with 7
	# sub-issues never created.
	#
	# The fix: before mutating state, count sub-issues whose status is not
	# in {merged, closed, skipped}. If any are non-terminal, emit a
	# structured `[external-finalize-partial]` warning, fire one Telegram
	# alert per distinct missing-issue set (dedup via sha256 of the
	# missing set, persisted on the state file as
	# `external_finalize_partial_alert_sig`), and `continue` to skip the
	# rest of this tick's processing for this project.  No state mutation.
	#
	# This test pins the placement (inside the elif-closed-and-merged arm,
	# BEFORE the jq state mutation), the gate condition (filter shape
	# matching the established `status != merged/closed/skipped` pattern
	# used elsewhere in the script), the dedup mechanism (sha256 signature
	# of the missing set), the early-exit (`continue` before any state
	# mutation runs), and the side-effect surface (the
	# `[external-finalize-partial]` warning marker and the WARNING-level
	# tg_notify so the operator can take action).
	poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
	# Anchor the window on the existing landmarks from
	# test_external_finalize_detect_marks_project_complete_when_final_pr_already_merged
	# so a refactor that moves the block also has to update both tests in
	# lockstep — preventing one test from regressing silently.
	elif_marker = (
		'elif [ "${_orch_extfin_pr_state}" = "closed" ] && '
		'[ "${_orch_extfin_pr_merged}" = "true" ]; then'
	)
	elif_idx = poller_body.find(elif_marker)
	assert elif_idx != -1, (
		"external-finalize elif (closed+merged) marker is missing from "
		"scripts/orchestrate_poll_process.sh; the completeness gate must "
		"live inside that arm, so the elif itself is required."
	)
	mutation_marker = "'.final_merge_pr = $final_pr"
	mutation_idx = poller_body.find(mutation_marker, elif_idx)
	assert mutation_idx != -1, (
		"external-finalize state mutation (jq `.final_merge_pr = $final_pr` "
		"line) is missing after the closed+merged elif; the completeness gate "
		"must precede that mutation."
	)
	gate_window = poller_body[elif_idx:mutation_idx]
	# Gate condition: must enumerate non-terminal sub-issues using the same
	# filter shape the rest of the script already uses for terminal-success
	# detection.  If a future refactor switches to a different filter shape
	# (e.g. flips the negation or drops `skipped`), this assertion catches
	# it before the gate starts mis-classifying skipped issues as missing.
	for needle, why in (
		(
			'.waves[]?.issues[]?',
			"completeness gate no longer iterates `.waves[]?.issues[]?`; without it the gate cannot inspect sub-issue states and would always treat the project as complete.",
		),
		(
			'select(.status == "not_created")',
			"completeness gate no longer filters on `status == \"not_created\"`; the gate must catch sub-issues that were never dispatched (the project #2734 failure mode) without false-positive-firing on sub-issues that have a github_issue and are merely behind the per-tick label reconciliation. Widening the filter without also reconciling sub-issue status from labels first would break the legitimate external-finalize tests that pass with a `pending` sub-issue whose linked PR is already merged.",
		),
		(
			'_orch_extfin_incomplete_count',
			"completeness gate no longer computes `_orch_extfin_incomplete_count`; without the count variable the `-gt 0` guard cannot fire and the partial-merge case is silently complete.",
		),
		(
			'-gt 0',
			"completeness gate no longer gates on `_orch_extfin_incomplete_count -gt 0`; without this guard either every project is treated as incomplete (false positive) or none are (false negative).",
		),
		(
			'[external-finalize-partial]',
			"completeness gate no longer emits the `[external-finalize-partial]` marker; downstream log-analysis tooling and the postmortem (docs/postmortems/2026-05-18-project-2734-stall.md) anchor on this exact marker.",
		),
		(
			'external_finalize_partial_alert_sig',
			"completeness gate no longer persists `external_finalize_partial_alert_sig` on the state file; without this dedup key the gate would fire a Telegram alert every poll tick (~12/hour) until the project is completed or de-scoped — alert fatigue is the exact failure mode the postmortem's layer 5 calls out.",
		),
		(
			'tg_notify',
			"completeness gate no longer fires `tg_notify` on first-detection of an incomplete external-finalize; without the alert the operator has no way to learn the project is stuck except by reading workflow logs.",
		),
		(
			'"WARNING"',
			"completeness gate no longer escalates the Telegram alert to WARNING level; without the WARNING tag the alert blends into INFO chatter and is easily missed.",
		),
		(
			'continue',
			"completeness gate no longer short-circuits with `continue` on incomplete detection; without it the gate would warn and then fall through to the state-mutation block, producing the exact false-completion broadcast the gate is meant to prevent.",
		),
	):
		assert needle in gate_window, (
			"external-finalize completeness gate in scripts/orchestrate_poll_process.sh "
			f"no longer contains `{needle}` between the closed+merged elif and the "
			f"state-mutation jq call — {why}"
		)
	# Hard requirement: no jq mutation that sets `.status = "complete"`
	# may appear inside the gate window itself.  The gate's only mutation
	# is the alert-dedup signature persist; any other state change would
	# bypass the wave-by-wave finalize path that owns project-status
	# transitions.
	# A subtlety: jq operations on `.external_finalize_partial_alert_sig`
	# are fine; what matters is that `.status = "complete"` (or
	# `.final_merge_status = "merged"`) does not land before the
	# `continue`.  Verify the gate window does NOT contain those terminal
	# transitions.
	assert '.status = "complete"' not in gate_window, (
		"external-finalize completeness gate must NOT transition `.status` to `complete` "
		"itself; that mutation belongs to the post-gate fall-through path which only "
		"runs when every sub-issue is terminal-success."
	)
	assert '.final_merge_status = "merged"' not in gate_window, (
		"external-finalize completeness gate must NOT mark `.final_merge_status = merged` "
		"itself; that mutation belongs to the post-gate fall-through path."
	)


def test_review_autofix_workflow_wires_optional_verifier_bootstrap_and_gate():
	# The resolver run: blocks were extracted into
	# scripts/review_conflict_prepare.sh and
	# scripts/review_conflict_resolve.sh by PR #1495 to stay under
	# GitHub Actions' 21,000-char per-step expression-template limit.
	# The workflow now bootstraps the support scripts and invokes them;
	# the integration-sync detection, template selection, and fingerprint
	# verifier gating live inside those scripts. This test verifies the
	# full wiring across workflow + scripts.
	wf_path = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
	stage_helper_path = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
	prepare_path = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
	resolve_path = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
	wf_body = wf_path.read_text(encoding="utf-8")
	stage_helper_body = stage_helper_path.read_text(encoding="utf-8")
	prepare_body = prepare_path.read_text(encoding="utf-8")
	resolve_body = resolve_path.read_text(encoding="utf-8")
	# Resolver safety scripts must prefer the main snapshot so wedged
	# integration branches still pick up the shipped self-heal helpers.
	assert '.codex-workflow-src/scripts/stage_workflow_support.sh' in wf_body
	assert '.codex-workflow-src-main/scripts/stage_workflow_support.sh' in wf_body
	assert (
		'MAIN_PRIMARY_BOOTSTRAP_SCRIPTS="verify_integration_fingerprints.py review_conflict_resolve.sh '
		'review_conflict_prepare.sh render_prompt.py"'
	) in stage_helper_body
	assert 'SUPPORT_ROOT_DIR="${RUNNER_TEMP}/coding-workflows-runtime-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in stage_helper_body
	assert 'SUPPORT_SCRIPTS_DIR="${SUPPORT_ROOT_DIR}/scripts"' in stage_helper_body
	assert 'SUPPORT_SCRIPTS_DIR="scripts"' not in stage_helper_body
	# render_prompt.py was moved out of OPTIONAL_BOOTSTRAP_SCRIPTS into
	# MAIN_PRIMARY_BOOTSTRAP_SCRIPTS by PR #3594 so main-side validator fixes
	# reach wedged branches (see the dedicated contract test in
	# tests/test_review_autofix_review_pipeline_contract.py); keep this list in
	# sync with review_autofix.yml and stage_workflow_support.sh.
	assert 'OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh build_semble_wrapper.sh semble_helpers.sh"' in stage_helper_body
	assert "for f in ${MAIN_PRIMARY_BOOTSTRAP_SCRIPTS}; do" in stage_helper_body
	assert "Bootstrapped ${f} from main snapshot (branch copy ignored)." in stage_helper_body
	# The bootstrap still enumerates the script name in review_autofix.yml
	# even after PR #1495 moved the resolver logic into support scripts.
	assert "verify_integration_fingerprints.py" in stage_helper_body
	# The workflow must invoke the extracted prepare + resolve scripts so
	# the integration-sync gate and fingerprint verifier actually run.
	assert "review_conflict_prepare.sh" in wf_body
	assert "review_conflict_resolve.sh" in wf_body
	# The prepare script selects the integration template on
	# orchestrator/project-* head refs and exports IS_INTEGRATION_SYNC.
	assert "orchestrator/project-*" in prepare_body
	assert "integration-sync-conflict-resolver.txt" in prepare_body
	assert "IS_INTEGRATION_SYNC" in prepare_body
	# The resolve script dispatches the verifier under the
	# IS_INTEGRATION_SYNC gate and aborts the [ai-merge-resolve] commit
	# when fingerprint verification rejects the resolver output.
	assert "IS_INTEGRATION_SYNC" in resolve_body
	assert "verify_integration_fingerprints.py" in resolve_body
	assert "--baseline-fingerprints-state" in resolve_body
	assert "--compare-against-baseline" in resolve_body
	assert "Aborting [ai-merge-resolve] commit: integration fingerprint verification" in resolve_body


def _extract_refresh_function_body(poller_body: str) -> str:
	"""Return the full text of `_refresh_integration_resolver_tooling`
	(including the closing `}`) from the poller script body by
	matching the function's outer brace pair. Standalone inner `{` / `}`
	command-group braces, operator-attached groups like `cmd && {`, and
	nested shell function definitions should not truncate the extracted
	body.
	"""
	lines = poller_body.splitlines()
	open_marker = "_refresh_integration_resolver_tooling() {"
	try:
		start_idx = next(i for i, line in enumerate(lines) if line == open_marker)
	except StopIteration:
		raise AssertionError(
			f"{open_marker!r} not found in scripts/orchestrate_poll_process.sh — "
			"function was renamed or removed."
		) from None
	depth = 1
	nested_fn_open_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{$")
	command_group_open_re = re.compile(
		r"^(?:\{|(?:[^#\"']|\"[^\"]*\"|'[^']*')*(?:&&|\|\||;|\|)\s*\{)\s*(?:#.*)?$"
	)
	for idx in range(start_idx + 1, len(lines)):
		stripped = lines[idx].strip()
		if nested_fn_open_re.match(stripped) or command_group_open_re.match(stripped):
			depth += 1
		elif stripped == "}":
			depth -= 1
			if depth == 0:
				return "\n".join(lines[start_idx:idx + 1])
	raise AssertionError(
		"closing brace for _refresh_integration_resolver_tooling not found"
	)


def test_extract_refresh_function_body_keeps_operator_attached_command_groups():
	sample = """_refresh_integration_resolver_tooling() {
	  echo start
	  test -n \"${x:-}\" && {
	    echo inner
	  }
	  echo after  # note | {
	}
	after() {
	  :
	}"""
	expected = """_refresh_integration_resolver_tooling() {
	  echo start
	  test -n \"${x:-}\" && {
	    echo inner
	  }
	  echo after  # note | {
	}"""
	assert _extract_refresh_function_body(sample) == expected


def test_resolver_tooling_refresh_function_has_merge_base_divergence_guard():
	# Static contract: _refresh_integration_resolver_tooling must
	# compute the merge-base of integration_branch vs default_branch
	# once before the per-file loop, and skip any file whose
	# integration-branch hash differs from its merge-base hash.
	#
	# Without this guard, the [ai-maint] resolver-tooling refresh
	# silently reverts files that merged sub-issue PRs intentionally
	# modified (e.g. PR #2738 landing the Phase 1A baseline/delta
	# verifier into scripts/verify_integration_fingerprints.py). The
	# post-resolve merged sub-issue fingerprint verifier then flags
	# those reverts as a contract violation and the orchestrator
	# refuses to dispatch the next wave. That is exactly the failure
	# documented in issue #2734.
	poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
	fn_body = _extract_refresh_function_body(poller_body)

	assert "git merge-base" in fn_body, (
		"_refresh_integration_resolver_tooling must run `git merge-base` "
		"before the per-file refresh loop (regression guard for issue #2734)."
	)
	# Merge-base must be computed against the integration AND default
	# branch refs, not just one of them.
	for ref in (
		"refs/remotes/origin/${integration_branch}",
		"refs/remotes/origin/${default_branch}",
	):
		assert ref in fn_body, (
			f"_refresh_integration_resolver_tooling's merge-base resolution "
			f"must reference {ref!r}; without it the divergence check would "
			f"compare against the wrong baseline."
		)
	# Per-file tree-path lookups must use --verify/--quiet so a missing
	# path resolves to an empty string rather than the literal REV:PATH
	# token on stdout.
	for lookup in (
		'main_hash="$(git rev-parse --verify --quiet "refs/remotes/origin/${default_branch}:${f}"',
		'int_hash="$(git rev-parse --verify --quiet "HEAD:${f}"',
		'base_hash="$(git rev-parse --verify --quiet "${merge_base}:${f}"',
	):
		assert lookup in fn_body, (
			"_refresh_integration_resolver_tooling must use `git rev-parse "
			"--verify --quiet` for every tree-path hash lookup so missing "
			"files produce an empty string instead of a literal REV:PATH token."
		)
	# The function must `continue` (skip refresh) when the integration
	# branch's hash for the file diverges from the merge-base hash —
	# this is the actual guard that prevents the issue #2734
	# regression. Match the conditional and the `continue` together so
	# the assertion fails if a future refactor accidentally drops the
	# `continue` while keeping the comparison.
	diverge_skip_re = re.compile(
		r'if\s+\[\s+"\$\{int_hash\}"\s+!=\s+"\$\{base_hash\}"\s+\]\s*;\s*then'
		r'[\s\S]*?\bcontinue\b',
	)
	assert diverge_skip_re.search(fn_body), (
		"_refresh_integration_resolver_tooling must `continue` (skip the "
		"checkout/refresh) when int_hash differs from base_hash. Without "
		"this, the function silently reverts files that a merged sub-issue "
		"PR has modified on the integration branch — see issue #2734."
	)


def test_resolver_tooling_refresh_function_has_3way_merge_fallback():
	# Static contract for P5 (docs/postmortems/2026-05-18-project-2734-stall.md).
	# When BOTH the integration branch and main have changed an
	# allowlisted file since the merge-base, PR #2760's divergence
	# guard skipped the refresh — preserving the sub-issue PR change
	# but losing the main-side toolchain fix. P5 layers a 3-way merge
	# fallback alongside the guard: if both sides changed but the
	# changes don't conflict, combine them; only fall back to skip on
	# real conflicts.
	#
	# This test pins:
	#   - presence of the merged_3way_count counter,
	#   - the `git merge-file` invocation,
	#   - the conditional that distinguishes "main unchanged" (skip)
	#     from "both changed" (try 3-way merge),
	#   - that the 3-way path stages via `git add` (so it lands in the
	#     refresh commit alongside the deadlock-breaker checkout path),
	#   - the conflict fallback (skip on non-zero `git merge-file` exit).
	poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
	fn_body = _extract_refresh_function_body(poller_body)

	assert "merged_3way_count=0" in fn_body, (
		"_refresh_integration_resolver_tooling must initialize merged_3way_count "
		"alongside refreshed_count / skipped_count / drifted_count. Without the "
		"counter the post-refresh summary cannot distinguish 3-way merges from "
		"deadlock-breaker checkouts."
	)
	assert ': > "${merge_tmpdir}/base"' not in fn_body, (
		"_refresh_integration_resolver_tooling must not fabricate an empty 3-way "
		"merge base on `git cat-file` failure. A zero-byte ancestor silently "
		"degrades the merge and can lose the true merge-base context."
	)
	assert "could not materialize one or more 3-way merge inputs" in fn_body, (
		"_refresh_integration_resolver_tooling must warn and skip when any 3-way "
		"merge input blob cannot be materialized. Without this guard the function "
		"can run `git merge-file` with missing or stale tmpfile inputs."
	)
	assert "git merge-file" in fn_body, (
		"_refresh_integration_resolver_tooling must call `git merge-file` to do "
		"the 3-way merge of integration + main edits when both sides changed "
		"since the merge-base. Without this the function falls back to PR #2760's "
		"skip behavior and the toolchain fix never reaches the integration branch."
	)
	# The skip sub-case (main unchanged) must come BEFORE the 3-way
	# merge sub-case so the cheap test runs first.
	skip_subcase_re = re.compile(
		r'if\s+\[\s+-z\s+"\$\{base_hash\}"\s+\]\s+\|\|\s+\[\s+"\$\{main_hash\}"\s+=\s+"\$\{base_hash\}"\s+\]\s*;\s*then'
	)
	assert skip_subcase_re.search(fn_body), (
		"3-way merge logic must short-circuit when main is unchanged from "
		"merge-base (no work to do) BEFORE attempting the merge. Without this "
		"short-circuit, the function would 3-way merge an unchanged main into "
		"the integration version, wasting work and adding noise to the refresh."
	)
	# The merge must use the standard base/int/main triple in the
	# correct argument order: `git merge-file <current> <base> <other>`
	# — `<current>` is the integration version, `<base>` the merge-base,
	# `<other>` the main version. Swapping any of these would silently
	# produce the wrong merge or pick the wrong side on conflict.
	assert '"${merge_tmpdir}/int" "${merge_tmpdir}/base" "${merge_tmpdir}/main"' in fn_body, (
		"git merge-file argument order must be `int base main` (current, base, "
		"other). Swapping the order would silently corrupt the merge — the "
		"integration version is the file we want to update in place, the "
		"merge-base is the common ancestor, main is the new content to merge in."
	)
	# Successful 3-way merge must increment merged_3way_count AND stage
	# the merged content via git add so it lands in the refresh commit.
	assert "merged_3way_count=$((merged_3way_count + 1))" in fn_body, (
		"successful 3-way merge must increment merged_3way_count so the "
		"post-refresh summary and commit-message body can report it."
	)
	assert 'git checkout -- "${f}"' in fn_body, (
		"the 3-way merge path must revert the worktree copy when `git add` fails. "
		"Without the revert, a staging failure leaves the poller worktree dirty and "
		"out of sync with the refresh commit."
	)
	cp_fail_re = re.compile(
		r'if\s+cp\s+"\$\{merge_tmpdir\}/int"\s+"\$\{f\}"[\s\S]{0,800}?\belse\b[\s\S]{0,200}?git\s+checkout\s+--\s+"\$\{f\}"'
	)
	assert cp_fail_re.search(fn_body), (
		"the 3-way merge path must revert the worktree copy when copying the merged "
		"tmpfile back into `${f}` fails. Without the revert, a partial copy can leave "
		"the poller worktree dirty even though the file was excluded from the refresh commit."
	)
	# Conflict path must fall through to `skipped_count` (matching the
	# existing skip semantics from PR #2760) — never stage a file with
	# conflict markers. The conflict path is reached when `git merge-file`
	# returns non-zero (1+ for conflicts, 255 for binary/error).
	skip_re = re.compile(
		r'git\s+merge-file[\s\S]{0,1500}?\belse\b[\s\S]{0,500}?skipped_count=\$\(\(skipped_count\s*\+\s*1\)\)'
	)
	assert skip_re.search(fn_body), (
		"3-way merge must fall back to `skipped_count += 1` when "
		"`git merge-file` returns non-zero (conflict or binary file). "
		"Without this fallback the function would either lose conflict "
		"information silently or stage a file with conflict markers."
	)


def test_resolver_tooling_refresh_does_not_clobber_files_changed_on_integration_branch():
	# Behavioural regression for issue #2734: run the real
	# _refresh_integration_resolver_tooling function against a
	# throwaway git fixture where the integration branch has its own
	# committed change to a refresh-allowlisted file (simulating a
	# merged sub-issue PR like #2738), and verify the function leaves
	# that change intact instead of overwriting it with the
	# default_branch version.
	#
	# Fixture layout:
	#   bare.git           — origin
	#   work/              — single clone with main + integration branches
	#     scripts/check_resolver_diff.sh   ← refresh-allowlisted file
	#
	# Timeline:
	#   1. main has 'main v1'.
	#   2. orchestrator/project-99 branched off main, then committed
	#      'sub-issue v1' (the merged sub-issue change we must preserve).
	#   3. main bumped to 'main v2' (so per-file hashes differ — the
	#      legacy hash-only check would otherwise refresh).
	#   4. Run the real refresh function with cwd=work and origin
	#      pointing at bare.git.
	# Expected: the integration branch's file is still 'sub-issue v1'.
	tmp_root = Path(tempfile.mkdtemp(prefix="refresh-noclobber-"))
	try:
		bare = tmp_root / "bare.git"
		work = tmp_root / "work"
		git_env = _git_test_env()
		subprocess.run(
			["git", "init", "--bare", "--quiet", str(bare)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		subprocess.run(
			["git", "init", "--quiet", str(work)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		for k, v in [
			("user.name", "test"),
			("user.email", "t@example.com"),
			("commit.gpgsign", "false"),
			("commit.gpgSign", "false"),
		]:
			subprocess.run(
				["git", "-C", str(work), "config", k, v],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)
		(work / "scripts").mkdir()
		# scripts/check_resolver_diff.sh is a real entry in the
		# refresh_files allowlist (see scripts/orchestrate_poll_process.sh).
		refresh_target = work / "scripts" / "check_resolver_diff.sh"
		refresh_target.write_text("# main v1\n", encoding="utf-8")

		def _git(*args: str) -> None:
			subprocess.run(
				["git", "-C", str(work), *args],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)

		_git("checkout", "-B", "main")
		_git("add", "-A")
		_git("commit", "-m", "initial main", "--quiet")
		_git("remote", "add", "origin", str(bare))
		_git("push", "-u", "origin", "main", "--quiet")
		# Integration branch with a merged sub-issue change to the
		# allowlisted file.
		_git("checkout", "-B", "orchestrator/project-99")
		refresh_target.write_text("# sub-issue v1\n", encoding="utf-8")
		_git("add", "-A")
		_git("commit", "-m", "merged sub-issue PR", "--quiet")
		_git("push", "-u", "origin", "orchestrator/project-99", "--quiet")
		# Bump main with a different change so the legacy per-file
		# hash check would treat the file as "drifted" and refresh.
		_git("checkout", "main")
		refresh_target.write_text("# main v2\n", encoding="utf-8")
		_git("add", "-A")
		_git("commit", "-m", "main v2", "--quiet")
		_git("push", "origin", "main", "--quiet")

		poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
		fn_body = _extract_refresh_function_body(poller_body)

		runtime_dir = tmp_root / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		runner = tmp_root / "run.sh"
		# Use `set -uo pipefail` (no -e) so the function's own internal
		# fail-open paths (`|| true`, `|| echo ""`, `|| continue`) behave
		# the same way they do when sourced into the real poller — the
		# real poller has `set -e` at the top, but every command inside
		# the refresh function explicitly handles its own failure.
		runner.write_text(
			"#!/usr/bin/env bash\n"
			"set -uo pipefail\n"
			f"{fn_body}\n"
			f"cd {str(work)!r}\n"
			f"export RUNTIME_DIR={str(runtime_dir)!r}\n"
			"_refresh_integration_resolver_tooling orchestrator/project-99 main\n",
			encoding="utf-8",
		)
		runner.chmod(0o755)
		result = subprocess.run(
			["bash", str(runner)],
			capture_output=True, text=True, timeout=60, env=git_env,
		)
		assert result.returncode == 0, (
			"_refresh_integration_resolver_tooling fixture run failed before "
			"the no-clobber assertions could validate its output.\n"
			f"stdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}\n"
		)
		# Source-of-truth for what's actually on the integration branch
		# is the bare repo. Fetch it via the clone to read.
		subprocess.run(
			["git", "-C", str(work), "fetch", "origin", "--quiet"],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		actual = subprocess.run(
			["git", "-C", str(work), "show",
			 "origin/orchestrator/project-99:scripts/check_resolver_diff.sh"],
			capture_output=True, text=True, check=True, env=git_env,
		).stdout
		assert actual == "# sub-issue v1\n", (
			"_refresh_integration_resolver_tooling silently reverted a file "
			"the integration branch had its own committed changes to "
			"(issue #2734 regression).\n"
			f"Expected '# sub-issue v1' on the integration branch.\n"
			f"Got: {actual!r}\n"
			f"---\nrefresh stdout:\n{result.stdout}\n"
			f"---\nrefresh stderr:\n{result.stderr}\n"
		)
		# Belt-and-braces: the function should log a skip line naming
		# the file, so an operator scanning the orchestrator log can see
		# why the refresh did not advance the branch for this file.
		assert "skip scripts/check_resolver_diff.sh" in result.stdout, (
			"_refresh_integration_resolver_tooling did not log the "
			"expected skip-on-divergence message for the protected file.\n"
			f"stdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}\n"
		)
	finally:
		shutil.rmtree(tmp_root, ignore_errors=True)


def test_resolver_tooling_refresh_3way_merges_disjoint_edits_from_both_branches():
	# Behavioural test for P5 (docs/postmortems/2026-05-18-project-2734-stall.md).
	# When BOTH the integration branch and main have edited an allowlisted
	# file since the merge-base, AND the edits are in non-overlapping
	# regions, the 3-way merge fallback must combine them so neither
	# side's intent is lost.
	#
	# This is the case PR #2760's divergence guard could not handle:
	# the guard correctly preserves the sub-issue PR change but loses
	# the toolchain fix that main shipped to the same file. The P5
	# layer fills the gap by trying `git merge-file` before skipping.
	#
	# Fixture layout:
	#   bare.git           — origin
	#   work/              — single clone with main + integration branches
	#     scripts/check_resolver_diff.sh   — allowlisted, 5-line file
	#
	# Timeline:
	#   1. main has 5 lines of content with `MAIN_LINE_1` at top.
	#   2. orchestrator/project-99 branched off, edited only the LAST
	#      line ("SUB_ISSUE_FOOTER").
	#   3. main edited only the FIRST line ("MAIN_LINE_1_FIXED").
	#   4. Run the real refresh function. The clean 3-way merge must
	#      yield BOTH "MAIN_LINE_1_FIXED" at top AND "SUB_ISSUE_FOOTER"
	#      at bottom on the integration branch.
	tmp_root = Path(tempfile.mkdtemp(prefix="refresh-3way-"))
	try:
		bare = tmp_root / "bare.git"
		work = tmp_root / "work"
		git_env = _git_test_env()
		subprocess.run(
			["git", "init", "--bare", "--quiet", str(bare)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		subprocess.run(
			["git", "init", "--quiet", str(work)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		for k, v in [
			("user.name", "test"),
			("user.email", "t@example.com"),
			("commit.gpgsign", "false"),
			("commit.gpgSign", "false"),
		]:
			subprocess.run(
				["git", "-C", str(work), "config", k, v],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)
		(work / "scripts").mkdir()
		refresh_target = work / "scripts" / "check_resolver_diff.sh"
		refresh_target.write_text(
			"MAIN_LINE_1\n"
			"MAIN_LINE_2\n"
			"MAIN_LINE_3\n"
			"MAIN_LINE_4\n"
			"MAIN_LINE_5\n",
			encoding="utf-8",
		)

		def _git(*args: str) -> None:
			subprocess.run(
				["git", "-C", str(work), *args],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)

		_git("checkout", "-B", "main")
		_git("add", "-A")
		_git("commit", "-m", "initial main", "--quiet")
		_git("remote", "add", "origin", str(bare))
		_git("push", "-u", "origin", "main", "--quiet")

		# Integration branches off main and edits only line 5 (the
		# footer). This simulates a merged sub-issue PR adding a comment
		# at the bottom of a toolchain script.
		_git("checkout", "-B", "orchestrator/project-99")
		refresh_target.write_text(
			"MAIN_LINE_1\n"
			"MAIN_LINE_2\n"
			"MAIN_LINE_3\n"
			"MAIN_LINE_4\n"
			"SUB_ISSUE_FOOTER\n",
			encoding="utf-8",
		)
		_git("add", "-A")
		_git("commit", "-m", "merged sub-issue: add footer", "--quiet")
		_git("push", "-u", "origin", "orchestrator/project-99", "--quiet")

		# Main edits only line 1 (the header). Simulates the toolchain
		# fix the orchestrator wants to propagate to the integration
		# branch.
		_git("checkout", "main")
		refresh_target.write_text(
			"MAIN_LINE_1_FIXED\n"
			"MAIN_LINE_2\n"
			"MAIN_LINE_3\n"
			"MAIN_LINE_4\n"
			"MAIN_LINE_5\n",
			encoding="utf-8",
		)
		_git("add", "-A")
		_git("commit", "-m", "toolchain fix", "--quiet")
		_git("push", "origin", "main", "--quiet")

		poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
		fn_body = _extract_refresh_function_body(poller_body)

		runtime_dir = tmp_root / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		runner = tmp_root / "run.sh"
		runner.write_text(
			"#!/usr/bin/env bash\n"
			"set -uo pipefail\n"
			f"{fn_body}\n"
			f"cd {str(work)!r}\n"
			f"export RUNTIME_DIR={str(runtime_dir)!r}\n"
			"_refresh_integration_resolver_tooling orchestrator/project-99 main\n",
			encoding="utf-8",
		)
		runner.chmod(0o755)
		result = subprocess.run(
			["bash", str(runner)],
			capture_output=True, text=True, timeout=60, env=git_env,
		)
		assert result.returncode == 0, (
			"_refresh_integration_resolver_tooling 3-way merge fixture run "
			"failed before the merge-result assertions could validate.\n"
			f"stdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}\n"
		)
		subprocess.run(
			["git", "-C", str(work), "fetch", "origin", "--quiet"],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		actual = subprocess.run(
			["git", "-C", str(work), "show",
			 "origin/orchestrator/project-99:scripts/check_resolver_diff.sh"],
			capture_output=True, text=True, check=True, env=git_env,
		).stdout
		# After the 3-way merge, the integration branch's file must
		# contain BOTH the main-side fix (line 1) AND the sub-issue
		# footer (line 5). Losing either side defeats the whole point of
		# the merge.
		assert "MAIN_LINE_1_FIXED" in actual, (
			"3-way merge lost the main-side toolchain fix.\n"
			f"file contents:\n{actual}\n---\n"
			f"refresh stdout:\n{result.stdout}\n"
			f"---\nrefresh stderr:\n{result.stderr}\n"
		)
		assert "SUB_ISSUE_FOOTER" in actual, (
			"3-way merge lost the sub-issue footer — the exact regression "
			"PR #2760's divergence guard prevented and that P5 must "
			"continue to prevent. If the 3-way merge ever clobbers the "
			"integration-branch change, the project-#2734 failure mode "
			"recurs.\n"
			f"file contents:\n{actual}\n---\n"
			f"refresh stdout:\n{result.stdout}\n"
			f"---\nrefresh stderr:\n{result.stderr}\n"
		)
		# Conflict markers must not appear — those would mean the merge
		# fell back to the skip path but staged the file anyway, which
		# would trip the post-resolve verifier.
		assert "<<<<<<<" not in actual and ">>>>>>>" not in actual, (
			"3-way merge result contains conflict markers — the merge "
			"must EITHER produce a clean result OR fall through to the "
			"skip path. Staging a file with conflict markers would trip "
			"the fingerprint verifier and reproduce the wave-dispatch "
			"deadlock that P5 is meant to break.\n"
			f"file contents:\n{actual}\n"
		)
		# The log line for 3-way merges must surface so an operator
		# auditing the refresh commit can see this file was merged
		# rather than clobbered.
		assert "3-way merged scripts/check_resolver_diff.sh" in result.stdout, (
			"_refresh_integration_resolver_tooling did not log the "
			"expected `3-way merged` line for the merged file. Without "
			"this log line, operators reviewing the orchestrator log "
			"have no way to tell that this file came from a 3-way merge "
			"vs the deadlock-breaker checkout path.\n"
			f"stdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}\n"
		)
	finally:
		shutil.rmtree(tmp_root, ignore_errors=True)


def test_resolver_tooling_refresh_3way_merge_falls_back_to_skip_on_conflict():
	# Behavioural test for the P5 conflict fallback. When the 3-way
	# merge would produce conflict markers (because main and the
	# integration branch edited the SAME region of the file in
	# incompatible ways), the function must NOT stage the conflict-
	# markered file — it must fall back to the existing skip behavior
	# so the normal main->integration sync can surface the conflict
	# under operator review.
	tmp_root = Path(tempfile.mkdtemp(prefix="refresh-3way-conflict-"))
	try:
		bare = tmp_root / "bare.git"
		work = tmp_root / "work"
		git_env = _git_test_env()
		subprocess.run(
			["git", "init", "--bare", "--quiet", str(bare)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		subprocess.run(
			["git", "init", "--quiet", str(work)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		for k, v in [
			("user.name", "test"),
			("user.email", "t@example.com"),
			("commit.gpgsign", "false"),
			("commit.gpgSign", "false"),
		]:
			subprocess.run(
				["git", "-C", str(work), "config", k, v],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)
		(work / "scripts").mkdir()
		refresh_target = work / "scripts" / "check_resolver_diff.sh"
		refresh_target.write_text("LINE_1\nLINE_2\nLINE_3\n", encoding="utf-8")

		def _git(*args: str) -> None:
			subprocess.run(
				["git", "-C", str(work), *args],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)

		_git("checkout", "-B", "main")
		_git("add", "-A")
		_git("commit", "-m", "initial", "--quiet")
		_git("remote", "add", "origin", str(bare))
		_git("push", "-u", "origin", "main", "--quiet")

		# Both branches edit the SAME line in incompatible ways — must
		# produce a conflict that git merge-file cannot auto-resolve.
		_git("checkout", "-B", "orchestrator/project-99")
		refresh_target.write_text("LINE_1\nSUB_ISSUE_CHANGE\nLINE_3\n", encoding="utf-8")
		_git("add", "-A")
		_git("commit", "-m", "sub-issue edit on line 2", "--quiet")
		_git("push", "-u", "origin", "orchestrator/project-99", "--quiet")

		_git("checkout", "main")
		refresh_target.write_text("LINE_1\nMAIN_CHANGE\nLINE_3\n", encoding="utf-8")
		_git("add", "-A")
		_git("commit", "-m", "main edit on line 2", "--quiet")
		_git("push", "origin", "main", "--quiet")

		poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
		fn_body = _extract_refresh_function_body(poller_body)

		runtime_dir = tmp_root / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		runner = tmp_root / "run.sh"
		runner.write_text(
			"#!/usr/bin/env bash\n"
			"set -uo pipefail\n"
			f"{fn_body}\n"
			f"cd {str(work)!r}\n"
			f"export RUNTIME_DIR={str(runtime_dir)!r}\n"
			"_refresh_integration_resolver_tooling orchestrator/project-99 main\n",
			encoding="utf-8",
		)
		runner.chmod(0o755)
		result = subprocess.run(
			["bash", str(runner)],
			capture_output=True, text=True, timeout=60, env=git_env,
		)
		assert result.returncode == 0, (
			f"refresh exited non-zero\nstdout:{result.stdout}\nstderr:{result.stderr}"
		)
		subprocess.run(
			["git", "-C", str(work), "fetch", "origin", "--quiet"],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		actual = subprocess.run(
			["git", "-C", str(work), "show",
			 "origin/orchestrator/project-99:scripts/check_resolver_diff.sh"],
			capture_output=True, text=True, check=True, env=git_env,
		).stdout
		# On conflict the function must SKIP — integration-branch content
		# must be unchanged from the sub-issue version.
		assert actual == "LINE_1\nSUB_ISSUE_CHANGE\nLINE_3\n", (
			"3-way merge with conflict did NOT fall back to skip — the "
			"integration branch was modified despite an unresolvable "
			"merge. This is the exact failure mode P5's conflict-fallback "
			"branch is meant to prevent: staging conflict markers (or "
			"silently picking one side) would trip the fingerprint "
			"verifier and re-introduce the wave-dispatch deadlock.\n"
			f"file contents:\n{actual}\n---\n"
			f"refresh stdout:\n{result.stdout}\n"
			f"---\nrefresh stderr:\n{result.stderr}\n"
		)
		# The log line must surface the skip-on-conflict so operators
		# know the file needs a manual resolve via normal sync.
		assert "skip scripts/check_resolver_diff.sh" in result.stdout, (
			"3-way merge conflict path did not log the expected skip line."
			f"\nstdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}\n"
		)
	finally:
		shutil.rmtree(tmp_root, ignore_errors=True)


def test_resolver_tooling_refresh_still_refreshes_files_unchanged_on_integration_branch():
	# Companion to the no-clobber test above: prove the deadlock-
	# breaker still works when the integration branch is UNCHANGED for
	# a refresh-allowlisted file. This is the legitimate use case the
	# function was originally written for — main shipped a fix to the
	# resolver toolchain, the integration branch is stuck on the older
	# version, and the normal main->integration sync cannot proceed
	# until the integration branch picks up the fix. The merge-base
	# divergence guard introduced for issue #2734 must NOT regress
	# this path.
	tmp_root = Path(tempfile.mkdtemp(prefix="refresh-deadlock-"))
	try:
		bare = tmp_root / "bare.git"
		work = tmp_root / "work"
		git_env = _git_test_env()
		subprocess.run(
			["git", "init", "--bare", "--quiet", str(bare)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		subprocess.run(
			["git", "init", "--quiet", str(work)],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		for k, v in [
			("user.name", "test"),
			("user.email", "t@example.com"),
			("commit.gpgsign", "false"),
			("commit.gpgSign", "false"),
		]:
			subprocess.run(
				["git", "-C", str(work), "config", k, v],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)
		(work / "scripts").mkdir()
		refresh_target = work / "scripts" / "check_resolver_diff.sh"
		main_only_target = work / "scripts" / "targeted_file_context.py"
		refresh_target.write_text("# main v1\n", encoding="utf-8")

		def _git(*args: str) -> None:
			subprocess.run(
				["git", "-C", str(work), *args],
				check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
			)

		_git("checkout", "-B", "main")
		_git("add", "-A")
		_git("commit", "-m", "initial main", "--quiet")
		_git("remote", "add", "origin", str(bare))
		_git("push", "-u", "origin", "main", "--quiet")
		# Integration branch with NO change to the allowlisted file —
		# this is the deadlock-breaker scenario.
		_git("checkout", "-B", "orchestrator/project-99")
		(work / "scripts" / "unrelated.sh").write_text("# unrelated\n", encoding="utf-8")
		_git("add", "-A")
		_git("commit", "-m", "unrelated integration commit", "--quiet")
		_git("push", "-u", "origin", "orchestrator/project-99", "--quiet")
		# Bump main with a fix to the allowlisted file.
		_git("checkout", "main")
		refresh_target.write_text("# main v2 with toolchain fix\n", encoding="utf-8")
		# Also add a second allowlisted file that exists only on main.
		# Missing-path tree lookups must resolve to empty strings here;
		# otherwise git rev-parse writes literal REV:PATH tokens and the
		# refresh incorrectly treats the file as integration-owned drift.
		main_only_target.write_text("# main-only helper\n", encoding="utf-8")
		_git("add", "-A")
		_git("commit", "-m", "main: ship toolchain fix", "--quiet")
		_git("push", "origin", "main", "--quiet")

		poller_body = POLLER_SCRIPT.read_text(encoding="utf-8")
		fn_body = _extract_refresh_function_body(poller_body)
		runtime_dir = tmp_root / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)
		runner = tmp_root / "run.sh"
		runner.write_text(
			"#!/usr/bin/env bash\n"
			"set -uo pipefail\n"
			f"{fn_body}\n"
			f"cd {str(work)!r}\n"
			f"export RUNTIME_DIR={str(runtime_dir)!r}\n"
			"_refresh_integration_resolver_tooling orchestrator/project-99 main\n",
			encoding="utf-8",
		)
		runner.chmod(0o755)
		result = subprocess.run(
			["bash", str(runner)],
			capture_output=True, text=True, timeout=60, env=git_env,
		)
		assert result.returncode == 0, (
			"_refresh_integration_resolver_tooling fixture run failed before "
			"the deadlock-breaker assertions could validate its output.\n"
			f"stdout:\n{result.stdout}\n---\nstderr:\n{result.stderr}\n"
		)
		subprocess.run(
			["git", "-C", str(work), "fetch", "origin", "--quiet"],
			check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=git_env,
		)
		actual = subprocess.run(
			["git", "-C", str(work), "show",
			 "origin/orchestrator/project-99:scripts/check_resolver_diff.sh"],
			capture_output=True, text=True, check=True, env=git_env,
		).stdout
		assert actual == "# main v2 with toolchain fix\n", (
			"_refresh_integration_resolver_tooling failed to refresh a "
			"file the integration branch had NOT modified — the "
			"deadlock-breaking path regressed.\n"
			f"Expected '# main v2 with toolchain fix' on the integration "
			f"branch.\nGot: {actual!r}\n"
			f"---\nrefresh stdout:\n{result.stdout}\n"
			f"---\nrefresh stderr:\n{result.stderr}\n"
		)
		added_actual = subprocess.run(
			["git", "-C", str(work), "show",
			 "origin/orchestrator/project-99:scripts/targeted_file_context.py"],
			capture_output=True, text=True, check=True, env=git_env,
		).stdout
		assert added_actual == "# main-only helper\n", (
			"_refresh_integration_resolver_tooling failed to refresh a "
			"main-only allowlisted file that was absent on the integration "
			"branch. Missing-path tree lookups must resolve to empty strings, "
			"not literal REV:PATH tokens.\n"
			f"Expected '# main-only helper' on the integration branch.\n"
			f"Got: {added_actual!r}\n"
			f"---\nrefresh stdout:\n{result.stdout}\n"
			f"---\nrefresh stderr:\n{result.stderr}\n"
		)
	finally:
		shutil.rmtree(tmp_root, ignore_errors=True)


def _test_elapsed_ms(started_at: float) -> int:
	return max(0, int((time.monotonic() - started_at) * 1000))


def _emit_test_runner_event(
	event: str,
	test_name: str,
	elapsed_ms: int,
	status: str,
	output_stream,
	*,
	rank: int | None = None,
) -> None:
	payload = {
		"elapsed_ms": elapsed_ms,
		"event": event,
		"status": status,
		"test_name": test_name,
	}
	if rank is not None:
		payload["rank"] = rank
	serialized_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
	# Keep event JSON parseable when a running test leaves stdout mid-line.
	with _TEST_RUNNER_OUTPUT_LOCK:
		output_stream.write(f"\n{_TEST_RUNNER_EVENT_PREFIX}{serialized_payload}\n")
		output_stream.flush()


def _test_heartbeat_worker(
	stop_event: threading.Event,
	test_name: str,
	started_at: float,
	heartbeat_interval_sec: float,
	output_stream,
) -> None:
	if heartbeat_interval_sec <= 0:
		return
	while not stop_event.wait(heartbeat_interval_sec):
		_emit_test_runner_event(
			"heartbeat",
			test_name,
			_test_elapsed_ms(started_at),
			"running",
			output_stream,
		)


def _run_selected_tests(
	test_funcs: list,
	*,
	heartbeat_interval_sec: float = _TEST_RUNNER_HEARTBEAT_INTERVAL_SEC,
	slowest_limit: int = _TEST_RUNNER_SLOWEST_LIMIT,
) -> int:
	passed = 0
	failed = 0
	results: list[tuple[str, int, str]] = []
	runner_output = sys.stdout
	for func in test_funcs:
		name = func.__name__
		started_at = time.monotonic()
		_emit_test_runner_event("start", name, 0, "running", runner_output)
		heartbeat_stop = None
		heartbeat_thread = None
		try:
			heartbeat_stop = threading.Event()
			heartbeat_thread = threading.Thread(
				target=_test_heartbeat_worker,
				args=(
					heartbeat_stop,
					name,
					started_at,
					heartbeat_interval_sec,
					runner_output,
				),
				daemon=True,
				name=f"{_TEST_RUNNER_HEARTBEAT_THREAD_PREFIX}{name}",
			)
			heartbeat_thread.start()
		except Exception:
			if heartbeat_stop is not None:
				try:
					heartbeat_stop.set()
				except Exception:
					pass
			if heartbeat_thread is not None:
				try:
					heartbeat_thread.join()
				except Exception:
					pass
			heartbeat_thread = None

		failure = None
		status = "pass"
		try:
			func()
		except Exception as exc:
			failure = exc
			status = "fail"
		finally:
			if heartbeat_stop is not None:
				try:
					heartbeat_stop.set()
				except Exception:
					pass
			if heartbeat_thread is not None:
				try:
					heartbeat_thread.join()
				except Exception:
					pass

		elapsed_ms = _test_elapsed_ms(started_at)
		results.append((name, elapsed_ms, status))
		_emit_test_runner_event("complete", name, elapsed_ms, status, runner_output)
		if failure is None:
			print(f"  PASS  {name}", file=runner_output, flush=True)
			passed += 1
		else:
			print(f"  FAIL  {name}: {failure}", file=runner_output, flush=True)
			failed += 1

	for rank, (name, elapsed_ms, status) in enumerate(
		sorted(results, key=lambda result: (-result[1], result[0]))[:max(0, slowest_limit)],
		start=1,
	):
		_emit_test_runner_event(
			"slowest", name, elapsed_ms, status, runner_output, rank=rank
		)

	print(
		f"\n{passed} passed, {failed} failed, {passed + failed} total",
		file=runner_output,
		flush=True,
	)
	return 1 if failed > 0 else 0


def test_custom_runner_emits_timing_heartbeat_and_preserves_exit_semantics():
	import contextlib
	import io

	def synthetic_fast():
		pass

	def synthetic_slow():
		print("synthetic partial", end="")
		time.sleep(0.2)

	def synthetic_failure():
		raise RuntimeError("synthetic failure")

	output = io.StringIO()
	with contextlib.redirect_stdout(output):
		exit_code = _run_selected_tests(
			[synthetic_fast, synthetic_slow, synthetic_failure],
			heartbeat_interval_sec=0.005,
			slowest_limit=2,
		)
	assert exit_code == 1
	assert "synthetic partial\nTEST_CASE_EVENT: " in output.getvalue()
	lines = output.getvalue().splitlines()
	assert "  PASS  synthetic_fast" in lines
	assert "  PASS  synthetic_slow" in lines
	assert "  FAIL  synthetic_failure: synthetic failure" in lines
	assert lines[-1] == "2 passed, 1 failed, 3 total"

	events = [
		json.loads(line.removeprefix(_TEST_RUNNER_EVENT_PREFIX))
		for line in lines
		if line.startswith(_TEST_RUNNER_EVENT_PREFIX)
	]
	for test_name, expected_status in (
		("synthetic_fast", "pass"),
		("synthetic_slow", "pass"),
		("synthetic_failure", "fail"),
	):
		test_events = [
			event
			for event in events
			if event["test_name"] == test_name and event["event"] != "slowest"
		]
		assert test_events[0] == {
			"elapsed_ms": 0,
			"event": "start",
			"status": "running",
			"test_name": test_name,
		}
		complete_indexes = [
			index for index, event in enumerate(test_events) if event["event"] == "complete"
		]
		assert complete_indexes == [len(test_events) - 1]
		assert test_events[-1]["status"] == expected_status
		assert test_events[-1]["elapsed_ms"] >= 0
	assert any(
		event["event"] == "heartbeat" and event["test_name"] == "synthetic_slow"
		for event in events
	)

	slowest_events = [event for event in events if event["event"] == "slowest"]
	assert [event["rank"] for event in slowest_events] == [1, 2]
	assert [
		(-event["elapsed_ms"], event["test_name"])
		for event in slowest_events
	] == sorted(
		(-event["elapsed_ms"], event["test_name"])
		for event in slowest_events
	)
	synthetic_thread_names = {
		f"{_TEST_RUNNER_HEARTBEAT_THREAD_PREFIX}{test_name}"
		for test_name in ("synthetic_fast", "synthetic_slow", "synthetic_failure")
	}
	assert not any(
		thread.name in synthetic_thread_names
		for thread in threading.enumerate()
	)

	pass_output = io.StringIO()
	with contextlib.redirect_stdout(pass_output):
		assert _run_selected_tests(
			[synthetic_fast], heartbeat_interval_sec=0.005, slowest_limit=1
		) == 0
	assert pass_output.getvalue().splitlines()[-1] == "1 passed, 0 failed, 1 total"
	assert not any(
		thread.name in synthetic_thread_names
		for thread in threading.enumerate()
	)

	pass_output.seek(0)
	pass_output.truncate(0)
	with contextlib.redirect_stdout(pass_output):
		assert _run_selected_tests(
			[synthetic_fast], heartbeat_interval_sec=0, slowest_limit=-1
		) == 0
	assert '"event":"heartbeat"' not in pass_output.getvalue()
	assert '"event":"slowest"' not in pass_output.getvalue()


def main() -> int:
	# Force line-buffered stdout so PASS/FAIL messages surface to CI logs as
	# each test completes, instead of sitting in Python's default block buffer
	# (which, under a pipe on GitHub Actions, hides all progress until the
	# process exits and can make a running suite look like a silent hang).
	try:
		sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass

	selected_names = list(sys.argv[1:])
	tests_by_name = {
		name: func
		for name, func in sorted(globals().items())
		if name.startswith("test_") and callable(func)
	}
	if selected_names:
		missing = [name for name in selected_names if name not in tests_by_name]
		for name in missing:
			print(f"  FAIL  {name}: unknown test name", flush=True)
		if missing:
			return 1
		test_funcs = [tests_by_name[name] for name in selected_names]
	else:
		test_funcs = list(tests_by_name.values())
	return _run_selected_tests(test_funcs)


if __name__ == "__main__":
	raise SystemExit(main())
