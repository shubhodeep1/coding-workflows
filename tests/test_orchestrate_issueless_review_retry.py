#!/usr/bin/env python3
"""Issue-less review retry pass in the orchestrator poller (issue #9487).

A review run that fails before the editor (reviewers_failed,
provider_unavailable, sandbox_prepare_failed) posts "AI review/autofix
produced no output - will retry", but the standalone stall pass only follows
labelled issues, so a PR with no linked issue was never retried
(shubhodeep1/tele-funtoken-msg-scoring PR #5219 sat for three days). These
tests run the real conflict-sweep batch slice and the real pass from
scripts/orchestrate_poll_process.sh with stubbed GitHub helpers, plus the
no-output comment text of scripts/review_autofix_step_post_editor_summary.sh.

CI runs these cases through tests/test_alert_levels_human_only.py, which
collects them; keep the test names unique across both files.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
STEP_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_editor_summary.sh"
POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
AGENTS_FRAGMENT = REPO_ROOT / "agents.d" / "9487-issueless-review-retry.md"
ORCHESTRATE_LIB = REPO_ROOT / "scripts" / "orchestrate_lib.py"

LOGIN = "pipeline-bot"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
OLD_SUPPORT = "1" * 40
PINNED = "2" * 40

BATCH_START = "declare -A _STANDALONE_CLEAN_PRS=()"
BATCH_END = "for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do"

LINE_RE = re.compile(
	r"^ISSUELESS_REVIEW_RETRY outcome=(?P<outcome>retriggered|waiting_for_release|already_retried|in_flight|skipped) "
	r"pr=(?P<pr>[0-9]+) repo=o/r head=(?P<head>[0-9a-f]{40}|unknown) support=(?P<support>[0-9a-f]{40}|none) "
	r"pinned=(?P<pinned>[0-9a-f]{40}|none)(?: reason=(?P<reason>[a-z_]+))?(?: marker=(?P<marker>write_failed))?$"
)


def _require_jq_for_issueless_retry() -> None:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")


def _poller_text() -> str:
	return POLLER_SCRIPT.read_text(encoding="utf-8")


def _issueless_bash_function(script: str, signature: str) -> str:
	start = script.index(signature)
	end = script.index("\n}\n", start) + 3
	return script[start:end]


def _issueless_constants(script: str) -> str:
	lines = re.findall(r"^ISSUELESS_REVIEW_RETRY_[A-Z_]+=(?:'[^'\n]*'|\"[^\"\n]*\")$", script, re.MULTILINE)
	assert len(lines) >= 6, lines
	return "\n".join(lines)


def _now_iso(days_ago: float = 0.0) -> str:
	return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure_body(head: str = HEAD_A, reason: str = "reviewers_failed", support: str | None = OLD_SUPPORT, fp: str = "abcdef0123") -> str:
	support_field = f" support={support}" if support else ""
	return (
		"**AI review/autofix produced no output — will retry**\n\n"
		"The reviewer stage failed, so the editor never ran.\n\n"
		f"<!-- review-autofix-failure:v1 head={head} reason={reason} fp={fp} degraded=0 run=77{support_field} -->"
	)


def _retry_body(head: str = HEAD_A, support: str = OLD_SUPPORT) -> str:
	return f"Issue-less review retry: re-dispatched.\n\n<!-- issueless-review-retry:v1 head={head} support={support} -->"


def _row(number: int, *, head: str = HEAD_A, cross: object = False, owner: str | None = "o", draft: bool = False) -> dict:
	row: dict = {"number": number, "headRefName": f"feature/{number}", "headRefOid": head, "baseRefName": "main", "isDraft": draft}
	if cross is not None:
		row["isCrossRepository"] = cross
	if owner is not None:
		row["headRepositoryOwner"] = {"login": owner}
	return row


def _node(
	number: int,
	comments: list[tuple[str, str]],
	*,
	head: str = HEAD_A,
	state: str = "OPEN",
	draft: bool = False,
	labels: tuple[str, ...] = (),
	closing: tuple[tuple[int, tuple[str, ...]], ...] = (),
	closing_total: int | None = None,
	updated: str | None = None,
) -> dict:
	return {
		"number": number,
		"state": state,
		"headRefName": f"feature/{number}",
		"headRefOid": head,
		"isDraft": draft,
		"mergeStateStatus": "BLOCKED",
		"updatedAt": updated or _now_iso(),
		"labels": {"nodes": [{"name": name} for name in labels]},
		"closingIssuesReferences": {
			"totalCount": len(closing) if closing_total is None else closing_total,
			"nodes": [{"number": num, "labels": {"nodes": [{"name": name} for name in names]}} for num, names in closing],
		},
		"comments": {
			"pageInfo": {"hasPreviousPage": False},
			"nodes": [{"body": body, "databaseId": idx + 1, "author": {"login": author}} for idx, (author, body) in enumerate(comments)],
		},
	}


def _run_cycle(
	rows: list[dict],
	nodes: list[dict],
	*,
	graphql_ok: bool = True,
	list_failed: bool = False,
	login: str = LOGIN,
	support_state: str = "ok",
	pinned: str = PINNED,
	cap: str = "5",
	max_age: str = "7",
	dispatch_rc: dict[int, int] | None = None,
	post_rc: int = 0,
	dump_map: bool = False,
) -> dict:
	"""Run the real batch slice and pass; return output lines and call logs."""
	_require_jq_for_issueless_retry()
	script = _poller_text()
	batch = script[script.index(BATCH_START):script.index(BATCH_END)]
	functions = (
		_issueless_bash_function(script, "_issueless_review_retry_decide() {")
		+ _issueless_bash_function(script, "run_issueless_review_retry_pass() {")
	)
	dispatch_rc = dispatch_rc or {}
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		prs_file = tmp_path / "prs.json"
		prs_file.write_text(json.dumps(rows), encoding="utf-8")
		graphql_file = tmp_path / "graphql.json"
		graphql_file.write_text(
			json.dumps({"data": {"repository": {f"p{node['number']}": node for node in nodes}}}),
			encoding="utf-8",
		)
		dispatch_log = tmp_path / "dispatch.log"
		post_dir = tmp_path / "posts"
		post_dir.mkdir()
		dispatch_cases = "".join(f"{n}) return {rc} ;; " for n, rc in dispatch_rc.items())
		lines = [
			"set -euo pipefail",
			"GITHUB_REPOSITORY=o/r",
			f"ISSUELESS_REVIEW_RETRY_MAX_PER_CYCLE={shlex.quote(cap)}",
			f"ISSUELESS_REVIEW_RETRY_MAX_AGE_DAYS={shlex.quote(max_age)}",
			_issueless_constants(script),
			"STANDALONE_CONFLICT_ATTEMPTS_MARKER='<!-- ai:standalone-conflict-attempts:v1 '",
			f"STANDALONE_PRS=\"$(cat {shlex.quote(str(prs_file))})\"",
			"STANDALONE_COUNT=\"$(printf '%s' \"${STANDALONE_PRS}\" | jq 'length')\"",
			f"_conflict_sweep_list_failed={'true' if list_failed else 'false'}",
			f"GRAPHQL_FILE={shlex.quote(str(graphql_file))}",
			f"GRAPHQL_OK={'1' if graphql_ok else '0'}",
			f"DISPATCH_LOG={shlex.quote(str(dispatch_log))}",
			f"POST_DIR={shlex.quote(str(post_dir))}",
			f"POST_RC={post_rc}",
			"gh() {",
			"  if [ \"${2:-}\" = graphql ]; then [ \"${GRAPHQL_OK}\" = 1 ] || return 1; cat \"${GRAPHQL_FILE}\"; return 0; fi",
			"  local prev='' arg n",
			"  n=\"$(ls \"${POST_DIR}\" | wc -l | tr -d ' ')\"",
			"  for arg in \"$@\"; do",
			"    if [ \"${prev}\" = -f ] && [ \"${arg#body=}\" != \"${arg}\" ]; then printf '%s' \"${arg#body=}\" > \"${POST_DIR}/${n}.body\"; fi",
			"    prev=\"${arg}\"",
			"  done",
			"  printf '%s\\n' \"$2\" > \"${POST_DIR}/${n}.path\"",
			"  return \"${POST_RC}\"",
			"}",
			"gh_retry() { \"$@\"; }",
			"UNBLOCK_TRUSTED_LOGIN=''",
			f"unblock_trusted_login() {{ UNBLOCK_TRUSTED_LOGIN={shlex.quote(login)}; printf '%s' \"${{UNBLOCK_TRUSTED_LOGIN}}\"; }}",
			f"NOOP_CAP_REVIEW_SUPPORT_STATE={shlex.quote(support_state)}",
			f"NOOP_CAP_REVIEW_SUPPORT_SHA={shlex.quote(pinned if support_state == 'ok' else '')}",
			"_noop_cap_review_support_sha() { return 0; }",
			f"_dispatch_review_for_conflicts() {{ printf '%s %s\\n' \"$1\" \"$2\" >> \"${{DISPATCH_LOG}}\"; case \"$1\" in {dispatch_cases}esac; return 0; }}",
			batch,
			functions,
		]
		if dump_map:
			lines.append('for _k in "${!_STANDALONE_ISSUELESS_RETRY_STATE[@]}"; do printf "ENTRY %s %s\\n" "${_k}" "${_STANDALONE_ISSUELESS_RETRY_STATE[${_k}]}"; done')
			lines.append('echo "PREFETCH_FAILED=${_STANDALONE_ISSUELESS_RETRY_PREFETCH_FAILED}"')
		lines.append('run_issueless_review_retry_pass || echo "ISSUELESS_REVIEW_RETRY outcome=skipped reason=error rc=$?"')
		lines.append('echo "SLICE_DONE"')
		harness = tmp_path / "harness.sh"
		harness.write_text("\n".join(lines) + "\n", encoding="utf-8")
		result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=120)
		posts = []
		for path_file in sorted(post_dir.glob("*.path"), key=lambda p: int(p.stem)):
			body_file = post_dir / f"{path_file.stem}.body"
			posts.append((path_file.read_text(encoding="utf-8").strip(), body_file.read_text(encoding="utf-8") if body_file.exists() else ""))
		out_lines = result.stdout.splitlines()
		entries = {}
		for line in out_lines:
			if line.startswith("ENTRY "):
				_, num, payload = line.split(" ", 2)
				entries[int(num)] = json.loads(payload)
		return {
			"rc": result.returncode,
			"stdout": result.stdout,
			"stderr": result.stderr,
			"lines": [line for line in out_lines if line.startswith("ISSUELESS_REVIEW_RETRY ")],
			"dispatches": dispatch_log.read_text(encoding="utf-8").splitlines() if dispatch_log.exists() else [],
			"posts": posts,
			"entries": entries,
		}


def _parsed(run: dict) -> list[dict]:
	parsed = []
	for line in run["lines"]:
		match = LINE_RE.match(line)
		assert match, line
		parsed.append(match.groupdict())
	return parsed


def _single(run: dict) -> dict:
	assert run["rc"] == 0, run["stderr"]
	assert "SLICE_DONE" in run["stdout"], run["stderr"]
	parsed = _parsed(run)
	assert len(parsed) == 1, run["lines"]
	return parsed[0]


def test_issueless_retry_retriggers_once_then_already_retried() -> None:
	comments = [(LOGIN, "AI autofix editor summary"), (LOGIN, _failure_body())]
	first = _run_cycle([_row(5219)], [_node(5219, comments)])
	line = _single(first)
	assert line["outcome"] == "retriggered" and line["pr"] == "5219"
	assert line["head"] == HEAD_A and line["support"] == OLD_SUPPORT and line["pinned"] == PINNED
	assert line["marker"] is None
	assert first["dispatches"] == ["5219 feature/5219"]
	assert len(first["posts"]) == 1
	path, body = first["posts"][0]
	assert path == "repos/o/r/issues/5219/comments"
	assert body == (
		f"Issue-less review retry: re-dispatched the review of `{HEAD_A}`; review support `{OLD_SUPPORT}` → `{PINNED}`.\n\n"
		f"<!-- issueless-review-retry:v1 head={HEAD_A} support={OLD_SUPPORT} -->"
	)
	assert "Issue-less review retry pass complete. Retriggered: 1." in first["stdout"]

	second = _run_cycle([_row(5219)], [_node(5219, comments + [(LOGIN, body)])])
	line = _single(second)
	assert line["outcome"] == "already_retried" and line["pr"] == "5219"
	assert second["dispatches"] == [] and second["posts"] == []


def test_issueless_retry_prefetch_failure_fails_open() -> None:
	comments = [(LOGIN, _failure_body())]
	run = _run_cycle([_row(10)], [_node(10, comments)], graphql_ok=False, dump_map=True)
	assert run["rc"] == 0, run["stderr"]
	assert "SLICE_DONE" in run["stdout"]
	assert run["entries"] == {}
	assert "PREFETCH_FAILED=true" in run["stdout"]
	assert run["lines"] == ["ISSUELESS_REVIEW_RETRY outcome=skipped reason=prefetch_unavailable repo=o/r"]
	assert run["dispatches"] == [] and run["posts"] == []

	listed_failed = _run_cycle([], [], list_failed=True, dump_map=True)
	assert listed_failed["rc"] == 0 and "PREFETCH_FAILED=true" in listed_failed["stdout"]
	assert listed_failed["lines"] == ["ISSUELESS_REVIEW_RETRY outcome=skipped reason=prefetch_unavailable repo=o/r"]


def test_issueless_retry_map_entry_shape_and_open_head_validation() -> None:
	rows = [_row(1), _row(2), _row(3, head=HEAD_B), _row(4, cross=None, owner=None)]
	nodes = [
		_node(1, [(LOGIN, _failure_body()), ("someone", "hi"), (LOGIN, _retry_body())], labels=("bug",), closing=((9, ("ai:implementing",)),)),
		_node(2, [(LOGIN, _failure_body())], state="CLOSED"),
		_node(3, [(LOGIN, _failure_body(head=HEAD_B))], head=HEAD_A),
		_node(4, [], closing_total=None),
	]
	run = _run_cycle(rows, nodes, dump_map=True)
	assert run["rc"] == 0, run["stderr"]
	assert "PREFETCH_FAILED=false" in run["stdout"]
	assert sorted(run["entries"]) == [1, 4]
	entry = run["entries"][1]
	assert entry["oid"] == HEAD_A and entry["ref"] == "feature/1" and entry["draft"] is False
	assert entry["cross"] == "false" and entry["owner"] == "o" and entry["labels"] == ["bug"]
	assert entry["closing_total"] == 1 and entry["closing_seen"] == 1 and entry["linked_pipeline"] is True
	assert entry["comments"][0] == {"a": LOGIN, "fail": {"head": HEAD_A, "reason": "reviewers_failed", "support": OLD_SUPPORT, "fp": "abcdef0123"}, "retry": None}
	assert entry["comments"][1] == {"a": "someone", "fail": None, "retry": None}
	assert entry["comments"][2] == {"a": LOGIN, "fail": None, "retry": {"head": HEAD_A, "support": OLD_SUPPORT}}
	assert run["entries"][4]["cross"] == "unknown" and run["entries"][4]["owner"] == ""


def test_issueless_retry_support_gates() -> None:
	same = _single(_run_cycle([_row(11)], [_node(11, [(LOGIN, _failure_body(support=PINNED))])]))
	assert same["outcome"] == "waiting_for_release"
	missing = _single(_run_cycle([_row(12)], [_node(12, [(LOGIN, _failure_body(support=None))])]))
	assert missing["outcome"] == "skipped" and missing["reason"] == "marker_support_missing"
	unresolved = _run_cycle([_row(13)], [_node(13, [(LOGIN, _failure_body())])], support_state="unresolved")
	line = _single(unresolved)
	assert line["outcome"] == "skipped" and line["reason"] == "support_unresolved" and line["pinned"] == "none"
	assert unresolved["dispatches"] == []


def test_issueless_retry_linked_pipeline_issue_is_ignored() -> None:
	linked = _single(_run_cycle([_row(21)], [_node(21, [(LOGIN, _failure_body())], closing=((7, ("ai:implementing",)),))]))
	assert linked["outcome"] == "skipped" and linked["reason"] == "linked_pipeline_issue"
	truncated = _single(_run_cycle([_row(22)], [_node(22, [(LOGIN, _failure_body())], closing=((7, ("bug",)),), closing_total=11)]))
	assert truncated["outcome"] == "skipped" and truncated["reason"] == "linked_pipeline_issue"
	unlabelled = _run_cycle([_row(23)], [_node(23, [(LOGIN, _failure_body())], closing=((7, ("bug",)),))])
	assert _single(unlabelled)["outcome"] == "retriggered"
	assert unlabelled["dispatches"] == ["23 feature/23"]


def test_issueless_retry_trust_and_own_line_markers() -> None:
	forged = _run_cycle([_row(31)], [_node(31, [("outsider", _failure_body())])])
	assert forged["rc"] == 0 and forged["lines"] == [] and forged["dispatches"] == []
	forged_retry = _run_cycle([_row(32)], [_node(32, [(LOGIN, _failure_body()), ("outsider", _retry_body())])])
	assert _single(forged_retry)["outcome"] == "retriggered"
	inline = (
		"First error: `<!-- review-autofix-failure:v1 head=" + HEAD_A + " reason=reviewers_failed support=" + OLD_SUPPORT + " -->` quoted"
	)
	quoted = _run_cycle([_row(33)], [_node(33, [(LOGIN, inline)])])
	assert quoted["lines"] == [] and quoted["dispatches"] == []
	later_summary = _run_cycle([_row(34)], [_node(34, [(LOGIN, _failure_body()), (LOGIN, "AI autofix editor summary")])])
	assert later_summary["lines"] == []
	no_login = _run_cycle([_row(35)], [_node(35, [(LOGIN, _failure_body())])], login="")
	assert no_login["lines"] == ["ISSUELESS_REVIEW_RETRY outcome=skipped reason=trusted_login_unresolved repo=o/r"]
	assert no_login["dispatches"] == []


def test_issueless_retry_eligibility_gates() -> None:
	fork = _single(_run_cycle([_row(41, cross=True)], [_node(41, [(LOGIN, _failure_body())])]))
	assert fork["reason"] == "fork_or_unknown_head"
	unknown = _single(_run_cycle([_row(42, cross=None)], [_node(42, [(LOGIN, _failure_body())])]))
	assert unknown["reason"] == "fork_or_unknown_head"
	other_owner = _single(_run_cycle([_row(43, owner="evil")], [_node(43, [(LOGIN, _failure_body())])]))
	assert other_owner["reason"] == "fork_or_unknown_head"
	draft = _single(_run_cycle([_row(44, draft=True)], [_node(44, [(LOGIN, _failure_body())], draft=True)]))
	assert draft["reason"] == "draft"
	for label in ("ai:review-blocked", "force-review", "ai:scope-blocked"):
		excluded = _single(_run_cycle([_row(45)], [_node(45, [(LOGIN, _failure_body())], labels=(label,))]))
		assert excluded["reason"] == "excluded_label", label
	old = _single(_run_cycle([_row(46)], [_node(46, [(LOGIN, _failure_body())], updated=_now_iso(days_ago=9))]))
	assert old["reason"] == "too_old"
	no_age_filter = _run_cycle([_row(47)], [_node(47, [(LOGIN, _failure_body())], updated=_now_iso(days_ago=9))], max_age="0")
	assert _single(no_age_filter)["outcome"] == "retriggered"
	moved = _run_cycle([_row(48, head=HEAD_B)], [_node(48, [(LOGIN, _failure_body(head=HEAD_A))], head=HEAD_B)])
	assert moved["lines"] == [] and moved["dispatches"] == []
	not_retryable = _run_cycle([_row(49)], [_node(49, [(LOGIN, _failure_body(reason="editor_empty_noop"))])])
	assert not_retryable["lines"] == []
	for reason in ("provider_unavailable", "sandbox_prepare_failed"):
		ok = _single(_run_cycle([_row(50)], [_node(50, [(LOGIN, _failure_body(reason=reason))])]))
		assert ok["outcome"] == "retriggered", reason


def test_issueless_retry_repeated_reviewer_failure_is_not_retried() -> None:
	# Issue #9533: two newest trusted reviewers_failed markers on this head
	# with one fp under the still-pinned support SHA mean a deterministic
	# crash; do not queue another review.
	fp_x = "c" * 64
	fp_y = "d" * 64
	repeated = _run_cycle([_row(61)], [_node(61, [(LOGIN, _failure_body(fp=fp_x, support=PINNED)), (LOGIN, _failure_body(fp=fp_x, support=PINNED))])])
	line = _single(repeated)
	assert line["outcome"] == "skipped" and line["reason"] == "repeated_reviewer_failure"
	assert line["head"] == HEAD_A
	assert repeated["dispatches"] == [] and repeated["posts"] == []
	# A different fp, a prior marker on another head or support, an untrusted
	# prior marker or a single failure keeps today's decision (wait for a
	# newer support release).
	for comments in (
		[(LOGIN, _failure_body(fp=fp_y, support=PINNED)), (LOGIN, _failure_body(fp=fp_x, support=PINNED))],
		[(LOGIN, _failure_body(head=HEAD_B, fp=fp_x, support=PINNED)), (LOGIN, _failure_body(fp=fp_x, support=PINNED))],
		[(LOGIN, _failure_body(fp=fp_x, support=OLD_SUPPORT)), (LOGIN, _failure_body(fp=fp_x, support=PINNED))],
		[("outsider", _failure_body(fp=fp_x, support=PINNED)), (LOGIN, _failure_body(fp=fp_x, support=PINNED))],
		[(LOGIN, _failure_body(fp=fp_x, support=PINNED))],
	):
		run = _run_cycle([_row(62)], [_node(62, comments)])
		assert _single(run)["outcome"] == "waiting_for_release", comments
		assert run["dispatches"] == []
	# A newer pinned support SHA still re-dispatches a repeated crash: the
	# gate counts per support version, so the new support gets a fresh count.
	newer = _run_cycle([_row(64)], [_node(64, [(LOGIN, _failure_body(fp=fp_x)), (LOGIN, _failure_body(fp=fp_x))])])
	assert _single(newer)["outcome"] == "retriggered"
	assert newer["dispatches"] == ["64 feature/64"]
	# Another reason with a repeated fp is out of scope for this rule.
	other = _run_cycle([_row(63)], [_node(63, [(LOGIN, _failure_body(reason="sandbox_prepare_failed", fp=fp_x, support=PINNED)), (LOGIN, _failure_body(reason="sandbox_prepare_failed", fp=fp_x, support=PINNED))])])
	assert _single(other)["outcome"] == "waiting_for_release"
	# REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false turns the rule off.
	saved = os.environ.get("REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED")
	os.environ["REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED"] = "false"
	try:
		disabled = _run_cycle([_row(65)], [_node(65, [(LOGIN, _failure_body(fp=fp_x, support=PINNED)), (LOGIN, _failure_body(fp=fp_x, support=PINNED))])])
	finally:
		if saved is None:
			os.environ.pop("REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED", None)
		else:
			os.environ["REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED"] = saved
	assert _single(disabled)["outcome"] == "waiting_for_release"


def test_issueless_retry_cycle_cap() -> None:
	rows = [_row(n) for n in range(101, 108)]
	nodes = [_node(n, [(LOGIN, _failure_body())]) for n in range(101, 108)]
	capped = _run_cycle(rows, nodes)
	parsed = _parsed(capped)
	assert [p["outcome"] for p in parsed].count("retriggered") == 5
	assert [p["pr"] for p in parsed if p["reason"] == "cycle_cap"] == ["106", "107"]
	assert len(capped["dispatches"]) == 5 and len(capped["posts"]) == 5
	log_only = _run_cycle(rows, nodes, cap="0")
	parsed = _parsed(log_only)
	assert len(parsed) == 7 and all(p["reason"] == "cycle_cap" for p in parsed)
	assert log_only["dispatches"] == [] and log_only["posts"] == []


def test_issueless_retry_dispatch_outcomes() -> None:
	comments = [(LOGIN, _failure_body())]
	inflight = _run_cycle([_row(61)], [_node(61, comments)], dispatch_rc={61: 2})
	assert _single(inflight)["outcome"] == "in_flight" and inflight["posts"] == []
	failed = _run_cycle([_row(62)], [_node(62, comments)], dispatch_rc={62: 1})
	line = _single(failed)
	assert line["outcome"] == "skipped" and line["reason"] == "dispatch_failed" and failed["posts"] == []
	write_failed = _run_cycle([_row(63)], [_node(63, comments)], post_rc=1)
	line = _single(write_failed)
	assert line["outcome"] == "retriggered" and line["marker"] == "write_failed"


def test_issueless_retry_batch_aliases_request_new_fields() -> None:
	script = _poller_text()
	alias_line = next(line for line in script.splitlines() if 'p${_standalone_num}: pullRequest(number: ${_standalone_num})' in line)
	for field in (
		"updatedAt",
		"labels(first: 30) { nodes { name } }",
		"closingIssuesReferences(first: 10) { totalCount nodes { number labels(first: 30) { nodes { name } } } }",
		"comments(last: 100)",
	):
		assert field in alias_line, field


def test_issueless_retry_pass_runs_after_noop_sweep() -> None:
	script = _poller_text()
	noop_summary = script.index('echo "Noop-suspicious recovery skipped (fingerprint cap already applied on head)')
	call = script.index('\nrun_issueless_review_retry_pass || echo "ISSUELESS_REVIEW_RETRY outcome=skipped reason=error rc=$?"\n')
	restore = script.rindex("\n_standalone_sweep_alert_level_restore")
	assert noop_summary < call < restore
	assert script.count("\nrun_issueless_review_retry_pass ") == 1
	# The retry pass reuses the existing dispatch path; retrigger_review is not called.
	body = _issueless_bash_function(script, "run_issueless_review_retry_pass() {")
	assert '_dispatch_review_for_conflicts "${pr}" "${head_ref}"' in body
	assert "retrigger_review" not in body


def test_issueless_retry_label_sets_match_their_sources() -> None:
	script = _poller_text()
	literal = re.search(r"local pipeline_labels='(\[[^']*\])'", script)
	copy = re.search(r"^ISSUELESS_REVIEW_RETRY_PIPELINE_LABELS_JSON='(\[[^']*\])'$", script, re.MULTILINE)
	assert literal and copy
	assert json.loads(copy.group(1)) == json.loads(literal.group(1))
	exclude = json.loads(re.search(r"^ISSUELESS_REVIEW_RETRY_EXCLUDE_LABELS_JSON='(\[[^']*\])'$", script, re.MULTILINE).group(1))
	tree = ast.parse(ORCHESTRATE_LIB.read_text(encoding="utf-8"))
	latch: list[str] = []
	for node in tree.body:
		target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
		if isinstance(target, ast.Name) and target.id == "STALL_RECOVERY_LATCH_LABELS":
			latch = list(ast.literal_eval(node.value))
	assert latch
	for label in latch + ["ai:review-blocked", "ai:needs-human", "ai:claude-capacity-wait", "force-review", "e2e-smoke-test"]:
		assert label in exclude, label


def test_issueless_retry_workflow_env_and_agents_fragment() -> None:
	workflow = POLL_WORKFLOW.read_text(encoding="utf-8")
	assert workflow.count("          ISSUELESS_REVIEW_RETRY_MAX_PER_CYCLE: ${{ vars.ISSUELESS_REVIEW_RETRY_MAX_PER_CYCLE || '5' }}\n") == 3
	assert workflow.count("          ISSUELESS_REVIEW_RETRY_MAX_AGE_DAYS: ${{ vars.ISSUELESS_REVIEW_RETRY_MAX_AGE_DAYS || '7' }}\n") == 3
	assert workflow.count("STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS: ${{") == 3
	fragment = AGENTS_FRAGMENT.read_text(encoding="utf-8")
	assert '<!-- agents: section="Stable log prefixes (contractual)" -->' in fragment
	assert "\nLOG_PREFIX.name=ISSUELESS_REVIEW_RETRY\n" in fragment
	script = _poller_text()
	assert 'ISSUELESS_REVIEW_RETRY_MAX_PER_CYCLE="${ISSUELESS_REVIEW_RETRY_MAX_PER_CYCLE:-5}"' in script
	assert 'ISSUELESS_REVIEW_RETRY_MAX_AGE_DAYS="${ISSUELESS_REVIEW_RETRY_MAX_AGE_DAYS:-7}"' in script


# Post editor summary step: the no-output comment names the issue-less pass.
_ISSUELESS_FAKE_GH = """gh() {
  local prev="" arg
  for arg in "$@"; do
    if [ "${prev}" = "-f" ] && [ "${arg#body=}" != "${arg}" ]; then
      printf '%s' "${arg#body=}" > "${FAKE_GH_BODY_FILE}"
    fi
    prev="${arg}"
  done
  return 0
}
gh_retry() { "$@"; }
"""
LEGACY_SENTENCE = "The stall poller will retry the review on a subsequent cycle."
ISSUELESS_SENTENCE = "the orchestrator poller's issue-less review retry pass re-dispatches this review once"


def _run_noop_step(linked_issues_json: str | None) -> tuple[int, str]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		support_dir = tmp_path / "support"
		support_dir.mkdir()
		(support_dir / "gh_helpers.sh").write_text(_ISSUELESS_FAKE_GH, encoding="utf-8")
		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir()
		summary_file = tmp_path / "editor_summary.txt"
		summary_file.write_text("", encoding="utf-8")
		committed_file = tmp_path / "committed_files.txt"
		committed_file.write_text("", encoding="utf-8")
		github_env = tmp_path / "github_env"
		github_env.write_text("", encoding="utf-8")
		body_file = tmp_path / "posted_body.txt"
		env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"HOME": str(tmp_path),
			"PYTHONDONTWRITEBYTECODE": "1",
			"FAKE_GH_BODY_FILE": str(body_file),
			"SUPPORT_SCRIPTS_DIR": str(support_dir),
			"RUNTIME_DIR": str(runtime_dir),
			"EDITOR_SUMMARY_FILE": str(summary_file),
			"COMMITTED_FILES_FILE": str(committed_file),
			"PR_EDITOR_COMMENT_FILE": str(tmp_path / "pr_editor_comment.txt"),
			"GITHUB_ENV": str(github_env),
			"PR_NUMBER": "5219",
			"AUTOFIX_STEP_REPOSITORY": "owner/repo",
			"AUTOFIX_STEP_SERVER_URL": "https://github.com",
			"AUTOFIX_STEP_RUN_ID": "4242",
			"GITHUB_RUN_ID": "4242",
			"REVIEWERS_STEP_OUTCOME": "failure",
		}
		if linked_issues_json is not None:
			env["LINKED_ISSUES_JSON"] = linked_issues_json
		result = subprocess.run(["bash", str(STEP_SCRIPT)], capture_output=True, text=True, env=env, timeout=120)
		body = body_file.read_text(encoding="utf-8") if body_file.exists() else ""
		return result.returncode, body


def test_issueless_retry_noop_comment_names_the_issueless_pass() -> None:
	_require_jq_for_issueless_retry()
	rc, body = _run_noop_step("[]")
	assert rc == 1
	assert body.startswith("**AI review/autofix produced no output — will retry**")
	assert ISSUELESS_SENTENCE in body and LEGACY_SENTENCE not in body
	for value in (None, '[{"number":1}]', "not json", ""):
		rc, body = _run_noop_step(value)
		assert rc == 1, value
		assert body.startswith("**AI review/autofix produced no output — will retry**"), value
		assert LEGACY_SENTENCE in body and ISSUELESS_SENTENCE not in body, value
