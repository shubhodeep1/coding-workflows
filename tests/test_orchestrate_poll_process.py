#!/usr/bin/env python3
"""Deterministic tests for orchestrate_poll_process.sh validation state handling."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"


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


def _run_poller(
	*,
	state: dict,
	enable_validation: str,
	max_validate_cycles: str,
	tracking_labels: list[str] | None = None,
	tracking_comments: list[str] | None = None,
	issue_labels: dict[int, list[str]] | None = None,
	gql_mode: str = "full",
	gql_labels: dict[int, list[str]] | None = None,
	codex_json: dict | None = None,
	fail_validation_dispatch: bool = False,
	prs: list[dict] | None = None,
	existing_branches: list[str] | None = None,
	merge_conflict_on_sync: bool = False,
	blocked_check_shas: list[str] | None = None,
) -> dict:
	tracking_num = 192
	tracking_labels = tracking_labels or []
	tracking_comments = tracking_comments or []
	issue_labels = issue_labels or {10: ["ai:merged"]}
	gql_labels = gql_labels or {}
	prs = prs or []
	existing_branches = existing_branches or ["main"]
	blocked_check_shas = blocked_check_shas or []
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
		for inum, labels in issue_labels.items():
			issues[str(inum)] = {
				"labels": list(labels),
				"comments": [],
				"body": f"Issue {inum}",
				"closed": False,
			}

		store = {
			"issues": issues,
			"next_comment_id": 2 + len(tracking_comments),
			"validation_dispatches": [],
			"closed_issues": [],
			"graphql_mode": gql_mode,
			"graphql_labels": {str(k): list(v) for k, v in gql_labels.items()},
			"graphql_calls": 0,
			"issue_label_calls": {},
			"fail_validation_dispatch": fail_validation_dispatch,
			"default_branch": "main",
			"prs": prs,
			"existing_branches": existing_branches,
			"merge_conflict_on_sync": merge_conflict_on_sync,
			"merge_calls": [],
			"blocked_check_shas": blocked_check_shas,
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
		if arg.startswith('-'):
			i += 1
			continue
		if path is None:
			path = arg
		i += 1
	return path, jq, method, fields


if not args:
	sys.exit(0)

if args[0] == 'label' and len(args) >= 3 and args[1] == 'create':
	sys.exit(0)

if args[0] == 'issue' and len(args) >= 3 and args[1] == 'list':
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
	sys.exit(1)

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
		i += 1
	next_num = store.get('next_issue_number', 900)
	store['next_issue_number'] = next_num + 1
	store['issues'][str(next_num)] = {'labels': [], 'comments': [], 'body': body, 'closed': False, 'title': title}
	store.setdefault('created_issues', []).append({'number': next_num, 'title': title})
	save()
	print(f'https://github.com/owner/repo/issues/{next_num}')
	sys.exit(0)

if args[0] == 'api':
	path, jq, method, fields = parse_api()
	if path is None:
		print('{}')
		sys.exit(0)

	if path == 'graphql':
		mode = store.get('graphql_mode', 'full')
		store['graphql_calls'] = int(store.get('graphql_calls', 0)) + 1
		save()
		if mode == 'error':
			print('graphql failed', file=sys.stderr)
			sys.exit(1)
		query = ''
		for f in fields:
			if f.startswith('query='):
				query = f.split('=', 1)[1]
		aliases = []
		for _, issue_num in re.findall(r'i(\d+)\s*:\s*issue\(number:\s*(\d+)\)', query):
			aliases.append(int(issue_num))
		repo = {}
		for num in aliases:
			if mode == 'partial' and aliases and num == aliases[-1]:
				continue
			labels = store.get('graphql_labels', {}).get(str(num), get_issue(num).get('labels', []))
			repo[f'i{num}'] = {'labels': {'nodes': [{'name': label} for label in labels]}}
		print(json.dumps({'data': {'repository': repo}}))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)/comments(?:\?per_page=100)?$', path)
	if m and method == 'GET' and not fields:
		issue = get_issue(m.group(1))
		print(json.dumps(issue['comments']))
		sys.exit(0)

	m = re.search(r'/issues/(\d+)/comments$', path)
	if m and fields:
		issue = get_issue(m.group(1))
		body = ''
		for f in fields:
			if f.startswith('body='):
				body = f.split('=', 1)[1]
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
		if jq == '.body':
			print(issue.get('body', ''))
		else:
			print(json.dumps({'body': issue.get('body', '')}))
		sys.exit(0)

	m = re.search(r'/pulls/(\d+)$', path)
	if m:
		pr_num = int(m.group(1))
		pr = None
		for item in store.get('prs', []):
			if item.get('number') == pr_num:
				pr = item
				break
		if pr is None:
			print('{}')
			sys.exit(0)
		if jq == '.state':
			print(pr.get('state', 'open'))
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
		else:
			print(json.dumps({'state': pr.get('state', 'open'), 'mergeable': pr.get('mergeable', True)}))
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
		if branch in store.get('existing_branches', ['main']):
			print(json.dumps({'ref': f'refs/heads/{branch}', 'object': {'sha': 'mocksha'}}))
			sys.exit(0)
		print('not found', file=sys.stderr)
		sys.exit(1)

	if path.endswith('/timeline'):
		if jq:
			print('')
		else:
			print('[]')
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

	print('{}')
	sys.exit(0)

print('Unsupported gh call: ' + ' '.join(args), file=sys.stderr)
sys.exit(1)
'''
		_write_exec(bin_dir / "gh", gh_mock)

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
				"ENABLE_VALIDATION": enable_validation,
				"MAX_VALIDATE_CYCLES": max_validate_cycles,
				"GH_MOCK_STORE": str(store_file),
				"MOCK_CODEX_JSON": json.dumps(codex_json),
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			}
		)

		proc = subprocess.run(
			["bash", str(POLLER_SCRIPT)],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
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
	assert result["graphql_calls"] == 1
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
	assert result["graphql_calls"] == 1
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
	assert result["graphql_calls"] == 1
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


def test_final_merge_conflict_sets_merge_conflict_status():
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
	assert result["latest_state"]["status"] == "merge_conflict"
	assert result["latest_state"]["final_merge_status"] == "conflict"
	assert result["latest_state"]["final_merge_pr"] == 351


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
	assert result.get("merge_calls", []) == []



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


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


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
