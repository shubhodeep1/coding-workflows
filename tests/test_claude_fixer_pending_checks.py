"""Contract for issue #4900: a clean Claude-fixer review that finishes before
CI must not become a 0-finding hand-off, and must auto-merge once the head's
check runs finish green.

Covers scripts/claude_fixer_pending_checks.py (the readiness half), its call
from the claude-pr-catch-all sweep (scripts/claude_pr_sweep.py), how
.claude/scripts/check_in_status.py routes a pending-checks head (unchanged:
`wait`, then `ci-failed` once a check fails), and the PR #4869 sequence end
to end: the hand-off step's comment, then the sweep with CI still running,
green, or failed. Every GitHub call goes to a stub `gh` on PATH.

Issue #5147: the marker is bound to the reviewed base (the v2 line's
`base_sha` and `base_ref_sha256`), so a PR retargeted after a clean review
is never merged on it.

Issue #5148: no merge while a newer review of the PR may still be running,
or when the latest newer one did not succeed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


pending_checks = _load("claude_fixer_pending_checks", ROOT / "scripts" / "claude_fixer_pending_checks.py")
sweeper = _load("claude_pr_sweep", ROOT / "scripts" / "claude_pr_sweep.py")
checker = sweeper.check_in_status
HANDOFF_SCRIPT = ROOT / "scripts" / "review_autofix_step_claude_fixer_handoff.sh"

REPO = "o/r"
PR = 42
HEAD = "c" * 40
OTHER_HEAD = "d" * 40
REF = "claude/fix-lint"
PLAN_REF = "claude/implement-plan-demo-phase-1"
AUTHOR = "workflow-bot"
RUN_ID = 99
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
DIGEST = "a" * 64
BASE_REF = "claude/implement-plan-demo"
BASE_SHA = "e" * 40
OTHER_BASE_SHA = "f" * 40


def _ref_digest(ref: str) -> str:
	return hashlib.sha256(ref.encode("utf-8")).hexdigest()


NOW = dt.datetime(2026, 9, 29, 3, 0, tzinfo=dt.timezone.utc)

LEDGER_EMPTY = """=== CONSENSUS FINDINGS ===
(No findings reported.)
=== END CONSENSUS FINDINGS ===

=== CONSENSUS TASK GAPS ===
(No task gaps reported.)
=== END CONSENSUS TASK GAPS ===

