#!/usr/bin/env python3
"""Contract tests for the noop-suspicious recovery sweep in
`scripts/orchestrate_poll_process.sh`.

The sweep recovers PRs that hit
`EDITOR_NOOP_SUSPICIOUS=true` in `.github/workflows/review_autofix.yml`
and would otherwise stall forever (the workflow's "Enable auto-merge"
step is gated off). Issue-linked PRs eventually recover via the
generic standalone-stall recovery path, but linked-issue-less PRs
(branches like `claude/**`) never do — the original incident was
shubhodeep1/tele-funtoken-msg-scoring#3053.

These tests are grep-based contracts on the sweep's invariants:
detection, retry counter shape, dispatch path, force-merge gates,
shared-helper reuse, and Telegram alert levels. They are deliberately
NOT behavioural — the existing `tests/test_orchestrate_poll_process.py`
infrastructure for sandboxed poller runs is heavy enough that adding
behavioural fixtures for this sweep would dwarf the sweep itself. The
contracts cover the failure modes that matter:

  1. The sweep section header and its core invariants survive future
     edits.
  2. The retry threshold matches the design spec (3).
  3. Detection uses the exact warning literal the workflow emits
     (`⚠️ **Editor no-op suspicious**`) — drift between the two
     would break the recovery silently.
  4. The force-merge gate consults the shared audit helper, not a
     local re-implementation that could drift from review_autofix.yml.
  5. Force-merge fails CLOSED on every individual precondition.
  6. The sweep reuses the conflict sweep's `STANDALONE_PRS` cache
     (no second `gh pr list` per cycle, per CLAUDE.md §15).
"""

from __future__ import annotations

import re
import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
HELPER = REPO_ROOT / "scripts" / "validate_editor_audit.sh"

SWEEP_HEADER = "Standalone PR noop-suspicious recovery sweep"
NOOP_WARNING_LITERAL = "⚠️ **Editor no-op suspicious**"


