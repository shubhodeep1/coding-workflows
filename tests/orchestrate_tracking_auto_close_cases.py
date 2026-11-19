#!/usr/bin/env python3
"""close_completed_tracking_issue_if_ready (issue #8685).

A complete project's tracking issue used to stay open for good. The poller now
closes it on a later tick once the activation verdict is final, the release
callback is handled, the plan-split source is settled and no backpressure
label is left. These tests drive the function extracted from the poller with
a stub gh, fixture STATE_FILE, comments and labels.

The module is not named test_*: tests/test_activation_verify.py collects its
test_* functions, because CI already runs that file (ci.yml wiring is outside
issue #8685's file grant; see tests/test_ci_wires_every_test_file.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
REPO = "owner/repo"
TRACKING = 900



def _fn(name: str) -> str:
	match = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n", POLLER, re.S | re.M)
	assert match, name
	return match.group(0)


def _has_label_fn() -> str:
	# jq 1.6 rejects `$label` (a reserved word until jq 1.7, which the Actions
	# runners ship), so on an older local jq the same filter runs with the
	# variable renamed; on jq >= 1.7 the poller's function runs verbatim.
	text = _fn("has_label")
	version = subprocess.run(["jq", "--version"], text=True, capture_output=True, check=False).stdout.strip() if shutil.which("jq") else ""
	if re.fullmatch(r"jq-1\.[0-6](\.\d+)?", version):
		text = text.replace("--arg label", "--arg wanted").replace("index($label)", "index($wanted)")
	return text


_FUNCTIONS = _has_label_fn() + "".join(_fn(name) for name in (
	"is_truthy",
	"activation_final_verdict_from_comments",
	"_plan_split_source_issue_from_comments",
	"close_completed_tracking_issue_if_ready",
))

_HARNESS = r'''
set -euo pipefail
gh_retry() { "$@"; }
post_state_comment() { echo POST_STATE >> "${GH_LOG}.state"; [ "${FAKE_STATE_POST_FAIL:-}" != true ]; }
gh() {
  printf '%s\0' "$@" >> "${GH_LOG}"; printf '\n' >> "${GH_LOG}"
  case "$*" in
    *"-X PATCH"*) [ "${FAKE_PATCH_FAIL:-}" != true ] || return 1 ;;
    *"/comments"*) [ "${FAKE_COMMENT_FAIL:-}" != true ] || return 1 ;;
  esac
  return 0
}
'''

VERDICT_URL = f"https://github.com/{REPO}/issues/{TRACKING}#issuecomment-42"


def _verdict(verdict: str = "LIVE", association: str = "OWNER", login: str = "maintainer", url: str = VERDICT_URL) -> dict:
	return {
		"author_association": association, "user": {"login": login}, "html_url": url,
		"body": f"Activation verdict.\n\n<!-- ai:activation:v1 verdict={verdict} source=project-{TRACKING} -->\n",
	}


def _partial() -> dict:
	return {"author_association": "OWNER", "user": {"login": "maintainer"},
		"body": f"Incomplete.\n<!-- ai:activation:v1 partial=true source=project-{TRACKING} -->"}


def _split_marker() -> dict:
	return {"author_association": "OWNER", "user": {"login": "maintainer"},
		"body": "<!-- ai:plan-split-source:v1 issue=123 -->\n## Split from #123\n"}


def _close_marker() -> dict:
	return {"author_association": "OWNER", "user": {"login": "maintainer"},
		"body": f"Closed.\n\n<!-- ai:tracking-auto-close:v1 issue={TRACKING} -->"}


def _run(status: str = "complete", comments: list | None = None, labels: list | None = None, state: dict | None = None,
	env: dict | None = None, calls: int = 1) -> tuple[str, list[list[str]], dict, int]:
	if not shutil.which("jq"):
		pytest.skip("jq is required to run the poller functions")
	with tempfile.TemporaryDirectory() as workdir:
		tmp = Path(workdir)
		state_file = tmp / "state.json"
		state_file.write_text(json.dumps(state if state is not None else {"status": status}), encoding="utf-8")
		log = tmp / "gh.log"
		run_env = {k: v for k, v in os.environ.items() if k not in (
			"ORCHESTRATOR_CLOSE_COMPLETED_TRACKING", "ACTIVATION_VERIFY_ENABLED", "FAKE_PATCH_FAIL", "FAKE_COMMENT_FAIL",
			"FAKE_STATE_POST_FAIL")}
		run_env.update({
			"GH_LOG": str(log), "STATE_FILE": str(state_file), "TRACKING_NUM": str(TRACKING),
			"GITHUB_REPOSITORY": REPO, "COMPREHENSIVE_CYCLE_MARKER_TRUSTED_ASSOCIATIONS": "OWNER,MEMBER,COLLABORATOR",
			"COMMENTS_FETCH_OK": "true", "STATUS": status,
			"COMMENTS_JSON": json.dumps([_verdict()] if comments is None else comments),
			"LABELS_JSON": json.dumps(["ai:orchestrator-tracking"] if labels is None else labels),
		})
		run_env.update(env or {})
		call = 'close_completed_tracking_issue_if_ready "${STATUS}" "${COMMENTS_JSON}" "${LABELS_JSON}"\n'
		result = subprocess.run(["bash", "-c", _HARNESS + _FUNCTIONS + call * calls], env=run_env, text=True,
			capture_output=True, check=True)
		raw = log.read_text(encoding="utf-8") if log.exists() else ""
		gh_calls = [line.rstrip("\0").split("\0") for line in raw.split("\0\n") if line.strip("\0\n")]
		state_posts = len(Path(f"{log}.state").read_text().splitlines()) if Path(f"{log}.state").exists() else 0
		return result.stdout + result.stderr, gh_calls, json.loads(state_file.read_text(encoding="utf-8")), state_posts


def _posts(calls: list[list[str]]) -> list[str]:
	return [c[-1].split("=", 1)[1] for c in calls if c[:2] == ["api", f"repos/{REPO}/issues/{TRACKING}/comments"]]


def _patches(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if c[:3] == ["api", "-X", "PATCH"]]


# --- closes ------------------------------------------------------------------


def test_live_verdict_closes_once_with_comment_then_patch() -> None:
	out, calls, state, state_posts = _run()
	assert f"TRACKING_AUTO_CLOSE tracking_issue={TRACKING} outcome=closed verdict=LIVE" in out, out
	[body] = _posts(calls)
	[patch] = _patches(calls)
	assert calls.index(patch) > next(i for i, c in enumerate(calls) if c[-1] == "body=" + body)
	assert patch[3] == f"repos/{REPO}/issues/{TRACKING}" and "state=closed" in patch and "state_reason=completed" in patch
	assert "**LIVE**" in body and f"([verdict]({VERDICT_URL}))" in body
	assert "ai:operator-step" not in body
	assert "Reopen this issue to bring it back under the poller." in body
	assert body.endswith(f"<!-- ai:tracking-auto-close:v1 issue={TRACKING} -->")
	assert state["tracking_auto_close"]["outcome"] == "closed" and state["tracking_auto_close"]["verdict"] == "LIVE"
	assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", state["tracking_auto_close"]["at"])
	assert state_posts == 1


def test_dormant_verdict_points_at_the_operator_step_issue() -> None:
	out, calls, state, _ = _run(comments=[_verdict("DORMANT")])
	assert "outcome=closed verdict=DORMANT" in out, out
	[body] = _posts(calls)
	assert "**DORMANT**" in body
	assert f"https://github.com/{REPO}/issues?q=is%3Aissue+is%3Aopen+label%3Aai%3Aoperator-step" in body
	assert "\n\nThe remaining operator steps" in body and "operator-step\n\nThe release callback" in body
	assert body.endswith(f"<!-- ai:tracking-auto-close:v1 issue={TRACKING} -->")
	assert state["tracking_auto_close"]["verdict"] == "DORMANT"


def test_disabled_activation_verification_counts_as_final() -> None:
	out, calls, state, _ = _run(comments=[], env={"ACTIVATION_VERIFY_ENABLED": "false"})
	assert "outcome=closed verdict=none" in out, out
	assert "ACTIVATION_VERIFY_ENABLED=false" in _posts(calls)[0]
	assert state["tracking_auto_close"]["verdict"] == "none"


def test_settled_follow_ups_close() -> None:
	labels = ["ai:orchestrator-tracking", "ai:comprehensive-test-pending"]
	state = {"status": "complete", "comprehensive_release_callback": {"handled": True},
		"plan_split_source": {"issue": 123, "outcome": "closed"}}
	out, calls, _, _ = _run(comments=[_split_marker(), _verdict()], labels=labels, state=state)
	assert "outcome=closed" in out, out
	assert len(_patches(calls)) == 1


# --- defers --------------------------------------------------------------------


@pytest.mark.parametrize(("kwargs", "reason"), [
	({"comments": []}, "no_activation_verdict"),
	({"comments": [_partial()]}, "no_activation_verdict"),
	({"comments": [_verdict(association="NONE", login="stranger")]}, "no_activation_verdict"),
	({"comments": [_verdict(verdict="LIVE").copy() | {"body": "<!-- ai:activation:v1 verdict=LIVE source=project-901 -->"}]},
		"no_activation_verdict"),
	({"labels": ["ai:orchestrator-tracking", "ai:comprehensive-test-pending"]}, "release_callback_pending"),
	({"labels": ["ai:orchestrator-tracking", "ai:comprehensive-test-pending"],
		"state": {"status": "complete", "comprehensive_release_callback": {"handled": False}}}, "release_callback_pending"),
	({"comments": [_split_marker(), _verdict()]}, "plan_split_pending"),
	({"labels": ["ai:orchestrator-tracking", "ai:integration-backpressure"]}, "backpressure_label"),
	({"env": {"COMMENTS_FETCH_OK": "false"}}, "comments_unavailable"),
	# A failed labels read gives [] (get_issue_labels_json), which must not
	# count as "no pending release callback / no backpressure label".
	({"labels": []}, "labels_unavailable"),
	({"labels": ["ai:comprehensive-test-pending"]}, "labels_unavailable"),
])
def test_unmet_condition_defers_without_writes(kwargs, reason) -> None:
	out, calls, state, state_posts = _run(**kwargs)
	assert f"TRACKING_AUTO_CLOSE tracking_issue={TRACKING} outcome=deferred reason={reason}" in out, out
	assert calls == [] and "tracking_auto_close" not in state and state_posts == 0


def test_untrusted_split_marker_does_not_hold_the_close() -> None:
	untrusted = {**_split_marker(), "author_association": "NONE", "user": {"login": "stranger"}}
	out, _, _, _ = _run(comments=[untrusted, _verdict()])
	assert "outcome=closed" in out, out


def test_disabled_logs_once_per_process_and_makes_no_call() -> None:
	out, calls, state, _ = _run(env={"ORCHESTRATOR_CLOSE_COMPLETED_TRACKING": "false"}, calls=2)
	assert out.count("TRACKING_AUTO_CLOSE") == 1 and "outcome=deferred reason=disabled" in out, out
	assert calls == [] and "tracking_auto_close" not in state


# --- no-ops ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "validation-failed", "in_progress"])
def test_non_complete_status_is_a_silent_no_op(status) -> None:
	out, calls, state, _ = _run(status=status)
	assert calls == [] and "TRACKING_AUTO_CLOSE" not in out and "tracking_auto_close" not in state


def test_recorded_outcome_is_a_silent_no_op_even_when_reopened() -> None:
	state = {"status": "complete", "tracking_auto_close": {"outcome": "closed", "at": "2026-11-02T00:00:00Z", "verdict": "LIVE"}}
	out, calls, _, state_posts = _run(state=state)
	assert calls == [] and "TRACKING_AUTO_CLOSE" not in out and state_posts == 0


# --- failures and retry ---------------------------------------------------------


def test_patch_failure_defers_and_retry_does_not_repost_the_comment() -> None:
	out, calls, state, _ = _run(env={"FAKE_PATCH_FAIL": "true"})
	assert "outcome=deferred reason=close_failed" in out, out
	assert len(_posts(calls)) == 1 and len(_patches(calls)) == 1
	assert "tracking_auto_close" not in state
	# Next tick: the trusted marker comment is in COMMENTS, so only the PATCH runs.
	out, calls, state, _ = _run(comments=[_verdict(), _close_marker()])
	assert "outcome=closed" in out, out
	assert _posts(calls) == [] and len(_patches(calls)) == 1
	assert state["tracking_auto_close"]["outcome"] == "closed"


def test_untrusted_close_marker_does_not_suppress_the_comment() -> None:
	forged = {**_close_marker(), "author_association": "NONE", "user": {"login": "stranger"}}
	_, calls, _, _ = _run(comments=[_verdict(), forged])
	assert len(_posts(calls)) == 1


def test_state_save_failure_after_close_warns() -> None:
	out, calls, _, state_posts = _run(env={"FAKE_STATE_POST_FAIL": "true"})
	assert "outcome=closed verdict=LIVE state_persist=failed" in out, out
	assert state_posts == 2  # one retry before giving up
	assert "::warning::TRACKING_AUTO_CLOSE tracking_issue=900 closed, but its state was not saved" in out, out
	assert len(_patches(calls)) == 1
	out, _, _, _ = _run()
	assert "state_persist=ok" in out and "::warning::" not in out, out


def test_comment_failure_never_patches() -> None:
	out, calls, state, _ = _run(env={"FAKE_COMMENT_FAIL": "true"})
	assert "outcome=deferred reason=comment_failed" in out, out
	assert _patches(calls) == [] and "tracking_auto_close" not in state


@pytest.mark.parametrize("url", [
	f"https://github.com/other/repo/issues/{TRACKING}#issuecomment-42",
	f"https://github.com/{REPO}/issues/{TRACKING + 1}#issuecomment-42",
	VERDICT_URL + "\n/judge_resume",
	"javascript:alert(1)",
])
def test_untrusted_verdict_url_is_left_out(url) -> None:
	out, calls, _, _ = _run(comments=[_verdict(url=url)])
	assert "outcome=closed verdict=LIVE" in out, out
	[body] = _posts(calls)
	assert "([verdict](" not in body and "/judge_resume" not in body
	assert not any(line.lstrip().startswith("/") for line in body.splitlines())


# --- wiring ------------------------------------------------------------------------


def test_terminal_block_calls_it_once_after_the_partial_verdict_retry() -> None:
	call = 'close_completed_tracking_issue_if_ready "${PROJECT_STATUS}" "${COMMENTS:-[]}" "${TRACKING_LABELS:-[]}"'
	assert POLLER.count(call) == 1
	terminal = POLLER.index('if [ "${PROJECT_STATUS}" = "complete" ] || [ "${PROJECT_STATUS}" = "failed" ] || [ "${PROJECT_STATUS}" = "validation-failed" ]; then')
	partial = POLLER.index('endswith("<!-- ai:activation:v1 partial=true source=" + $src + " -->")', terminal)
	backpressure = POLLER.index("BACKPRESSURE_CLEARED tracking_issue=${TRACKING_NUM} reason=project_complete", terminal)
	skip = POLLER.index('echo "Project already ${PROJECT_STATUS}, skipping."', terminal)
	assert backpressure < partial < POLLER.index(call) < skip


def test_activation_verify_guard_uses_the_shared_helper() -> None:
	body = _fn("run_project_activation_verify")
	assert 'activation_final_verdict_from_comments "${COMMENTS:-[]}" "${TRACKING_NUM}"' in body
	assert "_plan_split_source_issue_from_comments" in _fn("close_plan_split_source_if_needed")


def test_poll_workflow_passes_the_variable_with_default_true() -> None:
	line = "ORCHESTRATOR_CLOSE_COMPLETED_TRACKING: ${{ vars.ORCHESTRATOR_CLOSE_COMPLETED_TRACKING || 'true' }}"
	text = POLL_WF.read_text(encoding="utf-8")
	assert text.count(line) == 2
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(text)
	steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
	for name in ("Process each tracking issue", "Run standalone stall recovery without active projects"):
		step = next(step for step in steps if step.get("name") == name)
		assert step["env"]["ORCHESTRATOR_CLOSE_COMPLETED_TRACKING"] == "${{ vars.ORCHESTRATOR_CLOSE_COMPLETED_TRACKING || 'true' }}"
