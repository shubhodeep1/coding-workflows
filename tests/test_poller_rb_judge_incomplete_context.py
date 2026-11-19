#!/usr/bin/env python3
"""BUG-002 (analysis/workflow-optimization-2026-10-03-2.md), poller caller half.

The poller's review-blocked judge assembles ``RB_PR_CONTEXT_JSON`` from PR
comments. When a comment read failed -- the helper reports
``comments_incomplete: true`` or one of the inline legacy paginated reads
fails -- the poller now logs ``RB_JUDGE_CONTEXT_INCOMPLETE``, adds a note to
the judge prompt, and withholds every merge-enabling outcome for this tick
(merge, merge_with_followup, final-retry fix, and the no-change "treat as
merge" path). Non-merge outcomes and complete contexts are unchanged.

The cases drive the extracted poller blocks with a fake ``gh`` on PATH. They
are collected in CI through tests/test_gh_pr_comments_argv_limit.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"

GATE_BEGIN = "# BEGIN rb judge incomplete-context merge gate (BUG-002)"
GATE_END = "# END rb judge incomplete-context merge gate (BUG-002)"
INCOMPLETE_ELIF = 'elif [ "${_rb_ctx_comments_incomplete:-false}" = "true" ]; then'

_requires_jq_rb_incomplete = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to run the poller block")

# Same endpoint routing as tests/test_gh_pr_comments_argv_limit.py: a missing
# file prints nothing and exits 0; a ``<name>.fail`` marker exits 1.
_FAKE_GH_RB_INCOMPLETE = r"""#!/usr/bin/env bash
d="${FAKE_GH_DIR}"
for a in "$@"; do
	case "$a" in
		graphql) cat "$d/graphql.json" 2>/dev/null; exit 0 ;;
		*/issues/*/comments*) [ -e "$d/comments.fail" ] && { echo boom >&2; exit 1; }; cat "$d/comments.json" 2>/dev/null; exit 0 ;;
		*/pulls/*/comments*) [ -e "$d/review.fail" ] && { echo boom >&2; exit 1; }; cat "$d/review.json" 2>/dev/null; exit 0 ;;
		repos/*/pulls/*) [ -e "$d/meta.fail" ] && { echo boom >&2; exit 1; }; cat "$d/meta.json" 2>/dev/null; exit 0 ;;
	esac
done
exit 0
"""

_POLLER_PREFIX_RB_INCOMPLETE = "GITHUB_REPOSITORY=o/r\nRB_PR=9\nRB_PRELOADED_META='{}'\n"
_REPORT_RB_INCOMPLETE = (
	'\nprintf \'%s\' "${RB_PR_CONTEXT_JSON}" > ctx.json\n'
	'printf \'FLAG=%s\\nREASONS=%s\\n\' "${_rb_ctx_comments_incomplete}" "${_rb_ctx_incomplete_reasons}"\n'
)


def _rb_incomplete_setup(tmp_path: Path, files: dict[str, object], failing: tuple[str, ...] = ()) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	fake = bin_dir / "gh"
	fake.write_text(_FAKE_GH_RB_INCOMPLETE, encoding="utf-8")
	fake.chmod(0o755)
	data = tmp_path / "data"
	data.mkdir()
	for name, value in files.items():
		text = value if isinstance(value, str) else json.dumps(value)
		(data / name).write_text(text, encoding="utf-8")
	for name in failing:
		(data / f"{name}.fail").write_text("", encoding="utf-8")
	tmpdir = tmp_path / "tmpdir"
	tmpdir.mkdir()
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID"}
	}
	env.update(
		{
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"FAKE_GH_DIR": str(data),
			"TMPDIR": str(tmpdir),
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"GH_RATE_LIMIT_BREAKER_FILE": str(tmp_path / "breaker"),
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	return env


def _rb_incomplete_run(tmp_path: Path, env: dict[str, str], body: str, source_helpers: bool) -> subprocess.CompletedProcess[str]:
	script = "set -euo pipefail\n"
	if source_helpers:
		script += f"source '{GH_HELPERS}'\n"
	script += "sleep() { :; }\n" + body + "\n"
	return subprocess.run(
		["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=120,
	)


def _rb_incomplete_marker_block(begin: str, end: str) -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index(begin)
	stop = text.index(end, start) + len(end)
	return text[start:stop]


def _rb_incomplete_assembly_block() -> str:
	return _rb_incomplete_marker_block(
		"# Comment JSON reaches jq through --slurpfile, never argv",
		"unset _rb_ctx_rc _rb_ctx_empty_reason",
	)


def _rb_incomplete_gate_block() -> str:
	return _rb_incomplete_marker_block(GATE_BEGIN, GATE_END)


def _rb_incomplete_lines(stderr: str) -> list[str]:
	return [line for line in stderr.splitlines() if "RB_JUDGE_CONTEXT_INCOMPLETE" in line]


def _rb_incomplete_report(stdout: str) -> dict[str, str]:
	out: dict[str, str] = {}
	for line in stdout.splitlines():
		if "=" in line:
			key, _, value = line.partition("=")
			out[key] = value
	return out


def _run_helper_stub(tmp_path: Path, helper_json: str) -> subprocess.CompletedProcess[str]:
	env = _rb_incomplete_setup(tmp_path, {})
	(tmp_path / "helper.json").write_text(helper_json, encoding="utf-8")
	body = (
		_POLLER_PREFIX_RB_INCOMPLETE
		+ "gh_pr_with_all_comments() { cat helper.json; }\n"
		+ _rb_incomplete_assembly_block()
		+ _REPORT_RB_INCOMPLETE
	)
	return _rb_incomplete_run(tmp_path, env, body, source_helpers=False)


# ---------------------------------------------------------------------------
# Assembly block
# ---------------------------------------------------------------------------


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_helper_flag_logs_once(tmp_path: Path) -> None:
	helper = {
		"meta": {},
		"comments": [],
		"review_comments": [],
		"comments_incomplete": True,
		"incomplete_reasons": ["issue_comments_read_failed"],
	}
	result = _run_helper_stub(tmp_path, json.dumps(helper))
	assert result.returncode == 0, result.stderr
	report = _rb_incomplete_report(result.stdout)
	assert report["FLAG"] == "true"
	assert report["REASONS"] == "issue_comments_read_failed"
	assert _rb_incomplete_lines(result.stderr) == [
		"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=9 reasons=issue_comments_read_failed action=defer_merge source=poller"
	]
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result.stderr


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_complete_context_is_unchanged(tmp_path: Path) -> None:
	helper = json.dumps(
		{"meta": {"title": "t"}, "comments": [{"author": "u", "body": "hi", "created_at": "x"}], "review_comments": []},
		separators=(",", ":"),
	)
	result = _run_helper_stub(tmp_path, helper)
	assert result.returncode == 0, result.stderr
	report = _rb_incomplete_report(result.stdout)
	assert report["FLAG"] == "false"
	assert report["REASONS"] == ""
	assert _rb_incomplete_lines(result.stderr) == []
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result.stderr
	assert (tmp_path / "ctx.json").read_text(encoding="utf-8") == helper


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_flag_survives_assembly_failure(tmp_path: Path) -> None:
	env = _rb_incomplete_setup(tmp_path, {})
	helper = '{"meta":{},"comments":[],"review_comments":[],"comments_incomplete":true,"incomplete_reasons":["review_comments_read_failed"]}'
	(tmp_path / "helper.json").write_text(helper, encoding="utf-8")
	body = (
		_POLLER_PREFIX_RB_INCOMPLETE
		+ "gh_pr_with_all_comments() { cat helper.json; return 3; }\n"
		+ _rb_incomplete_assembly_block()
		+ _REPORT_RB_INCOMPLETE
	)
	result = _rb_incomplete_run(tmp_path, env, body, source_helpers=False)
	assert result.returncode == 0, result.stderr
	report = _rb_incomplete_report(result.stdout)
	assert report["FLAG"] == "true"
	assert "::warning::RB_JUDGE_CONTEXT_EMPTY pr=9 reason=assembly_failed" in result.stderr
	assert len(_rb_incomplete_lines(result.stderr)) == 1


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_legacy_read_failures_set_flag(tmp_path: Path) -> None:
	env = _rb_incomplete_setup(tmp_path, {}, failing=("comments", "review"))
	body = (
		"unset -f gh_pr_with_all_comments _gh_pr_with_all_comments_rest\n"
		+ _POLLER_PREFIX_RB_INCOMPLETE
		+ _rb_incomplete_assembly_block()
		+ _REPORT_RB_INCOMPLETE
	)
	result = _rb_incomplete_run(tmp_path, env, body, source_helpers=True)
	assert result.returncode == 0, result.stderr
	report = _rb_incomplete_report(result.stdout)
	assert report["FLAG"] == "true"
	assert _rb_incomplete_lines(result.stderr) == [
		"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=9 "
		"reasons=issue_comments_read_failed,review_comments_read_failed action=defer_merge source=poller"
	]
	assert "RB_JUDGE_CONTEXT_EMPTY" not in result.stderr
	ctx = json.loads((tmp_path / "ctx.json").read_text(encoding="utf-8"))
	assert ctx == {"meta": {}, "comments": [], "review_comments": []}


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_legacy_success_keeps_sorted_output(tmp_path: Path) -> None:
	comments = [
		{"user": {"login": "b"}, "body": "second", "created_at": "2026-10-18T00:02:00Z"},
		{"user": {"login": "a"}, "body": "first", "created_at": "2026-10-18T00:01:00Z"},
	]
	review = [
		{"user": {"login": "r"}, "path": "z.py", "line": 1, "body": "later"},
		{"user": {"login": "r"}, "path": "a.py", "line": 5, "body": "earlier"},
	]
	env = _rb_incomplete_setup(tmp_path, {"comments.json": comments, "review.json": review})
	body = (
		"unset -f gh_pr_with_all_comments _gh_pr_with_all_comments_rest\n"
		+ _POLLER_PREFIX_RB_INCOMPLETE
		+ _rb_incomplete_assembly_block()
		+ _REPORT_RB_INCOMPLETE
	)
	result = _rb_incomplete_run(tmp_path, env, body, source_helpers=True)
	assert result.returncode == 0, result.stderr
	report = _rb_incomplete_report(result.stdout)
	assert report["FLAG"] == "false"
	assert _rb_incomplete_lines(result.stderr) == []
	ctx = json.loads((tmp_path / "ctx.json").read_text(encoding="utf-8"))
	assert [c["body"] for c in ctx["comments"]] == ["first", "second"]
	assert ctx["comments"][0] == {"author": "a", "body": "first", "created_at": "2026-10-18T00:01:00Z"}
	assert [c["body"] for c in ctx["review_comments"]] == ["earlier", "later"]
	assert ctx["review_comments"][0] == {"author": "r", "path": "a.py", "line": 5, "body": "earlier"}


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_reasons_are_sanitized(tmp_path: Path) -> None:
	helper = {
		"meta": {},
		"comments": [],
		"review_comments": [],
		"comments_incomplete": True,
		"incomplete_reasons": ["a\n::error::FAKE_PREFIX x=1", "b c", 7],
	}
	result = _run_helper_stub(tmp_path, json.dumps(helper))
	assert result.returncode == 0, result.stderr
	lines = _rb_incomplete_lines(result.stderr)
	assert len(lines) == 1
	assert "FAKE_PREFIX" not in result.stderr
	reasons = re.search(r"reasons=(\S*) action=defer_merge source=poller$", lines[0])
	assert reasons is not None
	assert re.fullmatch(r"[a-z0-9_,]+", reasons.group(1))


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_empty_reasons_fall_back_to_unknown(tmp_path: Path) -> None:
	helper = {"meta": {}, "comments": [], "review_comments": [], "comments_incomplete": True}
	result = _run_helper_stub(tmp_path, json.dumps(helper))
	assert result.returncode == 0, result.stderr
	assert _rb_incomplete_lines(result.stderr) == [
		"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=9 reasons=unknown action=defer_merge source=poller"
	]


# ---------------------------------------------------------------------------
# Merge gate
# ---------------------------------------------------------------------------


def _run_gate(tmp_path: Path, incomplete: str, action: str, is_final: str) -> subprocess.CompletedProcess[str]:
	env = _rb_incomplete_setup(tmp_path, {})
	body = (
		"RB_PR=9\nrb_issue=42\n"
		f"_rb_ctx_comments_incomplete={incomplete}\nRB_ACTION={action}\nIS_FINAL={is_final}\n"
		"rb_cleanup_combined_workspace() { echo CLEANUP; }\n"
		"for _ in 1; do\n"
		+ _rb_incomplete_gate_block()
		+ "\necho REACHED_CASE\ndone\n"
	)
	return _rb_incomplete_run(tmp_path, env, body, source_helpers=False)


@pytest.mark.parametrize(
	("action", "is_final"),
	[("merge", "false"), ("merge_with_followup", "false"), ("fix", "true"), ("merge", "true")],
)
def test_poller_rb_incomplete_gate_defers_merge_outcomes(tmp_path: Path, action: str, is_final: str) -> None:
	result = _run_gate(tmp_path, "true", action, is_final)
	assert result.returncode == 0, result.stderr
	assert "REACHED_CASE" not in result.stdout
	assert "CLEANUP" in result.stdout
	assert (
		f"[review-blocked] Merge deferred for PR #9 (issue #42): comment context incomplete; judge action {action} withheld this tick."
		in result.stdout
	)


@pytest.mark.parametrize(
	("action", "is_final"),
	[("fix", "false"), ("close_and_reissue", "false"), ("close_and_reissue", "true"), ("unknown_action", "false")],
)
def test_poller_rb_incomplete_gate_passes_non_merge_outcomes(tmp_path: Path, action: str, is_final: str) -> None:
	result = _run_gate(tmp_path, "true", action, is_final)
	assert result.returncode == 0, result.stderr
	assert "REACHED_CASE" in result.stdout
	assert "CLEANUP" not in result.stdout
	assert "Merge deferred" not in result.stdout


@pytest.mark.parametrize(
	("action", "is_final"),
	[("merge", "false"), ("merge_with_followup", "false"), ("fix", "true"), ("fix", "false"), ("close_and_reissue", "true")],
)
def test_poller_rb_incomplete_gate_complete_context_is_unchanged(tmp_path: Path, action: str, is_final: str) -> None:
	result = _run_gate(tmp_path, "false", action, is_final)
	assert result.returncode == 0, result.stderr
	assert "REACHED_CASE" in result.stdout
	assert "CLEANUP" not in result.stdout
	assert "Merge deferred" not in result.stdout


# ---------------------------------------------------------------------------
# Static placement
# ---------------------------------------------------------------------------


def test_poller_rb_incomplete_gate_sits_before_judge_comment_and_case() -> None:
	text = POLLER.read_text(encoding="utf-8")
	action = text.index('RB_ACTION="$(echo "${RB_JUDGE_JSON}" | jq -r \'.action\')"')
	gate = text.index(GATE_BEGIN)
	gate_end = text.index(GATE_END)
	post = text.index("# Post judge assessment to PR", action)
	# The gate has its own `case "${RB_ACTION}" in`; the action dispatch is the next one after the gate.
	case = text.index('case "${RB_ACTION}" in', gate_end)
	assert action < gate < gate_end < post < case
	assert text.count(GATE_BEGIN) == 1


def test_poller_rb_incomplete_no_fix_merge_path_is_guarded() -> None:
	text = POLLER.read_text(encoding="utf-8")
	treat = text.index('echo "  Treating as merge decision."')
	elif_pos = text.rindex(INCOMPLETE_ELIF, 0, treat)
	between = text[elif_pos:treat]
	assert 'RB_SKIP_RETRY_INCREMENT="true"' in between
	assert "no-fix merge withheld this tick" in between
	assert between.count("\n                else\n") == 1
	assert "if [ \"${RB_TARGET_MERGED}\" = \"true\" ]; then" in text[elif_pos - 800:elif_pos]


def test_poller_rb_incomplete_prompt_note_is_in_orchestrator_context() -> None:
	text = POLLER.read_text(encoding="utf-8")
	context = text.index("=== ORCHESTRATOR CONTEXT ===")
	protected = text.index("Protected-path fixes: unavailable", context)
	note = text.index("Comment context: incomplete (reasons: ", context)
	final = text.index('if [ "${IS_FINAL}" = "true" ]; then', note)
	prompt_end = text.index('} > "${RB_JUDGE_PROMPT_FILE}"', context)
	assert protected < note < final < prompt_end


def test_poller_rb_incomplete_adds_no_github_api_call() -> None:
	block = _rb_incomplete_assembly_block()
	assert block.count("gh api") == 2
	assert block.count("gh_retry") == 2
	assert "_safe_gh_jq" not in block
	assert "gh api" not in _rb_incomplete_gate_block()
	assert "gh_retry" not in _rb_incomplete_gate_block()


def test_poller_rb_incomplete_log_line_shape_in_source() -> None:
	block = _rb_incomplete_assembly_block()
	assert (
		'"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=${RB_PR} reasons=${_rb_ctx_incomplete_reasons} action=defer_merge source=poller" >&2'
		in block
	)
	assert "tr -cd 'a-z0-9_,'" in block


# ---------------------------------------------------------------------------
# No-fix chain (runtime)
# ---------------------------------------------------------------------------


def _rb_incomplete_no_fix_chain() -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index('echo "  Judge produced no file changes."')
	stop = text.index('echo "  Treating as merge decision."', start)
	return text[start:stop]


def _run_no_fix_chain(tmp_path: Path, incomplete: str, merged: str) -> subprocess.CompletedProcess[str]:
	env = _rb_incomplete_setup(tmp_path, {})
	body = (
		"RB_PR=9\nrb_issue=42\nGITHUB_REPOSITORY=o/r\nRB_SKIP_RETRY_INCREMENT=false\n"
		f"_rb_ctx_comments_incomplete={incomplete}\nRB_TARGET_MERGED={merged}\n"
		"gh_retry() { echo \"GH $*\"; }\ntg_notify() { echo TG; }\n_gh_url() { echo url; }\n"
		+ _rb_incomplete_no_fix_chain()
		+ "echo TREAT\nfi\necho \"SKIP=${RB_SKIP_RETRY_INCREMENT}\"\n"
	)
	return _rb_incomplete_run(tmp_path, env, body, source_helpers=False)


@pytest.mark.parametrize("merged", ["true", "false"])
def test_poller_rb_incomplete_no_fix_chain_defers_on_incomplete(tmp_path: Path, merged: str) -> None:
	result = _run_no_fix_chain(tmp_path, "true", merged)
	assert result.returncode == 0, result.stderr
	assert "TREAT" not in result.stdout
	assert "GH " not in result.stdout
	assert "TG" not in result.stdout
	assert "comment context incomplete" in result.stdout
	assert "SKIP=true" in result.stdout


@pytest.mark.parametrize(("merged", "expect_treat"), [("true", False), ("false", True)])
def test_poller_rb_incomplete_no_fix_chain_complete_is_unchanged(tmp_path: Path, merged: str, expect_treat: bool) -> None:
	result = _run_no_fix_chain(tmp_path, "false", merged)
	assert result.returncode == 0, result.stderr
	assert ("TREAT" in result.stdout) is expect_treat
	assert "comment context incomplete" not in result.stdout
	assert "SKIP=false" in result.stdout
	if merged == "true":
		assert "--remove-label ai:review-blocked" in result.stdout
		assert "TG" in result.stdout


@_requires_jq_rb_incomplete
def test_poller_rb_incomplete_legacy_transform_failure_reason(tmp_path: Path) -> None:
	env = _rb_incomplete_setup(tmp_path, {"comments.json": "not json {", "review.json": []})
	body = (
		"unset -f gh_pr_with_all_comments _gh_pr_with_all_comments_rest\n"
		+ _POLLER_PREFIX_RB_INCOMPLETE
		+ _rb_incomplete_assembly_block()
		+ _REPORT_RB_INCOMPLETE
	)
	result = _rb_incomplete_run(tmp_path, env, body, source_helpers=True)
	assert result.returncode == 0, result.stderr
	assert _rb_incomplete_lines(result.stderr) == [
		"::warning::RB_JUDGE_CONTEXT_INCOMPLETE pr=9 reasons=issue_comments_parse_failed action=defer_merge source=poller"
	]