=== FINDINGS FROM minimax ===
(No findings reported.)
=== END FINDINGS FROM minimax ===
"""


def _binding_line(head: str = HEAD, round_number: int = 1, ledger: str = DIGEST, base_ref: str = BASE_REF,
	base_sha: str = BASE_SHA) -> str:
	return (f"<!-- ai:claude-fixer-pending-checks:v2 head={head} round={round_number} ledger={ledger} "
		f"base_sha={base_sha} base_ref_sha256={_ref_digest(base_ref)} -->")


def _pending_body(head: str = HEAD, round_number: int = 1, run_url: str = RUN_URL, ledger: str = DIGEST,
	base_ref: str = BASE_REF, base_sha: str = BASE_SHA, binding: str | None = "default") -> str:
	"""A pending-checks comment as the hand-off step writes it; `binding=None` omits the v2 line (a v1-only marker)."""
	if binding == "default":
		binding = _binding_line(head, round_number, ledger, base_ref, base_sha)
	return (
		f"## Review round {round_number}: clean review, waiting for check runs\n\n"
		f"Reviewed head: `{head}` ([workflow run]({run_url})).\n"
		f"Reviewed base: `{base_ref}` at `{base_sha}`.\n"
		"Reviewer ledger entries: 0. Check runs still running on this head: `ci / lint`.\n\n"
		f"<!-- ai:claude-fixer-pending-checks:v1 head={head} round={round_number} ledger={ledger} -->"
		+ (f"\n{binding}" if binding else "")
	)


def _handoff_body(head: str = HEAD, round_number: int = 1) -> str:
	return (
		f"## Review round {round_number}: findings handed to the Claude session\n\n"
		f"Reviewed head: `{head}` ([workflow run]({RUN_URL})).\n\n"
		f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={head} round={round_number} -->\n"
		f"<!-- ai:claude-fixer-handoff:v2 head={head} round={round_number} ledger={DIGEST} -->"
	)


def _comment(comment_id: int, body: str, login: str = AUTHOR) -> dict:
	return {"id": comment_id, "user": {"login": login, "type": "User"}, "author_association": "OWNER",
		"created_at": "2026-09-29T00:51:30Z", "body": body}


def _pr(ref: str = REF, **overrides) -> dict:
	pr = {"number": PR, "state": "open", "merged": False, "draft": False, "labels": [], "mergeable_state": "clean",
		"auto_merge": None, "updated_at": "2026-09-29T00:51:30Z", "head": {"sha": HEAD, "ref": ref, "repo": {"full_name": REPO}},
		"base": {"ref": BASE_REF, "sha": BASE_SHA, "repo": {"full_name": REPO, "default_branch": "main"}},
		"user": {"login": "pr-author", "type": "User"}, "title": "t", "body": "Refs #4900"}
	pr.update(overrides)
	return pr


def _check_run(name: str, status: str = "completed", conclusion: str | None = "success", completed_at: str = "2026-09-29T00:55:09Z") -> dict:
	return {"id": abs(hash(name)) % 10**6, "name": name, "status": status, "conclusion": conclusion,
		"completed_at": completed_at if status == "completed" else None,
		"details_url": f"https://github.com/{REPO}/actions/runs/5/job/{abs(hash(name)) % 10**6}",
		"html_url": "https://github.com/x", "app": {"slug": "github-actions"}, "output": {"title": "", "summary": "done"}}


def _review_run(**overrides) -> dict:
	run = {"id": RUN_ID, "html_url": RUN_URL, "repository": {"full_name": REPO}, "path": ".github/workflows/internal-review.yml",
		"head_branch": REF, "head_sha": HEAD, "status": "completed", "conclusion": "success"}
	run.update(overrides)
	return run


# ---- a stub `gh` for every process (in-process reads, the collector, the auto-merge helper) ----

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
from urllib.parse import parse_qs

state = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text())
args = sys.argv[1:]
with open(os.environ["FAKE_GH_CALLS"], "a") as log:
	log.write(json.dumps(args) + "\n")
jq = args[args.index("--jq") + 1] if "--jq" in args else None
paths = [a for a in args[1:] if a.startswith("repos/")]
path = paths[0] if paths else ""
base = path.split("?", 1)[0]
query = {key: values[0] for key, values in parse_qs(path.split("?", 1)[1] if "?" in path else "").items()}

def runs_listing(runs):
	if "status" in query:
		runs = [run for run in runs if run.get("status") == query["status"]]
	if "branch" in query:
		runs = [run for run in runs if run.get("head_branch") == query["branch"]]
	emit({"total_count": len(runs), "workflow_runs": runs})

def emit(value):
	text = json.dumps(value)
	if jq is not None:
		text = subprocess.run(["jq", "-r", jq], input=text, capture_output=True, text=True, check=True).stdout
	sys.stdout.write(text if text.endswith("\n") else text + "\n")

def fail(message):
	sys.stderr.write(message + "\n")
	sys.exit(1)

if args[:2] == ["pr", "merge"]:
	if state.get("merge_fails"):
		fail("GraphQL: auto-merge is not allowed")
	sys.exit(0)
if args[:1] != ["api"]:
	fail("unsupported gh call")
if base == f"repos/o/r/pulls/42":
	emit(state["pr"])
elif base == "repos/o/r/pulls":
	emit([state["pr"]] if "page=1" in path else [])
elif base == "repos/o/r/issues/42/comments":
	emit(state["comments"] if "page=1" in path or "page=" not in path else [])
elif base == "repos/o/r/issues/42/labels":
	emit([{"name": label["name"]} for label in state["pr"].get("labels", [])])
elif base.endswith("/check-runs"):
	runs = state["check_runs"]
	if "--slurp" in args:
		emit([{"total_count": len(runs), "check_runs": runs}])
	else:
		emit({"total_count": len(runs), "check_runs": runs})
elif base == f"repos/o/r/actions/runs/{state['review_run']['id']}":
	emit(state["review_run"])
elif base == "repos/o/r/actions/runs":
	runs_listing(state.get("branch_runs", []))
elif base.startswith("repos/o/r/actions/workflows/") and base.endswith("/runs"):
	workflow = base.split("/")[-2]
	listings = state.get("dispatch_runs", {})
	if listings.get(workflow) is None:
		fail("gh: Not Found (HTTP 404)")
	if listings[workflow] == "error":
		fail("gh: Server Error (HTTP 502)")
	runs_listing(listings[workflow])
elif base == "repos/o/r/actions/variables/ENABLE_AUTO_MERGE":
	value = state.get("enable_auto_merge")
	if value == "forbidden":
		fail("gh: Resource not accessible by personal access token (HTTP 403)")
	if value is None:
		fail("gh: Not Found (HTTP 404)")
	emit({"name": "ENABLE_AUTO_MERGE", "value": value})
elif base.startswith("repos/o/r/commits/"):
	emit({"commit": {"committer": {"date": "2026-09-29T00:08:00Z"}}})
else:
	fail(f"unexpected path {path}")
'''


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	state_file = tmp_path / "gh_state.json"
	calls_file = tmp_path / "gh_calls.jsonl"
	calls_file.write_text("", encoding="utf-8")
	monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
	monkeypatch.setenv("FAKE_GH_STATE", str(state_file))
	monkeypatch.setenv("FAKE_GH_CALLS", str(calls_file))
	monkeypatch.setenv("GH_TOKEN", "x")
	monkeypatch.setenv("GH_RETRY_MAX_ATTEMPTS", "1")
	monkeypatch.delenv("GITHUB_ENV", raising=False)

	class Fake:
		def set(self, *, pr=None, comments=(), check_runs=(), review_run=None, enable_auto_merge=None, merge_fails=False,
			branch_runs=(), dispatch_runs=None):
			# dispatch_runs maps a workflow file to its workflow_dispatch runs;
			# a workflow left out (or None) answers 404, as a missing workflow
			# does. The default is this repo: internal-review.yml and
			# review_autofix.yml exist, ai-review.yml does not.
			state_file.write_text(json.dumps({
				"pr": pr or _pr(), "comments": list(comments), "check_runs": list(check_runs),
				"review_run": review_run or _review_run(), "enable_auto_merge": enable_auto_merge, "merge_fails": merge_fails,
				"branch_runs": list(branch_runs),
				"dispatch_runs": dispatch_runs if dispatch_runs is not None else {"internal-review.yml": [], "review_autofix.yml": []},
			}), encoding="utf-8")

		def calls(self) -> list[list[str]]:
			return [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()]

		def merges(self) -> list[list[str]]:
			return [call for call in self.calls() if call[:2] == ["pr", "merge"]]

	return Fake()


GREEN = [_check_run("ci / lint"), _check_run("ci / test")]
RUNNING = [_check_run("ci / lint", status="in_progress", conclusion=None), _check_run("ci / test")]
FAILED = [_check_run("ci / lint", conclusion="failure"), _check_run("ci / test")]


# ---- find_pending_marker ----

def test_marker_is_found_for_the_current_head_from_the_workflow_account():
	marker = pending_checks.find_pending_marker([_comment(5, _pending_body())], REPO, HEAD, AUTHOR)
	assert marker == {"comment_id": 5, "round": 1, "ledger": DIGEST, "run_id": RUN_ID, "run_url": RUN_URL, "created_at": "2026-09-29T00:51:30Z",
		"base_sha": BASE_SHA, "base_ref_sha256": _ref_digest(BASE_REF)}


def test_base_binding_needs_one_v2_line_matching_the_v1_line():
	"""Issue #5147: without exactly one v2 line for the same head, round, and ledger the marker is unbound."""
	assert pending_checks.base_ref_digest(BASE_REF) == _ref_digest(BASE_REF)
	# A lone surrogate hashes instead of raising, and never collides with a replacement character.
	assert pending_checks.base_ref_digest(BASE_REF + "\ud800") != pending_checks.base_ref_digest(BASE_REF + "\ufffd")
	cases = {
		"v1 only": _pending_body(binding=None),
		"two v2 lines": _pending_body() + "\n" + _binding_line(base_sha=OTHER_BASE_SHA),
		"v2 for another head": _pending_body(binding=_binding_line(head=OTHER_HEAD)),
		"v2 for another round": _pending_body(binding=_binding_line(round_number=2)),
		"v2 for another ledger": _pending_body(binding=_binding_line(ledger="b" * 64)),
		"v2 with a short base sha": _pending_body(binding=_binding_line().replace(f"base_sha={BASE_SHA}", "base_sha=eeee")),
		"quoted v2 line": _pending_body(binding="> " + _binding_line()),
	}
	for label, body in cases.items():
		marker = pending_checks.find_pending_marker([_comment(5, body)], REPO, HEAD, AUTHOR)
		assert marker is not None and marker["comment_id"] == 5, label
		assert marker["base_sha"] is None and marker["base_ref_sha256"] is None, label


def test_marker_trust_rules_fail_closed():
	body = _pending_body()
	cases = {
		"another author": [_comment(5, body, login="someone")],
		"no trusted author configured": None,
		"another head": [_comment(5, _pending_body(head=OTHER_HEAD))],
		"no issued header": [_comment(5, body.split("\n", 1)[1])],
		"quoted marker": [_comment(5, body.replace("<!-- ai:", "> <!-- ai:"))],
		"two markers": [_comment(5, body + "\n" + next(line for line in body.splitlines() if ":v1 " in line))],
		"round differs from header": [_comment(5, body.replace("round=1 ", "round=2 "))],
		"round zero": [_comment(5, _pending_body(round_number=0))],
		"run link to another repository": [_comment(5, _pending_body(run_url="https://github.com/x/y/actions/runs/99"))],
		"superseded by a later hand-off": [_comment(5, body), _comment(6, _handoff_body())],
	}
	for label, comments in cases.items():
		login = "" if comments is None else AUTHOR
		assert pending_checks.find_pending_marker(comments or [_comment(5, body)], REPO, HEAD, login) is None, label


