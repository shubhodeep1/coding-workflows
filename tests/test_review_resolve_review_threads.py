#!/usr/bin/env python3
"""Tests for review-thread resolution in the autofix pipeline.

The pipeline has always read PR review comments into the reviewer and
editor prompts, but nothing ever marked a thread resolved, so a comment
the editor fixed looked identical to one it never read. These tests pin
the mapping rules that decide which threads get resolved, and the
guard that keeps a mis-keyed audit entry from burying live feedback.

The mis-key regression is taken from shubhodeep1/fun-token-multi-chain
PR #404: a reviewer bot posted two contradictory findings at the same
services/session-server/src/repository.ts:1383 location an hour apart,
and the editor audited the older one as "already satisfied". Resolving
by path+line would have closed the newer, still-valid comment.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_BUILDER = REPO_ROOT / "scripts" / "review_resolve_review_threads_plan.py"
DRIVER = REPO_ROOT / "scripts" / "review_resolve_review_threads.sh"
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
REVIEW_AUTOFIX_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _context_entry(index: int, kind: str, comment_id: str, path: str, author: str = "copilot") -> str:
	return textwrap.dedent(
		f"""\
		entry[{index}].kind: {kind}
		entry[{index}].id: {comment_id}
		entry[{index}].author: {author}
		entry[{index}].path: {path}
		"""
	)


def _write_inputs(
	tmp_path: Path,
	audit_lines: str,
	context: str,
	threads: list[dict],
) -> dict[str, str]:
	summary = tmp_path / "editor_summary.txt"
	summary.write_text(
		"Changes made:\n- edited something\n\nPR comment audit:\n"
		+ audit_lines
		+ "\nRegression fingerprint:\n- n/a\n",
		encoding="utf-8",
	)
	context_file = tmp_path / "pr_all_comments_context.txt"
	context_file.write_text("PR_ALL_COMMENTS_CONTEXT\n\n" + context, encoding="utf-8")
	threads_file = tmp_path / "threads.json"
	threads_file.write_text(json.dumps(threads), encoding="utf-8")
	plan_file = tmp_path / "plan.jsonl"
	return {
		"EDITOR_SUMMARY_FILE": str(summary),
		"PR_ALL_COMMENTS_CONTEXT_FILE": str(context_file),
		"THREADS_JSON": str(threads_file),
		"PLAN_FILE": str(plan_file),
	}


def _build_plan(tmp_path: Path, audit_lines: str, context: str, threads: list[dict], **extra: str) -> list[dict]:
	env = os.environ.copy()
	env.update(_write_inputs(tmp_path, audit_lines, context, threads))
	env.update(extra)
	env.setdefault("REVIEW_APPLIED_CHANGES_PERSISTED", "true")
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(
		["python3", str(PLAN_BUILDER)],
		env=env,
		capture_output=True,
		text=True,
	)
	assert result.returncode == 0, result.stderr
	plan_path = Path(env["PLAN_FILE"])
	if not plan_path.exists():
		return []
	return [json.loads(line) for line in plan_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_applied_entry_is_resolved(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `services/session-server/src/repository.ts:1304` — applied; reserved state now precedes transfer.\n",
		_context_entry(0, "review_comment", "3859318471", "services/session-server/src/repository.ts"),
		[{"thread_id": "PRRT_a", "is_resolved": False, "comment_ids": [3859318471], "path": "services/session-server/src/repository.ts", "author": "copilot"}],
	)
	assert len(plan) == 1
	assert plan[0]["thread_id"] == "PRRT_a"
	assert plan[0]["disposition"] == "applied"


def test_already_satisfied_entry_is_resolved(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `lobby-frontend/lib/feeTank.ts:60` — already satisfied; receipt wait is deadline-bounded.\n",
		_context_entry(0, "review_comment", "42", "lobby-frontend/lib/feeTank.ts"),
		[{"thread_id": "PRRT_b", "is_resolved": False, "comment_ids": [42], "path": "lobby-frontend/lib/feeTank.ts", "author": "copilot"}],
	)
	assert [item["disposition"] for item in plan] == ["already satisfied"]


def test_ignored_entry_is_resolved_and_carries_its_reason(tmp_path: Path) -> None:
	"""An 'ignored' thread still resolves, but the reason must survive.

	The driver posts that reason into the thread before resolving, which
	is what lets a reviewer see the disagreement and reopen.
	"""
	plan = _build_plan(
		tmp_path,
		"- entry[3] Copilot — `services/session-server/src/feetank/adapters.ts:8` — ignored; ethers BigNumber does not wrap at uint256.\n",
		_context_entry(3, "review_comment", "77", "services/session-server/src/feetank/adapters.ts"),
		[{"thread_id": "PRRT_c", "is_resolved": False, "comment_ids": [77], "path": "services/session-server/src/feetank/adapters.ts", "author": "copilot"}],
	)
	assert len(plan) == 1
	assert plan[0]["disposition"] == "ignored"
	assert "BigNumber does not wrap" in plan[0]["reason"]


def test_ignored_entry_without_a_reason_stays_open(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:8` — ignored;\n",
		_context_entry(0, "review_comment", "77", "src/app.ts"),
		[{"thread_id": "PRRT_c", "is_resolved": False, "comment_ids": [77], "path": "src/app.ts", "author": "copilot"}],
	)
	assert plan == []


def test_long_ignored_reason_is_truncated_with_ellipsis(tmp_path: Path) -> None:
	long_reason = "x" * 600
	plan = _build_plan(
		tmp_path,
		f"- entry[0] Copilot — `src/app.ts:8` — ignored; {long_reason}\n",
		_context_entry(0, "review_comment", "77", "src/app.ts"),
		[{"thread_id": "PRRT_c", "is_resolved": False, "comment_ids": [77], "path": "src/app.ts", "author": "copilot"}],
	)
	assert len(plan[0]["reason"]) == 500
	assert plan[0]["reason"].endswith("...")
	assert plan[0]["reason"] == ("x" * 497) + "..."


def test_reason_keywords_do_not_override_the_disposition(tmp_path: Path) -> None:
	context = _context_entry(0, "review_comment", "77", "src/app.ts")
	threads = [{"thread_id": "PRRT_c", "is_resolved": False, "comment_ids": [77], "path": "src/app.ts", "author": "copilot"}]
	ignored_plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:8` — ignored; the change was already applied elsewhere.\n",
		context,
		threads,
	)
	assert ignored_plan[0]["disposition"] == "ignored"
	assert "already applied elsewhere" in ignored_plan[0]["reason"]

	applied_plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:8` — applied; skipped the unrelated rename.\n",
		context,
		threads,
	)
	assert applied_plan[0]["disposition"] == "applied"


def test_negated_fixed_and_addressed_dispositions_are_ignored(tmp_path: Path) -> None:
	context = _context_entry(0, "review_comment", "77", "src/app.ts")
	threads = [{"thread_id": "PRRT_c", "is_resolved": False, "comment_ids": [77], "path": "src/app.ts", "author": "copilot"}]
	for disposition_text in ("not fixed", "not addressed"):
		plan = _build_plan(
			tmp_path,
			f"- entry[0] Copilot — `src/app.ts:8` — {disposition_text}; the code is intentional.\n",
			context,
			threads,
		)
		assert plan[0]["disposition"] == "ignored"


def test_applied_entry_requires_a_productive_commit(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:8` — applied; guard added.\n",
		_context_entry(0, "review_comment", "77", "src/app.ts"),
		[{"thread_id": "PRRT_c", "is_resolved": False, "comment_ids": [77], "path": "src/app.ts", "author": "copilot"}],
		REVIEW_APPLIED_CHANGES_PERSISTED="false",
	)
	assert plan == []


def test_unlisted_entry_at_a_shared_location_stays_open(tmp_path: Path) -> None:
	"""PR #404 regression: two comments at one path, only one audited.

	The editor audited entry[9] (the older comment) as already satisfied.
	entry[10] is a newer, contradictory comment at the *same* path. Only
	the audited index may resolve; resolving by path would close both.
	"""
	context = (
		_context_entry(9, "review_comment", "3859567333", "services/session-server/src/repository.ts")
		+ _context_entry(10, "review_comment", "3859796825", "services/session-server/src/repository.ts")
	)
	threads = [
		{"thread_id": "PRRT_old", "is_resolved": False, "comment_ids": [3859567333], "path": "services/session-server/src/repository.ts", "author": "copilot"},
		{"thread_id": "PRRT_new", "is_resolved": False, "comment_ids": [3859796825], "path": "services/session-server/src/repository.ts", "author": "copilot"},
	]
	plan = _build_plan(
		tmp_path,
		"- entry[9] Copilot — `services/session-server/src/repository.ts:1383` — already satisfied; stale reserved rows stop counting after five minutes.\n",
		context,
		threads,
	)
	assert [item["thread_id"] for item in plan] == ["PRRT_old"]


def test_comment_body_cannot_redirect_a_resolve(tmp_path: Path) -> None:
	"""A comment body must not be able to overwrite another entry's id.

	PR_ALL_COMMENTS_CONTEXT_FILE dumps each comment's prose under
	entry[N].body after the structured fields. That prose is written by
	anyone who can comment on the PR, so a body containing a line shaped
	like "entry[0].id: <other>" would, under last-wins parsing, point
	entry[0] at an unrelated thread. First-wins parsing keeps the real
	field values authoritative.
	"""
	context = (
		_context_entry(0, "review_comment", "1234", "src/app.ts")
		+ "entry[0].body: please fix this\n"
		+ "entry[0].id: 9999\n"
		+ "entry[0].path: src/unrelated.ts\n"
	)
	threads = [
		{"thread_id": "PRRT_real", "is_resolved": False, "comment_ids": [1234], "path": "src/app.ts", "author": "copilot"},
		{"thread_id": "PRRT_victim", "is_resolved": False, "comment_ids": [9999], "path": "src/unrelated.ts", "author": "alice"},
	]
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; guard added.\n",
		context,
		threads,
	)
	assert [item["thread_id"] for item in plan] == ["PRRT_real"]


def test_path_disagreement_disqualifies_the_entry(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `services/session-server/src/server.ts:246` — applied; adapters map now spreads defined adapters.\n",
		_context_entry(0, "review_comment", "55", "services/session-server/src/repository.ts"),
		[{"thread_id": "PRRT_d", "is_resolved": False, "comment_ids": [55], "path": "services/session-server/src/repository.ts", "author": "copilot"}],
	)
	assert plan == []


def test_issue_comment_entry_has_no_thread(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[5] shubhodeep1 — applied; addressed in this pass.\n",
		_context_entry(5, "issue_comment", "999", ""),
		[{"thread_id": "PRRT_e", "is_resolved": False, "comment_ids": [999], "path": "", "author": "shubhodeep1"}],
	)
	assert plan == []


def test_human_authored_thread_is_eligible(tmp_path: Path) -> None:
	"""Resolution is not restricted to bot reviewers."""
	plan = _build_plan(
		tmp_path,
		"- entry[0] alice — `src/app.ts:10` — applied; guard added.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts", author="alice"),
		[{"thread_id": "PRRT_h", "is_resolved": False, "comment_ids": [1234], "path": "src/app.ts", "author": "alice"}],
	)
	assert [item["author"] for item in plan] == ["alice"]


def test_audited_reply_comment_resolves_its_thread(tmp_path: Path) -> None:
	"""An audited id may belong to a reply, not the thread anchor.

	PR_ALL_COMMENTS_CONTEXT_FILE is built from GET /pulls/<n>/comments,
	which returns the flat comment list with replies included, so the
	thread index has to cover every comment in a thread. Indexing only
	the anchor left audited reply ids unmatched — including replies this
	pipeline posts itself on "ignored" threads.
	"""
	plan = _build_plan(
		tmp_path,
		"- entry[1] Copilot — `src/app.ts:10` — applied; follow-up handled.\n",
		_context_entry(1, "review_comment", "5678", "src/app.ts"),
		[{"thread_id": "PRRT_thread", "is_resolved": False, "comment_ids": [1234, 5678], "path": "src/app.ts", "author": "copilot"}],
	)
	assert [item["thread_id"] for item in plan] == ["PRRT_thread"]
	# Matching remains keyed on the audited reply, while rationale replies
	# target the top-level comment because GitHub rejects replies to replies.
	assert plan[0]["comment_id"] == 5678
	assert plan[0]["reply_comment_id"] == 1234


def test_multiple_audited_comments_in_one_thread_are_planned_once(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		(
			"- entry[0] Copilot — `src/app.ts:10` — ignored; original suggestion is inapplicable.\n"
			"- entry[1] Copilot — `src/app.ts:10` — ignored; follow-up is also inapplicable.\n"
		),
		(
			_context_entry(0, "review_comment", "1234", "src/app.ts")
			+ _context_entry(1, "review_comment", "5678", "src/app.ts")
		),
		[{"thread_id": "PRRT_thread", "is_resolved": False, "comment_ids": [1234, 5678], "path": "src/app.ts", "author": "copilot"}],
	)
	assert len(plan) == 1
	assert plan[0]["comment_id"] == 1234


def test_ignored_duplicate_keeps_the_rationale_reply_requirement(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		(
			"- entry[0] Copilot — `src/app.ts:10` — already satisfied; original suggestion is covered.\n"
			"- entry[1] Copilot — `src/app.ts:10` — ignored; follow-up asks for unrelated behavior.\n"
		),
		(
			_context_entry(0, "review_comment", "1234", "src/app.ts")
			+ _context_entry(1, "review_comment", "5678", "src/app.ts")
		),
		[{"thread_id": "PRRT_thread", "is_resolved": False, "comment_ids": [1234, 5678], "path": "src/app.ts", "author": "copilot"}],
	)
	assert len(plan) == 1
	assert plan[0]["comment_id"] == 5678
	assert plan[0]["disposition"] == "ignored"
	assert "unrelated behavior" in plan[0]["reason"]


def test_already_resolved_thread_is_not_touched(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; done.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[{"thread_id": "PRRT_f", "is_resolved": True, "comment_ids": [1234], "path": "src/app.ts", "author": "copilot"}],
	)
	assert plan == []


def test_none_audit_produces_no_plan(tmp_path: Path) -> None:
	plan = _build_plan(
		tmp_path,
		"- none; no PR review or review_comment entries were present.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[{"thread_id": "PRRT_g", "is_resolved": False, "comment_ids": [1234], "path": "src/app.ts", "author": "copilot"}],
	)
	assert plan == []


def test_cap_truncates_and_reports_what_stayed_open(tmp_path: Path) -> None:
	audit = "".join(
		f"- entry[{i}] Copilot — `src/f{i}.ts:1` — applied; fixed.\n" for i in range(4)
	)
	context = "".join(_context_entry(i, "review_comment", str(100 + i), f"src/f{i}.ts") for i in range(4))
	threads = [
		{"thread_id": f"PRRT_{i}", "is_resolved": False, "comment_ids": [100 + i], "path": f"src/f{i}.ts", "author": "copilot"}
		for i in range(4)
	]
	env = os.environ.copy()
	env.update(_write_inputs(tmp_path, audit, context, threads))
	env["MAX_THREADS"] = "2"
	env["REVIEW_APPLIED_CHANGES_PERSISTED"] = "true"
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	result = subprocess.run(["python3", str(PLAN_BUILDER)], env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	plan = [json.loads(line) for line in Path(env["PLAN_FILE"]).read_text(encoding="utf-8").splitlines() if line.strip()]
	assert len(plan) == 2
	# The cap must be reported, never silent (§15).
	assert "REVIEW_RESOLVE_THREADS_MAX=2" in result.stderr
	assert "102" in result.stderr and "103" in result.stderr


def _run_driver(tmp_path: Path, gh_script: str, **extra: str) -> subprocess.CompletedProcess:
	"""Run the shell driver against a stub `gh` on PATH."""
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh_stub = bin_dir / "gh"
	gh_stub.write_text(gh_script, encoding="utf-8")
	gh_stub.chmod(0o755)

	env = os.environ.copy()
	env["PATH"] = f"{bin_dir}:{env['PATH']}"
	env["GITHUB_REPOSITORY"] = "owner/repo"
	env["PR_NUMBER"] = "404"
	env["GH_TOKEN"] = "stub"
	env["CALL_LOG"] = str(tmp_path / "calls.log")
	env["REVIEW_APPLIED_CHANGES_PERSISTED"] = "true"
	env.pop("GITHUB_ENV", None)
	env.update(extra)
	return subprocess.run(["bash", str(DRIVER)], env=env, capture_output=True, text=True)


GH_STUB = """#!/usr/bin/env bash
echo "$@" >> "${CALL_LOG}"
if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then
	case "$*" in
		*resolveReviewThread*)
			if [ -n "${RESOLVE_RESPONSE:-}" ]; then
				printf '%s\n' "${RESOLVE_RESPONSE}"
			else
				echo '{"data":{"resolveReviewThread":{"thread":{"id":"PRRT_a","isResolved":true}}}}'
			fi
			;;
		*)
			cat "${THREADS_FIXTURE}"
			;;
	esac
	exit 0
