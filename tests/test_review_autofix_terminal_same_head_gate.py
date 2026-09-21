"""Contract + behaviour tests for the review gate's terminal same-head skip.

PR #4077 (head ``0d30bc69``) reached the terminal same-head partial-finalize
state (``resume_state=no_progress``, ``resume_should_continue=false``) at
2026-09-12T15:55Z. The review_autofix_sweep cron then re-dispatched the PR
every 30 minutes for the next 12.5 hours (~25 runs, e.g. 34703936345), each
one restoring the cached terminal state and exiting neutral after ~6 minutes
of runner setup. The ``Evaluate review gate`` step now reads the newest
``<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->`` marker for the PR's current head and
skips a ``workflow_dispatch`` run when that marker is terminal.

These tests pin:
  1. the wiring in ``.github/workflows/review_autofix.yml`` (env defaults,
     the head_sha ride-along on the existing /pulls fetch, the bypasses,
     the stable log prefixes);
  2. the decision logic of the embedded Python parser, executed directly
     against marker fixtures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README = REPO_ROOT / "README.md"

HEAD = "0d30bc69a97515971054c8a867cdef4cb5bf6003"
OTHER_HEAD = "a3d0b5d9facbab7ba4c51ccf78dfe0cfd0236ebf"
BOT_LOGIN = "codex"
MARKER_AUTHOR_LOGIN = "workflow-pat-user"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _gate_block() -> str:
	lines = _workflow_text().splitlines()
	needle = "- name: Evaluate review gate"
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
	raise AssertionError("Evaluate review gate step not found")


def _terminal_parser_script() -> str:
	gate_lines = _gate_block().splitlines()
	start_idx = -1
	for idx, line in enumerate(gate_lines):
		if "python3 -I -B - <<'PY'" in line:
			start_idx = idx + 1
			break
	if start_idx < 0:
		raise AssertionError("terminal same-head parser heredoc not found in the gate step")
	body: list[str] = []
	for line in gate_lines[start_idx:]:
		if line.strip() == "PY":
			return textwrap.dedent("\n".join(body))
		body.append(line)
	raise AssertionError("terminal same-head parser heredoc missing its PY terminator")


def _marker(head_sha: str, *, resume_state: str, resume_round: int, should_continue: bool, limit: int = 3) -> str:
	continue_text = "true" if should_continue else "false"
	return textwrap.dedent(
		f"""\
		<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->
		**AI review/autofix partial finalize**

		Same-head resume stops here because {resume_state.replace('_', ' ')}.

		- Resume state: {resume_state}
		- Head SHA: {head_sha}

		```text
		partial_finalize=true
		reason=recoverable_failure
		phase=editor
		completed_scope=reviewers,consolidator,parser,ledger
		incomplete_scope=editor
		validated_edits_committed=false
		edits_pushed=false
		validation_tail_can_complete=true
		edits_withheld_for_safety=false
		withheld_reason=none
		head_sha={head_sha}
		resume_round={resume_round}
		resume_round_limit={limit}
		resume_state={resume_state}
		resume_should_continue={continue_text}
		```
		"""
	)


def _run_parser(comments: list[dict[str, object]], head_sha: str) -> dict[str, str]:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PARTIAL_MARKER_COMMENTS_JSON"] = json.dumps([{"author_login": MARKER_AUTHOR_LOGIN, **comment} for comment in comments])
	env["GATE_HEAD_SHA"] = head_sha
	env["GATE_BOT_LOGIN"] = BOT_LOGIN
	env["GATE_MARKER_AUTHOR_LOGIN"] = MARKER_AUTHOR_LOGIN
	result = subprocess.run(
		[sys.executable, "-"],
		input=_terminal_parser_script(),
		env=env,
		text=True,
		capture_output=True,
		check=True,
	)
	decision: dict[str, str] = {}
	for line in result.stdout.splitlines():
		key, sep, value = line.partition("=")
		if sep:
			decision[key] = value
	return decision


# ---------------------------------------------------------------------------
# Wiring contract
# ---------------------------------------------------------------------------


def test_gate_declares_terminal_same_head_env_with_defaults() -> None:
	gate = _gate_block()
	assert "AUTOFIX_SKIP_TERMINAL_SAME_HEAD: ${{ vars.AUTOFIX_SKIP_TERMINAL_SAME_HEAD }}" in gate
	# CLAUDE.md §4: the shell default is `true`; the var only ever turns it off.
	assert '[ "${AUTOFIX_SKIP_TERMINAL_SAME_HEAD:-true}" != "false" ]' in gate
	# Same expression as the retrigger_guard step so a stall-poller
	# force_rb_judge dispatch is never swallowed here.
	force_rb_judge_expression = "FORCE_RB_JUDGE: ${{ (inputs.force_rb_judge || github.event.inputs.force_rb_judge == 'true') && 'true' || 'false' }}"
	assert gate.count(force_rb_judge_expression) == 1
	assert _workflow_text().count(force_rb_judge_expression) == 2
	assert '[ "${FORCE_RB_JUDGE:-false}" != "true" ]' in gate


def test_gate_head_sha_rides_on_existing_pulls_fetch() -> None:
	gate = _gate_block()
	# §15: no second /pulls call — head_sha is added to the existing jq projection.
	assert gate.count('gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"') == 1
	assert 'head_sha: (.head.sha // "")' in gate
	assert """pr_head_sha_gate="$(printf '%s' "${_pr_gate}" | jq -r '.head_sha // ""' 2>/dev/null || echo "")\"""" in gate
	assert 'pr_head_sha_gate=""' in gate