def test_idle_poller_replays_only_trusted_failed_reclarify_once(tmp_path: Path) -> None:
	"""A source command, not a bot marker alone, authorizes the PAT write."""
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text('''#!/usr/bin/env bash
if [ "$1:$2" = "api:rate_limit" ]; then echo "${BUDGET}"; exit 0; fi
if [ "$1:$2" = "api:user" ]; then echo owner; exit 0; fi
if [ "$1:$2" = "issue:list" ]; then echo '[{"number":7}]'; exit 0; fi
if [ "$1:$2" = "api:--paginate" ]; then cat "${COMMENTS}"; exit 0; fi
if [ "$1:$2" = "api:repos/o/r/issues/7" ]; then
  echo '{"number":7,"state":"open","labels":[{"name":"ai:reclarify-requeue"}]}'; exit 0
fi
if [ "$1:$2" = "api:-X" ]; then echo "$*" >> "${CALLS}"; exit 0; fi
exit 1
''')
	gh.chmod(0o755)
	comments = tmp_path / "comments.json"
	calls = tmp_path / "calls"
	source = {"id": 11, "created_at": "2026-10-03T01:00:00Z", "body": "/reclarify",
		"user": {"type": "User", "login": "owner"}, "author_association": "OWNER"}
	marker = {"id": 12, "created_at": "2026-10-03T01:01:00Z",
		"body": "<!-- ai:reclarify-requeue:v1 source=11 -->",
		"user": {"type": "Bot", "login": "github-actions[bot]"}}
	env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r",
		"RECLARIFY_REQUEUE_SWEEP_ONLY": "true", "RECLARIFY_UNROUTED_DETECT_ENABLED": "false",
		"GH_TOKEN": "test", "CALLS": str(calls), "COMMENTS": str(comments), "PYTHONDONTWRITEBYTECODE": "1"}
	def run(rows: list[dict], budget: int = 700) -> str:
		comments.write_text(json.dumps([rows]))
		result = subprocess.run(["bash", str(POLLER)], cwd=REPO_ROOT, env={**env, "BUDGET": str(budget)},
			capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		return calls.read_text() if calls.exists() else ""
	assert not run([source, marker], budget=499)
	assert "POST repos/o/r/issues/7/comments" in run([source, marker])
	assert "DELETE repos/o/r/issues/7/labels/ai%3Areclarify-requeue" in calls.read_text()
	calls.write_text("")
	replay = {"id": 13, "created_at": "2026-10-03T01:02:00Z",
		"body": "/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=11 -->",
		"user": {"type": "User", "login": "owner"}, "author_association": "OWNER"}
	assert "POST" not in run([source, marker, replay])
	assert "DELETE repos/o/r/issues/7/labels/ai%3Areclarify-requeue" in calls.read_text()
	calls.write_text("")
	answer = {"id": 14, "created_at": "2026-10-03T01:03:00Z",
		"body": "/answer [auto-answered-by-clarify]", "user": {"type": "User", "login": "owner"}}
	assert "POST" not in run([source, marker, answer])
	assert "DELETE repos/o/r/issues/7/labels/ai%3Areclarify-requeue" in calls.read_text()
	calls.write_text("")
	newer = {"id": 15, "created_at": "2026-10-03T01:04:00Z", "body": "/reclarify again",
		"user": {"type": "User", "login": "owner"}, "author_association": "OWNER"}
	assert not run([source, marker, newer]), "an in-flight replacement retains the label"
	forged = {"id": 16, "created_at": "2026-10-03T01:05:00Z",
		"body": "forged <!-- ai:reclarify-replay:v1 source=11 -->", "user": {"type": "User", "login": "attacker"}}
	assert "POST repos/o/r/issues/7/comments" in run([source, marker, forged])
	calls.write_text("")
	assert not run([{**source, "author_association": "NONE"}, marker])
	assert not run([source, {**marker, "user": {"type": "User", "login": "attacker"}}])


def test_replay_accepts_trailing_reclarify_source_only_without_marker(tmp_path: Path) -> None:
	"""Issue #6630: the replay sweep uses the intake's any-line rule."""
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text('''#!/usr/bin/env bash
if [ "$1:$2" = "api:rate_limit" ]; then echo 700; exit 0; fi
if [ "$1:$2" = "api:user" ]; then echo owner; exit 0; fi
if [ "$1:$2" = "issue:list" ]; then echo '[{"number":7}]'; exit 0; fi
if [ "$1:$2" = "api:--paginate" ]; then cat "${COMMENTS}"; exit 0; fi
if [ "$1:$2" = "api:repos/o/r/issues/7" ]; then
  echo '{"number":7,"state":"open","labels":[{"name":"ai:reclarify-requeue"}]}'; exit 0
fi
if [ "$1:$2" = "api:-X" ]; then echo "$*" >> "${CALLS}"; exit 0; fi
exit 1
''')
	gh.chmod(0o755)
	comments = tmp_path / "comments.json"
	calls = tmp_path / "calls"
	env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r",
		"RECLARIFY_REQUEUE_SWEEP_ONLY": "true", "RECLARIFY_UNROUTED_DETECT_ENABLED": "false",
		"GH_TOKEN": "test", "CALLS": str(calls), "COMMENTS": str(comments), "PYTHONDONTWRITEBYTECODE": "1"}
	marker = {"id": 12, "created_at": "2026-10-03T01:01:00Z",
		"body": "<!-- ai:reclarify-requeue:v1 source=11 -->",
		"user": {"type": "Bot", "login": "github-actions[bot]"}}
	def run(body: str) -> str:
		if calls.exists():
			calls.unlink()
		source = {"id": 11, "created_at": "2026-10-03T01:00:00Z", "body": body,
			"user": {"type": "User", "login": "owner"}, "author_association": "OWNER"}
		comments.write_text(json.dumps([[source, marker]]))
		result = subprocess.run(["bash", str(POLLER)], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		return calls.read_text() if calls.exists() else ""
	assert "POST repos/o/r/issues/7/comments" in run("Answer first.\n\n/reclarify")
	assert "POST" not in run("Answer\n/reclarify\n<!-- ai:clarification-questions -->")
	assert "POST" not in run("please /reclarify later")


def test_idle_poller_flags_unrouted_blocked_reply_once(tmp_path: Path) -> None:
	"""Issue #6630: a blocked issue whose human reply never routed gets one advisory."""
	from datetime import datetime, timedelta, timezone

	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text('''#!/usr/bin/env bash
if [ "$1:$2" = "api:rate_limit" ]; then echo 700; exit 0; fi
if [ "$1:$2" = "api:user" ]; then exit 1; fi
if [ "$1:$2" = "issue:list" ]; then echo '[{"number":9,"labels":[{"name":"ai:blocked"}]}]'; exit 0; fi
if [ "$1:$2" = "api:graphql" ]; then
  echo graphql >> "${CALLS}"
  [ -n "${GRAPHQL_FAIL:-}" ] && exit 1
  cat "${GRAPHQL}"; exit 0
fi
if [ "$1:$2" = "api:-X" ]; then echo "$*" >> "${CALLS}"; exit 0; fi
exit 1
''')
	gh.chmod(0o755)
	graphql = tmp_path / "graphql.json"
	calls = tmp_path / "calls"
	now = datetime.now(timezone.utc)
	def ts(minutes: int) -> str:
		return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
	def comment(cid: int, body: str, minutes: int) -> dict:
		return {"databaseId": cid, "body": body, "createdAt": ts(minutes),
			"authorAssociation": "OWNER", "author": {"login": "owner", "__typename": "User"}}
	def write(comments: list) -> None:
		graphql.write_text(json.dumps({"data": {"repository": {"i9": {
			"number": 9, "labels": {"nodes": [{"name": "ai:blocked"}]},
			"comments": {"nodes": comments},
			"timelineItems": {"nodes": [{"createdAt": ts(120), "label": {"name": "ai:blocked"}}]}}}}}))
	env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r",
		"RECLARIFY_REQUEUE_SWEEP_ONLY": "true", "GH_TOKEN": "test", "GH_RETRY_MAX_ATTEMPTS": "1",
		"CALLS": str(calls), "GRAPHQL": str(graphql), "PYTHONDONTWRITEBYTECODE": "1",
		"TG_BOT_SECRET": "", "TG_ADMIN_CHAT_ID": ""}
	def run(**extra: str) -> tuple[str, str]:
		if calls.exists():
			calls.unlink()
		result = subprocess.run(["bash", str(POLLER)], cwd=REPO_ROOT, env={**env, **extra},
			capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		return result.stdout, calls.read_text() if calls.exists() else ""
	answer = comment(21, "Answer to the blocker.\n\n/reclarify", 30)
	write([comment(20, "Clarification blocked: human input required.", 119), answer])
	out, made = run()
	assert "RECLARIFY_UNROUTED issue=9 comment=21 reason=command_unrouted" in out and "outcome=flagged" in out
	assert made.count("POST repos/o/r/issues/9/comments") == 1
	assert "<!-- ai:reclarify-unrouted:v1 comment=21 -->" in made
	write([answer, comment(22, "advisory\n\n<!-- ai:reclarify-unrouted:v1 comment=21 -->", 5)])
	out, made = run()
	assert "POST" not in made and "outcome=flagged" not in out
	out, made = run(GRAPHQL_FAIL="1")
	assert "RECLARIFY_UNROUTED outcome=skip reason=graphql_unavailable" in out and "POST" not in made
	out, made = run(RECLARIFY_UNROUTED_DETECT_ENABLED="false")
	assert "graphql" not in made and "RECLARIFY_UNROUTED" not in out

def test_standalone_snapshot_batches_clean_prs_and_skips_draft_claude_reads() -> None:
	text = POLLER.read_text()
	conflict = text[text.index('echo "Standalone PR conflict sweep"'):text.index('echo "Standalone conflict sweep complete.')]
	noop = text[text.index('echo "Standalone PR noop-suspicious recovery sweep"'):]
	assert '--json number,headRefName,headRefOid,baseRefName,isDraft,isCrossRepository,headRepositoryOwner' in conflict
	assert '--limit 3000' in conflict
	assert '_standalone_aliases+="p${_standalone_num}: pullRequest(' in conflict
	assert 'gh_retry gh api graphql -f query=' in conflict
	assert 'hasPreviousPage == false' in conflict
	assert conflict.index('if [[ "${S_HEAD}" == claude/* ]] && [ "${S_DRAFT}" = "true" ]') < conflict.index('S_PR_JSON="$(gh_retry _safe_gh_jq')
	assert conflict.index('if [[ "${S_HEAD_OID}" =~ ^[0-9a-f]{40}$ ]] && [ "${_STANDALONE_CLEAN_PRS["${S_PR}"]:-}" = "${S_HEAD_OID}" ]') < conflict.index('S_PR_JSON="$(gh_retry _safe_gh_jq')
	assert noop.index('if [[ "${N_HEAD}" == claude/* ]] && [ "${N_DRAFT}" = "true" ]') < noop.index('N_COMMENTS_JSON="$(gh_retry _safe_gh_jq')
	assert noop.index('if [[ "${N_HEAD_OID}" =~ ^[0-9a-f]{40}$ ]] && [ "${_STANDALONE_NOOP_CLEAR_PRS["${N_PR}"]:-}" = "${N_HEAD_OID}" ]') < noop.index('N_COMMENTS_JSON="$(gh_retry _safe_gh_jq')


def test_standalone_batch_accepts_matching_sha_and_falls_back_on_uncertainty() -> None:
	text = POLLER.read_text()
	batch = text[text.index('declare -A _STANDALONE_CLEAN_PRS=()'):text.index('for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ ))')]
	with tempfile.TemporaryDirectory() as test_dir:
		root = Path(test_dir)
		gh = root / "gh"
		gh.write_text('''#!/usr/bin/env bash
if [ "$1:$2" = "api:graphql" ]; then
  printf 'called\\n' >> "$CALL_LOG"
  cat "$GRAPHQL_RESPONSE"
else
  exit 1
fi
''')
		gh.chmod(0o755)
		response_file = root / "response.json"
		call_log = root / "calls"
		sha = "a" * 40
		prs = [{"number": 7, "headRefName": "claude/a", "headRefOid": sha,
			"baseRefName": "main", "isDraft": False},
			{"number": 8, "headRefName": "ai/issue-8", "headRefOid": sha,
				"baseRefName": "main", "isDraft": False}]
		clean = {"number": 7, "headRefName": "claude/a", "headRefOid": sha,
			"state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN",
			"comments": {"pageInfo": {"hasPreviousPage": False}, "nodes": []}}
		run_script = ('gh_retry() { "$@"; }\n' + batch +
			'printf "clean=%s noop=%s\\n" "${_STANDALONE_CLEAN_PRS[7]:-unset}" "${_STANDALONE_NOOP_CLEAR_PRS[7]:-unset}"\n')
		env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r",
			"STANDALONE_PRS": json.dumps(prs), "STANDALONE_COUNT": "2",
			"GRAPHQL_RESPONSE": str(response_file), "CALL_LOG": str(call_log)}
		def run(reply: dict) -> str:
			response_file.write_text(json.dumps(reply))
			call_log.write_text("")
			result = subprocess.run(["bash", "-euo", "pipefail", "-c", run_script], env=env,
				capture_output=True, text=True)
			assert result.returncode == 0, result.stderr
			assert call_log.read_text().count("called") == 1
			return result.stdout
		assert f"clean={sha} noop={sha}" in run({"data": {"repository": {"p7": clean, "p8": {
			**clean, "number": 8, "headRefName": "ai/issue-8"}}}})
		assert "clean=unset noop=unset" in run({"data": {"repository": {"p7": {
			**clean, "headRefOid": "b" * 40}}}})
		assert f"clean=unset noop={sha}" in run({"data": {"repository": {"p7": {
			**clean, "mergeStateStatus": "DIRTY"}}}})
		assert f"clean={sha} noop=unset" in run({"data": {"repository": {"p7": {
			**clean, "comments": {"pageInfo": {"hasPreviousPage": True}, "nodes": []}}}}})
		assert "clean=unset noop=unset" in run({"data": {"repository": {"p7": clean}},
			"errors": [{"message": "incomplete"}]})


def _poller_text() -> str:
	return POLLER.read_text(encoding="utf-8")


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _sweep_block() -> str:
	"""Return the source slice from the sweep section header to EOF.

	The sweep is the final block of the poller; all contract checks
	below restrict their search to this block so an accidental match
	on unrelated code elsewhere can't falsely satisfy a contract."""
	text = _poller_text()
	start = text.find(SWEEP_HEADER)
	assert start != -1, f"Noop-suspicious recovery sweep section is missing — expected header {SWEEP_HEADER!r}"
	return text[start:]


