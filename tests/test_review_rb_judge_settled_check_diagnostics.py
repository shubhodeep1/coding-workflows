#!/usr/bin/env python3
"""Contract tests for the review-blocked judge's settled-check diagnostics
(issue #10137, unblock operator step for #9502).

When the judge approves a merge but the check-runs gate refuses it with
``judge_skip_reason=blocking_check_runs`` while nothing on the head is still
running (``PR_CHECKS_LAST_PENDING=0``), ``scripts/review_rb_judge.sh``:

- writes ``judge_blocking_checks_settled=true`` and
  ``judge_blocking_check_names`` to GITHUB_OUTPUT and mirrors them to
  GITHUB_ENV for the review-blocked comment step;
- when every settled blocking run was cancelled or timed out, requests one
  ``rerun-failed-jobs`` per Actions run, at most once per head and run id
  (deduplicated by a trusted marker comment);
- is gated by ``RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED`` (default on).

``scripts/review_autofix_step_post_review_blocked_comment.sh`` then names the
failed checks instead of saying they are in flight.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
COMMENT_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_review_blocked_comment.sh"

HEAD = "c6e2879e3df133ec9405fae46e69d58471a506b6"
LEGACY_HEADING = "AI review/autofix — merge gated by in-flight check-runs"
SETTLED_HEADING = "AI review/autofix — merge gated by failed required check-runs"


def _require_tools() -> None:
	if shutil.which("jq") is None or shutil.which("bash") is None:
		raise unittest.SkipTest("jq/bash not available in test environment")


def _judge_functions() -> str:
	text = JUDGE.read_text(encoding="utf-8")
	start = text.index("\nrb_judge_settled_check_diagnostics_enabled()\n")
	end = text.index("\npost_review_blocked_assessment() {", start)
	return text[start:end]


def _marker(run_id: str, head: str = HEAD) -> str:
	return f"<!-- rb-judge-settled-rerun:v1 head={head} run={run_id} -->"


def _run_judge(
	*,
	pending: str = "0",
	names: str = "",
	run_ids: str = "",
	comments: list[dict] | None = None,
	env: dict[str, str] | None = None,
	context_json: str | None = None,
	rerun_fail: bool = False,
	with_login_helper: bool = True,
) -> dict:
	_require_tools()
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		output = root / "output"
		genv = root / "env"
		calls = root / "calls.log"
		posts = root / "posts.log"
		for path in (output, genv, calls, posts):
			path.write_text("", encoding="utf-8")
		login_helper = ""
		if with_login_helper:
			login_helper = '_tg_tracking_author_login() { _TG_TRACKING_AUTHOR_CURRENT="PatBot"; }\n'
		script = (
			"set -euo pipefail\n"
			'gh_retry() { printf "%s|%s\\n" "${GH_RETRY_IDEMPOTENT:-}" "$*" >> "${CALLS_LOG}"; '
			'[ "${RERUN_FAIL}" != "1" ]; }\n'
			'rb_judge_post_issue_comment() { printf "%s\\n---END---\\n" "$*" >> "${POSTS_LOG}"; }\n'
			+ login_helper
			+ _judge_functions()
			+ "\nrb_judge_settled_check_diagnostics\n"
		)
		run_env = {
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"PYTHONDONTWRITEBYTECODE": "1",
			"GITHUB_OUTPUT": str(output),
			"GITHUB_ENV": str(genv),
			"CALLS_LOG": str(calls),
			"POSTS_LOG": str(posts),
			"RERUN_FAIL": "1" if rerun_fail else "0",
			"GITHUB_RUN_ID": "999",
			"REPOSITORY": "o/r",
			"PR_NUMBER": "9502",
			"RB_JUDGED_HEAD_SHA": HEAD,
			"RB_JUDGE_COMMENTS_INCOMPLETE": "false",
			"PR_CHECKS_LAST_PENDING": pending,
			"PR_CHECKS_LAST_BLOCKING_NAMES": names,
			"PR_CHECKS_LAST_BLOCKING_RUN_IDS": run_ids,
			"PR_CONTEXT_JSON": context_json if context_json is not None else json.dumps(
				{"meta": {}, "comments": comments or [], "review_comments": []}
			),
		}
		run_env.update(env or {})
		res = subprocess.run(["bash", "-c", script], env=run_env, capture_output=True, text=True, timeout=60)
		return {
			"rc": res.returncode,
			"stdout": res.stdout,
			"stderr": res.stderr,
			"output": output.read_text(encoding="utf-8"),
			"env": genv.read_text(encoding="utf-8"),
			"calls": [ln for ln in calls.read_text(encoding="utf-8").splitlines() if ln],
			"posts": [p for p in posts.read_text(encoding="utf-8").split("\n---END---\n") if p.strip()],
		}


def _rerun_calls(result: dict) -> list[str]:
	return [c for c in result["calls"] if "rerun-failed-jobs" in c]


# ---------------------------------------------------------------------------
# Pending, settled failure and cancelled cases.
# ---------------------------------------------------------------------------


def test_pending_writes_nothing_and_reruns_nothing() -> None:
	res = _run_judge(pending="1", names="CI:in_progress", run_ids="")
	assert res["rc"] == 0, res["stderr"]
	assert res["output"] == "" and res["env"] == "", res
	assert res["calls"] == [] and res["posts"] == [], res
	assert "RB_JUDGE_SETTLED_CHECKS" not in res["stdout"]


def test_settled_failure_writes_outputs_and_does_not_rerun() -> None:
	res = _run_judge(names="lint:failure\nreview / gate:cancelled", run_ids="111:failure\n222:cancelled")
	assert res["rc"] == 0, res["stderr"]
	assert "judge_blocking_checks_settled=true\n" in res["output"], res["output"]
	assert "judge_blocking_check_names=lint:failure, review / gate:cancelled\n" in res["output"], res["output"]
	assert "RB_JUDGE_BLOCKING_CHECKS_SETTLED=true\n" in res["env"], res["env"]
	assert "RB_JUDGE_BLOCKING_CHECK_NAMES=lint:failure, review / gate:cancelled\n" in res["env"], res["env"]
	assert _rerun_calls(res) == [] and res["posts"] == [], res
	assert "RB_JUDGE_SETTLED_CHECKS pr=9502 head=c6e2879 settled=true names=2 run=- action=diagnosed reason=settled_failure" in res["stdout"]


def test_cancelled_requests_one_rerun_and_posts_marker() -> None:
	res = _run_judge(names="lint:cancelled\nstatic-checks:timed_out", run_ids="4711:cancelled\n4711:timed_out")
	assert res["rc"] == 0, res["stderr"]
	assert "judge_blocking_checks_settled=true\n" in res["output"]
	reruns = _rerun_calls(res)
	assert reruns == ["true|gh api -X POST repos/o/r/actions/runs/4711/rerun-failed-jobs"], res["calls"]
	assert len(res["posts"]) == 1, res["posts"]
	post = res["posts"][0]
	assert post.startswith("-f body="), post
	assert post.rstrip().endswith("\n" + _marker("4711")), post
	assert "action=rerun_requested reason=cancelled_or_timed_out" in res["stdout"], res["stdout"]


def test_rerun_failure_still_posts_marker() -> None:
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", rerun_fail=True)
	assert len(_rerun_calls(res)) == 1
	assert len(res["posts"]) == 1 and res["posts"][0].rstrip().endswith(_marker("4711"))
	assert "action=rerun_failed reason=post_failed" in res["stdout"], res["stdout"]


def test_trusted_marker_suppresses_rerun() -> None:
	for author in ("github-actions[bot]", "github-actions", "patbot"):
		comments = [{"author": author, "body": f"re-ran\n\n{_marker('4711')}\n  "}]
		res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", comments=comments)
		assert _rerun_calls(res) == [] and res["posts"] == [], (author, res)
		assert "reason=already_requested" in res["stdout"], res["stdout"]


def test_marker_for_other_head_or_run_does_not_suppress() -> None:
	comments = [
		{"author": "github-actions[bot]", "body": _marker("4711", head="a" * 40)},
		{"author": "github-actions[bot]", "body": _marker("4712")},
	]
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", comments=comments)
	assert len(_rerun_calls(res)) == 1, res["calls"]


def test_untrusted_marker_does_not_suppress_rerun() -> None:
	comments = [
		{"author": "mallory", "body": _marker("4711")},
		{"author": "github-actions[bot]", "body": f"quoting {_marker('4711')} mid-text, not at the end"},
	]
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", comments=comments)
	assert len(_rerun_calls(res)) == 1, res["calls"]
	# Without the login helper only github-actions is trusted.
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled",
		comments=[{"author": "patbot", "body": _marker("4711")}], with_login_helper=False)
	assert len(_rerun_calls(res)) == 1, res["calls"]


def test_self_run_and_non_actions_check_are_skipped() -> None:
	res = _run_judge(names="lint:cancelled", run_ids="999:cancelled")
	assert _rerun_calls(res) == [] and res["posts"] == [], res
	assert "run=999 action=rerun_skipped reason=self_run" in res["stdout"], res["stdout"]
	# A mixed set skips only the non-Actions entry; the Actions run still gets
	# its one rerun.
	res = _run_judge(names="external:timed_out\nlint:cancelled", run_ids="none:timed_out\n4711:cancelled")
	assert _rerun_calls(res) == ["true|" + "gh api -X POST repos/o/r/actions/runs/4711/rerun-failed-jobs"], res["calls"]
	assert len(res["posts"]) == 1 and res["posts"][0].rstrip().endswith(_marker("4711")), res["posts"]
	assert res["stdout"].count("run=- action=rerun_skipped reason=non_actions_check") == 1, res["stdout"]
	assert "judge_blocking_checks_settled=true" in res["output"]
	res = _run_judge(names="external:timed_out", run_ids="none:timed_out")
	assert _rerun_calls(res) == [] and res["posts"] == [], res
	assert res["stdout"].count("reason=non_actions_check") == 1, res["stdout"]


def test_incomplete_or_unreadable_comments_skip_rerun() -> None:
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", env={"RB_JUDGE_COMMENTS_INCOMPLETE": "true"})
	assert _rerun_calls(res) == [], res["calls"]
	assert "reason=comments_unreadable" in res["stdout"], res["stdout"]
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", context_json="{}")
	assert _rerun_calls(res) == [], res["calls"]
	assert "reason=comments_unreadable" in res["stdout"], res["stdout"]


def test_unresolved_head_skips_rerun() -> None:
	res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled", env={"RB_JUDGED_HEAD_SHA": "abc"})
	assert _rerun_calls(res) == [], res["calls"]
	assert "reason=unresolved_head" in res["stdout"], res["stdout"]


def test_rerun_capped_at_three_runs() -> None:
	ids = "\n".join(f"{n}:cancelled" for n in (11, 12, 13, 14))
	res = _run_judge(names="a:cancelled\nb:cancelled\nc:cancelled\nd:cancelled", run_ids=ids)
	assert len(_rerun_calls(res)) == 3, res["calls"]
	assert "run=14 action=rerun_skipped reason=rerun_capped" in res["stdout"], res["stdout"]


def test_already_requested_runs_do_not_use_the_cap() -> None:
	"""A later pass: runs 11-13 already carry trusted markers, so they are
	skipped without a POST and must not fill the cap; run 14 gets its one
	rerun instead of being capped on every pass."""
	ids = "\n".join(f"{n}:cancelled" for n in (11, 12, 13, 14))
	comments = [{"author": "github-actions[bot]", "body": _marker(str(n))} for n in (11, 12, 13)]
	res = _run_judge(names="a:cancelled\nb:cancelled\nc:cancelled\nd:cancelled", run_ids=ids, comments=comments)
	reruns = _rerun_calls(res)
	assert len(reruns) == 1 and reruns[0].endswith("/actions/runs/14/rerun-failed-jobs"), res["calls"]
	assert res["stdout"].count("reason=already_requested") == 3, res["stdout"]
	assert "reason=rerun_capped" not in res["stdout"], res["stdout"]
	assert len(res["posts"]) == 1 and res["posts"][0].rstrip().endswith(_marker("14")), res["posts"]


def test_names_past_cap_are_marked_as_more() -> None:
	names = "\n".join(f"c{n}:failure" for n in range(1, 24))
	res = _run_judge(names=names, run_ids="1:failure")
	line = [ln for ln in res["output"].splitlines() if ln.startswith("judge_blocking_check_names=")][0]
	assert line.endswith(", c20:failure, (3 more)"), line
	assert "c21:failure" not in line, line
	assert "names=23 " in res["stdout"], res["stdout"]


def test_no_names_writes_nothing() -> None:
	res = _run_judge(names="", run_ids="")
	assert res["output"] == "" and res["env"] == "" and res["calls"] == [], res
	assert "reason=names_unavailable" in res["stdout"]


# ---------------------------------------------------------------------------
# Off switch.
# ---------------------------------------------------------------------------


def test_flag_values() -> None:
	for value in ("false", "FALSE", " False "):
		res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled",
			env={"RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED": value})
		assert res["output"] == "" and res["calls"] == [] and "::warning::" not in res["stdout"], (value, res)
	for value in ("", "true", "TRUE", " true "):
		res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled",
			env={"RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED": value})
		assert "judge_blocking_checks_settled=true" in res["output"] and len(_rerun_calls(res)) == 1, (value, res)
	for value in ("yes", "1", "on"):
		res = _run_judge(names="lint:cancelled", run_ids="4711:cancelled",
			env={"RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED": value})
		assert res["output"] == "" and res["calls"] == [], (value, res)
		assert res["stdout"].count("::warning::RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED") == 1, (value, res["stdout"])


# ---------------------------------------------------------------------------
# Injection: names are cleaned before reaching GITHUB_OUTPUT / GITHUB_ENV.
# ---------------------------------------------------------------------------


def test_names_are_cleaned_before_env_and_output() -> None:
	res = _run_judge(names="evil\rLD_PRELOAD=/tmp/x `@team`, y:failure", run_ids="1:failure")
	env_lines = [ln for ln in res["env"].splitlines() if ln]
	assert [ln.split("=", 1)[0] for ln in env_lines] == [
		"RB_JUDGE_BLOCKING_CHECKS_SETTLED", "RB_JUDGE_BLOCKING_CHECK_NAMES"], env_lines
	value = env_lines[1].split("=", 1)[1]
	assert re.fullmatch(r"[A-Za-z0-9 _./():\-]*(, [A-Za-z0-9 _./():\-]*)*", value), value
	assert "=" not in value and "`" not in value and "@" not in value, value
	out_lines = [ln for ln in res["output"].splitlines() if ln]
	assert len(out_lines) == 2, out_lines


# ---------------------------------------------------------------------------
# Static checks on the judge's call site.
# ---------------------------------------------------------------------------


def test_call_site_keeps_skip_reason_and_calls_helper_after_it() -> None:
	text = JUDGE.read_text(encoding="utf-8")
	anchor = '              echo "judge_skip_reason=blocking_check_runs" >> "$GITHUB_OUTPUT"\n'
	assert text.count(anchor) == 1
	after = text[text.index(anchor) + len(anchor):]
	assert after.startswith("              rb_judge_settled_check_diagnostics || true\n"), after[:200]
	assert 'blocking) _checks_reason="blocking_check_runs" ;;' in text


# ---------------------------------------------------------------------------
# Comment step script.
# ---------------------------------------------------------------------------


GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${GH_LOG}"
for arg in "$@"; do
  case "${arg}" in
    body=*) printf '%s' "${arg#body=}" > "${BODY_FILE}" ;;
  esac
done
exit 0
"""