def test_gate_terminal_skip_is_dispatch_only_and_has_bypasses() -> None:
	gate = _gate_block()
	# GitHub documents the called workflow's github context as the caller's
	# context, so wrapper workflow_dispatch runs retain this event name.
	assert '[ "${EVENT_NAME}" = "workflow_dispatch" ]' in gate
	assert "reusable workflow retains its caller's `github` context" in gate
	# force-review marker + label bypass, mirroring the deterministic skip.
	assert 'AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate:-unknown} reason=force_review_marker' in gate
	# Conflicted / unknown mergeability keeps the PR on codex-agent.
	assert 'elif [ "${pr_mergeable}" != "true" ]; then' in gate
	assert "reason=mergeable_not_true" in gate
	assert "reason=head_sha_unavailable" in gate
	# Only marker comments reach the parser.
	assert 'select((.body // "") | contains("<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->"))' in gate
	assert 'author_login: (.user.login // "")' in gate
	assert 'GATE_BOT_LOGIN="${AUTOFIX_BOT_LOGIN:-codex}"' in gate
	assert 'GATE_MARKER_AUTHOR_LOGIN="${terminal_marker_author_login}"' in gate
	assert "gh api user --jq '.login // \"\"'" in gate
	assert 'gh api --paginate -X GET "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments"' in gate
	# Fail-open paths are logged with stable prefixes.
	assert "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=marker_author_unavailable" in gate
	assert "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=api_error" in gate
	assert "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=parse_error" in gate
	# The skip decision itself.
	assert 'SKIP_REASON="terminal_same_head"' in gate
	assert "AUTOFIX_GATE_SKIP reason=terminal_same_head pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} resume_state=${terminal_resume_state}" in gate
	assert "AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD pr=${PR_NUMBER} head_sha=${pr_head_sha_gate}" in gate


def test_gate_terminal_skip_runs_after_self_trigger_skip_and_before_deterministic_skip() -> None:
	gate = _gate_block()
	self_trigger = gate.index('SKIP_REASON="self_triggered_autofix"')
	terminal = gate.index('SKIP_REASON="terminal_same_head"')
	deterministic = gate.index("# Deterministic pre-review skip (last gate check):")
	assert self_trigger < terminal < deterministic


def test_readme_documents_terminal_same_head_skip() -> None:
	readme = README.read_text(encoding="utf-8")
	assert "| `AUTOFIX_SKIP_TERMINAL_SAME_HEAD` | No | `true` | review_autofix |" in readme
	assert "AUTOFIX_GATE_SKIP reason=terminal_same_head" in readme
	assert "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED" in readme
	ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
	assert "python3 -m pytest -q -p no:cacheprovider tests/test_review_autofix_terminal_same_head_gate.py" in ci_workflow


# ---------------------------------------------------------------------------
# Parser behaviour
# ---------------------------------------------------------------------------


def test_parser_marks_newest_terminal_marker_for_current_head() -> None:
	assert MARKER_AUTHOR_LOGIN != BOT_LOGIN
	comments = [
		{"id": 1, "created_at": "2026-09-12T15:24:58Z", "body": _marker(HEAD, resume_state="in_progress", resume_round=2, should_continue=True)},
		{"id": 2, "created_at": "2026-09-12T15:55:33Z", "body": _marker(HEAD, resume_state="no_progress", resume_round=3, should_continue=False)},
	]
	decision = _run_parser(comments, HEAD)
	assert decision["terminal"] == "true"
	assert decision["matching_markers"] == "2"
	assert decision["resume_state"] == "no_progress"
	assert decision["resume_round"] == "3"
	assert decision["resume_round_limit"] == "3"
	assert decision["marker_comment_id"] == "2"
	assert decision["untrusted_markers"] == "0"