def test_sweep_section_exists():
	"""The sweep is present. The header appears in two intentional
	places: the documentation comment block and the runtime banner
	echo. Anything other than exactly 2 indicates either the sweep is
	missing (0/1) or the block was accidentally duplicated (4+)."""
	text = _poller_text()
	occurrences = text.count(SWEEP_HEADER)
	assert occurrences == 2, (
		f"Expected exactly 2 occurrences of the sweep header (1 in "
		f"docs comment, 1 in echo banner); found {occurrences}. If "
		f"this jumped to >2 the sweep block was likely pasted twice."
	)


def test_sweep_uses_exact_workflow_warning_literal():
	"""Detection must use the exact warning literal that
	`review_autofix.yml` emits — a drift here would break recovery
	silently. The literal appears in both files."""
	wf = _workflow_text()
	assert NOOP_WARNING_LITERAL in wf, "Workflow must still emit the noop-suspicious warning literal"
	sweep = _sweep_block()
	assert NOOP_WARNING_LITERAL in sweep, "Poller sweep must grep for the exact workflow literal"
	# And it must be assigned to a named constant (so a future renamer
	# updating one site is forced to update the other too, via the
	# test failure here if they don't).
	assert 'NOOP_WARNING_LITERAL=' in sweep


def test_retry_threshold_is_three():
	"""The design spec is 3 retries before force-merge. Pinning the
	constant guards against accidental bumps."""
	sweep = _sweep_block()
	assert "NOOP_MAX_RETRIES=3" in sweep


def test_sweep_uses_shared_audit_helper():
	"""The force-merge gate B (reviewer audit health) MUST call the
	shared helper, not a local re-implementation. Otherwise the poller
	and the workflow can disagree about what "audit healthy" means."""
	sweep = _sweep_block()
	assert "source scripts/validate_editor_audit.sh" in sweep, (
		"Sweep must source scripts/validate_editor_audit.sh — "
		"never re-implement the arithmetic check inline."
	)
	assert "validate_editor_audit_arithmetic" in sweep, (
		"Sweep must invoke the shared function validate_editor_audit_arithmetic."
	)


def _strip_shell_comments(text: str) -> str:
	"""Remove lines that are pure comments (start with `#` after
	whitespace). Lets contract tests check for forbidden tokens like
	`gh pr list` in actual executable code without false-matching on
	the documentation comments that explain why those tokens are
	absent."""
	out = []
	for line in text.splitlines():
		stripped = line.lstrip()
		if stripped.startswith("#"):
			continue
		out.append(line)
	return "\n".join(out)


def test_sweep_reuses_standalone_prs_cache():
	"""Per CLAUDE.md §15, the sweep MUST reuse the conflict sweep's
	`STANDALONE_PRS` cache rather than running a second `gh pr list`.
	A new `gh pr list` inside the sweep block is a regression."""
	sweep_code = _strip_shell_comments(_sweep_block())
	assert "STANDALONE_PRS" in sweep_code, "Sweep must reference STANDALONE_PRS"
	assert "gh pr list" not in sweep_code, (
		"Sweep must NOT call `gh pr list` again — it should reuse the "
		"STANDALONE_PRS cache populated by the conflict sweep above."
	)


def test_sweep_uses_dispatch_helper_not_raw_workflow_run():
	"""Re-dispatches must go through `_dispatch_review_for_conflicts`
	so the cycle-local `_CONFLICT_DISPATCH_TRACKER` and active-run
	guard fire. A raw `gh workflow run` would bypass both."""
	sweep = _sweep_block()
	assert "_dispatch_review_for_conflicts" in sweep
	# Permit `gh pr merge --squash --auto` (force-merge path) but
	# forbid `gh workflow run` (would race the existing guards).
	assert "gh workflow run" not in sweep


def test_force_merge_call_uses_squash_and_auto():
	"""The actual force-merge invocation must use `--squash --auto` to
	mirror the workflow's auto-merge step
	(`.github/workflows/review_autofix.yml:5073`).

	Restricted to executable code (comments and Telegram message
	strings are stripped) so error-message strings like
	"'gh pr merge --auto' failed" don't false-fail."""
	sweep_code = _strip_shell_comments(_sweep_block())
	# Match only invocations that look like commands (lines that
	# include `gh pr merge` not wrapped in single quotes from a
	# message string). The simplest heuristic: look for at least one
	# line that has both --squash and --auto on the same gh-pr-merge
	# line.
	for line in sweep_code.splitlines():
		if "gh pr merge" in line and "--squash" in line and "--auto" in line:
			return  # found a valid invocation
	raise AssertionError(
		"Sweep must contain at least one `gh pr merge ... --squash --auto` "
		"invocation in executable code (not in a comment or string)."
	)


def test_force_merge_skip_labels_include_force_review_and_e2e():
	"""Operator opt-outs: `force-review` lets a human pin a PR for
	manual review; `e2e-smoke-test` mirrors the workflow's existing
	auto-merge suppression for smoke-test PRs."""
	sweep = _sweep_block()
	assert "e2e-smoke-test" in sweep
	assert "force-review" in sweep


def test_force_merge_gate_distinguishes_debug_and_error_levels():
	"""Re-dispatches and a successful force-merge are pipeline work and
	alert at DEBUG (filtered by the default ALERT_MSG_LEVEL=WARNING).
	Force-merge gate failures leave the PR for a person: they page at
	ERROR through _noop_gate_failure_alert, once per PR head and gate.
	This distinction is part of the operator contract."""
	sweep = _sweep_block()
	debug_lines = [
		line for line in sweep.splitlines()
		if "tg_send_msg" in line and '"DEBUG"' in line
	]
	assert any("re-dispatched review_autofix" in line for line in debug_lines), debug_lines
	assert any("Force-merging PR" in line for line in debug_lines), debug_lines
	assert not [
		line for line in sweep.splitlines()
		if "tg_send_msg" in line and '"WARNING"' in line
	], "Routine sweep chatter must not page at WARNING."
	gate_lines = [line for line in sweep.splitlines() if "_noop_gate_failure_alert \"" in line]
	assert gate_lines, (
		"Sweep must page whenever a force-merge precondition fails, via the "
		"once-per-head _noop_gate_failure_alert helper."
	)
	assert 'tg_send_msg "${msg}" "ERROR"' in sweep, (
		"The gate-failure helper must page at ERROR so operators can tell "
		"'stuck PR genuinely broken' from routine chatter."
	)


def test_sweep_comment_refresh_failure_fails_closed():
	"""A comments-refresh API failure must abort the force-merge path for
	that cycle rather than evaluating Gate B on the stale pre-filter
	snapshot. Force-merge is a safety-sensitive fallback, so refresh
	failure is a closed gate, not a fail-open path."""
	sweep = _sweep_block()
	sweep_code = _strip_shell_comments(sweep)
	assert "could not refresh comments snapshot for noop-suspicious Gate B; failing force-merge closed this cycle." in sweep
	assert "could not refresh latest PR comments snapshot" in sweep
	assert "using pre-filter snapshot" not in sweep_code, (
		"Refresh failure must fail closed; executable sweep code should not "
		"continue Gate B using the pre-filter snapshot."
	)


def test_force_merge_each_gate_has_distinct_failure_branch():
	"""Gates A/B/C/D must each have an explicit failure branch that
	calls `continue` (so a downstream gate cannot accidentally fire
	on a PR that already failed an earlier gate). The contract is the
	individual gate identifiers showing up in alert text — operators
	debugging an ERROR alert need to see which gate tripped."""
	sweep = _sweep_block()
	for gate_id in ("gate A", "gate B", "gate C", "gate D"):
		assert gate_id in sweep, (
			f"Force-merge {gate_id} must have a labeled failure branch "
			"so operators reading the ERROR alert know which precondition failed."
		)


def test_force_merge_records_audit_trail_comment():
	"""Successful force-merges MUST post a PR comment alongside the
	Telegram alert. The audit trail on the PR thread is the durable
	record; Telegram is ephemeral."""
	sweep = _sweep_block()
	assert "Auto-merge enabled after" in sweep, (
		"Force-merge success path must post a PR comment explaining "
		"the override; the body is the audit trail."
	)
	assert "comments" in sweep, "Sweep must POST to issues/${PR}/comments"