fi
if [ "$1" = "api" ]; then
	if [ "${REPLY_FAIL:-false}" = "true" ] && [[ "$*" == *"/replies"* ]]; then
		exit 1
	fi
	echo '{"id":1}'
	exit 0
fi
exit 0
"""


def test_driver_resolves_an_applied_thread(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; guard added.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"pageInfo": {"hasNextPage": False, "endCursor": None},
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {"nodes": [{"databaseId": 1234, "path": "src/app.ts", "author": {"login": "copilot"}}]},
									}
								],
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "resolved=1" in result.stdout
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "resolveReviewThread" in calls
	# An applied disposition must not post a reply.
	assert "replies" not in calls


def test_driver_does_not_count_graphql_payload_errors(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; guard added.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {"nodes": [{"databaseId": 1234, "path": "src/app.ts"}]},
									}
								]
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		RESOLVE_RESPONSE='{"data":{"resolveReviewThread":null},"errors":[{"message":"denied"}]}',
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "resolved=0" in result.stdout
	assert "skipped=1" in result.stdout
	assert "resolve mutation failed" in result.stdout


def test_driver_warns_when_thread_query_returns_graphql_errors(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; guard added.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_error.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {"nodes": [{"databaseId": 1234, "path": "src/app.ts"}]},
									}
								]
							}
						}
					}
				}
			}
		)
		+ "\n"
		+ json.dumps({"data": None, "errors": [{"message": "denied"}]}),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "review-thread query returned GraphQL errors or incomplete data" in result.stdout
	assert "resolved=0" in result.stdout
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "resolveReviewThread" not in calls


def test_driver_replies_before_resolving_an_ignored_thread(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — ignored; the reviewer misread the guard.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"pageInfo": {"hasNextPage": False, "endCursor": None},
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {"nodes": [{"databaseId": 1234, "path": "src/app.ts", "author": {"login": "copilot"}}]},
									}
								],
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "resolved=1" in result.stdout
	assert "replied=1" in result.stdout
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "comments/1234/replies" in calls


def test_driver_replies_to_thread_anchor_when_a_reply_was_audited(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[1] Copilot — `src/app.ts:10` — ignored; the follow-up is inapplicable.\n",
		_context_entry(1, "review_comment", "5678", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"pageInfo": {"hasNextPage": False, "endCursor": None},
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {
											"nodes": [
												{"databaseId": 1234, "path": "src/app.ts", "replyTo": None, "author": {"login": "copilot"}},
												{"databaseId": 5678, "path": "src/app.ts", "replyTo": {"databaseId": 1234}, "author": {"login": "alice"}},
											]
										},
									}
								],
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "resolved=1" in result.stdout
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "comments/1234/replies" in calls
	assert "comments/5678/replies" not in calls


def test_driver_renders_ignored_reason_without_markdown_or_mentions(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — ignored; @org/team requested **bold** [text](https://example.com).\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"pageInfo": {"hasNextPage": False, "endCursor": None},
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {"nodes": [{"databaseId": 1234, "path": "src/app.ts", "author": {"login": "copilot"}}]},
									}
								],
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "@org/team" not in calls
	assert "\n    @\u200borg/team requested **bold** [text](https://example.com).\n" in calls


def test_driver_leaves_ignored_thread_open_when_rationale_reply_fails(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — ignored; the reviewer misread the guard.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"pageInfo": {"hasNextPage": False, "endCursor": None},
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
										"comments": {"nodes": [{"databaseId": 1234, "path": "src/app.ts", "author": {"login": "copilot"}}]},
									}
								],
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		REPLY_FAIL="true",
		GH_RETRY_MAX_ATTEMPTS="1",
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "resolved=0" in result.stdout
	assert "replied=0" in result.stdout
	assert "skipped=1" in result.stdout
	assert "leaving thread PRRT_a open" in result.stdout
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "comments/1234/replies" in calls
	assert "resolveReviewThread" not in calls


def test_driver_does_not_repeat_an_existing_rationale_reply(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — ignored; the reviewer misread the guard.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	fixture = tmp_path / "graphql_page.json"
	fixture.write_text(
		json.dumps(
			{
				"data": {
					"repository": {
						"pullRequest": {
							"reviewThreads": {
								"pageInfo": {"hasNextPage": False, "endCursor": None},
								"nodes": [
									{
										"id": "PRRT_a",
										"isResolved": False,
									"comments": {
										"nodes": [
											{"databaseId": 1234, "path": "src/app.ts", "author": {"login": "copilot"}},
											{
												"databaseId": 5678,
												"path": "src/app.ts",
												"body": "<!-- ai-autofix-review-resolution:PRRT_a -->\nprior rationale",
												"viewerDidAuthor": True,
												"author": {"login": "codex"},
											},
										]
									},
								}
								],
							}
						}
					}
				}
			}
		),
		encoding="utf-8",
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE=str(fixture),
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0, result.stderr
	assert "resolved=1" in result.stdout
	assert "replied=0" in result.stdout
	calls = Path(str(tmp_path / "calls.log")).read_text(encoding="utf-8")
	assert "comments/1234/replies" not in calls


def test_driver_is_disabled_by_the_kill_switch(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; guard added.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	result = _run_driver(
		tmp_path,
		GH_STUB,
		THREADS_FIXTURE="/nonexistent",
		REVIEW_RESOLVE_THREADS_ENABLED="false",
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0
	assert "resolved=0" in result.stdout
	assert not Path(str(tmp_path / "calls.log")).exists()


def test_driver_fails_open_when_the_thread_query_fails(tmp_path: Path) -> None:
	files = _write_inputs(
		tmp_path,
		"- entry[0] Copilot — `src/app.ts:10` — applied; guard added.\n",
		_context_entry(0, "review_comment", "1234", "src/app.ts"),
		[],
	)
	failing_stub = '#!/usr/bin/env bash\necho "$@" >> "${CALL_LOG}"\nexit 1\n'
	result = _run_driver(
		tmp_path,
		failing_stub,
		GH_RETRY_MAX_ATTEMPTS="1",
		THREADS_FIXTURE="/nonexistent",
		EDITOR_SUMMARY_FILE=files["EDITOR_SUMMARY_FILE"],
		PR_ALL_COMMENTS_CONTEXT_FILE=files["PR_ALL_COMMENTS_CONTEXT_FILE"],
	)
	assert result.returncode == 0
	assert "review-thread query failed" in result.stdout
	assert "resolved=0" in result.stdout


def test_scripts_are_staged_for_consumer_repos() -> None:
	staging = STAGE_HELPER.read_text(encoding="utf-8")
	assert "review_resolve_review_threads.sh" in staging
	assert "review_resolve_review_threads_plan.py" in staging


def test_workflow_invokes_the_resolver_after_editor_safety_checks() -> None:
	workflow = REVIEW_AUTOFIX_WF.read_text(encoding="utf-8")
	assert "review_resolve_review_threads.sh" in workflow
	assert "REVIEW_RESOLVE_THREADS_ENABLED" in workflow
	summary_step = workflow.index("- name: Post editor summary comment")
	changes_lost_step = workflow.index("- name: Detect editor-claimed-but-uncommitted changes")
	noop_step = workflow.index("- name: Validate editor no-op disposition")
	push_step = workflow.index("- name: Push all pending commits")
	resolve_step = workflow.index("- name: Resolve addressed PR review threads")
	assert summary_step < changes_lost_step < noop_step < push_step < resolve_step
	resolve_block = workflow[resolve_step:workflow.index("\n      - name:", resolve_step + 1)]
	assert 'if: "success() &&' in resolve_block
	assert "env.EDITOR_CHANGES_LOST != 'true'" in resolve_block
	assert "env.EDITOR_NOOP_SUSPICIOUS != 'true'" in resolve_block
	assert "REVIEW_APPLIED_CHANGES_PERSISTED: ${{ env.AUTOFIX_EDITS_PUSHED == 'true' && steps.commit_changes.outputs.did_commit == 'true' && steps.commit_changes.outputs.ledger_only_commit_strict != 'true' }}" in resolve_block