def test_parser_ignores_markers_for_other_heads() -> None:
	comments = [
		{"id": 7, "created_at": "2026-09-12T11:42:29Z", "body": _marker(OTHER_HEAD, resume_state="round_budget_exhausted", resume_round=3, should_continue=False)},
	]
	decision = _run_parser(comments, HEAD)
	assert decision["terminal"] == "false"
	assert decision["matching_markers"] == "0"
	assert decision["marker_comment_id"] == "unknown"


def test_parser_newest_marker_wins_even_when_older_one_was_terminal() -> None:
	# A later round that is allowed to continue (for example after the
	# operator raised REVIEW_MAX_RESUME_ROUNDS) must reopen the head.
	comments = [
		{"id": 3, "created_at": "2026-09-12T15:55:33Z", "body": _marker(HEAD, resume_state="round_budget_exhausted", resume_round=3, should_continue=False)},
		{"id": 4, "created_at": "2026-09-12T16:30:00Z", "body": _marker(HEAD, resume_state="in_progress", resume_round=4, should_continue=True, limit=5)},
	]
	decision = _run_parser(comments, HEAD)
	assert decision["terminal"] == "false"
	assert decision["matching_markers"] == "2"
	assert decision["resume_round"] == "4"


def test_parser_ignores_newer_markers_from_untrusted_authors() -> None:
	trusted_resumable = {"id": 12, "created_at": "2026-09-12T15:55:33Z", "body": _marker(HEAD, resume_state="in_progress", resume_round=2, should_continue=True)}
	untrusted_terminal = {"id": 13, "created_at": "2026-09-12T16:00:00Z", "author_login": BOT_LOGIN, "body": _marker(HEAD, resume_state="no_progress", resume_round=3, should_continue=False)}
	decision = _run_parser([trusted_resumable, untrusted_terminal], HEAD)
	assert decision["terminal"] == "false"
	assert decision["matching_markers"] == "1"
	assert decision["untrusted_markers"] == "1"
	assert decision["commit_bot_markers"] == "1"
	assert decision["marker_comment_id"] == "12"

	trusted_terminal = {"id": 14, "created_at": "2026-09-12T16:05:00Z", "body": _marker(HEAD, resume_state="no_progress", resume_round=3, should_continue=False)}
	untrusted_resumable = {"id": 15, "created_at": "2026-09-12T16:10:00Z", "author_login": "mallory", "body": _marker(HEAD, resume_state="in_progress", resume_round=4, should_continue=True, limit=5)}
	decision = _run_parser([trusted_terminal, untrusted_resumable], HEAD)
	assert decision["terminal"] == "true"
	assert decision["matching_markers"] == "1"
	assert decision["untrusted_markers"] == "1"
	assert decision["commit_bot_markers"] == "0"
	assert decision["marker_comment_id"] == "14"


def test_parser_head_match_is_case_insensitive_and_requires_partial_marker() -> None:
	comments = [
		{"id": 5, "created_at": "2026-09-12T15:55:33Z", "body": _marker(HEAD.upper(), resume_state="no_progress", resume_round=3, should_continue=False)},
		# Comment text that merely mentions the head but has no fenced block.
		{"id": 6, "created_at": "2026-09-12T16:00:00Z", "body": f"head_sha={HEAD}\nresume_should_continue=false"},
	]
	decision = _run_parser(comments, HEAD)
	assert decision["terminal"] == "true"
	assert decision["matching_markers"] == "1"
	assert decision["marker_comment_id"] == "5"


def test_parser_fails_open_on_garbage_input() -> None:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PARTIAL_MARKER_COMMENTS_JSON"] = "not json at all"
	env["GATE_HEAD_SHA"] = HEAD
	env["GATE_BOT_LOGIN"] = BOT_LOGIN
	env["GATE_MARKER_AUTHOR_LOGIN"] = MARKER_AUTHOR_LOGIN
	result = subprocess.run([sys.executable, "-"], input=_terminal_parser_script(), env=env, text=True, capture_output=True, check=False)
	assert result.returncode != 0
	assert result.stdout == ""
	assert "reason=parse_error" in _gate_block()

	# An empty head SHA can never match a marker.
	decision = _run_parser(
		[{"id": 9, "created_at": "2026-09-12T15:55:33Z", "body": _marker("", resume_state="no_progress", resume_round=3, should_continue=False)}],
		"",
	)
	assert decision["terminal"] == "false"


def test_parser_sanitises_values_for_log_lines() -> None:
	body = _marker(HEAD, resume_state="no_progress", resume_round=3, should_continue=False).replace(
		"resume_state=no_progress", "resume_state=no progress; rm -rf $HOME"
	)
	decision = _run_parser([{"id": 11, "created_at": "2026-09-12T15:55:33Z", "body": body}], HEAD)
	assert decision["terminal"] == "true"
	assert decision["resume_state"] == "noprogressrm-rfHOME"