def test_a_hand_off_for_an_older_head_does_not_supersede_the_marker():
	comments = [_comment(5, _pending_body()), _comment(6, _handoff_body(head=OTHER_HEAD))]
	assert pending_checks.find_pending_marker(comments, REPO, HEAD, AUTHOR)["comment_id"] == 5
	# A converged verification run posts its marker after the answered hand-off.
	comments = [_comment(4, _handoff_body()), _comment(5, _pending_body(round_number=2))]
	assert pending_checks.find_pending_marker(comments, REPO, HEAD, AUTHOR)["round"] == 2


# ---- parse_check_snapshot ----

def _snapshot(status="ready", head=HEAD, total=2, failed=0, incomplete=0, extra=""):
	return (f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {head}\ncollection_status: {status}\ntotal_check_runs: {total}\n"
		f"failed_count: {failed}\nincomplete_count: {incomplete}\n\n{extra}")


def test_snapshot_classification():
	assert pending_checks.parse_check_snapshot(_snapshot(), HEAD)["state"] == "ready"
	assert pending_checks.parse_check_snapshot(_snapshot("timeout", incomplete=1, extra="incomplete[0].name: ci / lint\n"), HEAD) == {
		"state": "incomplete", "detail": "ci / lint"}
	assert pending_checks.parse_check_snapshot(_snapshot(failed=1, extra="failed[0].name: ci / lint\n"), HEAD)["state"] == "failed"
	assert pending_checks.parse_check_snapshot(_snapshot(extra="failed[0].name: ci / lint\n"), HEAD)["state"] == "failed"
	for label, text in {
		"other head": _snapshot(head=OTHER_HEAD),
		"api error": _snapshot("api_error"),
		"disabled": _snapshot("disabled"),
		"writer error": _snapshot("writer_error"),
		"no check runs": _snapshot(total=0),
		"timeout without incomplete runs": _snapshot("timeout"),
		"empty": "",
		"malformed counts": _snapshot().replace("failed_count: 0", "failed_count: none"),
	}.items():
		assert pending_checks.parse_check_snapshot(text, HEAD)["state"] == "invalid", label


def test_read_check_snapshot_uses_the_collector_once_without_waiting(fake_gh):
	fake_gh.set(check_runs=RUNNING)
	assert pending_checks.read_check_snapshot(REPO, HEAD) == {"state": "incomplete", "detail": "ci / lint"}
	check_run_reads = [call for call in fake_gh.calls() if any(arg.endswith("/check-runs?per_page=100") for arg in call)]
	assert len(check_run_reads) == 1
	fake_gh.set(check_runs=GREEN)
	assert pending_checks.read_check_snapshot(REPO, HEAD)["state"] == "ready"


# ---- evaluate ----

def _evaluate(dry_run=False, author=AUTHOR):
	return pending_checks.evaluate(REPO, PR, author_login=author, dry_run=dry_run)


def test_ready_head_enables_head_bound_squash_auto_merge(fake_gh):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	result = _evaluate()
	assert result["state"] == "merge_enabled", result
	assert fake_gh.merges() == [["pr", "merge", "42", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]]


def test_dry_run_reports_ready_without_merging(fake_gh):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	assert _evaluate(dry_run=True)["state"] == "ready"
	assert fake_gh.merges() == []