def _run_comment_script(env: dict[str, str]) -> str:
	_require_tools()
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		support = root / "support"
		bindir = root / "bin"
		support.mkdir()
		bindir.mkdir()
		(support / "gh_helpers.sh").write_text("", encoding="utf-8")
		gh = bindir / "gh"
		gh.write_text(GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		body_file = root / "body.txt"
		run_env = {
			"PATH": f"{bindir}:/usr/bin:/bin",
			"HOME": tmp,
			"SUPPORT_SCRIPTS_DIR": str(support),
			"GH_LOG": str(root / "gh.log"),
			"BODY_FILE": str(body_file),
			"GITHUB_OUTPUT": str(root / "output"),
			"AUTOFIX_STEP_REPOSITORY": "o/r",
			"AUTOFIX_STEP_SERVER_URL": "https://github.com",
			"AUTOFIX_STEP_RUN_ID": "99",
			"PR_NUMBER": "9502",
			"MAX_AUTOFIX_ITERATIONS": "5",
			"AUTOFIX_ITERATION": "5",
			"AUTOFIX_FAILURE_HEAD_SHA": HEAD,
			"JUDGE_SKIP_REASON": "blocking_check_runs",
			"RB_JUDGE_STATUS": "success",
		}
		run_env.update(env)
		res = subprocess.run(
			["bash", "-c", 'gh() { bash "${GH_STUB_PATH}" "$@"; }; source "${COMMENT_SCRIPT_PATH}"'],
			env={**run_env, "GH_STUB_PATH": str(gh), "COMMENT_SCRIPT_PATH": str(COMMENT_SCRIPT)},
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert res.returncode == 0, res.stderr
		return body_file.read_text(encoding="utf-8")


LEGACY_DETAIL = (
	"The review-blocked judge approved this PR but other CI check-runs on the head commit had not yet "
	"completed when the merge was attempted."
)


def test_comment_names_settled_checks() -> None:
	body = _run_comment_script({
		"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "true",
		"RB_JUDGE_BLOCKING_CHECK_NAMES": "lint:failure, review / gate:cancelled",
	})
	assert body.startswith(f"**{SETTLED_HEADING}**"), body
	assert "\n- lint:failure\n- review / gate:cancelled\n" in body, body
	assert LEGACY_HEADING not in body and LEGACY_DETAIL not in body


def test_comment_keeps_judge_more_entry() -> None:
	names = ", ".join(f"c{n}:failure" for n in range(1, 21)) + ", (3 more)"
	body = _run_comment_script({"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "true", "RB_JUDGE_BLOCKING_CHECK_NAMES": names})
	assert "\n- c20:failure\n- (3 more)\n" in body, body


def test_comment_cleans_names_again() -> None:
	body = _run_comment_script({
		"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "true",
		"RB_JUDGE_BLOCKING_CHECK_NAMES": "`@team` ping:failure",
	})
	assert "\n- __team_ ping:failure\n" in body, body


def test_comment_keeps_legacy_text_when_not_settled_or_off() -> None:
	for env in (
		{},
		{"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "true", "RB_JUDGE_BLOCKING_CHECK_NAMES": ""},
		{"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "false", "RB_JUDGE_BLOCKING_CHECK_NAMES": "lint:failure"},
		{"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "true", "RB_JUDGE_BLOCKING_CHECK_NAMES": "lint:failure",
			"RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED": "false"},
		{"RB_JUDGE_BLOCKING_CHECKS_SETTLED": "true", "RB_JUDGE_BLOCKING_CHECK_NAMES": "lint:failure",
			"RB_JUDGE_SETTLED_CHECK_DIAGNOSTICS_ENABLED": "yes"},
	):
		body = _run_comment_script(env)
		assert body.startswith(f"**{LEGACY_HEADING}**\n\n{LEGACY_DETAIL}"), (env, body)
		assert SETTLED_HEADING not in body, env


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = skipped = failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
			passed += 1
		except unittest.SkipTest as exc:
			print(f"  SKIP  {func.__name__}: {exc}")
			skipped += 1
		except Exception as exc:  # noqa: BLE001
			print(f"  FAIL  {func.__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
