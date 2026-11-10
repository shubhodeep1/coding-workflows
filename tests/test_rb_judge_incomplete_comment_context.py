#!/usr/bin/env python3
"""BUG-002 (analysis/workflow-optimization-2026-10-03-2.md), judge half.

``gh_pr_with_all_comments`` / ``_gh_pr_with_all_comments_rest`` mark a PR
context whose comments read failed with ``comments_incomplete: true`` and
``incomplete_reasons``. The review-blocked judge (scripts/review_rb_judge.sh)
must not merge on such a context: it logs ``RB_JUDGE_CONTEXT_INCOMPLETE``,
tells the judge prompt, posts APPROVE as COMMENT and withholds every
merge-enabling outcome (auto-merge, direct merge, ai:ready-to-merge, head-gate
success). The inline legacy branch flags a failed paginated read the same way.
A complete context behaves exactly as before.

These tests extract the marker-delimited blocks of the judge script and run
them under bash with stubbed helpers, following the harness patterns of
tests/test_gh_pr_comments_argv_limit.py and
tests/test_review_rb_judge_head_binding.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
FRAGMENT = REPO_ROOT / "agents.d" / "wfopt-2026-10-03-2-rb-judge-context-incomplete.md"

requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the context block")

CTX_BEGIN = "# BEGIN rb judge context assembly (#7658)"
CTX_END = "# END rb judge context assembly (#7658)"
DOWNGRADE_BEGIN = "# BEGIN rb judge incomplete-context review-state downgrade (BUG-002)"
DOWNGRADE_END = "# END rb judge incomplete-context review-state downgrade (BUG-002)"
DEFER_BEGIN = "# BEGIN rb judge incomplete-context merge deferral (BUG-002)"
DEFER_END = "# END rb judge incomplete-context merge deferral (BUG-002)"
GATE_START_ANCHOR = 'RB_MERGE_ACTION="false"\ncase "${RB_ACTION}" in'
IHOLD_END = "# END rb-judge orchestrator integration hold (#8232)"
SHA = "a" * 40
DEFER_OUTPUTS = ["judge_action=skip", "judge_skip_reason=comments_context_incomplete"]

# Fake gh: the issue-comments and review-comments endpoints fail when a
# ``<name>.fail`` marker exists, otherwise print the canned JSON file.
FAKE_GH = r"""#!/usr/bin/env bash
d="${FAKE_GH_DIR}"
for a in "$@"; do
	case "$a" in
		*/issues/*/comments*) [ -e "$d/comments.fail" ] && { echo boom >&2; exit 1; }; cat "$d/comments.json" 2>/dev/null; exit 0 ;;
		*/pulls/*/comments*) [ -e "$d/review.fail" ] && { echo boom >&2; exit 1; }; cat "$d/review.json" 2>/dev/null; exit 0 ;;
	esac
done
exit 0
"""


def _text() -> str:
	return RB_JUDGE.read_text(encoding="utf-8")


def _marker_block(begin: str, end: str) -> str:
	text = _text()
	start = text.index(begin)
	stop = text.index(end, start) + len(end)
	return text[start:stop]


def _bash(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
	return subprocess.run(["bash", "-c", script], env=env, cwd=cwd, capture_output=True, text=True, check=False, timeout=60)


def _run_context(
	tmp_path: Path, helper_json: str | None, *, fail: tuple[str, ...] = (), pipefail: bool = True, helper_rc: int = 0
) -> dict:
	"""Run the context assembly block. ``helper_json=None`` drives the legacy branch."""
	bindir = tmp_path / "bin"
	bindir.mkdir(exist_ok=True)
	gh = bindir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	(tmp_path / "comments.json").write_text(
		json.dumps([{"user": {"login": "u"}, "body": "c1", "created_at": "2026-01-01T00:00:00Z"}]), encoding="utf-8"
	)
	(tmp_path / "review.json").write_text(
		json.dumps([{"user": {"login": "r"}, "path": "a.py", "line": 3, "body": "r1"}]), encoding="utf-8"
	)
	for name in fail:
		(tmp_path / f"{name}.fail").write_text("", encoding="utf-8")
	prelude = "set -euo pipefail\n" if pipefail else "set -eu\nset +o pipefail\n"
	prelude += "REPOSITORY=o/r\nPR_NUMBER=7\nPRELOADED_PR_META='{}'\n"
	prelude += 'gh_retry() { "$@"; }\n'
	if helper_json is None:
		prelude += "unset -f gh_pr_with_all_comments _gh_pr_with_all_comments_rest 2>/dev/null || true\n"
	else:
		(tmp_path / "helper.json").write_text(helper_json, encoding="utf-8")
		prelude += f'gh_pr_with_all_comments() {{ cat "${{FAKE_GH_DIR}}/helper.json"; return {int(helper_rc)}; }}\n'
	script = (
		prelude
		+ _marker_block(CTX_BEGIN, CTX_END)
		+ "\nprintf '%s' \"${PR_CONTEXT_JSON}\" > ctx.json\n"
		+ "printf '%s|%s\\n' \"${RB_JUDGE_COMMENTS_INCOMPLETE}\" \"${RB_JUDGE_COMMENTS_INCOMPLETE_REASONS}\" > flag.txt\n"
	)
	env = {
		"PATH": f"{bindir}:{os.environ['PATH']}",
		"FAKE_GH_DIR": str(tmp_path),
		# An inherited value must never mask a failure (or fake one).
		"RB_JUDGE_COMMENTS_INCOMPLETE": "false" if fail or helper_json else "true",
	}
	result = _bash(script, env, tmp_path)
	flag, _, reasons = (tmp_path / "flag.txt").read_text(encoding="utf-8").strip().partition("|") if (tmp_path / "flag.txt").exists() else ("", "", "")
	return {
		"rc": result.returncode,
		"stdout": result.stdout,
		"stderr": result.stderr,
		"flag": flag,
		"reasons": reasons,
		"ctx": (tmp_path / "ctx.json").read_text(encoding="utf-8") if (tmp_path / "ctx.json").exists() else "",
		"incomplete_lines": [line for line in result.stderr.splitlines() if "RB_JUDGE_CONTEXT_INCOMPLETE" in line],
	}


# ---------------------------------------------------------------------------
# Context assembly: helper path
# ---------------------------------------------------------------------------


@requires_jq
def test_helper_incomplete_context_sets_flag_and_warns_once(tmp_path: Path) -> None:
	payload = '{"meta":{},"comments":[],"review_comments":[],"comments_incomplete":true,"incomplete_reasons":["issue_comments_read_failed"]}'
	result = _run_context(tmp_path, payload)
	assert result["rc"] == 0, result["stderr"]
	assert result["flag"] == "true"
	assert result["reasons"] == "issue_comments_read_failed"
	assert result["incomplete_lines"] == [
		"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=7 reasons=issue_comments_read_failed action=defer_merge"
	]
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result["stderr"]


@requires_jq
@pytest.mark.parametrize(
	"payload",
	[
		'{"meta":{},"comments":[],"review_comments":[]}',
		'{"meta":{},"comments":[],"review_comments":[],"comments_incomplete":false}',
		'{"meta":{},"comments":[],"review_comments":[],"comments_incomplete":"true"}',
		'{"meta":{},"comments":[],"review_comments":[],"comments_incomplete":1}',
	],
)
def test_complete_or_non_boolean_context_is_not_flagged(tmp_path: Path, payload: str) -> None:
	result = _run_context(tmp_path, payload)
	assert result["rc"] == 0, result["stderr"]
	assert result["flag"] == "false"
	assert result["reasons"] == ""
	assert result["incomplete_lines"] == []
	assert result["ctx"] == payload


@requires_jq
def test_reasons_are_filtered_and_capped(tmp_path: Path) -> None:
	reasons = ["a;rm -rf", "bad\nline", "UPPER", "x" * 65, 5] + [f"r_{c}" for c in "abcdefghij"]
	payload = json.dumps(
		{"meta": {}, "comments": [], "review_comments": [], "comments_incomplete": True, "incomplete_reasons": reasons}
	)
	result = _run_context(tmp_path, payload)
	assert result["rc"] == 0, result["stderr"]
	assert result["reasons"] == ",".join(f"r_{c}" for c in "abcdefgh")
	assert len(result["incomplete_lines"]) == 1


@requires_jq
@pytest.mark.parametrize("reasons", [["A;B", "x y"], "not-a-list", None])
def test_all_invalid_reasons_report_unspecified(tmp_path: Path, reasons) -> None:
	doc = {"meta": {}, "comments": [], "review_comments": [], "comments_incomplete": True}
	if reasons is not None:
		doc["incomplete_reasons"] = reasons
	result = _run_context(tmp_path, json.dumps(doc))
	assert result["rc"] == 0, result["stderr"]
	assert result["flag"] == "true"
	assert result["incomplete_lines"] == ["::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=7 reasons=unspecified action=defer_merge"]


@requires_jq
def test_nonzero_helper_exit_with_incomplete_flag_still_defers(tmp_path: Path) -> None:
	payload = '{"meta":{},"comments":[],"review_comments":[],"comments_incomplete":true,"incomplete_reasons":["issue_comments_read_failed"]}'
	result = _run_context(tmp_path, payload, helper_rc=1)
	assert result["rc"] == 0, result["stderr"]
	assert result["flag"] == "true"
	assert result["ctx"] == "{}"
	assert "::warning::RB_JUDGE_CONTEXT_EMPTY pr=7 reason=assembly_failed" in result["stderr"]
	assert len(result["incomplete_lines"]) == 1


# ---------------------------------------------------------------------------
# Context assembly: inline legacy branch
# ---------------------------------------------------------------------------


@requires_jq
@pytest.mark.parametrize("pipefail", [True, False])
@pytest.mark.parametrize(
	("fail", "expected_reasons"),
	[
		(("comments",), "issue_comments_read_failed"),
		(("review",), "review_comments_read_failed"),
		(("comments", "review"), "issue_comments_read_failed,review_comments_read_failed"),
	],
)
def test_legacy_branch_flags_failed_paginated_reads(tmp_path: Path, fail, expected_reasons: str, pipefail: bool) -> None:
	result = _run_context(tmp_path, None, fail=fail, pipefail=pipefail)
	assert result["rc"] == 0, result["stderr"]
	assert "reason=helper_unavailable" in result["stderr"]
	assert result["flag"] == "true"
	assert result["incomplete_lines"] == [
		f"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=7 reasons={expected_reasons} action=defer_merge"
	]
	ctx = json.loads(result["ctx"])
	if "comments" in fail:
		assert ctx["comments"] == []
	else:
		assert [c["body"] for c in ctx["comments"]] == ["c1"]
	if "review" in fail:
		assert ctx["review_comments"] == []
	else:
		assert [c["body"] for c in ctx["review_comments"]] == ["r1"]
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result["stderr"]


@requires_jq
@pytest.mark.parametrize("pipefail", [True, False])
def test_legacy_branch_success_keeps_comments_and_resets_inherited_flag(tmp_path: Path, pipefail: bool) -> None:
	result = _run_context(tmp_path, None, pipefail=pipefail)
	assert result["rc"] == 0, result["stderr"]
	assert result["flag"] == "false"
	assert result["incomplete_lines"] == []
	ctx = json.loads(result["ctx"])
	assert [c["body"] for c in ctx["comments"]] == ["c1"]
	assert [c["body"] for c in ctx["review_comments"]] == ["r1"]


# ---------------------------------------------------------------------------
# Merge-deferral gate
# ---------------------------------------------------------------------------


def _gate_block() -> str:
	text = _text()
	start = text.index(GATE_START_ANCHOR)
	end = text.index(IHOLD_END, start) + len(IHOLD_END)
	return text[start:end]


def _gate_run(
	tmp_path: Path,
	action: str,
	*,
	incomplete: str,
	head_ref: str = "feature/x",
	is_final: str = "false",
	already_merged: str = "false",
) -> dict:
	calls = tmp_path / "calls.log"
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	prelude = textwrap.dedent(
		"""\
		set -uo pipefail
		gh_retry() { printf 'gh_retry %s\\n' "$(printf '%s' "$*" | tr '\\n' ' ')" >> "${CALL_LOG}"; return 0; }
		review_head_gate_post_status() { printf 'status %s\\n' "$*" >> "${CALL_LOG}"; return 0; }
		_resilient_phase_swap() { printf 'phase_swap %s\\n' "$*" >> "${CALL_LOG}"; return 0; }
		rb_security_merge_gate() { printf 'security_gate\\n' >> "${CALL_LOG}"; return 0; }
		"""
	)
	script = prelude + _gate_block() + "\necho FELLTHROUGH\n"
	env = {
		"PATH": "/usr/bin:/bin",
		"CALL_LOG": str(calls),
		"GITHUB_OUTPUT": str(output),
		"RB_ACTION": action,
		"IS_FINAL": is_final,
		"PR_ALREADY_MERGED": already_merged,
		"POST_REVIEW_HEAD_REF": head_ref,
		"RB_JUDGED_HEAD_SHA": SHA,
		"REPOSITORY": "owner/repo",
		"PR_NUMBER": "42",
		"RB_JUDGE_COMMENTS_INCOMPLETE": incomplete,
		"RB_JUDGE_COMMENTS_INCOMPLETE_REASONS": "issue_comments_read_failed",
	}
	result = _bash(script, env, tmp_path)
	return {
		"rc": result.returncode,
		"stdout": result.stdout,
		"stderr": result.stderr,
		"calls": calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [],
		"outputs": output.read_text(encoding="utf-8").splitlines(),
	}


@pytest.mark.parametrize("head_ref", ["feature/x", "orchestrator/project-12"])
@pytest.mark.parametrize(("action", "is_final"), [("merge", "false"), ("merge_with_followup", "false"), ("fix", "true")])
def test_incomplete_context_withholds_every_merge_outcome(tmp_path: Path, action: str, is_final: str, head_ref: str) -> None:
	result = _gate_run(tmp_path, action, incomplete="true", head_ref=head_ref, is_final=is_final)
	assert result["rc"] == 0, result["stderr"]
	assert "FELLTHROUGH" not in result["stdout"]
	assert result["outputs"] == DEFER_OUTPUTS
	assert (
		f"RB_JUDGE_CONTEXT_INCOMPLETE pr=42 reasons=issue_comments_read_failed action=merge_withheld judge_action={action}"
		in result["stdout"]
	)
	assert not any(c.startswith(("status ", "phase_swap")) for c in result["calls"])
	assert "security_gate" not in result["calls"]
	assert not any("pr merge" in c or "ai:ready-to-merge" in c for c in result["calls"])
	comments = [c for c in result["calls"] if "issues/42/comments" in c]
	assert len(comments) == 1 and "merge deferred: PR comment context incomplete" in comments[0]
	assert f"**{action}**" in comments[0]


@pytest.mark.parametrize(("action", "already_merged"), [("fix", "false"), ("close_and_reissue", "false"), ("merge", "true")])
def test_non_merge_outcomes_and_merged_prs_pass_through(tmp_path: Path, action: str, already_merged: str) -> None:
	result = _gate_run(tmp_path, action, incomplete="true", already_merged=already_merged)
	assert result["rc"] == 0, result["stderr"]
	assert "FELLTHROUGH" in result["stdout"]
	assert "RB_JUDGE_CONTEXT_INCOMPLETE" not in result["stdout"]
	assert result["outputs"] == []


def test_complete_context_still_merges_as_before(tmp_path: Path) -> None:
	result = _gate_run(tmp_path, "merge", incomplete="false")
	assert result["rc"] == 0, result["stderr"]
	assert "FELLTHROUGH" in result["stdout"]
	assert "RB_JUDGE_CONTEXT_INCOMPLETE" not in result["stdout"]
	assert result["outputs"] == []

	held = tmp_path / "orch"
	held.mkdir()
	result = _gate_run(held, "merge", incomplete="false", head_ref="orchestrator/project-12")
	assert "FELLTHROUGH" not in result["stdout"]
	assert result["outputs"] == [
		"judge_handled=true",
		"judge_action=orchestrator_integration_hold",
		"judge_skip_reason=orchestrator_integration_pr",
	]
	assert any(c.startswith(f"status owner/repo {SHA} success") for c in result["calls"])


@pytest.mark.parametrize(("action", "is_final"), [("merge", "false"), ("merge_with_followup", "false"), ("fix", "true")])
def test_complete_context_reaches_the_merge_stage(tmp_path: Path, action: str, is_final: str) -> None:
	"""A complete context passes the deferral and the security hold and reaches the merge commands.

	The block runs from the merge-action evaluation up to the merge error
	capture helpers (#7659), which sit right before the unchanged
	`gh pr merge` dispatch. The security gate stub passes, so reaching the end
	means the run proceeds to merge exactly as before this change.
	"""
	text = _text()
	start = text.index(GATE_START_ANCHOR)
	end = text.index("# BEGIN rb-judge merge error capture (#7659)", start)
	calls = tmp_path / "calls.log"
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	script = (
		"set -uo pipefail\n"
		'gh_retry() { printf \'gh_retry %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'review_head_gate_post_status() { printf \'status %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'_resilient_phase_swap() { printf \'phase_swap %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'rb_security_merge_gate() { printf \'security_gate\\n\' >> "${CALL_LOG}"; return 0; }\n'
		+ text[start:end]
		+ "\necho REACHED_MERGE_STAGE\n"
	)
	env = {
		"PATH": "/usr/bin:/bin",
		"CALL_LOG": str(calls),
		"GITHUB_OUTPUT": str(output),
		"RB_ACTION": action,
		"IS_FINAL": is_final,
		"PR_ALREADY_MERGED": "false",
		"POST_REVIEW_HEAD_REF": "feature/x",
		"RB_JUDGED_HEAD_SHA": SHA,
		"REPOSITORY": "owner/repo",
		"PR_NUMBER": "42",
		"RB_JUDGE_COMMENTS_INCOMPLETE": "false",
		"RB_JUDGE_COMMENTS_INCOMPLETE_REASONS": "",
	}
	result = _bash(script, env, tmp_path)
	assert result.returncode == 0, result.stderr
	assert "REACHED_MERGE_STAGE" in result.stdout
	assert "RB_JUDGE_CONTEXT_INCOMPLETE" not in result.stdout + result.stderr
	assert output.read_text(encoding="utf-8") == ""
	log_lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
	assert log_lines == ["security_gate"]


# Fake gh for the merge dispatch: records every call, succeeds.
FAKE_GH_RECORD = r"""#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "${CALL_LOG}"
exit 0
"""


@pytest.mark.parametrize("incomplete", ["false", "true"])
def test_merge_arm_dispatches_gh_pr_merge_only_for_a_complete_context(tmp_path: Path, incomplete: str) -> None:
	"""Run the real `merge` arm: a complete context issues the head-bound merge."""
	text = _text()
	start = text.index(GATE_START_ANCHOR)
	arm = text.index('case "${RB_ACTION}" in\n  merge)', text.index("# END rb-judge merge error capture (#7659)"))
	end = text.index("\n  fix)\n", arm)
	bindir = tmp_path / "bin"
	bindir.mkdir()
	gh = bindir / "gh"
	gh.write_text(FAKE_GH_RECORD, encoding="utf-8")
	gh.chmod(0o755)
	calls = tmp_path / "calls.log"
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	script = (
		"set -uo pipefail\n"
		'gh_retry() { "$@"; }\n'
		"_safe_gh_jq() { printf '%s' '{\"state\":\"open\",\"mergeable\":true}'; }\n"
		"_pr_base_fresh_for_merge() { return 0; }\n"
		"_pr_wait_for_required_checks() { return 0; }\n"
		'review_head_gate_post_status() { printf \'status %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'ensure_label_exists() { printf \'ensure_label %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'_resilient_phase_swap() { printf \'phase_swap %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'rb_security_merge_gate() { printf \'security_gate\\n\' >> "${CALL_LOG}"; return 0; }\n'
		+ text[start:end]
		+ "\nesac\necho REACHED_END\n"
	)
	env = {
		"PATH": f"{bindir}:/usr/bin:/bin",
		"TMPDIR": str(tmp_path),
		"CALL_LOG": str(calls),
		"GITHUB_OUTPUT": str(output),
		"RB_ACTION": "merge",
		"IS_FINAL": "false",
		"PR_ALREADY_MERGED": "false",
		"POST_REVIEW_HEAD_REF": "feature/x",
		"RB_JUDGED_HEAD_SHA": SHA,
		"REPOSITORY": "owner/repo",
		"PR_NUMBER": "42",
		"ENABLE_AUTO_MERGE": "true",
		"ISSUE_NUMBERS": "7",
		"RB_MERGE_METHOD_FLAG": "--squash",
		"RB_JUDGE_COMMENTS_INCOMPLETE": incomplete,
		"RB_JUDGE_COMMENTS_INCOMPLETE_REASONS": "issue_comments_read_failed",
	}
	result = _bash(script, env, tmp_path)
	assert result.returncode == 0, result.stderr
	log_lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
	merges = [line for line in log_lines if line.startswith("gh pr merge ")]
	outputs = output.read_text(encoding="utf-8").splitlines()
	if incomplete == "false":
		assert "REACHED_END" in result.stdout
		assert merges == [f"gh pr merge 42 --repo owner/repo --squash --auto --match-head-commit {SHA}"]
		assert f"status owner/repo {SHA} success review-blocked judge approved" in log_lines
		assert "phase_swap 7 ai:ready-to-merge" in log_lines
		assert outputs == ["judge_handled=true", "judge_action=merge", "judge_merge_status=merge_requested"]
		assert "RB_JUDGE_CONTEXT_INCOMPLETE" not in result.stdout + result.stderr
	else:
		assert "REACHED_END" not in result.stdout
		assert merges == []
		assert not any(line.startswith(("status ", "phase_swap", "ensure_label")) for line in log_lines)
		assert outputs == DEFER_OUTPUTS


@requires_jq
@pytest.mark.parametrize("incomplete", [True, False])
def test_helper_flag_reaches_the_merge_gate_end_to_end(tmp_path: Path, incomplete: bool) -> None:
	"""Helper JSON -> context block -> flag -> merge gate, in one shell."""
	doc = {"meta": {}, "comments": [], "review_comments": []}
	if incomplete:
		doc.update(comments_incomplete=True, incomplete_reasons=["issue_comments_read_failed"])
	(tmp_path / "helper.json").write_text(json.dumps(doc), encoding="utf-8")
	calls = tmp_path / "calls.log"
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	script = (
		"set -uo pipefail\n"
		"RB_JUDGE_COMMENTS_INCOMPLETE=false\nRB_JUDGE_COMMENTS_INCOMPLETE_REASONS=\n"
		"REPOSITORY=owner/repo\nPR_NUMBER=42\nPRELOADED_PR_META='{}'\n"
		'gh_pr_with_all_comments() { cat "${FAKE_GH_DIR}/helper.json"; }\n'
		'gh_retry() { printf \'gh_retry %s\\n\' "$(printf \'%s\' "$*" | tr \'\\n\' \' \')" >> "${CALL_LOG}"; return 0; }\n'
		'review_head_gate_post_status() { printf \'status %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'_resilient_phase_swap() { printf \'phase_swap %s\\n\' "$*" >> "${CALL_LOG}"; return 0; }\n'
		'rb_security_merge_gate() { printf \'security_gate\\n\' >> "${CALL_LOG}"; return 0; }\n'
		+ _marker_block(CTX_BEGIN, CTX_END)
		+ "\n"
		+ _gate_block()
		+ "\necho FELLTHROUGH\n"
	)
	env = {
		"PATH": os.environ["PATH"],
		"FAKE_GH_DIR": str(tmp_path),
		"CALL_LOG": str(calls),
		"GITHUB_OUTPUT": str(output),
		"RB_ACTION": "merge",
		"IS_FINAL": "false",
		"PR_ALREADY_MERGED": "false",
		"POST_REVIEW_HEAD_REF": "feature/x",
		"RB_JUDGED_HEAD_SHA": SHA,
	}
	result = _bash(script, env, tmp_path)
	assert result.returncode == 0, result.stderr
	log = calls.read_text(encoding="utf-8") if calls.exists() else ""
	if incomplete:
		assert "FELLTHROUGH" not in result.stdout
		assert output.read_text(encoding="utf-8").splitlines() == DEFER_OUTPUTS
		assert "action=merge_withheld judge_action=merge" in result.stdout
		assert "action=defer_merge" in result.stderr
		actions = [line for line in log.splitlines() if "issues/42/comments" not in line]
		assert not any("pr merge" in c or "ai:ready-to-merge" in c for c in actions)
		assert not any(c.startswith(("status ", "phase_swap", "security_gate")) for c in actions)
		assert sum("issues/42/comments" in line for line in log.splitlines()) == 1
	else:
		assert "FELLTHROUGH" in result.stdout
		assert output.read_text(encoding="utf-8") == ""
		assert "RB_JUDGE_CONTEXT_INCOMPLETE" not in result.stdout + result.stderr
		assert log == ""


def test_gate_runs_before_every_merge_site() -> None:
	text = _text()
	gate = text.index(DEFER_BEGIN)
	assert text.index('# BEGIN rb-judge orchestrator integration hold (#8232)') > gate
	assert text.index('case "${RB_ACTION}" in\n  merge)') > gate
	assert text.index(GATE_START_ANCHOR) < gate
	# Every merge call, including the transient-retry helper (#7659 / #8524),
	# sits after the gate.
	assert text.index("# BEGIN rb-judge merge error capture (#7659)") > gate
	merge_calls = [i for i in range(len(text)) if text.startswith('gh pr merge "${PR_NUMBER}"', i)]
	assert merge_calls
	assert all(i > gate for i in merge_calls)
	ready = [i for i in range(gate, len(text)) if text.startswith("--add-label 'ai:ready-to-merge'", i)]
	assert ready and all(i > gate for i in ready)


@pytest.mark.parametrize(
	"message", ["Judge staged no effective changes. Treating as merge.", "Judge produced no file changes. Treating as merge."]
)
@pytest.mark.parametrize("incomplete", ["true", "false"])
def test_noop_fix_merge_branches_defer_on_incomplete_context(tmp_path: Path, message: str, incomplete: str) -> None:
	text = _text()
	branch = text.split(f'echo "{message}"\n', 1)[1].split('ensure_label_exists "ai:ready-to-merge"', 1)[0]
	assert branch.index("rb_judge_comments_incomplete_defer_merge") < branch.index("! rb_security_merge_gate")
	defer = _marker_block(DEFER_BEGIN, DEFER_END)
	calls = tmp_path / "calls.log"
	output = tmp_path / "judge_output"
	output.write_text("", encoding="utf-8")
	script = (
		"set -euo pipefail\n"
		'gh_retry() { printf "gh_retry %s\\n" "$*" >> "${CALL_LOG}"; }\n'
		'rb_security_merge_gate() { echo security_gate >> "${CALL_LOG}"; return 0; }\n'
		'review_head_gate_post_status() { echo "status $*" >> "${CALL_LOG}"; }\n'
		+ defer + "\n" + branch + "echo LABELS_ALLOWED\n"
	)
	env = {
		"PATH": os.environ["PATH"],
		"GITHUB_OUTPUT": str(output),
		"CALL_LOG": str(calls),
		"RB_ACTION": "fix",
		"PR_NUMBER": "42",
		"REPOSITORY": "o/r",
		"RB_JUDGED_HEAD_SHA": SHA,
		"RB_JUDGE_COMMENTS_INCOMPLETE": incomplete,
	}
	result = _bash(script, env, tmp_path)
	assert result.returncode == 0, result.stderr
	log = calls.read_text(encoding="utf-8") if calls.exists() else ""
	if incomplete == "true":
		assert "LABELS_ALLOWED" not in result.stdout
		log_lines = log.splitlines()
		assert "security_gate" not in log_lines
		assert not any(line.startswith("status ") for line in log_lines)
		assert output.read_text(encoding="utf-8").splitlines() == DEFER_OUTPUTS
		assert "action=merge_withheld judge_action=fix_no_changes" in result.stdout
	else:
		assert "LABELS_ALLOWED" in result.stdout
		assert "security_gate" in log
		assert f"status o/r {SHA} success" in log
		assert output.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# Review-state downgrade, prompt note, fragment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	("incomplete", "state", "expected"),
	[
		("true", "APPROVE", "COMMENT"),
		("true", "APPROVE_WITH_COMMENTS", "COMMENT"),
		("true", "REQUEST_CHANGES", "REQUEST_CHANGES"),
		("true", "", ""),
		("false", "APPROVE", "APPROVE"),
	],
)
def test_review_state_downgrade(tmp_path: Path, incomplete: str, state: str, expected: str) -> None:
	script = "set -euo pipefail\n" + _marker_block(DOWNGRADE_BEGIN, DOWNGRADE_END) + '\necho "STATE=${RB_OUTBOUND_REVIEW_STATE}"\n'
	env = {
		"PATH": os.environ["PATH"],
		"PR_NUMBER": "42",
		"RB_JUDGE_COMMENTS_INCOMPLETE": incomplete,
		"RB_OUTBOUND_REVIEW_STATE": state,
	}
	result = _bash(script, env, tmp_path)
	assert result.returncode == 0, result.stderr
	assert f"STATE={expected}" in result.stdout.splitlines()
	downgraded = expected != state
	assert ("action=approve_downgraded" in result.stdout) is downgraded


@pytest.mark.parametrize(("state", "downgraded"), [("APPROVE", "true"), ("REQUEST_CHANGES", "false")])
def test_audit_label_names_the_cause_of_the_posted_state(tmp_path: Path, state: str, downgraded: str) -> None:
	"""Only the incomplete-context downgrade is labelled "approval withheld"; break-glass keeps its label."""
	script = "set -euo pipefail\n" + _marker_block(DOWNGRADE_BEGIN, DOWNGRADE_END) + '\necho "DOWNGRADED=${RB_JUDGE_APPROVAL_DOWNGRADED}"\n'
	env = {
		"PATH": os.environ["PATH"],
		"PR_NUMBER": "42",
		"RB_JUDGE_COMMENTS_INCOMPLETE": "true",
		"RB_OUTBOUND_REVIEW_STATE": state,
	}
	result = _bash(script, env, tmp_path)
	assert result.returncode == 0, result.stderr
	assert f"DOWNGRADED={downgraded}" in result.stdout.splitlines()
	text = _text()
	label = text.index("(PR comment context incomplete; approval withheld)")
	guard = text.rindex('if [ "${RB_JUDGE_APPROVAL_DOWNGRADED:-false}" = "true" ]; then', 0, label)
	assert label - guard < 200


def test_prompt_note_is_flag_guarded_and_precedes_inline_comments() -> None:
	text = _text()
	note = text.index("NOTE: The PR comment context below could not be fully read")
	guard = text.rindex('if [ "${RB_JUDGE_COMMENTS_INCOMPLETE:-false}" = "true" ]; then', 0, note)
	assert note - guard < 200
	assert note < text.index('echo "=== PR #${PR_NUMBER} INLINE REVIEW COMMENTS ==="')
	assert text.index(DOWNGRADE_BEGIN) < text.index('echo "judge_review_state_outbound=${RB_OUTBOUND_REVIEW_STATE}"')


def test_agents_fragment_registers_the_prefix() -> None:
	# The release fold moves the fragment into agents.md and deletes it, so
	# read the rendered agents text (agents.md plus any pending fragments).
	import sys

	sys.path.insert(0, str(Path(__file__).resolve().parent))
	from agents_doc import agents_text

	body = agents_text()
	assert "- `RB_JUDGE_CONTEXT_INCOMPLETE`" in body
	assert "LOG_PREFIX.name=RB_JUDGE_CONTEXT_INCOMPLETE" in body
	assert "scripts/review_rb_judge.sh" in body and "scripts/orchestrate_poll_process.sh" in body
