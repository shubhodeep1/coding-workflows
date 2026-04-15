#!/usr/bin/env python3
"""Deterministic tests for orchestrate_poll_process.sh validation state handling."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
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


# Directories and top-level files that the poller script under test needs
# to resolve at runtime via relative paths (helper scripts, prompt files,
# schema JSON, canonical instruction markdown). Each sandbox run gets an
# isolated copy of these so any file mutations the script performs are
# confined to the sandbox and cannot reach the real coding-workflows
# checkout the test runner started from.
_SANDBOX_DIRS = ("scripts", "prompts", ".github/ai")
_SANDBOX_FILES = (
	"agents.md",
	"codex_system_instructions.md",
	"ai_pipeline.md",
	"unattended_llm_system_instructions.md",
)


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
	"""
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
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "config", "user.email", "sandbox@example.com"],
		check=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "config", "user.name", "Poller Sandbox"],
		check=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "checkout", "-B", "main"],
		check=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		["git", "-C", str(target), "commit", "--allow-empty", "-m", "sandbox init", "--quiet"],
		check=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
	)
	subprocess.run(
		[
			"git", "-C", str(target), "remote", "add",
			"origin", "https://github.com/test-harness/poller-sandbox.git",
		],
		check=True,
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


def test_judge_reasoning_effort_logic_is_adaptive_after_cycle_three():
	script = POLLER_SCRIPT.read_text(encoding="utf-8")
	assert 'JUDGE_INVOCATION_CYCLE=$((JUDGE_CYCLE + 1))' in script
	assert 'if [ "${JUDGE_INVOCATION_CYCLE}" -gt 3 ] && [ "${MODEL_REASONING_EFFORT_JUDGE}" = "xhigh" ]; then' in script
	assert 'EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE="high"' in script
	assert 'EFFECTIVE_MODEL_REASONING_EFFORT_JUDGE' in script


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


def _state_comment(state: dict) -> str:
	return "<!-- ORCHESTRATOR_STATE_V1\n" + json.dumps(state) + "\nORCHESTRATOR_STATE_V1 -->"


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def _extract_latest_state(comments: list[dict]) -> dict:
	for comment in reversed(comments):
		body = comment.get("body", "")
		if "ORCHESTRATOR_STATE_V1" not in body:
			continue
		match = re.search(r"<!-- ORCHESTRATOR_STATE_V1\n(.*?)\nORCHESTRATOR_STATE_V1 -->", body, flags=re.S)
		if not match:
			continue
		return json.loads(match.group(1))
	raise AssertionError("No ORCHESTRATOR_STATE_V1 comment found")


def _extract_state_payloads(comments: list[dict]) -> list[str]:
	payloads: list[str] = []
	for comment in comments:
		body = comment.get("body", "")
		if "ORCHESTRATOR_STATE_V1" not in body:
			continue
		match = re.search(r"<!-- ORCHESTRATOR_STATE_V1\n(.*?)\nORCHESTRATOR_STATE_V1 -->", body, flags=re.S)
		if match:
			payloads.append(match.group(1))
	return payloads


def _run_poller(
	*,
	state: dict,
	enable_validation: str,
	max_validate_cycles: str,
	tracking_labels: list[str] | None = None,
	tracking_comments: list[str] | None = None,
	issue_labels: dict[int, list[str]] | None = None,
	issue_comments: dict[int, list[str]] | None = None,
	gql_mode: str = "full",
	gql_labels: dict[int, list[str]] | None = None,
	codex_json: dict | None = None,
	fail_validation_dispatch: bool = False,
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
	mock_stall_judge_json: dict | None = None,
	fail_issue_comment_get_after: dict[int, int] | None = None,
	fail_branch_ref_after: dict[str, int] | None = None,
	fail_branch_ref_not_found_after: dict[str, int] | None = None,
	codex_touch_file: str | None = None,
	mock_git_push_success: bool = False,
	enable_stall_judge: str = "true",
	enable_stall_human_terminalization: str = "false",
	stall_judge_trigger_count: str = "2",
	enable_clean_wave_judge_skip: str = "true",
) -> dict:
	tracking_num = 192
	tracking_labels = tracking_labels or []
	tracking_comments = tracking_comments or []
	if issue_labels is None:
		issue_labels = {10: ["ai:merged"]}
	issue_comments = issue_comments or {}
	gql_labels = gql_labels or {}
	prs = prs or []
	pr_api_sequence = pr_api_sequence or {}
	existing_branches = existing_branches or ["main"]
	blocked_check_shas = blocked_check_shas or []
	validation_workflow_runs = validation_workflow_runs or []
	issue_closed = issue_closed or {}
	issue_linked_prs = issue_linked_prs or {}
	merge_tree_conflict_paths = merge_tree_conflict_paths or []
	timeline_fail_for_issues = timeline_fail_for_issues or []
	update_branch_fail_for_prs = update_branch_fail_for_prs or []
	review_dispatch_fail_for_prs = review_dispatch_fail_for_prs or []
	active_autofix_runs = active_autofix_runs or []
	mock_stall_judge_json = mock_stall_judge_json or {}
	fail_issue_comment_get_after = fail_issue_comment_get_after or {}
	fail_branch_ref_after = fail_branch_ref_after or {}
	fail_branch_ref_not_found_after = fail_branch_ref_not_found_after or {}
	codex_json = codex_json or {
		"status": "complete",
		"justification": "done",
		"assessment": "all work complete",
		"new_issues": [],
		"issues_to_revert": [],
	}

	with tempfile.TemporaryDirectory(prefix="poller-test-") as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		home_dir = tmp / "home"
		runtime_dir = tmp / "runtime"
		store_file = tmp / "gh_store.json"
		bin_dir.mkdir(parents=True)
		home_dir.mkdir(parents=True)
		runtime_dir.mkdir(parents=True)

		issues: dict[str, dict] = {
			str(tracking_num): {
				"labels": list(tracking_labels),
				"comments": [
					{"id": 1, "body": _state_comment(state)},
					*[
						{"id": idx + 2, "body": comment_body}
						for idx, comment_body in enumerate(tracking_comments)
					],
				],
				"body": "Tracking issue body",
				"closed": False,
			}
		}
		next_comment_id = 2 + len(tracking_comments)
		for inum, labels in issue_labels.items():
			raw_comments = issue_comments.get(inum, [])
			issue_comment_entries = []
			for comment_body in raw_comments:
				issue_comment_entries.append({"id": next_comment_id, "body": comment_body})
				next_comment_id += 1
			issues[str(inum)] = {
				"labels": list(labels),
				"comments": issue_comment_entries,
				"body": f"Issue {inum}",
				"closed": bool(issue_closed.get(inum, False)),
			}

		store = {
			"issues": issues,
			"next_comment_id": next_comment_id,
			"validation_dispatches": [],
			"review_dispatches": [],
			"closed_issues": [],
			"graphql_mode": gql_mode,
			"graphql_labels": {str(k): list(v) for k, v in gql_labels.items()},
			"graphql_calls": 0,
			"label_batch_graphql_calls": 0,
			"issue_label_calls": {},
			"fail_validation_dispatch": fail_validation_dispatch,
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
			"fail_branch_ref_after": {str(k): int(v) for k, v in fail_branch_ref_after.items()},
			"fail_branch_ref_not_found_after": {str(k): int(v) for k, v in fail_branch_ref_not_found_after.items()},
		}
		store_file.write_text(json.dumps(store), encoding="utf-8")

		(runtime_dir / "tracking_issues.json").write_text(
			json.dumps([{"number": tracking_num, "title": "Test tracking"}]),
			encoding="utf-8",
		)

		gh_mock = r'''#!/usr/bin/env python3
import json
import re
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
	sys.exit(0)

if args[0] == 'issue' and len(args) >= 3 and args[1] == 'list':
	label = None
	state = 'open'
	for i, arg in enumerate(args):
		if arg == '--label' and i + 1 < len(args):
			label = args[i + 1]
		if arg == '--state' and i + 1 < len(args):
			state = args[i + 1]
	pipeline_labels = {
		'ai:clarification',
		'ai:planning',
		'ai:awaiting-approval',
		'ai:implementing',
		'ai:done',
		'ai:ready-to-merge',
	}
	results = []
	if label in pipeline_labels:
		for issue_num, issue in store.get('issues', {}).items():
			is_closed = bool(issue.get('closed'))
			if state == 'open' and is_closed:
				continue
			if state == 'closed' and not is_closed:
				continue
			if label in issue.get('labels', []):
				results.append({'number': int(issue_num)})
	print(json.dumps(results))
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
		runs.append({'status': run.get('status', 'queued')})
	if jq_query:
		import subprocess as _sp
		p = _sp.run(['jq', '-r', jq_query], input=json.dumps(runs), capture_output=True, text=True)
		print(p.stdout.rstrip())
	else:
		print(json.dumps(runs))
	sys.exit(0)

if args[0] == 'pr' and len(args) >= 2 and args[1] == 'list':
	base = None
	head = None
	jq_query = None
	for i, arg in enumerate(args):
		if arg == '--base' and i + 1 < len(args):
			base = args[i + 1]
		if arg == '--head' and i + 1 < len(args):
			head = args[i + 1]
		if arg == '--jq' and i + 1 < len(args):
			jq_query = args[i + 1]
	prs = []
	for pr in store.get('prs', []):
		if base and pr.get('baseRefName') != base:
			continue
		if head and pr.get('headRefName') != head:
			continue
		prs.append(pr)
	if jq_query == '.[0].number // empty':
		if prs:
			print(prs[0].get('number'))
		else:
			print('')
	else:
		print(json.dumps(prs))
	sys.exit(0)

if args[0] == 'pr' and len(args) >= 2 and args[1] == 'create':
	base = ''
	head = ''
	title = ''
	body = ''
	i = 2
	while i < len(args):
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
	next_num = store.get('next_pr_number', 300)
	store['next_pr_number'] = next_num + 1
	pr = {
		'number': next_num,
		'state': 'open',
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
	issue = get_issue(num)
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
		i += 1
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

	if path == 'graphql':
		mode = store.get('graphql_mode', 'full')
		store['graphql_calls'] = int(store.get('graphql_calls', 0)) + 1
		query = ''
		for f in fields:
			if f.startswith('query='):
				query = f.split('=', 1)[1]
		# Classify the query so tests can count wave-label-batch attempts
		# independently of unrelated GraphQL callers (standalone-stall
		# marker search, candidate details batch). The wave label batch
		# uses aliased issue(number:N) selectors and requests only labels —
		# no comments(last:) and no search(query:).
		is_label_batch = (
			bool(re.search(r'i\d+\s*:\s*issue\(number:\s*\d+\)', query))
			and 'comments(last:' not in query
			and 'search(query:' not in query
		)
		if is_label_batch:
			store['label_batch_graphql_calls'] = int(store.get('label_batch_graphql_calls', 0)) + 1
		save()
		if mode == 'error':
			print('graphql failed', file=sys.stderr)
			sys.exit(1)
		aliases = []
		for _, issue_num in re.findall(r'i(\d+)\s*:\s*issue\(number:\s*(\d+)\)', query):
			aliases.append(int(issue_num))
		repo = {}
		for num in aliases:
			if mode == 'partial' and aliases and num == aliases[-1]:
				continue
			issue = get_issue(num)
			labels = store.get('graphql_labels', {}).get(str(num), issue.get('labels', []))
			comment_nodes = []
			if 'comments(last:' in query:
				for comment in issue.get('comments', [])[-100:]:
					comment_nodes.append({
						'databaseId': comment.get('id'),
						'body': comment.get('body', ''),
						'createdAt': comment.get('created_at', '2026-01-01T00:00:00Z'),
					})
			repo[f'i{num}'] = {
				'number': num,
				'labels': {'nodes': [{'name': label} for label in labels]},
				'comments': {'nodes': comment_nodes},
			}
		print(json.dumps({'data': {'repository': repo}}))
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
		issue['comments'].append({'id': cid, 'body': body})
		save()
		print(json.dumps({'id': cid}))
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
		issue = get_issue(m.group(1))
		issue_state = 'closed' if issue.get('closed') else 'open'
		if jq == '.body':
			print(issue.get('body', ''))
		elif jq == '.state':
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
		pr_state = pr.get('stateFromApi', pr.get('state', 'open'))
		if jq == '.state':
			print(pr_state)
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
				'state': pr_state,
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
		save()
		if branch in store.get('existing_branches', ['main']):
			print(json.dumps({'ref': f'refs/heads/{branch}', 'object': {'sha': 'mocksha'}}))
			sys.exit(0)
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
		assert real_git is not None
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
	sys.exit(0)

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
				"MODEL_EDITOR": "openai/gpt-5.3-codex",
				"MODEL_REASONING_EFFORT_JUDGE": "xhigh",
				"TG_BOT_SECRET": "",
				"TG_ADMIN_CHAT_ID": "",
				"TOOL_CALL_BUDGET_JUDGE": "60",
				"SERENA_VERSION": "main",
				"SERENA_LANGUAGES": "",
				"SERENA_DISABLED": "true",
				"SERENA_IGNORED_DIRS": "",
				"MAX_REVIEW_BLOCKED_RETRIES": "2",
				"MAX_VALIDATION_RECOVERY_ATTEMPTS": "0",
				"ENABLE_STALL_JUDGE": enable_stall_judge,
				"ENABLE_STALL_HUMAN_TERMINALIZATION": enable_stall_human_terminalization,
				"STALL_JUDGE_TRIGGER_COUNT": stall_judge_trigger_count,
				"ENABLE_CLEAN_WAVE_JUDGE_SKIP": enable_clean_wave_judge_skip,
				"ENABLE_VALIDATION": enable_validation,
				"MAX_VALIDATE_CYCLES": max_validate_cycles,
				"GH_MOCK_STORE": str(store_file),
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"REAL_GIT_BIN": real_git,
				"MOCK_CODEX_JSON": json.dumps(codex_json),
				"MOCK_GIT_PUSH_SUCCESS": "true" if mock_git_push_success else "false",
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

		proc = _run_poller_subprocess(
			["bash", str(POLLER_SCRIPT)],
			cwd=str(REPO_ROOT),
			env=env,
		)
		if proc.returncode != 0:
			raise AssertionError(
				"poller exited non-zero\n"
				f"stdout:\n{proc.stdout}\n"
				f"stderr:\n{proc.stderr}"
			)

		result = json.loads(store_file.read_text(encoding="utf-8"))
		tracking_issue = result["issues"][str(tracking_num)]
		result["latest_state"] = _extract_latest_state(tracking_issue["comments"])
		result["tracking_labels"] = tracking_issue["labels"]
		result["tracking_closed"] = tracking_issue.get("closed", False)
		result["merge_calls"] = result.get("merge_calls", [])
		result["review_dispatches"] = result.get("review_dispatches", [])
		result["update_branch_calls"] = result.get("update_branch_calls", [])
		result["stdout"] = proc.stdout
		result["stderr"] = proc.stderr
		return result


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


def test_review_blocked_merged_followup_retargets_to_integration_branch():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["waves"][0]["issues"][0]["status"] = "review-blocked"

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		pr_api_sequence={
			901: [
				{
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
				},
				{
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
				},
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
			]
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

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		pr_api_sequence={
			901: [
				{
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
				},
				{
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
				},
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
			]
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

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		pr_api_sequence={
			901: [
				{
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
				},
				{
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
				},
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
			]
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

	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:review-blocked"]},
		issue_linked_prs={10: 901},
		pr_api_sequence={
			901: [
				{
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
				},
				{
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
				},
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
			]
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
			body for body in first_comment_bodies if "ORCHESTRATOR_STATE_V1" not in body
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
			body for body in first_comment_bodies if "ORCHESTRATOR_STATE_V1" not in body
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
			body for body in first_comment_bodies if "ORCHESTRATOR_STATE_V1" not in body
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


def test_final_merge_treats_closed_merged_pr_as_success():
	state = _base_state(status="in_progress")
	state["integration_branch"] = "orchestrator/project-192"
	state["final_merge_status"] = "conflict"
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
	assert result["latest_state"]["status"] == "failed"
	assert "closed without merge" in result["latest_state"].get("validation_failure_reason", "")
	assert "ai:merged" not in result["issues"]["501"]["labels"]
	assert "merged PR lookup failed" in (result["stdout"] + result["stderr"])



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
		tracking_labels=["ai:validation-failed"],
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
	assert len(result["validation_dispatches"]) == 1


def test_revalidate_ignored_when_no_comment():
	"""Without a /revalidate comment, a validation-failed project stays skipped."""
	state = _base_state(status="failed")
	state["validation_failure_reason"] = "Some failure"
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		tracking_labels=["ai:validation-failed"],
	)
	ls = result["latest_state"]
	assert ls["status"] == "failed", f"Expected status=failed, got {ls['status']}"
	assert "ai:validation-failed" in result["tracking_labels"]
	assert result["validation_dispatches"] == []


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


def test_stall_judge_escalate_human_with_gate_disabled_falls_back_to_non_human_action():
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
	tracking_comments = [c.get("body", "") for c in result["issues"]["192"]["comments"]]
	assert issue_entry["github_issue"] != 10
	assert issue_entry["status"] == "pending"
	assert issue_entry["stall_recovery_count"] == 0
	assert "ai:needs-human" not in result["issues"]["10"]["labels"]
	assert "ai:closed" in result["issues"]["10"]["labels"]
	assert result["issues"]["10"]["closed"] is True
	assert any("**Effective action:** close_and_reissue" in body for body in tracking_comments)


def test_stall_judge_escalate_human_with_gate_enabled_preserves_escalation_behavior():
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


def test_stall_recovery_disables_human_terminalization_by_default():
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
		enable_stall_judge="false",
		stall_judge_trigger_count="9",
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["github_issue"] != 10
	assert issue_entry["status"] == "pending"
	assert issue_entry["stall_recovery_count"] == 0
	assert "ai:needs-human" not in result["issues"]["10"]["labels"]
	assert "ai:closed" in result["issues"]["10"]["labels"]
	assert result["issues"]["10"]["closed"] is True


def test_stall_recovery_allows_human_terminalization_when_enabled():
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
		enable_stall_judge="false",
		stall_judge_trigger_count="9",
		enable_stall_human_terminalization="true",
	)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3
	assert "ai:needs-human" in result["issues"]["10"]["labels"]


def test_standalone_stall_recovery_disables_human_terminalization_by_default():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	standalone_state_comment = """<!-- AI_STANDALONE_STALL_STATE_V1
{"schema_version":1,"last_seen_phase":"ai:implementing","status_since_ts":1,"stall_recovery_count":2}
AI_STANDALONE_STALL_STATE_V1 -->"""
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		gql_mode="partial",
		issue_labels={42: ["ai:implementing"]},
		issue_comments={42: [standalone_state_comment]},
		enable_stall_judge="false",
		stall_judge_trigger_count="9",
	)
	assert "ai:needs-human" not in result["issues"]["42"]["labels"]
	assert "ai:closed" in result["issues"]["42"]["labels"]
	assert result["issues"]["42"]["closed"] is True


def test_standalone_stall_recovery_allows_human_terminalization_when_enabled():
	state = _base_state(status="in_progress")
	state["waves"][0]["issues"][0]["status"] = "merged"
	standalone_state_comment = """<!-- AI_STANDALONE_STALL_STATE_V1
{"schema_version":1,"last_seen_phase":"ai:implementing","status_since_ts":1,"stall_recovery_count":2}
AI_STANDALONE_STALL_STATE_V1 -->"""
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		gql_mode="partial",
		issue_labels={42: ["ai:implementing"]},
		issue_comments={42: [standalone_state_comment]},
		enable_stall_judge="false",
		stall_judge_trigger_count="9",
		enable_stall_human_terminalization="true",
	)
	assert "ai:needs-human" in result["issues"]["42"]["labels"]



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
	# At stall_recovery_count=2 for phase ai:awaiting-approval, the declarative
	# fallback action is auto_approve (human terminalization ladder is not used
	# in this test), so /approved is posted and ai:needs-human is not added.
	assert "ai:needs-human" not in result["issues"]["10"]["labels"]
	assert any("/approved" in body for body in issue_comments)
	issue_entry = result["latest_state"]["waves"][0]["issues"][0]
	assert issue_entry["stall_recovery_count"] == 3


def test_stall_judge_escalate_human_falls_back_when_human_terminalization_disabled():
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
		mock_stall_judge_json={
			"action": "escalate_human",
			"justification": "needs operator",
			"target_pr": None,
			"head_ref": None,
		},
	)
	issue_comments = [c.get("body", "") for c in result["issues"]["10"]["comments"]]
	assert "ai:needs-human" not in result["issues"]["10"]["labels"]
	assert "ai:closed" in result["issues"]["10"]["labels"]
	assert result["issues"]["10"]["closed"] is True
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
				"MODEL_EDITOR": "openai/gpt-5.3-codex",
				"MODEL_REASONING_EFFORT_JUDGE": "xhigh",
				"TG_BOT_SECRET": "",
				"TG_ADMIN_CHAT_ID": "",
				"TOOL_CALL_BUDGET_JUDGE": "60",
				"SERENA_VERSION": "main",
				"SERENA_LANGUAGES": "",
				"SERENA_DISABLED": "true",
				"SERENA_IGNORED_DIRS": "",
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


def main() -> int:
	# Force line-buffered stdout so PASS/FAIL messages surface to CI logs as
	# each test completes, instead of sitting in Python's default block buffer
	# (which, under a pipe on GitHub Actions, hides all progress until the
	# process exits and can make a running suite look like a silent hang).
	try:
		import sys
		sys.stdout.reconfigure(line_buffering=True)
	except Exception:
		pass

	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