@pytest.mark.parametrize("label, setup, expected", [
	("checks still running", dict(check_runs=RUNNING), "waiting"),
	("a check failed", dict(check_runs=FAILED), "checks_failed"),
	("a check was cancelled", dict(check_runs=[_check_run("ci / lint", conclusion="cancelled")]), "checks_failed"),
	("no check runs", dict(check_runs=[]), "snapshot_invalid"),
	("no marker", dict(comments=[]), "no_marker"),
	("marker for an older head", dict(comments=[_comment(5, _pending_body(head=OTHER_HEAD))]), "no_marker"),
	("review run failed", dict(review_run=_review_run(conclusion="failure")), "run_unverified"),
	("review run on another branch", dict(review_run=_review_run(head_branch="main")), "run_unverified"),
	("review run from another workflow", dict(review_run=_review_run(path=".github/workflows/ci.yml")), "run_unverified"),
	("blocked label", dict(pr=_pr(labels=[{"name": "ai:review-blocked"}])), "not_eligible"),
	("merge conflict", dict(pr=_pr(mergeable_state="dirty")), "not_eligible"),
	("draft", dict(pr=_pr(draft=True)), "not_eligible"),
	("auto-merge already on", dict(pr=_pr(auto_merge={"merge_method": "squash"})), "not_eligible"),
	("closed", dict(pr=_pr(state="closed")), "not_eligible"),
	("not a claude head", dict(pr=_pr(ref="ai/issue-7")), "not_eligible"),
	("malformed head object", dict(pr=_pr(head="claude/fix-lint")), "not_eligible"),
	("auto-merge disabled for the repo", dict(enable_auto_merge="false"), "auto_merge_disabled"),
	("auto-merge variable unreadable", dict(enable_auto_merge="forbidden"), "auto_merge_setting_unreadable"),
	("merge refused", dict(merge_fails=True), "merge_failed"),
])
def test_every_other_state_fails_closed(fake_gh, label, setup, expected):
	config = dict(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	config.update(setup)
	fake_gh.set(**config)
	result = _evaluate()
	assert result["state"] == expected, (label, result)
	if expected != "merge_failed":
		assert fake_gh.merges() == [], label


def _dispatched_review_run(**overrides) -> dict:
	# A review the sweep dispatched from the default branch (issue #4618):
	# its branch and sha are main's, and the run title binds it to the PR.
	run = {"event": "workflow_dispatch", "head_branch": "main", "head_sha": OTHER_HEAD,
		"display_title": f"Internal: AI Review & Autofix [pr:{PR}]"}
	run.update(overrides)
	return _review_run(**run)


def test_sweep_dispatched_review_run_verifies_by_title(fake_gh):
	pr = _pr(base={"ref": "main", "sha": BASE_SHA, "repo": {"full_name": REPO, "default_branch": "main"}})
	fake_gh.set(pr=pr, comments=[_comment(5, _pending_body(base_ref="main"))], check_runs=GREEN, review_run=_dispatched_review_run())
	result = _evaluate()
	assert result["state"] == "merge_enabled", result
	assert fake_gh.merges() == [["pr", "merge", "42", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]]


@pytest.mark.parametrize("label, run_overrides, pr_base", [
	("title names another PR", dict(display_title="Internal: AI Review & Autofix [pr:7]"), "main"),
	("not a dispatch", dict(event="push"), "main"),
	("default branch unknown", {}, None),
	("dispatched from another branch", dict(head_branch="stable"), "main"),
])
def test_default_branch_review_run_needs_the_full_dispatch_binding(fake_gh, label, run_overrides, pr_base):
	base = {"ref": "main", "sha": BASE_SHA, "repo": {"full_name": REPO, "default_branch": pr_base}} if pr_base else {"ref": "main", "sha": BASE_SHA}
	fake_gh.set(pr=_pr(base=base), comments=[_comment(5, _pending_body(base_ref="main"))], check_runs=GREEN,
		review_run=_dispatched_review_run(**run_overrides))
	result = _evaluate()
	assert result["state"] == "run_unverified", (label, result)
	assert fake_gh.merges() == [], label


# ---- a newer review of the PR (issue #5148) ----

def _run(run_id: int, *, workflow: str = "internal-review.yml", status: str = "completed", conclusion: str | None = "success",
	head_branch: str = REF, event: str = "pull_request", title: str = "t") -> dict:
	return {"id": run_id, "path": f".github/workflows/{workflow}", "status": status,
		"conclusion": conclusion if status == "completed" else None, "head_branch": head_branch, "event": event,
		"display_title": title, "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}


def _dispatch(run_id: int, *, pr: int = PR, **overrides) -> dict:
	# What review_autofix_sweep.yml starts every 30 minutes, and what a
	# force-review PR gets: an internal-review.yml dispatch from main, bound to
	# its PR only by the run title.
	return _run(run_id, event="workflow_dispatch", head_branch="main", title=f"Internal: AI Review & Autofix [pr:{pr}]", **overrides)


def _dispatch_runs(**listings) -> dict:
	runs = {"internal-review.yml": [], "review_autofix.yml": []}
	runs.update(listings)
	return runs


def test_forced_review_running_from_the_default_branch_blocks_the_merge(fake_gh):
	# The audit's scenario: the old head's checks are green and its marker is
	# live, while a forced review dispatched from main is still reviewing it.
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN,
		dispatch_runs=_dispatch_runs(**{"internal-review.yml": [_dispatch(RUN_ID + 7, status="in_progress")]}))
	result = _evaluate()
	assert result["state"] == "review_active", result
	assert f"run {RUN_ID + 7} (in_progress)" in result["reason"]
	assert fake_gh.merges() == []


@pytest.mark.parametrize("label, branch_runs, listings", [
	("review run queued on the head branch", [_run(RUN_ID + 1, status="queued")], {}),
	("any workflow still running on the head branch", [_run(RUN_ID + 1, workflow="ci.yml", status="in_progress")], {}),
	("pending on the head branch", [_run(RUN_ID + 1, status="pending")], {}),
	("waiting on the head branch", [_run(RUN_ID + 1, status="waiting")], {}),
	("the marker's own run re-run", [_run(RUN_ID, status="in_progress")], {}),
	("an older review still running", [_run(RUN_ID - 5, status="in_progress")], {}),
	("sweep dispatch for this PR queued", [], {"internal-review.yml": [_dispatch(RUN_ID + 2, status="queued")]}),
	("convergence or direct review_autofix.yml dispatch (no PR binding)", [],
		{"review_autofix.yml": [_run(RUN_ID + 3, workflow="review_autofix.yml", status="in_progress", event="workflow_dispatch",
			head_branch="main")]}),
	("consumer ai-review.yml dispatch (no PR binding)", [],
		{"internal-review.yml": None, "review_autofix.yml": None,
			"ai-review.yml": [_run(RUN_ID + 4, workflow="ai-review.yml", status="queued", event="workflow_dispatch", head_branch="main")]}),
	# The stall poller's force_rb_judge path (conformance run 1): it runs
	# review_autofix.yml through its own wrapper, past the pending-checks skip.
	("stall-poller review_rb_judge_dispatch.yml dispatch (no PR binding)", [],
		{"review_rb_judge_dispatch.yml": [_run(RUN_ID + 5, workflow="review_rb_judge_dispatch.yml", status="in_progress",
			event="workflow_dispatch", head_branch="main", title="Internal: Review-Blocked Judge Dispatch")]}),
])
def test_an_active_review_defers_the_merge(fake_gh, label, branch_runs, listings):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN, branch_runs=branch_runs,
		dispatch_runs=_dispatch_runs(**listings))
	result = _evaluate()
	assert result["state"] == "review_active", (label, result)
	assert fake_gh.merges() == [], label


def test_an_active_review_defers_a_dry_run_too(fake_gh):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN, branch_runs=[_run(RUN_ID + 1, status="queued")])
	assert _evaluate(dry_run=True)["state"] == "review_active"


@pytest.mark.parametrize("label, branch_runs, listings", [
	("newer head-branch review failed", [_run(RUN_ID + 1, conclusion="failure")], {}),
	("newer head-branch review cancelled", [_run(RUN_ID + 1, conclusion="cancelled")], {}),
	("newer consumer review timed out", [_run(RUN_ID + 1, workflow="ai-review.yml", conclusion="timed_out")], {}),
	("newer sweep dispatch for this PR failed", [], {"internal-review.yml": [_dispatch(RUN_ID + 2, conclusion="failure")]}),
	("the latest of several newer reviews failed",
		[_run(RUN_ID + 1, conclusion="success")], {"internal-review.yml": [_dispatch(RUN_ID + 2, conclusion="failure")]}),
])
def test_an_unsuccessful_newer_review_supersedes_the_marker(fake_gh, label, branch_runs, listings):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN, branch_runs=branch_runs,
		dispatch_runs=_dispatch_runs(**listings))
	result = _evaluate()
	assert result["state"] == "review_superseded", (label, result)
	assert fake_gh.merges() == [], label


@pytest.mark.parametrize("label, branch_runs, listings", [
	("the marker's run itself on the head branch", [_run(RUN_ID)], {}),
	("newer gate-skipped sweep dispatch succeeded", [], {"internal-review.yml": [_dispatch(RUN_ID + 2)]}),
	("a failed newer review then a successful one",
		[_run(RUN_ID + 1, conclusion="failure")], {"internal-review.yml": [_dispatch(RUN_ID + 2)]}),
	("an older review failed", [_run(RUN_ID - 1, conclusion="failure")], {}),
	("a newer non-review run failed on the head branch", [_run(RUN_ID + 1, workflow="ci.yml", conclusion="failure")], {}),
	("an active sweep dispatch for another PR", [], {"internal-review.yml": [_dispatch(RUN_ID + 2, pr=7, status="in_progress")]}),
	("a failed sweep dispatch for another PR", [], {"internal-review.yml": [_dispatch(RUN_ID + 2, pr=7, conclusion="failure")]}),
	("finished unbound dispatches", [],
		{"review_autofix.yml": [_run(RUN_ID + 3, workflow="review_autofix.yml", conclusion="failure", event="workflow_dispatch",
			head_branch="main")]}),
	("no review workflow dispatch listings at all (404)", [],
		{"internal-review.yml": None, "review_autofix.yml": None}),
	("a finished review_rb_judge_dispatch.yml dispatch", [],
		{"review_rb_judge_dispatch.yml": [_run(RUN_ID + 5, workflow="review_rb_judge_dispatch.yml", conclusion="failure",
			event="workflow_dispatch", head_branch="main", title="Internal: Review-Blocked Judge Dispatch")]}),
])
def test_settled_reviews_let_the_merge_through(fake_gh, label, branch_runs, listings):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN, branch_runs=branch_runs,
		dispatch_runs=_dispatch_runs(**listings))
	result = _evaluate()
	assert result["state"] == "merge_enabled", (label, result)
	assert fake_gh.merges() == [["pr", "merge", "42", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]], label