def test_sweep_force_merge_gate_skips_when_no_productive_commit():
	"""Gate C: at least one `[ai-autofix]` / `[judge-fix]` commit must
	exist before force-merge fires. A PR whose editor was broken from
	the very first invocation is NOT force-merge eligible — the
	whole point of force-merge is "the editor produced N productive
	rounds and is now stuck in convergence."
	"""
	sweep = _sweep_block()
	# The detection block already filters for productive-commit
	# timestamp; we confirm the force-merge path explicitly references
	# the absence-of-productive-commit case.
	assert "no prior [ai-autofix] / [judge-fix] commit" in sweep


def _gate_d_block() -> str:
	"""Return the sweep slice from the Gate D marker to the force-merge."""
	sweep = _sweep_block()
	start = sweep.find("# Gate D")
	end = sweep.find("# All gates passed", start)
	assert start != -1 and end != -1, "Gate D block markers missing"
	return sweep[start:end]


def test_sweep_force_merge_gate_d_checks_required_checks():
	"""Gate D blocks force-merge through the shared required-checks gate
	(scripts/pr_checks_lib.sh), which treats any required check-run whose
	newest run is not success/neutral/skipped (failure, cancelled,
	timed_out, action_required) as a blocker, and fails closed when the
	gate is unavailable."""
	gate_d = _gate_d_block()
	assert "_pr_checks_completed" in gate_d
	assert "command -v _pr_checks_completed" in gate_d
	assert "reason=helper_unavailable" in gate_d


def test_sweep_force_merge_gate_d_uses_shared_checks_gate():
	"""Gate D must call the shared wait gate with a zero-minute budget (one
	evaluation, no sleep; it wraps _pr_checks_completed with the required-set
	filter and requires the named CI check, issue #7126) and must not carry
	its own check-runs query, so pr_checks_lib.sh stays the single source of
	truth."""
	gate_d = _gate_d_block()
	gate_d_code = _strip_shell_comments(gate_d)
	assert '_pr_wait_for_required_checks "${N_PR}" "${N_HEAD_SHA}" "${N_BASE_REF}" 0' in gate_d_code
	assert "command -v _pr_wait_for_required_checks" in gate_d_code
	assert "check-runs?per_page=100" not in gate_d_code
	assert "PR_CHECKS_WAIT_OUTCOME" in gate_d_code
	assert "NOOP_RECOVERY_BLOCKED=$((NOOP_RECOVERY_BLOCKED + 1))" in gate_d_code
	assert "Noop-suspicious force-merge gate D failed" in gate_d_code
	# Base ref comes from the Gate A PR read, with the cached base as fallback.
	assert "N_BASE_REF=\"$(echo \"${N_PR_JSON}\" | jq -r '.base.ref" in gate_d_code
	assert 'N_BASE_REF="${N_BASE:-}"' in gate_d_code


def test_sweep_skips_orchestrator_project_branches():
	"""Integration / orchestrator-managed branches have their own
	merge cadence; this sweep must NOT touch them."""
	sweep = _sweep_block()
	assert '[[ "${N_BASE}" == orchestrator/project-* ]] || [[ "${N_HEAD}" == orchestrator/project-* ]]' in sweep, (
		"Sweep must explicitly skip both PRs targeting orchestrator/project-* base branches "
		"and final integration PRs whose head branch is orchestrator/project-*."
	)


def test_sweep_skips_forward_merge_fallback_prs():
	"""Forward-merge fallback PRs (head ref `auto/forward-merge-stable-*`,
	opened by `.github/workflows/forward-merge-stable-to-main.yml`) MUST
	be merged via GitHub's "Create a merge commit" button so the
	2-parent merge keeps `stable`'s tip reachable from `main`. The
	sweep's force-merge path executes `gh pr merge --squash --auto`,
	which silently strips that ancestry and trips
	`promote-main-to-stable.yml`'s pre-flight
	`git merge-base --is-ancestor` check on the next promote run.

	`review_autofix.yml`'s codex-agent "Enable auto-merge on PR" step
	and the `deterministic-skip-merge` sibling job both already
	suppress auto-merge for this head-ref prefix, so the noop counter
	never clears via a productive [ai-autofix] commit on this PR
	class — the sweep would otherwise loop forever then force-merge
	the very thing the workflow chain is designed to protect.
	"""
	sweep = _sweep_block()
	assert '[[ "${N_HEAD}" == auto/forward-merge-stable-* ]]' in sweep, (
		"Sweep must explicitly skip PRs whose head ref matches "
		"`auto/forward-merge-stable-*` so the force-merge `--squash --auto` "
		"path cannot strip the 2-parent ancestry that "
		"`promote-main-to-stable.yml` relies on. Mirrors the existing "
		"suppressors in `.github/workflows/review_autofix.yml`."
	)


def test_sweep_forward_merge_skip_precedes_api_calls():
	"""The forward-merge skip MUST sit alongside the
	`orchestrator/project-*` skip, before the per-PR comments fetch.
	Otherwise every forward-merge fallback PR burns a paginated
	`issues/<n>/comments` GraphQL/REST round-trip every poll cycle
	(§15), and a transient API failure on that call could allow the
	force-merge path to fire on the very PR class it's supposed to
	skip."""
	sweep = _sweep_block()
	forward_skip_idx = sweep.find('[[ "${N_HEAD}" == auto/forward-merge-stable-* ]]')
	comments_fetch_idx = sweep.find('issues/${N_PR}/comments')
	assert forward_skip_idx != -1, "Forward-merge skip must exist (covered by sibling test)."
	assert comments_fetch_idx != -1, (
		"Sweep must still fetch issue comments — the pre-filter is "
		"the whole point of the cheap-path check."
	)
	assert forward_skip_idx < comments_fetch_idx, (
		"Forward-merge skip must precede the per-PR comments fetch so "
		"this PR class costs zero extra API calls and cannot fail open "
		"on a transient comments-fetch error."
	)


def test_sweep_counts_only_post_productive_warnings():
	"""The retry counter is "noop warnings newer than latest
	[ai-autofix] / [judge-fix] commit". This is the implicit reset on
	new head SHA — a new productive commit drops the count to 0.
	"""
	sweep = _sweep_block()
	# The jq filter must compare comment created_at against the
	# productive commit timestamp.
	assert "N_LATEST_PROD_TS" in sweep
	assert "[ai-autofix]" in sweep
	assert "[judge-fix]" in sweep
	assert 'select((.created_at // "") > $since)' in sweep


def test_sweep_stale_warning_guard_short_circuits_before_retry_counting():
	"""Warnings older than the latest productive commit must be skipped
	before retry counting, or stale noop comments could still drive the
	redispatch / force-merge decision on a newer head SHA."""
	sweep = _sweep_block()
	assert re.search(
		r'if \[ -n "\$\{N_LATEST_PROD_TS\}" \] && \[\[ "\$\{N_NOOP_LATEST_TS\}" < "\$\{N_LATEST_PROD_TS\}" \]\]; then',
		sweep,
	), (
		"Sweep must short-circuit when the latest noop warning predates "
		"the latest productive commit."
	)
	assert "stale, skipping." in sweep, (
		"Stale-warning branch must log that the PR was skipped so operator "
		"logs explain why the retry counter reset."
	)


def test_sweep_appears_after_conflict_sweep():
	"""Ordering matters: the conflict sweep must run first so a
	dirty PR gets routed to the conflict resolver rather than the
	noop-suspicious force-merge (which would refuse it anyway via
	mergeable_state=dirty, but doing conflict resolution first is
	cheaper and avoids spurious ERROR alerts)."""
	text = _poller_text()
	conflict_idx = text.find("Standalone PR conflict sweep")
	noop_idx = text.find(SWEEP_HEADER)
	assert conflict_idx != -1 and noop_idx != -1
	assert conflict_idx < noop_idx, (
		"Conflict sweep must precede the noop-suspicious sweep in the script."
	)


def test_sweep_emits_summary_line():
	"""Every poll cycle must log how many PRs were dispatched /
	force-merged / blocked so operators can grep `Noop-suspicious
	recovery complete` in the orchestrator log."""
	sweep = _sweep_block()
	assert "Noop-suspicious recovery complete" in sweep


CAP_SKIP_START = "# Fingerprint-cap skip (see the sweep header)."
CAP_SKIP_END = "_noop_dispatch_rc=0"
CAP_HEAD = "d0c5b24683522700e6e5aaa19f2d7052715c684b"


