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
	codex_json: dict | None = None,
	fail_validation_dispatch: bool = False,
) -> dict:
	tracking_num = 192
	tracking_labels = tracking_labels or []
	tracking_comments = tracking_comments or []
	issue_labels = issue_labels or {10: ["ai:merged"]}
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
			"fail_validation_dispatch": fail_validation_dispatch,
			"default_branch": "main",
		}
		store_file.write_text(json.dumps(store), encoding="utf-8")

		(runtime_dir / "tracking_issues.json").write_text(
			json.dumps([{"number": tracking_num, "title": "Test tracking"}]),
			encoding="utf-8",
		)

		_write_exec(
			bin_dir / "gh",
			"""#!/usr/bin/env python3
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
		if arg == '--paginate':
			path = args[i + 1]
			i += 2
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

if args[0] == 'workflow' and len(args) >= 3 and args[1] == 'run':
	if args[2] in ('ai-validate.yml', 'internal-validate.yml'):
		if store.get('fail_validation_dispatch'):
			print('dispatch failed', file=sys.stderr)
			sys.exit(1)
		tracking = None
		for i, arg in enumerate(args):
			if arg == '-f' and i + 1 < len(args) and args[i + 1].startswith('tracking_issue='):
				tracking = args[i + 1].split('=', 1)[1]
		store['validation_dispatches'].append({'workflow': args[2], 'tracking_issue': tracking})
		save()
		sys.exit(0)
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

if args[0] == 'api':
	path, jq, method, fields = parse_api()
	if path is None:
		print('{}')
		sys.exit(0)

	m = re.search(r'/issues/(\\d+)/comments(?:\\?per_page=100)?$', path)
	if m and method == 'GET' and not fields:
		issue = get_issue(m.group(1))
		print(json.dumps(issue['comments']))
		sys.exit(0)

	m = re.search(r'/issues/(\\d+)/comments$', path)
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

	m = re.search(r'/issues/(\\d+)/labels$', path)
	if m:
		issue = get_issue(m.group(1))
		labels = issue['labels']
		if jq:
			print(json.dumps(labels))
		else:
			print(json.dumps([{'name': l} for l in labels]))
		sys.exit(0)

	m = re.search(r'/issues/(\\d+)$', path)
	if m:
		issue = get_issue(m.group(1))
		if jq == '.body':
			print(issue.get('body', ''))
		else:
			print(json.dumps({'body': issue.get('body', '')}))
		sys.exit(0)

	if path.endswith('/timeline'):
		if jq:
			print('')
		else:
			print('[]')
		sys.exit(0)

	if re.search(r'/commits/.+/check-runs$', path):
		if jq:
			print('[]')
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
""",
		)

		_write_exec(
			bin_dir / "codex",
			"""#!/usr/bin/env python3
import json
import os

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
				"MODEL_REASONING_EFFORT_JUDGE": "high",
				"TG_BOT_SECRET": "",
				"TG_ADMIN_CHAT_ID": "",
				"TOOL_CALL_BUDGET_JUDGE": "60",
				"SERENA_VERSION": "main",
				"SERENA_LANGUAGES": "",
				"SERENA_DISABLED": "true",
				"SERENA_IGNORED_DIRS": "",
				"MAX_REVIEW_BLOCKED_RETRIES": "2",
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
		return result


# ---------------------------------------------------------------------------
# Tests: orchestrate poll validation lifecycle
# ---------------------------------------------------------------------------


def test_complete_verdict_enters_validation_mode_when_enabled():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="true",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "validating"
	assert result["latest_state"]["judge_cycle"] == 1
	assert result["latest_state"]["validation_cycle"] == 1
	assert "ai:validating" in result["tracking_labels"]
	assert result["tracking_closed"] is False
	assert len(result["validation_dispatches"]) == 1



def test_complete_verdict_closes_when_validation_disabled():
	state = _base_state(status="in_progress")
	result = _run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)
	assert result["latest_state"]["status"] == "complete"
	assert result["tracking_closed"] is True
	assert result["validation_dispatches"] == []



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

def test_validated_label_marks_complete_and_closes():
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
	assert result["tracking_closed"] is True
	assert "ai:validated" in result["tracking_labels"]


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