def test_a_failed_run_listing_read_raises_for_the_sweep_to_log(fake_gh):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN,
		dispatch_runs=_dispatch_runs(**{"review_autofix.yml": "error"}))
	# The evaluator loads its own copy of check_in_status.
	with pytest.raises(pending_checks.check_in_status.ReadError):
		_evaluate()
	assert fake_gh.merges() == []


_DROP = object()


def _malformed(run: dict, **fields) -> dict:
	run = dict(run)
	for key, value in fields.items():
		if value is _DROP:
			run.pop(key)
		else:
			run[key] = value
	return run


@pytest.mark.parametrize("label, branch_runs, listings", [
	# PR #5183 review round 1: a run that cannot be ordered against the
	# marker's run, or classified as active, fails the read instead of being
	# skipped (a newer failed review with a string id used to be ignored).
	("newer failed head-branch review with a string id", [_malformed(_run(RUN_ID + 1, conclusion="failure"), id=str(RUN_ID + 1))], {}),
	("head-branch run with no id", [_malformed(_run(RUN_ID + 1), id=_DROP)], {}),
	("sweep dispatch for this PR with a boolean id", [], {"internal-review.yml": [_malformed(_dispatch(RUN_ID + 2), id=True)]}),
	("head-branch run with no status", [_malformed(_run(RUN_ID + 1), status=_DROP)], {}),
	("unbound dispatch with a null status", [],
		{"review_autofix.yml": [_malformed(_run(RUN_ID + 3, workflow="review_autofix.yml", event="workflow_dispatch",
			head_branch="main"), status=None)]}),
	# PR #5178 review round 2: an internal-review.yml dispatch is bound to its
	# PR by title alone, so one without a title could be this PR's review.
	("sweep dispatch with no title, still running", [],
		{"internal-review.yml": [_malformed(_dispatch(RUN_ID + 2, status="in_progress"), display_title=_DROP)]}),
	("sweep dispatch with a null title, failed", [],
		{"internal-review.yml": [_malformed(_dispatch(RUN_ID + 2, conclusion="failure"), display_title=None)]}),
])
def test_a_malformed_run_in_a_listing_raises_for_the_sweep_to_log(fake_gh, label, branch_runs, listings):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN, branch_runs=branch_runs,
		dispatch_runs=_dispatch_runs(**listings))
	with pytest.raises(pending_checks.check_in_status.ReadError, match="malformed runs listing"):
		_evaluate()
	assert fake_gh.merges() == [], label


def test_a_missing_head_branch_runs_listing_is_not_read_as_no_runs(fake_gh, monkeypatch):
	# Only a review workflow's dispatch listing may 404 (the workflow does not
	# exist in that repo); the head-branch listing always exists.
	real_api = pending_checks.check_in_status.gh_api

	def branch_listing_404(path):
		if path.startswith("repos/o/r/actions/runs?branch="):
			raise pending_checks.check_in_status.ReadError(f"gh api {path} failed: gh: Not Found (HTTP 404)")
		return real_api(path)

	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	monkeypatch.setattr(pending_checks.check_in_status, "gh_api", branch_listing_404)
	with pytest.raises(pending_checks.check_in_status.ReadError):
		_evaluate()
	assert fake_gh.merges() == []


def test_the_run_reads_come_only_after_a_ready_snapshot(fake_gh):
	# §15: a PR whose checks are still running costs no run listing reads.
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=RUNNING)
	assert _evaluate()["state"] == "waiting"
	assert not [call for call in fake_gh.calls() if "/actions/runs?" in " ".join(call) or "/actions/workflows/" in " ".join(call)]


def test_the_stated_run_read_budget_matches_the_listings_read(fake_gh):
	# PR #5178 review round 1: the §15 budget is restated in three docstrings,
	# and no other test reads them, so a new review workflow would leave the
	# counts stale.
	unbound = len(pending_checks.UNBOUND_DISPATCH_REVIEW_WORKFLOWS)
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	assert _evaluate()["state"] == "merge_enabled"
	listings = [call for call in fake_gh.calls() if "/actions/runs?" in " ".join(call) or "/actions/workflows/" in " ".join(call)]
	assert len(listings) == 2 + unbound
	assert f"Reads, {2 + unbound} calls in all" in pending_checks.check_review_runs.__doc__
	assert f"each of the {unbound} UNBOUND_DISPATCH_REVIEW_WORKFLOWS entries" in pending_checks.check_review_runs.__doc__
	for doc in (pending_checks.__doc__, sweeper.__doc__):
		assert re.search(rf"1\s+head-branch runs read,?(?: and)?\s+{1 + unbound}\s+workflow_dispatch runs reads", doc)


@pytest.mark.parametrize("label, later_comments", [
	("a newer review handed findings off", [_comment(5, _pending_body()), _comment(9, _handoff_body())]),
	("a newer clean review posted its own marker", [_comment(5, _pending_body()), _comment(9, _pending_body(round_number=2))]),
	("the marker was deleted", []),
])
def test_a_marker_that_changed_during_the_run_reads_is_not_authorized(fake_gh, monkeypatch, label, later_comments):
	# A newer review that finished between the first comment read and the run
	# reads has already posted its comment; the re-read must see it.
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	reads = []
	real_list = pending_checks.check_in_status.gh_api_list

	def comments_then_later(path):
		if path.endswith("/issues/42/comments"):
			reads.append(path)
			return real_list(path) if len(reads) == 1 else later_comments
		return real_list(path)

	monkeypatch.setattr(pending_checks.check_in_status, "gh_api_list", comments_then_later)
	result = _evaluate()
	assert result["state"] == "review_superseded", (label, result)
	assert len(reads) == 2
	assert fake_gh.merges() == [], label


def test_unset_workflow_author_trusts_no_marker(fake_gh):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	assert _evaluate(author="")["state"] == "no_marker"
	assert fake_gh.merges() == []


def _reads_after_the_marker(fake_gh) -> list[list[str]]:
	"""Calls past the PR and comments reads: check runs, the review run, the variable, the merge."""
	return [call for call in fake_gh.calls()
		if not any(arg.startswith((f"repos/{REPO}/pulls/{PR}", f"repos/{REPO}/issues/{PR}/comments")) for arg in call)]