def _cap_skip_block() -> str:
	"""Return the Step 3a fingerprint-cap skip snippet, verbatim."""
	sweep = _sweep_block()
	start = sweep.find(CAP_SKIP_START)
	assert start != -1, "Step 3a must carry the fingerprint-cap skip block"
	end = sweep.find(CAP_SKIP_END, start)
	assert end != -1
	return sweep[start:end]


def _support_resolver_block() -> str:
	"""Return the verbatim `_noop_cap_review_support_sha` definition and its state."""
	sweep = _sweep_block()
	start = sweep.find('NOOP_CAP_REVIEW_SUPPORT_SHA=""')
	assert start != -1, "Sweep must declare the review-support SHA cache"
	end = sweep.find("\n}\n", sweep.find("_noop_cap_review_support_sha() {", start))
	assert end != -1
	return sweep[start:end + 3]


def _run_cap_skip(comments: list, commits: list, login: str | None, *, repo: str = "o/consumer",
		engine_sha: str = "", ai_review: str | None = None) -> tuple[str, int]:
	"""Run the verbatim skip snippet inside a one-iteration loop.

	`gh_retry` / `_safe_gh_jq` are stubbed; `login=None` makes the
	identity lookup fail. `repo` / `engine_sha` / `ai_review` (the
	consumer's `.github/workflows/ai-review.yml` text) drive the
	review-support SHA resolution. Returns (stdout, GET /user call count)."""
	import json
	import shlex
	import subprocess
	import tempfile

	# Fixture JSON is single-quoted with shlex.quote so Bash performs no
	# command substitution (backticks, $(...)) or parameter expansion on
	# comment bodies before the verbatim snippet sees them.
	with tempfile.TemporaryDirectory() as tmp:
		calls = Path(tmp) / "calls"
		if ai_review is not None:
			wf_dir = Path(tmp) / ".github" / "workflows"
			wf_dir.mkdir(parents=True)
			(wf_dir / "ai-review.yml").write_text(ai_review)
		script = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
	echo x >> {json.dumps(str(calls))}
	{"return 1" if login is None else f"printf '%s' {json.dumps(login)}"}
}}
N_PR=4332
N_NOOP_COUNT=2
NOOP_MAX_RETRIES=3
NOOP_RECOVERY_CAP_SKIPPED=0
NOOP_CAP_TRUSTED_LOGIN=""
NOOP_CAP_TRUSTED_LOGIN_STATE="unset"
GITHUB_REPOSITORY={json.dumps(repo)}
ORCHESTRATOR_ENGINE_SHA={json.dumps(engine_sha)}
{_support_resolver_block()}
N_COMMENTS_JSON={shlex.quote(json.dumps(comments))}
N_COMMITS_JSON={shlex.quote(json.dumps(commits))}
for _pr in a b; do
{_cap_skip_block()}
	echo "DISPATCH pr=${{N_PR}}"
done
echo "SKIPPED=${{NOOP_RECOVERY_CAP_SKIPPED}}"
"""
		result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, cwd=tmp)
		call_count = len(calls.read_text().splitlines()) if calls.exists() else 0
		return result.stdout, call_count


def _cap_comment(login: str, head: str = CAP_HEAD, support: str = "") -> dict:
	support_field = f" support={support}" if support else ""
	return {
		"user": {"login": login},
		"body": f"**AI review/autofix stopped: identical failure repeated**\n<!-- review-autofix-failure-cap:v1 head={head} fp={'a' * 64} reason=editor_empty_noop count=3{support_field} -->",
	}


SUPPORT_NEW = "5" * 40
SUPPORT_OLD = "6" * 40


def test_cap_skip_matching_support_in_source_repo_skips():
	"""Issue #6625: in this repository the review support SHA is the
	poller's engine SHA; a cap marker carrying it still skips."""
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1", support=SUPPORT_NEW)], [{"sha": CAP_HEAD}], "shubhodeep1",
		repo="shubhodeep1/coding-workflows", engine_sha=SUPPORT_NEW)
	assert "DISPATCH" not in out, out
	assert f"support={SUPPORT_NEW}" in out
	assert "SKIPPED=2" in out


def test_cap_skip_other_support_version_redispatches():
	"""A cap marker written by an older review support version does not
	stop the gate any more, so the sweep re-dispatches and logs why."""
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1", support=SUPPORT_OLD)], [{"sha": CAP_HEAD}], "shubhodeep1",
		repo="shubhodeep1/coding-workflows", engine_sha=SUPPORT_NEW)
	assert out.count("DISPATCH") == 2, out
	assert f"NOOP_RECOVERY_FINGERPRINT_CAP_STALE_SUPPORT pr=4332 head={CAP_HEAD} support={SUPPORT_NEW}" in out


def test_cap_skip_support_text_outside_marker_does_not_count():
	"""The support SHA must be a field of the cap marker: an older cap comment
	that quotes ` support=<current> ` elsewhere (e.g. its first error) is stale."""
	stale = _cap_comment("shubhodeep1", support=SUPPORT_OLD)
	stale["body"] = f"First error: `x support={SUPPORT_NEW} y`\n" + stale["body"]
	out, _ = _run_cap_skip([stale], [{"sha": CAP_HEAD}], "shubhodeep1",
		repo="shubhodeep1/coding-workflows", engine_sha=SUPPORT_NEW)
	assert out.count("DISPATCH") == 2, out
	assert "NOOP_RECOVERY_FINGERPRINT_CAP_STALE_SUPPORT" in out


def test_cap_skip_inline_quoted_marker_does_not_count():
	"""A whole current-support cap marker quoted inline (a failure comment's
	"First error" code span) is not a cap marker on its own line, so the PR
	is dispatched as if no cap marker existed (no cap log, no identity lookup)."""
	quoted = _cap_comment("shubhodeep1", support=SUPPORT_NEW)
	quoted["body"] = "**AI review/autofix failed**\n\n**First error:** `" + quoted["body"].split("\n", 1)[1] + "`"
	out, calls = _run_cap_skip([quoted], [{"sha": CAP_HEAD}], "shubhodeep1",
		repo="shubhodeep1/coding-workflows", engine_sha=SUPPORT_NEW)
	assert out.count("DISPATCH") == 2, out
	assert "NOOP_RECOVERY_SKIP_FINGERPRINT_CAP" not in out, out
	assert calls == 0


def test_cap_skip_inline_quoted_marker_does_not_count_when_support_unresolved():
	"""With the support SHA unresolved the any-version fallback applies, but an
	inline-quoted cap marker is still not a cap marker (the gate ignores it)."""
	quoted = _cap_comment("shubhodeep1")
	quoted["body"] = "**AI review/autofix failed**\n\n**First error:** `" + quoted["body"].split("\n", 1)[1] + "`"
	out, calls = _run_cap_skip([quoted], [{"sha": CAP_HEAD}], "shubhodeep1")
	assert out.count("DISPATCH") == 2, out
	assert "NOOP_RECOVERY_SKIP_FINGERPRINT_CAP" not in out
	assert calls == 0


def test_cap_skip_legacy_marker_redispatches_when_support_resolved():
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1")], [{"sha": CAP_HEAD}], "shubhodeep1",
		repo="shubhodeep1/coding-workflows", engine_sha=SUPPORT_NEW)
	assert out.count("DISPATCH") == 2, out
	assert "NOOP_RECOVERY_FINGERPRINT_CAP_STALE_SUPPORT" in out


def test_cap_skip_consumer_support_from_ai_review_pin():
	"""Consumer pollers run `stable`, but reviews run the SHA pinned in the
	local ai-review.yml wrapper; that pin is the support version."""
	wrapper = f"    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@{SUPPORT_NEW} # stable\n"
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1", support=SUPPORT_NEW)], [{"sha": CAP_HEAD}], "shubhodeep1",
		engine_sha=SUPPORT_OLD, ai_review=wrapper)
	assert "DISPATCH" not in out, out
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1", support=SUPPORT_OLD)], [{"sha": CAP_HEAD}], "shubhodeep1",
		engine_sha=SUPPORT_OLD, ai_review=wrapper)
	assert out.count("DISPATCH") == 2, out


