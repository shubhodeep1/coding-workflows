#!/usr/bin/env python3
"""Tests for scripts/plan_split_dispatch.sh (Q151/Q152/Q164: plan too large for one grant)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "plan_split_dispatch.sh"
PLAN = "Implementation Plan\n\n### 1. Files likely to change\n" + "".join(f"- `scripts/f{i}.sh`\n" for i in range(51))

# A fake gh: answers reads from FAKE_* variables and logs every call.
FAKE_GH = r'''#!/usr/bin/env bash
printf '%s\0' "$@" >> "${GH_LOG}"; printf '\n' >> "${GH_LOG}"
case "$1 $2" in
  "api user") echo "${FAKE_LOGIN}"; exit 0 ;;
  "workflow run")
    [ -z "${FAKE_DISPATCH_ERROR}" ] || { echo "${FAKE_DISPATCH_ERROR}" >&2; exit 1; }
    exit 0 ;;
  "issue edit") [ "${FAKE_EDIT_FAIL:-}" != true ] || exit 1; exit 0 ;;
esac
if [ "$1" = api ] && [ "$2" = --paginate ]; then
  # FAKE_COMMENTS_AFTER answers the re-read after the split marker is posted.
  if [ -n "${FAKE_COMMENTS_AFTER:-}" ] && [ -f "${GH_LOG}.listed" ]; then printf '%s\n' "${FAKE_COMMENTS_AFTER}"; exit 0; fi
  : > "${GH_LOG}.listed"; printf '%s\n' "${FAKE_COMMENTS}"; exit 0
fi
if [ "$1" = api ]; then
  case "$2" in
    */actions/workflows/internal-orchestrate.yml) [ -n "${FAKE_INTERNAL_STATE}" ] || exit 1; echo "${FAKE_INTERNAL_STATE}"; exit 0 ;;
    */actions/workflows/ai-orchestrate.yml) [ -n "${FAKE_CONSUMER_STATE}" ] || exit 1; echo "${FAKE_CONSUMER_STATE}"; exit 0 ;;
    */comments) [ "${FAKE_COMMENT_FAIL:-}" != true ] || exit 1; [ -z "${FAKE_MARKER_ID:-}" ] || echo "${FAKE_MARKER_ID}"; exit 0 ;;
    repos/owner/repo) echo main; exit 0 ;;
  esac
fi
exit 0
'''


def _issue(**overrides) -> dict:
	issue = {"number": 123, "title": "Retire the old poller", "state": "open", "body": "Do it.",
		"labels": [{"name": "ai:awaiting-approval"}]}
	issue.update(overrides)
	return issue


def _run(issue: dict | None = None, *, plan: str = PLAN, internal: str = "active", consumer: str = "",
	comments: list | None = None, dispatch_error: str = "", extra_env: dict | None = None) -> tuple[int, str, list[list[str]]]:
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		bindir = tmp / "bin"
		bindir.mkdir()
		(bindir / "gh").write_text(FAKE_GH, encoding="utf-8")
		(bindir / "gh").chmod(0o755)
		issue_file = tmp / "issue.json"
		issue_file.write_text(json.dumps(issue or _issue()), encoding="utf-8")
		plan_file = tmp / "plan.txt"
		plan_file.write_text(plan, encoding="utf-8")
		log = tmp / "gh.log"
		env = {k: v for k, v in os.environ.items() if not k.startswith(("PLAN_SPLIT_", "GITHUB_"))}
		env.update({
			"PATH": f"{bindir}:{env['PATH']}", "GH_LOG": str(log), "FAKE_LOGIN": "pipeline-bot",
			"FAKE_COMMENTS": "\n".join(json.dumps(c) for c in (comments or [])),
			"FAKE_INTERNAL_STATE": internal, "FAKE_CONSUMER_STATE": consumer, "FAKE_DISPATCH_ERROR": dispatch_error,
			"REPO": "owner/repo", "ISSUE_NUMBER": "123", "ISSUE_JSON_FILE": str(issue_file),
			"PLAN_FILE": str(plan_file), "PLAN_PATH_COUNT": "51", "RUN_URL": "https://github.com/owner/repo/actions/runs/9",
		})
		env.update(extra_env or {})
		# The script sources scripts/*.sh relative to its cwd; none exist here,
		# so its fallbacks (plain gh, no Telegram) are exercised.
		result = subprocess.run(["bash", str(SCRIPT)], cwd=tmp, env=env, text=True, capture_output=True)
		raw = log.read_text(encoding="utf-8") if log.exists() else ""
		calls = [line.rstrip("\0").split("\0") for line in raw.split("\0\n") if line.strip("\0\n")]
		return result.returncode, result.stdout + result.stderr, calls


def _dispatches(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:2] == ["workflow", "run"]]


def _field(call: list[str], name: str) -> str:
	values = [call[i + 1] for i, arg in enumerate(call[:-1]) if arg == "-f" and call[i + 1].startswith(name + "=")]
	assert len(values) == 1, (name, call)
	return values[0].split("=", 1)[1]


def test_library_repo_dispatches_a_claude_project_and_marks_the_issue() -> None:
	code, out, calls = _run()
	assert code == 0, out
	assert "PLAN_SPLIT issue=123 outcome=dispatched wrapper=internal-orchestrate.yml paths=51" in out
	[dispatch] = _dispatches(calls)
	assert dispatch[2] == "internal-orchestrate.yml" and dispatch[dispatch.index("--ref") + 1] == "main"
	assert _field(dispatch, "engine") == "claude"
	description = _field(dispatch, "project_description")
	assert description.splitlines()[0] == "Retire the old poller (split from #123)"
	assert "switches a behaviour on" in description and "at most 50 entries in files_touched" in description
	assert description.endswith(PLAN.rstrip("\n"))
	assert _field(dispatch, "tracking_comment").startswith("<!-- ai:plan-split-source:v1 issue=123 -->\n")
	posts = [c for c in calls if c[0] == "api" and c[1] == "repos/owner/repo/issues/123/comments"]
	assert len(posts) == 1 and "<!-- ai:plan-split:v1 issue=123 wrapper=internal-orchestrate.yml -->" in posts[0][-1]
	[edit] = [c for c in calls if c[:2] == ["issue", "edit"]]
	assert edit[edit.index("--add-label") + 1] == "ai:split-to-project"
	assert edit[edit.index("--remove-label") + 1] == "ai:awaiting-approval"


def test_consumer_repo_uses_ai_orchestrate() -> None:
	code, out, calls = _run(internal="", consumer="active")
	assert code == 0, out
	assert _dispatches(calls)[0][2] == "ai-orchestrate.yml"


def test_holds_without_dispatch() -> None:
	cases = [
		({"internal": "", "consumer": "disabled_manually"}, "orchestrator_wrapper_inactive"),
		({"internal": "", "consumer": ""}, "orchestrator_wrapper_missing"),
		({"issue": _issue(labels=[{"name": "ai:orchestrator-managed"}])}, "orchestrator_managed"),
		({"issue": _issue(body="Managed by: AI Orchestrator\n")}, "orchestrator_managed"),
		({"issue": _issue(labels=[{"name": "ai:split-to-project"}])}, "already_split"),
		({"comments": [{"login": "pipeline-bot", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=x -->\nold"}]}, "already_split"),
		({"plan": PLAN + "x" * 60000}, "description_too_large"),
		({"extra_env": {"PLAN_SPLIT_TO_PROJECT_ENABLED": "false"}}, "disabled"),
	]
	for kwargs, reason in cases:
		code, out, calls = _run(**kwargs)
		assert code == 3, (reason, out)
		assert f"outcome=skipped reason={reason}" in out, (reason, out)
		assert _dispatches(calls) == [] and not any(c[:2] == ["issue", "edit"] for c in calls), reason


def test_a_marker_from_someone_else_does_not_block_the_split() -> None:
	code, out, _ = _run(comments=[{"login": "stranger", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=x -->"}])
	assert code == 0, out


def test_a_released_split_may_split_again() -> None:
	split = {"login": "pipeline-bot", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=x -->"}
	released = {"login": "pipeline-bot", "body": "<!-- ai:plan-split-released:v1 issue=123 -->\nfailed"}
	code, out, _ = _run(comments=[split, released])
	assert code == 0, out
	# A newer split after the release blocks again; a stranger cannot release.
	code, out, _ = _run(comments=[split, released, split])
	assert code == 3 and "reason=already_split" in out
	code, out, _ = _run(comments=[split, {**released, "login": "stranger"}])
	assert code == 3 and "reason=already_split" in out


def _comment_posts(calls: list[list[str]]) -> list[str]:
	return [c[-1].split("=", 1)[1] for c in calls if c[0] == "api" and c[1] == "repos/owner/repo/issues/123/comments"]


def test_the_issue_is_marked_before_the_dispatch() -> None:
	# The orchestrator run links (or releases) only an issue that already
	# carries the label, so the label and marker land before the dispatch.
	_, _, calls = _run()
	kinds = [("edit" if c[:2] == ["issue", "edit"] else "comment" if c[:2] == ["api", "repos/owner/repo/issues/123/comments"]
		else "dispatch" if c[:2] == ["workflow", "run"] else None) for c in calls]
	kinds = [k for k in kinds if k]
	assert kinds == ["edit", "comment", "dispatch"], kinds


def test_rejected_dispatch_releases_the_split_and_holds() -> None:
	for error, reason in (
		("could not create workflow dispatch event: HTTP 422: Unexpected inputs provided: [\"tracking_comment\"]",
			"wrapper_without_tracking_comment"),
		("could not create workflow dispatch event: HTTP 404: Not Found", "dispatch_failed"),
	):
		code, out, calls = _run(dispatch_error=error)
		assert code == 3 and f"reason={reason}" in out, out
		assert len(_dispatches(calls)) == 1  # never retried: a retry could start a second project
		edits = [c for c in calls if c[:2] == ["issue", "edit"]]
		assert len(edits) == 2 and edits[1][edits[1].index("--remove-label") + 1] == "ai:split-to-project"
		assert edits[1][edits[1].index("--add-label") + 1] == "ai:awaiting-approval"
		posts = _comment_posts(calls)
		assert len(posts) == 2 and posts[1].startswith("<!-- ai:plan-split-released:v1 issue=123 -->")
		# The release marker comes before the label removal: a label removed
		# without a release marker would leave the split blocked for good.
		assert calls.index(edits[1]) > next(i for i, c in enumerate(calls) if c[-1] == "body=" + posts[1])


def test_a_concurrent_split_keeps_only_the_earliest_marker() -> None:
	# Two plan runs race past the dedupe check and both post a marker; only
	# the run whose marker comes first since the last release dispatches.
	other = {"id": 400, "login": "pipeline-bot", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=x -->"}
	ours = {"id": 500, "login": "pipeline-bot", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=y -->"}
	code, out, calls = _run(extra_env={"FAKE_MARKER_ID": "500",
		"FAKE_COMMENTS_AFTER": "\n".join(json.dumps(c) for c in (other, ours))})
	assert code == 0 and "outcome=deduplicated" in out and "first_marker=400" in out, out
	assert _dispatches(calls) == []
	assert any(c[:3] == ["api", "-X", "DELETE"] and c[3] == "repos/owner/repo/issues/comments/500" for c in calls)
	# The earliest marker is ours (an older split was released): dispatch.
	released = {"id": 300, "login": "pipeline-bot", "body": "<!-- ai:plan-split-released:v1 issue=123 -->"}
	code, out, calls = _run(extra_env={"FAKE_MARKER_ID": "500",
		"FAKE_COMMENTS_AFTER": "\n".join(json.dumps(c) for c in ({**other, "id": 200}, released, ours))})
	assert code == 0 and "outcome=dispatched" in out and len(_dispatches(calls)) == 1, out


def test_unconfirmed_dispatch_keeps_the_split() -> None:
	# A failure without an HTTP 4xx status may have started a project: the
	# label stays (no /approved, no second split) and a person is asked.
	code, out, calls = _run(dispatch_error="Post https://api.github.com/...: net/http: timeout")
	assert code == 0 and "outcome=dispatch_unconfirmed" in out, out
	assert len(_dispatches(calls)) == 1
	assert len([c for c in calls if c[:2] == ["issue", "edit"]]) == 1
	posts = _comment_posts(calls)
	assert len(posts) == 2 and posts[1].startswith("<!-- ai:plan-split-released:v1 issue=123 -->")


def test_failed_marking_holds_without_dispatch() -> None:
	code, out, calls = _run(extra_env={"FAKE_EDIT_FAIL": "true"})
	assert code == 3 and "reason=label_failed" in out and _dispatches(calls) == [], out
	code, out, calls = _run(extra_env={"FAKE_COMMENT_FAIL": "true", "PLAN_SPLIT_RETRY_SLEEP_SECS": "0"})
	assert code == 3 and "reason=marker_failed" in out and _dispatches(calls) == [], out
	# The release marker could not be posted either (3 attempts), so the
	# label stays on: removing it would leave a split marker with no release
	# marker after it, blocking every later /reclarify.
	edits = [c for c in calls if c[:2] == ["issue", "edit"]]
	assert len(edits) == 1 and "stays on" in out
	release_posts = [c for c in calls if c[:2] == ["api", "repos/owner/repo/issues/123/comments"]
		and c[-1].startswith("body=<!-- ai:plan-split-released:v1 issue=123 -->")]
	assert len(release_posts) == 3


def test_failed_marker_never_releases_a_concurrent_split() -> None:
	# Another plan run posted its split marker and dispatched; this run's
	# marker POST failed. Releasing would clear that run's label, so it exits
	# 0 without a release marker or label removal.
	other = {"id": 400, "login": "pipeline-bot", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=x -->"}
	code, out, calls = _run(extra_env={"FAKE_COMMENT_FAIL": "true", "PLAN_SPLIT_RETRY_SLEEP_SECS": "0",
		"FAKE_COMMENTS_AFTER": json.dumps(other)})
	assert code == 0 and "outcome=deduplicated reason=concurrent_split_marker_failed" in out, out
	assert _dispatches(calls) == [] and len([c for c in calls if c[:2] == ["issue", "edit"]]) == 1
	assert not any(c[-1].startswith("body=<!-- ai:plan-split-released:v1") for c in calls)


def test_failed_marker_post_that_landed_is_released_not_deduplicated() -> None:
	# The marker POST failed client-side but GitHub created the comment: the
	# re-read finds this run's own marker (it carries this run's RUN_URL).
	# It is not another run's reservation, so the split is released and the
	# plan held instead of being left stuck under ai:split-to-project.
	own = {"id": 500, "login": "pipeline-bot", "body": "<!-- ai:plan-split:v1 issue=123 wrapper=internal-orchestrate.yml -->\n"
		"(`internal-orchestrate.yml`, https://github.com/owner/repo/actions/runs/9). The project splits it."}
	code, out, calls = _run(extra_env={"FAKE_COMMENT_FAIL": "true", "PLAN_SPLIT_RETRY_SLEEP_SECS": "0",
		"FAKE_COMMENTS_AFTER": json.dumps(own)})
	assert code == 3 and "reason=marker_failed" in out and "deduplicated" not in out, out
	assert _dispatches(calls) == []
	assert any(c[-1].startswith("body=<!-- ai:plan-split-released:v1 issue=123 -->") for c in calls)
	# Another run whose URL merely starts with this one (runs/90) still counts.
	other = {**own, "body": own["body"].replace("runs/9)", "runs/90)")}
	code, out, _ = _run(extra_env={"FAKE_COMMENT_FAIL": "true", "PLAN_SPLIT_RETRY_SLEEP_SECS": "0",
		"FAKE_COMMENTS_AFTER": json.dumps(other)})
	assert code == 0 and "reason=concurrent_split_marker_failed" in out, out


# --- poller: close the source issue when the project completes (Q152) -------

POLLER = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
_CLOSE_FN = re.search(r"^close_plan_split_source_if_needed\(\) \{\n.*?^\}\n", POLLER, re.S | re.M).group(0)
_MARKER = "<!-- ai:plan-split-source:v1 issue=123 -->\n## Split from #123\n"
_POLL_HARNESS = r'''
set -uo pipefail
gh_retry() { "$@"; }
_safe_gh_jq() { gh api "$@"; }
post_state_comment() { echo POST_STATE >> "${GH_LOG}"; }
gh() {
  printf '%s\0' "$@" >> "${GH_LOG}"; printf '\n' >> "${GH_LOG}"
  if [ "$1 $2" = "api repos/owner/repo/issues/123" ]; then
    [ -n "${FAKE_SOURCE}" ] || return 1
    printf '%s' "${FAKE_SOURCE}"
  fi
  return 0
}
'''


def _close(status: str, comments: list, source: dict | None, state: dict | None = None) -> tuple[str, list[list[str]], dict]:
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		state_file = tmp / "state.json"
		state_file.write_text(json.dumps(state or {"status": status}), encoding="utf-8")
		log = tmp / "gh.log"
		env = {**os.environ, "GH_LOG": str(log), "STATE_FILE": str(state_file), "TRACKING_NUM": "900",
			"GITHUB_REPOSITORY": "owner/repo", "COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS": "OWNER,MEMBER,COLLABORATOR",
			"FAKE_SOURCE": json.dumps(source) if source is not None else "", "COMMENTS_JSON": json.dumps(comments),
			"STATUS": status}
		script = _POLL_HARNESS + _CLOSE_FN + 'close_plan_split_source_if_needed "${STATUS}" "${COMMENTS_JSON}"\n'
		result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, check=True)
		raw = log.read_text(encoding="utf-8") if log.exists() else ""
		calls = [line.rstrip("\0").split("\0") for line in raw.split("\0\n") if line.strip("\0\n")]
		return result.stdout + result.stderr, calls, json.loads(state_file.read_text(encoding="utf-8"))


_TRUSTED = [{"body": _MARKER, "author_association": "OWNER", "user": {"login": "pipeline-bot"}}]
_OPEN_SPLIT = {"state": "open", "labels": [{"name": "ai:split-to-project"}]}


def test_completed_project_closes_its_source_issue_once() -> None:
	out, calls, state = _close("complete", _TRUSTED, _OPEN_SPLIT)
	assert "PLAN_SPLIT_SOURCE tracking_issue=900 source_issue=123 outcome=closed" in out, out
	patch = [c for c in calls if c[:3] == ["api", "-X", "PATCH"]]
	assert patch and patch[0][3] == "repos/owner/repo/issues/123" and "state_reason=completed" in patch[0]
	assert any(c[:2] == ["api", "repos/owner/repo/issues/123/comments"] for c in calls)
	assert state["plan_split_source"] == {"issue": 123, "outcome": "closed"}
	# Later ticks read the recorded outcome and make no API call.
	out, calls, _ = _close("complete", _TRUSTED, _OPEN_SPLIT, state=state)
	assert calls == [] and "PLAN_SPLIT_SOURCE" not in out


def test_source_issue_left_alone() -> None:
	untrusted = [{**_TRUSTED[0], "author_association": "NONE", "user": {"login": "stranger"}}]
	for status, comments in (("failed", _TRUSTED), ("in_progress", _TRUSTED), ("complete", untrusted), ("complete", [])):
		out, calls, state = _close(status, comments, _OPEN_SPLIT)
		assert calls == [] and "plan_split_source" not in state, (status, out)
	for source, outcome in (({"state": "closed", "labels": [{"name": "ai:split-to-project"}]}, "already_closed"),
			({"state": "open", "labels": [{"name": "ai:awaiting-approval"}]}, "not_split")):
		out, calls, state = _close("complete", _TRUSTED, source)
		assert f"outcome={outcome}" in out and not any(c[:3] == ["api", "-X", "PATCH"] for c in calls)
		assert state["plan_split_source"]["outcome"] == outcome


def test_a_failed_lookup_retries_on_the_next_tick() -> None:
	out, calls, state = _close("complete", _TRUSTED, None)
	assert "outcome=deferred reason=source_lookup_failed" in out
	assert "plan_split_source" not in state and not any(c[:3] == ["api", "-X", "PATCH"] for c in calls)


def test_orchestrate_links_the_tracking_issue_on_the_source() -> None:
	text = (REPO_ROOT / ".github" / "workflows" / "orchestrate.yml").read_text(encoding="utf-8")
	assert "sed -n 's/^<!-- ai:plan-split-source:v1 issue=\\([1-9][0-9]*\\) -->$/\\1/p'" in text
	assert 'index("ai:split-to-project") != null' in text and "PLAN_SPLIT_LINKED" in text
	template = (REPO_ROOT / "workflow-templates" / "ai-orchestrate.yml").read_text(encoding="utf-8")
	assert "      tracking_comment:\n" in template and "tracking_comment: ${{ inputs.tracking_comment }}" in template
	poll_call = 'close_plan_split_source_if_needed "${PROJECT_STATUS}" "${COMMENTS:-[]}"'
	assert POLLER.count(poll_call) == 1


def test_orchestrate_releases_the_source_only_before_a_tracking_issue_exists() -> None:
	import yaml

	workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "orchestrate.yml").read_text(encoding="utf-8"))
	steps = next(job for job in workflow["jobs"].values() if any(s.get("name") == "Create tracking issue" for s in job.get("steps", [])))["steps"]
	release = next(step for step in steps if step.get("name") == "Release plan-split source issue")
	assert release["if"] == ("failure() && steps.tracking_issue.outputs.tracking_issue_number == '' "
		"&& contains(inputs.tracking_comment, '<!-- ai:plan-split-source:v1 issue=')")
	assert release["continue-on-error"] is True
	assert "<!-- ai:plan-split-released:v1 issue=${source_issue} -->" in release["run"]
	assert '--remove-label "ai:split-to-project"' in release["run"]
	# Release marker before the label removal (a label removed without the
	# marker would leave the split marker blocking every /reclarify).
	assert release["run"].index('--remove-label "ai:split-to-project"') > release["run"].index("ai:plan-split-released:v1")


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