@pytest.mark.parametrize("label, pr_base, body, expected", [
	# Issue #5147: clean review of the phase PR into its project branch, then
	# the author retargets it to main without pushing. The head is unchanged.
	("retargeted to main", {"ref": "main", "sha": OTHER_BASE_SHA}, _pending_body(), "base_changed"),
	("retargeted, base sha unchanged", {"ref": "main", "sha": BASE_SHA}, _pending_body(), "base_changed"),
	("same ref, base sha changed", {"ref": BASE_REF, "sha": OTHER_BASE_SHA}, _pending_body(), "base_changed"),
	("PR base has no sha", {"ref": BASE_REF}, _pending_body(), "base_changed"),
	("PR base has no ref", {"sha": BASE_SHA}, _pending_body(), "base_changed"),
	# Review round 1 of PR #5186: a base ref JSON decodes to a lone surrogate must not crash the digest.
	("PR base ref with a lone surrogate", {"ref": BASE_REF + "\ud800", "sha": BASE_SHA}, _pending_body(), "base_changed"),
	("marker without a base binding", {"ref": BASE_REF, "sha": BASE_SHA}, _pending_body(binding=None), "base_unbound"),
], ids=lambda value: value if isinstance(value, str) and "\n" not in value and not value.startswith("base_") else "")
def test_a_base_other_than_the_reviewed_one_never_merges(fake_gh, label, pr_base, body, expected):
	fake_gh.set(pr=_pr(base=pr_base), comments=[_comment(5, body)], check_runs=GREEN)
	result = _evaluate()
	assert result["state"] == expected, (label, result)
	assert "review sweep reviews this head again" in result["reason"], label
	assert result["head_sha"] == HEAD
	# Decided from the PR read already made: no check-run, run, variable, or merge call.
	assert _reads_after_the_marker(fake_gh) == [], label
	assert fake_gh.merges() == [], label


def test_a_fresh_review_of_the_new_base_merges_again(fake_gh):
	"""After the retarget the next review sweep reviews the head against main; its marker binds main and wins."""
	old = _comment(5, _pending_body())
	fresh = _comment(9, _pending_body(round_number=1, base_ref="main", base_sha=OTHER_BASE_SHA))
	pr = _pr(base={"ref": "main", "sha": OTHER_BASE_SHA})
	fake_gh.set(pr=pr, comments=[old], check_runs=GREEN)
	assert _evaluate()["state"] == "base_changed"
	fake_gh.set(pr=pr, comments=[old, fresh], check_runs=GREEN)
	assert _evaluate()["state"] == "merge_enabled"
	assert fake_gh.merges() == [["pr", "merge", "42", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]]


def test_a_retarget_back_to_an_earlier_reviewed_base_is_not_merged_on_the_older_marker(fake_gh):
	"""The latest marker (bound to main) wins even when an older one matches the restored base; the gate follows the same comment, so the head is reviewed again."""
	old = _comment(5, _pending_body())
	fresh = _comment(9, _pending_body(round_number=1, base_ref="main", base_sha=OTHER_BASE_SHA))
	fake_gh.set(pr=_pr(), comments=[old, fresh], check_runs=GREEN)
	result = _evaluate()
	assert result["state"] == "base_changed"
	assert _reads_after_the_marker(fake_gh) == []
	assert fake_gh.merges() == []


def test_enable_auto_merge_variable_reads(fake_gh):
	for stored, expected in ((None, "true"), ("true", "true"), ("false", "false"), ("forbidden", None)):
		fake_gh.set(enable_auto_merge=stored)
		assert pending_checks.read_enable_auto_merge(REPO) == expected, stored


@pytest.mark.parametrize("stderr, stdout, expected", [
	("gh: Not Found (HTTP 404)\n", "", "true"),
	("gh: Not Found\n", "", "true"),
	("", '{"message":"Not Found","documentation_url":"https://docs.github.com","status":"404"}', "true"),
	("gh: warning\n", '{"message": "Not Found", "status": "404"}', "true"),
	("gh: Resource not accessible by personal access token (HTTP 403)\n", '{"status":"403"}', None),
	("gh: Bad credentials (HTTP 401)\n", "", None),
	("", "", None),
])
def test_unset_variable_is_recognised_from_any_404_form(monkeypatch, stderr, stdout, expected):
	"""An unset variable reads as the default however `gh` words the 404; anything else stays unreadable."""
	monkeypatch.setattr(pending_checks.subprocess, "run",
		lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout=stdout, stderr=stderr))
	assert pending_checks.read_enable_auto_merge(REPO) == expected


def test_auto_merge_success_line_matches_the_helper():
	"""The success signal the evaluator reads is the line the helper prints after `gh pr merge --auto` succeeded."""
	lines = (ROOT / "scripts" / "review_enable_auto_merge.sh").read_text(encoding="utf-8").splitlines()
	echoes = [index for index, line in enumerate(lines)
		if line.strip().startswith(f'echo "{pending_checks.AUTO_MERGE_ENABLED_LINE_PREFIX}')]
	assert len(echoes) == 1, echoes
	merge = max(index for index, line in enumerate(lines[:echoes[0]]) if "gh pr merge" in line and "--auto" in line)
	assert lines[merge].strip().startswith("if ") and echoes[0] - merge <= 3


@pytest.mark.parametrize("returncode, stdout, expected", [
	(0, "Enabling auto-merge (squash) on PR #42...\nAuto-merge enabled. PR will merge once all required checks pass.\n", True),
	(0, "Auto-merge disabled (set ENABLE_AUTO_MERGE=true to enable).\n", False),
	(0, "::warning::Could not enable auto-merge on PR #42.\n", False),
	(1, "Auto-merge enabled. PR will merge once all required checks pass.\n", False),
])
def test_enable_auto_merge_reads_the_helper_result(monkeypatch, returncode, stdout, expected):
	monkeypatch.setattr(pending_checks.subprocess, "run",
		lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, stdout=stdout, stderr=""))
	assert pending_checks.enable_auto_merge(REPO, PR, HEAD, "true")["enabled"] is expected


# ---- check_in_status.py routes a pending-checks head unchanged ----

def _check_in_status_json(fake_gh, *argv) -> dict:
	env = {**os.environ, "CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN": AUTHOR, "PYTHONDONTWRITEBYTECODE": "1"}
	proc = subprocess.run(["python3", str(ROOT / ".claude" / "scripts" / "check_in_status.py"), "--repo", REPO, "--pr", str(PR), *argv],
		capture_output=True, text=True, env=env)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	return json.loads(proc.stdout)


@pytest.mark.parametrize("ref", [REF, PLAN_REF])
def test_check_in_status_waits_on_a_pending_checks_head(fake_gh, ref):
	fake_gh.set(pr=_pr(ref=ref), comments=[_comment(5, _pending_body())], check_runs=RUNNING, review_run=_review_run(head_branch=ref))
	for argv in ((), ("--hand-back",)):
		verdict = _check_in_status_json(fake_gh, *argv)
		assert verdict["state"] != "review-round" and verdict["action"] == "wait", (argv, verdict)
	fake_gh.set(pr=_pr(ref=ref), comments=[_comment(5, _pending_body())], check_runs=GREEN, review_run=_review_run(head_branch=ref))
	for argv in ((), ("--hand-back",)):
		assert _check_in_status_json(fake_gh, *argv)["action"] == "wait", argv


def test_check_in_status_hands_a_later_failed_check_back_as_ci_failed(fake_gh):
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=FAILED)
	verdict = _check_in_status_json(fake_gh, "--hand-back")
	assert verdict["state"] == "ci-failed" and verdict["action"] == "hand_back_fixer", verdict


# ---- the claude-pr-catch-all sweep ----

def _sweep(pending=sweeper.evaluate_pending_checks, **overrides):
	kwargs = dict(min_age_hours=0, dry_run=False, self_repo="o/self", queue_token="ghs_x", run_id="123",
		run_url="https://github.com/o/self/actions/runs/123", allowed=[REPO, "o/self"],
		queue=lambda *a: pytest.fail("must not queue"), queued=lambda *a: set(), pending_checks=pending)
	kwargs.update(overrides)
	return sweeper.sweep([REPO], NOW, **kwargs)