def test_cap_skip_unresolved_support_keeps_legacy_skip():
	"""An unrendered `@stable` wrapper (or none) leaves the support SHA
	unresolved: any trusted cap marker for the head still skips."""
	wrapper = "    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable\n"
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1", support=SUPPORT_OLD)], [{"sha": CAP_HEAD}], "shubhodeep1",
		ai_review=wrapper)
	assert "DISPATCH" not in out, out
	assert "support=unresolved" in out
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1")], [{"sha": CAP_HEAD}], "shubhodeep1",
		repo="shubhodeep1/coding-workflows", engine_sha="abc1234")
	assert "DISPATCH" not in out, out


def test_cap_skip_suppresses_redispatch_when_cap_applied_on_head():
	"""PR #4332 regression: a trusted cap marker on the current head means
	the gate will end every dispatch, so the sweep must not re-dispatch
	(or alert) on every cycle. The identity lookup is issued once and
	cached across PRs."""
	out, calls = _run_cap_skip([_cap_comment("Shubhodeep1")], [{"sha": "0" * 40}, {"sha": CAP_HEAD}], "shubhodeep1")
	assert "DISPATCH" not in out, out
	assert f"NOOP_RECOVERY_SKIP_FINGERPRINT_CAP pr=4332 head={CAP_HEAD} count=2 max=3" in out, out
	assert "SKIPPED=2" in out, out
	assert calls == 1, f"GET /user must be issued once per cycle, got {calls}"


def test_cap_skip_harness_preserves_shell_metacharacters():
	"""Fixture bodies with backticks, `$(...)` and `$VAR` must reach the
	snippet byte for byte. Double-quoted assignment let Bash run command
	substitution on them (an unbalanced backtick aborted the shell)."""
	stray = {"user": {"login": "mallory"}, "body": "stray ` backtick and $(nope) and $HOME"}
	out, _ = _run_cap_skip([_cap_comment("shubhodeep1"), stray], [{"sha": CAP_HEAD}], "shubhodeep1")
	assert "DISPATCH" not in out, out
	assert "SKIPPED=2" in out, out
	assert f"NOOP_RECOVERY_SKIP_FINGERPRINT_CAP pr=4332 head={CAP_HEAD}" in out, out


def test_cap_skip_ignores_marker_for_older_head():
	"""A push creates a new head; a cap marker for the old head must not
	block recovery, and no identity lookup is spent on it."""
	out, calls = _run_cap_skip([_cap_comment("shubhodeep1", head="1" * 40)], [{"sha": CAP_HEAD}], "shubhodeep1")
	assert out.count("DISPATCH") == 2, out
	assert calls == 0


def test_cap_skip_ignores_untrusted_marker_author():
	"""A marker copied into a comment by another user must not suppress
	recovery."""
	out, _ = _run_cap_skip([_cap_comment("mallory")], [{"sha": CAP_HEAD}], "shubhodeep1")
	assert out.count("DISPATCH") == 2, out


def test_cap_skip_fails_open_when_identity_unresolvable():
	"""Identity lookup failure keeps the legacy re-dispatch and is not
	retried for every PR in the same cycle."""
	out, calls = _run_cap_skip([_cap_comment("shubhodeep1")], [{"sha": CAP_HEAD}], None)
	assert out.count("DISPATCH") == 2, out
	assert "fingerprint-cap skip disabled this cycle" in out
	assert calls == 1


def test_cap_skip_fails_open_without_head_sha():
	out, calls = _run_cap_skip([_cap_comment("shubhodeep1")], [], "shubhodeep1")
	assert out.count("DISPATCH") == 2, out
	assert calls == 0


def test_cap_skip_marker_literal_matches_workflow():
	"""The sweep must look for the exact marker prefix the workflow's
	fingerprint-cap-block job posts."""
	wf = _workflow_text()
	assert "<!-- review-autofix-failure-cap:v1 head=${PR_HEAD_SHA} " in wf
	assert "<!-- review-autofix-failure-cap:v1 head=${N_NOOP_CAP_HEAD_SHA} " in _cap_skip_block()


# ---------------------------------------------------------------------------
# Behavioural harness for the force-merge fallback (issue #7126): runs the
# real sweep slice with stubbed GitHub helpers.

NOOP_HEAD = "c" * 40
PIPELINE_LOGIN = "pipeline-bot"
CODEX_IDENTITY = {"name": "codex-bot", "email": "codex@users.noreply.github.com"}


def _noop_sweep_slice() -> str:
	text = _poller_text()
	start = text.index('NOOP_WARNING_LITERAL="')
	end = text.index("write_state_snapshot_actions_runs_export || true", start)
	return text[start:end]


def _noop_snapshot(**overrides) -> list:
	pr = {"number": 5, "headRefName": "ai/issue-5", "headRefOid": NOOP_HEAD,
		"baseRefName": "main", "isDraft": False, "isCrossRepository": False,
		"headRepositoryOwner": {"login": "o"}}
	pr.update(overrides)
	return [pr]


def _noop_comments(login: str = PIPELINE_LOGIN, summary_login: str | None = None, count: int = 3,
		warning_head: str | None = NOOP_HEAD) -> list:
	# Real warnings end with the review-autofix-failure:v1 marker naming the
	# reviewed head; warning_head=None drops it.
	marker = (f"\n\n<!-- review-autofix-failure:v1 head={warning_head} reason=editor_empty_noop "
		f"fp={'a' * 64} degraded=0 -->") if warning_head else ""
	rows = [{"id": 100 + i, "created_at": f"2026-10-02T0{i}:00:00Z", "user": {"login": login},
		"body": "⚠️ **Editor no-op suspicious** retry" + marker} for i in range(count)]
	rows.append({"id": 200, "created_at": "2026-10-02T09:00:00Z",
		"user": {"login": summary_login or login},
		"body": "AI autofix editor summary\ntotal 2; applied 0; already applied 0; ignored 2"})
	return rows


def _noop_commits(identity: dict | None = None) -> list:
	ident = dict(identity or CODEX_IDENTITY, date="2026-10-01T00:00:00Z")
	return [{"sha": NOOP_HEAD, "commit": {"message": "[ai-autofix] fix", "committer": ident, "author": ident}}]


def _noop_pr(head_sha: str = NOOP_HEAD, head_repo: str | None = "o/r") -> dict:
	return {"state": "open", "draft": False, "mergeable": True, "mergeable_state": "clean",
		"head": {"sha": head_sha, "repo": ({"full_name": head_repo} if head_repo else None)},
		"base": {"ref": "main"}, "labels": []}


def _noop_activity(actor: str = PIPELINE_LOGIN, before: str = "0" * 40, after: str = NOOP_HEAD) -> list:
	# Repository activity API rows, newest first; the push actor is the
	# authenticated pusher.
	return [{"activity_type": "push", "ref": "refs/heads/ai/issue-5", "before": before,
		"after": after, "actor": {"login": actor}}]