def test_sweep_runs_the_pending_pass_only_for_open_verdicts(monkeypatch):
	verdicts = {1: {"done": False, "state": "open"}, 2: {"done": False, "state": "claimed"}, 3: {"done": False, "state": "held"},
		4: {"done": True, "state": "ci-failed", "kind": "ci", "head_sha": HEAD}}
	monkeypatch.setattr(sweeper, "list_candidates", lambda repo: [{"number": n, "head_ref": REF} for n in verdicts])
	monkeypatch.setattr(sweeper.check_in_status, "check_pr_hand_back", lambda repo, number, *a, **k: verdicts[number])
	monkeypatch.setattr(sweeper.claude_fix_claim, "post_claim", lambda *a: (0, {"posted": True}))
	seen = []

	def fake_pending(repo, number, dry_run):
		seen.append((repo, number, dry_run))
		return {"state": "merge_enabled", "head_sha": HEAD, "reason": "green"}

	summary = _sweep(pending=fake_pending, queue=lambda *a: 5)
	assert seen == [(REPO, 1, False)]
	assert summary["pending_checks_merged"] == 1 and summary["queued"] == 1


@pytest.mark.parametrize("error", [
	sweeper.check_in_status.ReadError("HTTP 502"),
	AttributeError("'NoneType' object has no attribute 'get'"),
	KeyError("run_id"),
	TypeError("malformed payload"),
	ValueError("malformed payload"),
	OSError(28, "No space left on device"),
	IndexError("list index out of range"),
	RuntimeError("unexpected failure in the pass"),
])
def test_sweep_pending_pass_fails_open_per_pr(monkeypatch, capsys, error):
	monkeypatch.setattr(sweeper, "list_candidates", lambda repo: [{"number": 1, "head_ref": REF}, {"number": 2, "head_ref": REF}])
	monkeypatch.setattr(sweeper.check_in_status, "check_pr_hand_back", lambda *a, **k: {"done": False, "state": "open"})

	def flaky(repo, number, dry_run):
		if number == 1:
			raise error
		return {"state": "merge_enabled", "head_sha": HEAD}

	summary = _sweep(pending=flaky)
	assert summary["errors"] == 1 and summary["pending_checks_merged"] == 1
	assert "pending_checks_failed repo=o/r pr=#1" in capsys.readouterr().out


def test_sweep_pending_pass_survives_an_unwritable_snapshot_dir(fake_gh, monkeypatch, capsys):
	# The real pass: a live marker reaches read_check_snapshot, whose temp
	# directory fails on the first candidate (disk full). The sweep logs it
	# and still evaluates the next one instead of ending the run for every
	# later PR and repo. The fake gh serves one PR, so it is listed twice.
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", AUTHOR)
	monkeypatch.setattr(sweeper, "list_candidates", lambda repo: [{"number": PR, "head_ref": REF}, {"number": PR, "head_ref": REF}])
	monkeypatch.setattr(sweeper.check_in_status, "check_pr_hand_back", lambda *a, **k: {"done": False, "state": "open"})
	fake_gh.set(comments=[_comment(5, _pending_body())], check_runs=GREEN)
	real_tempfile = sweeper.claude_fixer_pending_checks.tempfile
	calls = []

	def temporary_directory():
		calls.append(1)
		if len(calls) == 1:
			raise OSError(28, "No space left on device")
		return real_tempfile.TemporaryDirectory()

	monkeypatch.setattr(sweeper.claude_fixer_pending_checks, "tempfile",
		type("FakeTempfile", (), {"TemporaryDirectory": staticmethod(temporary_directory)}))
	summary = _sweep()
	assert summary["errors"] == 1 and summary["pending_checks_merged"] == 1
	assert f"pending_checks_failed repo=o/r pr=#{PR} error=[Errno 28] No space left on device" in capsys.readouterr().out
	assert len(fake_gh.merges()) == 1


def test_sweep_without_a_pending_pass_behaves_as_before(monkeypatch):
	monkeypatch.setattr(sweeper, "list_candidates", lambda repo: [{"number": 1, "head_ref": REF}])
	monkeypatch.setattr(sweeper.check_in_status, "check_pr_hand_back", lambda *a, **k: {"done": False, "state": "open"})
	assert _sweep(pending=None)["skipped"] == 1


def test_main_wires_the_pending_pass(monkeypatch):
	captured = {}
	monkeypatch.setattr(sweeper, "sweep", lambda repos, now, **kwargs: captured.update(kwargs) or {"repos": 0})
	sweeper.main(["--self-repo", "o/self", "--registry", "/nonexistent"], now=NOW)
	assert captured["pending_checks"] is sweeper.evaluate_pending_checks


# ---- PR #4869 end to end: hand-off step, then the sweep ----

def _run_handoff_step(tmp: Path, snapshot: str, payload: dict | None = None) -> str:
	"""Run the real hand-off step on a clean ledger; return the one comment body it posts.

	`payload` is the review run's PR snapshot (PR_PAYLOAD_FILE); the default
	is the phase PR into BASE_REF at BASE_SHA.
	"""
	tmp = tmp / "handoff"
	tmp.mkdir()
	payload_file = tmp / "pr_payload.json"
	payload_file.write_text(json.dumps(_pr() if payload is None else payload), encoding="utf-8")
	support = tmp / "support"
	support.mkdir()
	(support / "post_review_comment.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
	(support / "collect_pr_check_runs_context.py").write_text(
		f"import os\nfrom pathlib import Path\nPath(os.environ['PR_CHECK_RUNS_CONTEXT_FILE']).write_text({snapshot!r})\n",
		encoding="utf-8")
	ledger = tmp / "reviewer_consensus.txt"
	ledger.write_text(LEDGER_EMPTY, encoding="utf-8")
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	checks = tmp / "checks.txt"
	checks.write_text("", encoding="utf-8")
	env = {**os.environ, "PR_NUMBER": str(PR), "GITHUB_REPOSITORY": REPO, "HEAD_SHA": HEAD, "HEAD_REF": REF,
		"CLAUDE_FIXER_ROUND_INDEX": "0", "AUTOFIX_PRE_REVIEW_RESOLVE": "false", "REVIEWER_CONSENSUS_FILE": str(ledger),
		"PR_CHECK_RUNS_CONTEXT_FILE": str(checks), "SUPPORT_SCRIPTS_DIR": str(support), "GITHUB_RUN_ID": str(RUN_ID),
		"RUNTIME_DIR": str(tmp), "GITHUB_ENV": str(github_env), "REVIEWERS_SUCCESSFUL": "6", "GITHUB_SERVER_URL": "https://github.com",
		"PR_PAYLOAD_FILE": str(payload_file)}
	calls_before = len(Path(os.environ["FAKE_GH_CALLS"]).read_text().splitlines())
	# The step posts through `gh api -X POST ... --input <file>`; capture it.
	capture = tmp / "posted.json"
	(tmp / "bin").mkdir()
	(tmp / "bin" / "gh").write_text(
		"#!/usr/bin/env python3\nimport json, sys\nargs = sys.argv[1:]\n"
		f"json.dump(json.load(open(args[args.index('--input') + 1])), open({str(capture)!r}, 'w'))\n", encoding="utf-8")
	(tmp / "bin" / "gh").chmod(0o755)
	env["PATH"] = f"{tmp / 'bin'}{os.pathsep}{env['PATH']}"
	proc = subprocess.run(["bash", "-c", f'source "{HANDOFF_SCRIPT}"'], env=env, capture_output=True, text=True)
	assert proc.returncode == 0, proc.stderr
	assert "CLAUDE_FIXER_ZERO_FINDINGS" not in github_env.read_text()
	assert len(Path(os.environ["FAKE_GH_CALLS"]).read_text().splitlines()) == calls_before
	return json.loads(capture.read_text())["body"]


PR_4869_SNAPSHOT = (f"PR_CHECK_RUNS_CONTEXT\nhead_sha: {HEAD}\ncollection_status: timeout\ntotal_check_runs: 2\n"
	"failed_count: 0\nincomplete_count: 1\n\nincomplete[0].name: ci / lint\nincomplete[0].status: in_progress\n")


def test_pr_4869_sequence_auto_merges_without_a_findings_hand_off(fake_gh, tmp_path):
	body = _run_handoff_step(tmp_path, PR_4869_SNAPSHOT)
	assert "ai:claude-fixer-handoff" not in body
	ledger = hashlib.sha256(LEDGER_EMPTY.encode()).hexdigest()
	assert f"ledger={ledger}" in body
	assert f"Reviewed base: `{BASE_REF}` at `{BASE_SHA}`." in body.splitlines()
	assert (f"<!-- ai:claude-fixer-pending-checks:v2 head={HEAD} round=1 ledger={ledger} "
		f"base_sha={BASE_SHA} base_ref_sha256={_ref_digest(BASE_REF)} -->") in body.splitlines()
	comments = [_comment(5, body)]
	monkeypatch = pytest.MonkeyPatch()
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", AUTHOR)
	try:
		# 00:51:30Z: hand-off posted, CI `lint` still running — nothing happens.
		fake_gh.set(comments=comments, check_runs=RUNNING)
		summary = _sweep()
		assert summary["pending_checks_merged"] == 0 and summary["due"] == 0 and fake_gh.merges() == []
		assert summary["pending_checks_waiting"] == 1
		# 00:55:09Z: `lint` finishes green — the next sweep enables auto-merge.
		fake_gh.set(comments=comments, check_runs=GREEN)
		summary = _sweep()
	finally:
		monkeypatch.undo()
	assert summary["pending_checks_merged"] == 1 and summary["due"] == 0 and summary["pending_checks_waiting"] == 0
	assert fake_gh.merges() == [["pr", "merge", "42", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]]


def test_pr_4869_sequence_waits_for_a_forced_review_before_merging(fake_gh, tmp_path):
	# Issue #5148: the same sequence, but someone forced a new review of the
	# head (a force-review dispatch from main) before the sweep ran.
	body = _run_handoff_step(tmp_path, PR_4869_SNAPSHOT)
	comments = [_comment(5, body)]
	queued = []
	monkeypatch = pytest.MonkeyPatch()
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", AUTHOR)
	monkeypatch.setattr(sweeper.claude_fix_claim, "post_claim", lambda *a: (0, {"posted": True, "reason": "ok"}))
	try:
		# CI is green, the forced review is still running: no merge.
		fake_gh.set(comments=comments, check_runs=GREEN,
			dispatch_runs=_dispatch_runs(**{"internal-review.yml": [_dispatch(RUN_ID + 7, status="in_progress")]}))
		summary = _sweep()
		assert summary["pending_checks_merged"] == 0 and summary["errors"] == 0 and fake_gh.merges() == []
		# It found something and handed it off: a review round for a fixer, never a merge.
		fake_gh.set(comments=comments + [_comment(9, _handoff_body())], check_runs=GREEN,
			dispatch_runs=_dispatch_runs(**{"internal-review.yml": [_dispatch(RUN_ID + 7)]}))
		summary = _sweep(queue=lambda self_repo, token, repo, number, head, kind, *rest: queued.append((number, head, kind)) or 7)
	finally:
		monkeypatch.undo()
	assert summary["due"] == 1 and summary["pending_checks_merged"] == 0
	assert queued == [(PR, HEAD, "review")]
	assert fake_gh.merges() == []


def test_pr_4869_sequence_with_a_failed_check_hands_off_as_ci_failed(fake_gh, tmp_path):
	body = _run_handoff_step(tmp_path, PR_4869_SNAPSHOT)
	queued = []
	monkeypatch = pytest.MonkeyPatch()
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", AUTHOR)
	monkeypatch.setattr(sweeper.claude_fix_claim, "post_claim", lambda *a: (0, {"posted": True, "reason": "ok"}))
	try:
		fake_gh.set(comments=[_comment(5, body)], check_runs=FAILED)
		summary = _sweep(queue=lambda self_repo, token, repo, number, head, kind, *rest: queued.append((number, head, kind)) or 7)
	finally:
		monkeypatch.undo()
	assert summary["due"] == 1 and summary["pending_checks_merged"] == 0
	assert queued == [(PR, HEAD, "ci")]
	assert fake_gh.merges() == []


def test_issue_5147_retarget_after_the_pending_comment_never_merges(fake_gh, tmp_path):
	"""The finding's exploit end to end: the review of the phase PR into its project
	branch posts a pending-checks comment, the author retargets the PR to main
	without pushing, CI goes green, and the sweep still enables nothing."""
	body = _run_handoff_step(tmp_path, PR_4869_SNAPSHOT)
	monkeypatch = pytest.MonkeyPatch()
	monkeypatch.setenv("CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN", AUTHOR)
	try:
		fake_gh.set(pr=_pr(base={"ref": "main", "sha": OTHER_BASE_SHA}), comments=[_comment(5, body)], check_runs=GREEN)
		summary = _sweep()
	finally:
		monkeypatch.undo()
	assert summary["pending_checks_merged"] == 0 and summary["due"] == 0
	assert fake_gh.merges() == []


@pytest.mark.parametrize("label, payload", [
	("no payload base", {**_pr(), "base": None}),
	("payload base without a sha", {**_pr(), "base": {"ref": BASE_REF}}),
	("payload base with a short sha", {**_pr(), "base": {"ref": BASE_REF, "sha": "eeee"}}),
	("payload base without a ref", {**_pr(), "base": {"sha": BASE_SHA}}),
])
def test_handoff_without_a_reviewed_base_posts_no_pending_checks_comment(fake_gh, tmp_path, label, payload):
	"""No base to bind: fail closed to the ordinary hand-off instead of an unbound pending marker."""
	body = _run_handoff_step(tmp_path, PR_4869_SNAPSHOT, payload=payload)
	assert "ai:claude-fixer-pending-checks" not in body, label
	assert f"<!-- ai:claude-fixer-handoff:v1 kind=findings head={HEAD} round=1 -->" in body.splitlines(), label