def _run_noop_sweep(*, snapshot=None, comments=None, commits=None, pr=None,
		wait_outcome: str = "ok", wait_total: str = "5", login: str = PIPELINE_LOGIN,
		runs_read: str = "true", activity=None, ci_name: str = "lint") -> dict:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		log = root / "log"
		fixtures = {
			"snapshot": snapshot if snapshot is not None else _noop_snapshot(),
			"comments": comments if comments is not None else _noop_comments(),
			"commits": commits if commits is not None else _noop_commits(),
			"pr": pr if pr is not None else _noop_pr(),
			"activity": activity if activity is not None else _noop_activity(),
		}
		for name, value in fixtures.items():
			(root / f"{name}.json").write_text(json.dumps(value))
		script = """
set -euo pipefail
FX="$1"; LOG="$2"
rec() { printf '%s\\n' "$*" >> "${LOG}"; }
gh_retry() { "$@"; }
gh() { rec "GH $*"; return 0; }
_safe_gh_jq() {
	local a
	for a in "$@"; do
		case "${a}" in
			repos/*/issues/*/comments*) rec "READ comments"; cat "${FX}/comments.json"; return 0 ;;
			repos/*/pulls/*/commits*) rec "READ commits"; cat "${FX}/commits.json"; return 0 ;;
			repos/*/activity*) rec "READ activity ${a}"; [ -s "${FX}/activity.json" ] || return 1; cat "${FX}/activity.json"; return 0 ;;
			repos/*/pulls/*) rec "READ pr"; cat "${FX}/pr.json"; return 0 ;;
			user) printf '%s' "${TRUSTED}"; return 0 ;;
		esac
	done
	return 1
}
UNBLOCK_TRUSTED_LOGIN=""
unblock_trusted_login() { UNBLOCK_TRUSTED_LOGIN="${TRUSTED}"; printf '%s' "${UNBLOCK_TRUSTED_LOGIN}"; }
tg_send_msg() { rec "TG $2 $1"; }
_gh_url() { printf 'https://example.invalid/%s' "$1"; }
_dispatch_review_for_conflicts() { rec "DISPATCH $1"; return 0; }
validate_editor_audit_arithmetic() { return 0; }
_pr_checks_completed() { return 0; }
_pr_wait_for_required_checks() {
	rec "WAIT $*"
	PR_CHECKS_WAIT_OUTCOME="${WAIT_OUTCOME}"
	PR_CHECKS_LAST_TOTAL="${WAIT_TOTAL}"
	PR_CHECKS_RUNS_READ="${WAIT_RUNS_READ}"
	PR_CHECKS_LAST_CI_NAME="${WAIT_CI_NAME}"
	case "${WAIT_OUTCOME}" in ok|allow_all) return 0 ;; *) return 1 ;; esac
}
GITHUB_REPOSITORY="o/r"
ORCHESTRATOR_ENGINE_SHA=""
declare -A _STANDALONE_NOOP_CLEAR_PRS=()
STANDALONE_PRS="$(cat "${FX}/snapshot.json")"
STANDALONE_COUNT="$(printf '%s' "${STANDALONE_PRS}" | jq 'length')"
""" + _noop_sweep_slice()
		env = {**os.environ, "TRUSTED": login, "WAIT_OUTCOME": wait_outcome, "WAIT_TOTAL": wait_total,
			"WAIT_RUNS_READ": runs_read, "WAIT_CI_NAME": ci_name, "PYTHONDONTWRITEBYTECODE": "1"}
		result = subprocess.run(["bash", "-c", script, "sweep", str(root), str(log)], cwd=tmp, env=env,
			capture_output=True, text=True)
		assert result.returncode == 0, result.stderr + result.stdout
		lines = log.read_text().splitlines() if log.exists() else []
		return {
			"stdout": result.stdout,
			"log": lines,
			"merges": [line for line in lines if line.startswith("GH pr merge")],
			"dispatches": [line for line in lines if line.startswith("DISPATCH")],
			"alerts": [line for line in lines if line.startswith("TG ERROR")],
			"reads": [line for line in lines if line.startswith("READ")],
		}


def test_noop_fork_pr_is_skipped_before_any_read():
	for snap in (_noop_snapshot(isCrossRepository=True), _noop_snapshot(isCrossRepository=None),
			_noop_snapshot(headRepositoryOwner=None), _noop_snapshot(headRepositoryOwner={"login": "mallory"})):
		if snap[0]["isCrossRepository"] is None:
			del snap[0]["isCrossRepository"]
		out = _run_noop_sweep(snapshot=snap)
		assert not out["reads"] and not out["merges"] and not out["dispatches"], out
		assert "head repository not this repository" in out["stdout"]


def test_noop_head_owner_compare_is_case_insensitive():
	out = _run_noop_sweep(snapshot=_noop_snapshot(headRepositoryOwner={"login": "O"}))
	assert len(out["merges"]) == 1, out


def test_noop_untrusted_comments_drive_nothing():
	out = _run_noop_sweep(comments=_noop_comments(login="mallory"))
	assert not out["merges"] and not out["dispatches"], out


def test_noop_untrusted_summary_fails_gate_b():
	out = _run_noop_sweep(comments=_noop_comments(summary_login="mallory"))
	assert not out["merges"], out
	assert any("gate B" in a for a in out["alerts"]), out


def test_noop_forged_productive_commit_fails_gate_c():
	out = _run_noop_sweep(commits=_noop_commits({"name": "mallory", "email": "m@x"}))
	assert not out["merges"], out
	assert any("gate C" in a for a in out["alerts"]), out


def test_noop_claude_and_new_codex_identities_count_as_productive():
	"""Writers commit as claude-bot or codex-bot on the .invalid domain; both
	count, as does the legacy codex-bot pair (the default fixture)."""
	for ident in ({"name": "claude-bot", "email": "claude-bot@users.noreply.invalid"},
			{"name": "codex-bot", "email": "codex-bot@users.noreply.invalid"}):
		out = _run_noop_sweep(commits=_noop_commits(ident))
		assert len(out["merges"]) == 1, (ident, out)
	# Name and email must match one pair exactly.
	for ident in ({"name": "claude-bot", "email": "codex@users.noreply.github.com"},
			{"name": "claude-bot", "email": "claude@users.noreply.github.com"}):
		out = _run_noop_sweep(commits=_noop_commits(ident))
		assert not out["merges"], (ident, out)
		assert any("gate C" in a for a in out["alerts"]), (ident, out)


def test_noop_productive_commit_not_pushed_by_pipeline_fails_gate_c():
	"""Issue #7126: git identity is forgeable, so the qualifying commit must
	also be inside a push by the pipeline login."""
	out = _run_noop_sweep(activity=_noop_activity(actor="mallory"))
	assert not out["merges"], out
	assert len(out["alerts"]) == 1 and "pushed by the pipeline account" in out["alerts"][0], out
	assert any("gate=C_pusher" in line for line in out["log"]), out
	assert any("activity?ref=refs%2Fheads%2Fai%2Fissue-5" in line for line in out["log"]), out


def test_noop_pipeline_push_before_forged_commit_does_not_cover_it():
	"""A trusted push that ends before the qualifying commit does not cover it."""
	base = "b" * 40
	commits = [{"sha": base, "commit": {"message": "human change",
			"committer": dict(CODEX_IDENTITY, date="2026-09-30T00:00:00Z"),
			"author": dict(CODEX_IDENTITY, date="2026-09-30T00:00:00Z")}}] + _noop_commits()
	out = _run_noop_sweep(commits=commits, activity=_noop_activity(after=base))
	assert not out["merges"], out
	assert any("gate=C_pusher" in line for line in out["log"]), out
	covered = _run_noop_sweep(commits=commits, activity=_noop_activity(before=base))
	assert len(covered["merges"]) == 1, covered


def test_noop_unreadable_activity_waits_without_paging():
	"""A non-array activity payload is a failed read: retry next sweep, no page."""
	out = _run_noop_sweep(activity={"message": "Server Error"})
	assert not out["merges"] and not out["alerts"], out
	assert "reason=activity_unreadable" in out["stdout"]
	# An empty (readable) listing has no covering push: fail closed, page once.
	empty = _run_noop_sweep(activity=[])
	assert not empty["merges"] and any("gate=C_pusher" in line for line in empty["log"]), empty


def test_noop_full_activity_page_names_its_cause():
	"""A full 100-record page without a covering push still fails closed and
	pages once, but the alert names the truncated page as the cause."""
	full = _noop_activity(actor="mallory") * 100
	out = _run_noop_sweep(activity=full)
	assert not out["merges"], out
	assert len(out["alerts"]) == 1 and "reason=activity_page_full" in out["alerts"][0], out
	assert "reason=activity_page_full" not in _run_noop_sweep(activity=_noop_activity(actor="mallory"))["alerts"][0]


def test_noop_unreadable_ci_runs_with_no_check_runs_waits_without_paging():
	"""The unreadable runs listing is checked before the empty-listing page."""
	out = _run_noop_sweep(runs_read="false", wait_total="0")
	assert not out["merges"] and not out["alerts"], out
	assert "reason=ci_runs_unreadable" in out["stdout"]


def test_noop_pending_checks_wait_without_paging():
	out = _run_noop_sweep(wait_outcome="timeout")
	assert not out["merges"] and not out["alerts"], out
	assert "gate D waiting" in out["stdout"]
	assert any(line == f"WAIT 5 {NOOP_HEAD} main 0" for line in out["log"]), out


def test_noop_failed_checks_page_once():
	out = _run_noop_sweep(wait_outcome="failed")
	assert not out["merges"], out
	assert len(out["alerts"]) == 1 and "gate D" in out["alerts"][0], out
	assert any("gate=D_check" in line for line in out["log"]), out


def test_noop_no_check_runs_fails_closed():
	out = _run_noop_sweep(wait_total="0")
	assert not out["merges"], out
	assert len(out["alerts"]) == 1 and "reason=no_check_runs" in out["alerts"][0], out


def test_noop_allow_all_without_ci_check_fails_closed():
	"""Issue #7126: under the allow-all sentinel the shared gate counts no
	check-runs, so without a named CI check nothing shows a check ran."""
	for ci_name in ("none", ""):
		out = _run_noop_sweep(wait_outcome="allow_all", wait_total="", ci_name=ci_name)
		assert not out["merges"], out
		assert len(out["alerts"]) == 1 and "reason=no_check_runs_verified" in out["alerts"][0], out
	assert len(_run_noop_sweep(wait_outcome="allow_all", wait_total="", ci_name="lint")["merges"]) == 1


def test_noop_unreadable_ci_runs_wait_without_paging():
	"""Issue #7126: the shared helper reads an unreadable actions/runs
	listing as zero pending runs; Gate D must not merge on that."""
	out = _run_noop_sweep(runs_read="false")
	assert not out["merges"] and not out["alerts"], out
	assert "reason=ci_runs_unreadable" in out["stdout"]
	assert len(_run_noop_sweep(runs_read="disabled")["merges"]) == 1


def test_noop_all_gates_pass_merge_bound_to_head():
	out = _run_noop_sweep()
	assert len(out["merges"]) == 1, out
	assert f"--match-head-commit {NOOP_HEAD}" in out["merges"][0]
	assert "--squash --auto" in out["merges"][0]


def test_noop_warning_for_other_head_waits_without_paging():
	"""Issue #7126: the audit Gate B validated must come from a review of
	the head being merged. A newer push the review has not finished with
	waits for the next sweep without paging."""
	out = _run_noop_sweep(comments=_noop_comments(warning_head="e" * 40))
	assert not out["merges"] and not out["alerts"], out
	assert "latest noop warning reviewed" in out["stdout"]


def test_noop_warning_without_head_marker_fails_closed():
	out = _run_noop_sweep(comments=_noop_comments(warning_head=None))
	assert not out["merges"], out
	assert len(out["alerts"]) == 1 and "reviewed-head marker" in out["alerts"][0], out
	assert any("gate=B_head" in line for line in out["log"]), out


def test_noop_snapshot_without_head_logs_missing_snapshot():
	out = _run_noop_sweep(snapshot=_noop_snapshot(headRefOid=""))
	assert not out["merges"], out
	assert "open-PR snapshot has no head SHA" in out["stdout"]
	assert "head moved during sweep" not in out["stdout"]


def test_noop_head_moved_during_sweep_skips():
	out = _run_noop_sweep(pr=_noop_pr(head_sha="d" * 40))
	assert not out["merges"] and not out["alerts"], out
	assert "head moved during sweep" in out["stdout"]


def test_noop_live_fork_head_skips():
	for repo in ("mallory/r", None):
		out = _run_noop_sweep(pr=_noop_pr(head_repo=repo))
		assert not out["merges"], out


def test_noop_unresolved_login_fails_closed():
	out = _run_noop_sweep(login="")
	assert not out["merges"] and not out["dispatches"], out


def test_noop_under_threshold_trusted_warning_dispatches():
	out = _run_noop_sweep(comments=_noop_comments(count=1))
	assert len(out["dispatches"]) == 1 and not out["merges"], out


def test_every_poller_merge_is_head_bound():
	"""Issue #7126: every `gh pr merge` the poller runs is bound to the head
	its checks were read for. The merge_with_followup site passes its
	--match-head-commit through an array, as does the final integration
	merge (an open final PR always has a validated head there)."""
	array_bound = {"_rb_mwf_match_arg", "finalize_merge_match_arg"}
	for line in _strip_shell_comments(_poller_text()).splitlines():
		if "gh pr merge " not in line or line.strip().startswith("echo"):
			continue
		if "'gh pr merge" in line or '"gh pr merge' in line:
			continue  # message text, not an invocation
		assert "--match-head-commit" in line or any(name in line for name in array_bound), line


def test_noop_productive_identity_matches_commit_writers():
	text = _poller_text()
	# The legacy pair stays: commits made before the .invalid emails still count.
	assert 'NOOP_PRODUCTIVE_COMMIT_NAME="codex-bot"' in text
	assert 'NOOP_PRODUCTIVE_COMMIT_EMAIL="codex@users.noreply.github.com"' in text
	match = re.search(r"^NOOP_PRODUCTIVE_COMMIT_IDENTITIES_JSON='(.*)'$", text, re.M)
	assert match, "NOOP_PRODUCTIVE_COMMIT_IDENTITIES_JSON must be a single-quoted literal"
	identities = json.loads(match.group(1))
	assert {"name": "codex-bot", "email": "codex@users.noreply.github.com"} in identities
	for rel in ("scripts/review_commit_changes.sh", "scripts/review_rb_judge.sh"):
		writer = (REPO_ROOT / rel).read_text()
		# Each writer commits as claude-bot or codex-bot; both must count.
		for name in ("claude-bot", "codex-bot"):
			email = f"{name}@users.noreply.invalid"
			assert re.search(rf'git config user\.name "{name}"', writer), (rel, name)
			assert re.search(rf'git config user\.email "{re.escape(email)}"', writer), (rel, email)
			assert {"name": name, "email": email} in identities, (rel, name)
		assert "codex@users.noreply.github.com" not in writer, rel


def test_noop_force_merge_controls_named_by_security_pass_findings_stay_in_place():
	"""Security-pass fix cycle 2 of #7126 re-reported
	`noop-force-merge-forged-comment-gates` and
	`noop-gate-d-ignores-pending-checks-unbound-merge` because the audit read
	the default branch's support copy of this script. Pin each control those
	findings name, so a later sync that drops one fails here."""
	text = _poller_text()
	conflict = text[text.index('echo "Standalone PR conflict sweep"'):text.index(SWEEP_HEADER)]
	pr_list = [line for line in conflict.splitlines() if "--json number,headRefName" in line]
	assert pr_list, "the open-PR snapshot must request its fields explicitly"
	assert all("isCrossRepository" in line and "headRepositoryOwner" in line for line in pr_list), pr_list
	sweep_code = _strip_shell_comments(_sweep_block())
	selections = [m.start() for m in re.finditer(r'--arg lit "\$\{NOOP_WARNING_LITERAL\}"', sweep_code)]
	assert len(selections) >= 3, "noop-warning selections moved; re-check the author filter"
	for start in selections:
		jq_filter = sweep_code[start:sweep_code.index("2>/dev/null", start)]
		assert '(.user.login // "") == $login' in jq_filter, jq_filter
	summary_lines = [line for line in sweep_code.splitlines() if 'startswith("AI autofix editor summary")' in line]
	assert summary_lines and all('(.user.login // "") == $login' in line for line in summary_lines), summary_lines
	# Gate C: a qualifying commit must carry the pipeline git identity and
	# arrive in a push by the pipeline login on this head ref.
	gate_c = sweep_code[sweep_code.index("N_PROD_PUSHED="):sweep_code.index('if [ "${N_PROD_PUSHED}" != "true" ]')]
	assert '--argjson ids "${NOOP_PRODUCTIVE_COMMIT_IDENTITIES_JSON}"' in gate_c, gate_c
	assert '(.value.commit.committer // {}) as $cm | (.value.commit.author // {}) as $au' in gate_c, gate_c
	assert 'any($ids[]; (($cm.name // "") == .name and ($cm.email // "") == .email)' in gate_c, gate_c
	assert '(($au.name // "") == .name and ($au.email // "") == .email)' in gate_c, gate_c
	assert '(.actor.login // "") == $login and (.ref // "") == $ref' in gate_c, gate_c
	assert "select(.b < $k and $k <= .a)" in gate_c, gate_c
	assert '_pr_wait_for_required_checks "${N_PR}" "${N_HEAD_SHA}" "${N_BASE_REF}" 0' in sweep_code
	merges = [line for line in sweep_code.splitlines() if "gh pr merge" in line and "--squash" in line]
	assert len(merges) == 1, merges
	assert '--match-head-commit "${N_HEAD_SHA}"' in merges[0], merges


def main() -> int:
	# Direct `python3 tests/<file>.py` entrypoint — the repo's CI runs
	# tests via that pattern rather than pytest discovery, so without
	# this block the contract assertions never execute under CI.
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0

	for func in test_funcs:
		name = func.__name__
		try:
			if func.__code__.co_argcount == 1:
				with tempfile.TemporaryDirectory() as test_dir:
					func(Path(test_dir))
			else:
				func()
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1
		except Exception as e:
			print(f"  ERROR {name}: {type(e).__name__}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
