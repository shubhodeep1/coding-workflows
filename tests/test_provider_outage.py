"""Contract for model-provider outage handling (issue #5773).

Covers the classifier in `scripts/workflow_failure_heal.py` (`provider_unavailable`,
the fingerprint override, the cap and streak counters) and the marker, probe and
resume in `scripts/provider_outage.py`.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "provider_outage"


def _load(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


heal = _load("workflow_failure_heal", ROOT / "scripts" / "workflow_failure_heal.py")
outage = _load("provider_outage", ROOT / "scripts" / "provider_outage.py")

SELF = "shubhodeep1/coding-workflows"
CONSUMER = "shubhodeep1/consumer"
LOGIN = "workflow-bot"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
NOW = dt.datetime(2026, 9, 30, 23, 30, tzinfo=dt.timezone.utc)
OPENED = "2026-09-30T17:20:00Z"
EXCERPT = (FIXTURES / "run-36748847333-reviewer-excerpt.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_real_outage_excerpt_is_credits_402():
	result = heal.detect_provider_outage([EXCERPT])
	assert result["outage"] is True
	assert (result["provider"], result["status"], result["kind"], result["key"]) == ("openrouter", "402", "credits", "OPENROUTER_API_KEY")
	assert heal.render_provider_outage_line(result) == "provider=openrouter status=402 kind=credits key=OPENROUTER_API_KEY"


def test_openrouter_json_402_and_auth_401_classify_on_sight():
	assert heal.detect_provider_outage(['{"error":{"message":"x","code":402}}'])["kind"] == "credits"
	auth = heal.detect_provider_outage(['level=ERROR providerID=openrouter error.error="AI_APICallError: User not found."'])
	assert (auth["outage"], auth["kind"], auth["status"]) == (True, "auth", "401")
	assert heal.detect_provider_outage(["No auth credentials found"])["kind"] == "auth"


@pytest.mark.parametrize(
	("statuses", "extra", "expected"),
	[
		(["failed", "failed"], "", True),
		(["failed", "success"], "", False),
		([], "", False),
		([], "Pass 1 complete: 0 reviewers successful.", True),
	],
)
def test_rate_limit_needs_proof_that_no_reviewer_succeeded(statuses, extra, expected):
	line = 'level=ERROR providerID=openrouter error.error="AI_APICallError: Rate limit exceeded"'
	result = heal.detect_provider_outage([line, extra], status_values=statuses)
	assert result["outage"] is expected
	if expected:
		assert (result["kind"], result["status"]) == ("rate_limit", "429")


def test_server_error_parses_its_status():
	line = "providerID=openrouter statusCode: 503 Service Unavailable"
	result = heal.detect_provider_outage([line], status_values=["failed"])
	assert (result["outage"], result["kind"], result["status"]) == (True, "server", "503")
	assert heal.detect_provider_outage([line], status_values=["success"])["outage"] is False


def test_github_errors_and_echoed_step_scripts_never_classify():
	github = "gh: HTTP 401: Bad credentials (https://api.github.com/user)\nHTTP Error 429: Too Many Requests\nHTTP Error 502: Bad Gateway"
	assert heal.detect_provider_outage([github], status_values=["failed"])["outage"] is False
	echoed = (
		"2026-09-30T10:00:00.0000000Z ##[group]Run echo check\n"
		"2026-09-30T10:00:00.0000000Z \x1b[36;1mecho 'Insufficient credits / HTTP Error 402'\x1b[0m\n"
		"2026-09-30T10:00:00.0000000Z ##[endgroup]\n"
		"2026-09-30T10:00:01.0000000Z all good\n"
	)
	assert heal.detect_provider_outage([echoed])["outage"] is False


def test_evidence_line_is_redacted():
	result = heal.detect_provider_outage(["Insufficient credits Authorization: Bearer sk-or-v1-secretvalue"])
	assert result["outage"] and "secretvalue" not in result["evidence"]


def test_read_provider_logs_reads_dirs_and_status_files(tmp_path):
	(tmp_path / "pass1_a.log").write_text(EXCERPT, encoding="utf-8")
	(tmp_path / "status_pass1_a.txt").write_text("failed\n", encoding="utf-8")
	(tmp_path / "ignored.json").write_text("Insufficient credits", encoding="utf-8")
	texts, statuses = heal.read_provider_logs([str(tmp_path), str(tmp_path / "missing")], [])
	assert statuses == ["failed"] and len(texts) == 1


def test_status_files_are_read_even_when_logs_exhaust_the_budget(tmp_path, monkeypatch):
	monkeypatch.setattr(heal, "PROVIDER_LOG_MAX_TOTAL_BYTES", 10)
	monkeypatch.setattr(heal, "PROVIDER_LOG_MAX_FILES", 1)
	# "a_…" and "r_…" sort before "status_…"; they alone exceed both caps.
	(tmp_path / "a_reviewer.log").write_text("x" * 100 + "\nHTTP 429 rate limit openrouter\n", encoding="utf-8")
	(tmp_path / "r_reviewer.log").write_text("y" * 100, encoding="utf-8")
	(tmp_path / "status_pass1_a.txt").write_text("failed\n", encoding="utf-8")
	(tmp_path / "status_pass1_b.txt").write_text("success\n", encoding="utf-8")
	texts, statuses = heal.read_provider_logs([str(tmp_path)], [])
	assert statuses == ["failed", "success"] and len(texts) == 1
	assert heal.detect_provider_outage(texts, status_values=statuses)["zero_success"] is False


def _fingerprint(tmp_path, *extra):
	return subprocess.run(
		[sys.executable, str(ROOT / "scripts" / "workflow_failure_heal.py"), "autofix-failure-fingerprint",
			"--head-sha", HEAD, "--run-id", "36748847333", *extra],
		capture_output=True, text=True, check=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
	).stdout


def test_fingerprint_names_a_provider_outage_over_an_explicit_reason(tmp_path):
	logs = tmp_path / "previous_reviews"
	logs.mkdir()
	(logs / "pass1_openai_gpt-6-luna.log").write_text(EXCERPT, encoding="utf-8")
	(logs / "status_pass1_openai_gpt-6-luna.txt").write_text("failed\n", encoding="utf-8")
	out = _fingerprint(tmp_path, "--failure-reason", "reviewers_failed", "--provider-log-dir", str(logs),
		"--provider-log-dir", str(tmp_path / "absent"))
	assert "reason=provider_unavailable" in out
	assert "provider_outage=provider=openrouter status=402 kind=credits key=OPENROUTER_API_KEY" in out
	assert f"marker=<!-- review-autofix-failure:v1 head={HEAD} reason=provider_unavailable " in out
	plain = _fingerprint(tmp_path, "--failure-reason", "reviewers_failed")
	assert "reason=reviewers_failed" in plain and "provider_outage=" not in plain


def _marker_comment(comment_id, reason, fp="f" * 64, run="1", head=HEAD, login=LOGIN, title="**AI review/autofix failed — needs human intervention**"):
	return {"id": comment_id, "user": {"login": login}, "created_at": "2026-09-30T18:00:00Z",
		"body": f"{title}\n\n<!-- review-autofix-failure:v1 head={head} reason={reason} fp={fp} degraded=0 run={run} -->"}


def test_outage_markers_never_count_toward_the_identical_failure_cap():
	only_outage = [_marker_comment(i, "provider_unavailable", run=str(i), title="**AI review/autofix paused: model provider unavailable**")
		for i in range(1, 6)]
	assert heal.count_identical_failures(only_outage, head_sha=HEAD, author_login=LOGIN)["count"] == 0
	mixed = [
		_marker_comment(1, "workflow_failure", run="1"),
		_marker_comment(2, "workflow_failure", run="2"),
		_marker_comment(3, "provider_unavailable", fp="e" * 64, run="3", title="**AI review/autofix paused: model provider unavailable**"),
	]
	result = heal.count_identical_failures(mixed, head_sha=HEAD, author_login=LOGIN)
	assert (result["count"], result["reason"]) == (2, "workflow_failure")


def test_outage_comments_never_count_toward_the_heal_streak():
	comments = [_marker_comment(i, "provider_unavailable", run=str(i)) for i in range(1, 4)]
	assert heal.count_autofix_failure_streak(comments) == 0
	comments.insert(0, _marker_comment(9, "workflow_failure", run="9"))
	assert heal.count_autofix_failure_streak(comments) == 1


def test_parse_provider_outage_line():
	text = "failure_reason=provider_unavailable\nprovider_outage provider=openrouter status=402 kind=credits key=OPENROUTER_API_KEY\n"
	assert heal.parse_provider_outage_line(text) == {"provider": "openrouter", "status": "402", "kind": "credits", "key": "OPENROUTER_API_KEY"}
	assert heal.parse_provider_outage_line("provider_outage provider=x status=abc kind=credits key=K") is None


# ---------------------------------------------------------------------------
# Fake GitHub
# ---------------------------------------------------------------------------


class FakeGitHub:
	def __init__(self, lists=None, objects=None, fail=()):
		self.lists = {key: list(value) for key, value in (lists or {}).items()}
		self.objects = dict(objects or {})
		self.fail = set(fail)
		self.calls: list[tuple] = []
		self.next_issue = 100

	def _check(self, key):
		if key in self.fail:
			raise outage.GitHubError(f"gh api {key} failed: HTTP 500")

	def get(self, path):
		self.calls.append(("GET", path))
		self._check(path)
		return self.objects.get(path)

	def get_list(self, path, list_key=None):
		self.calls.append(("LIST", path))
		self._check(path)
		return list(self.lists.get(path, []))

	def send(self, method, path, body=None):
		self.calls.append((method, path, body))
		self._check((method, path))
		if method == "POST" and path == f"repos/{SELF}/issues":
			number = self.next_issue
			issue = {"number": number, "state": "open", "user": {"login": LOGIN}, "body": body["body"],
				"labels": [{"name": name} for name in body["labels"]], "created_at": "2026-09-30T17:20:00Z",
				"html_url": f"https://github.com/{SELF}/issues/{number}"}
			self.lists.setdefault(f"repos/{SELF}/issues?labels={outage.OUTAGE_LABEL}&state=open", []).append(issue)
			return issue
		if method == "PATCH" and path.startswith(f"repos/{SELF}/issues/"):
			number = int(path.rsplit("/", 1)[1])
			key = f"repos/{SELF}/issues?labels={outage.OUTAGE_LABEL}&state=open"
			self.lists[key] = [issue for issue in self.lists.get(key, []) if issue["number"] != number]
		return None

	def sent(self, method, fragment=""):
		return [call for call in self.calls if call[0] == method and fragment in call[1]]


def _marker_issue(number=10, login=LOGIN, label=outage.OUTAGE_LABEL, body=None, created_at=OPENED):
	return {"number": number, "state": "open", "user": {"login": login}, "labels": [{"name": label}],
		"created_at": created_at, "html_url": f"https://github.com/{SELF}/issues/{number}",
		"body": body if body is not None else outage.render_marker_body(provider="openrouter", status="402", kind="credits",
			key="OPENROUTER_API_KEY", source=f"{SELF}#5324 (review run 1)", opened_at=OPENED)}


MARKERS = f"repos/{SELF}/issues?labels={outage.OUTAGE_LABEL}&state=open"


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


def test_marker_body_round_trips_and_names_the_key_not_its_value():
	body = _marker_issue()["body"]
	assert outage.parse_marker(body) == {"provider": "openrouter", "status": "402", "kind": "credits", "key": "OPENROUTER_API_KEY"}
	assert "OPENROUTER_API_KEY" in body and "sk-or" not in body


def test_trusted_open_markers_ignore_other_authors_labels_and_bodies():
	issues = [
		_marker_issue(12),
		_marker_issue(11, login="someone-else"),
		_marker_issue(13, label="bug"),
		_marker_issue(14, body="no marker"),
		{**_marker_issue(15), "pull_request": {}},
		_marker_issue(10),
	]
	assert [marker["number"] for marker in outage.trusted_open_markers(issues, LOGIN)] == [10, 12]
	assert outage.trusted_open_markers(issues, "") == []


def test_record_opens_one_marker_and_alerts_once():
	gh = FakeGitHub(objects={"user": {"login": LOGIN}})
	first = outage.record(gh, SELF, provider="openrouter", status="402", kind="credits", key="OPENROUTER_API_KEY",
		source=f"{SELF}#5324 (review run 1)", now=NOW)
	assert first["action"] == "created" and first["alert"] is True
	assert "openrouter HTTP 402 (credits) on key OPENROUTER_API_KEY" in first["alert_text"]
	assert gh.sent("POST", "/labels") and len(gh.sent("POST", f"repos/{SELF}/issues")) == 1
	second = outage.record(gh, SELF, provider="openrouter", status="402", kind="credits", key="OPENROUTER_API_KEY",
		source=f"{CONSUMER}#7 (review run 2)", now=NOW)
	assert second == {"action": "exists", "issue": first["issue"], "alert": False}
	assert len(gh.sent("POST", f"repos/{SELF}/issues")) == 1


def test_record_closes_its_own_duplicate_after_a_race():
	gh = FakeGitHub(objects={"user": {"login": LOGIN}})
	gh.next_issue = 11
	original_send = gh.send

	def racing_send(method, path, body=None):
		created = original_send(method, path, body)
		if method == "POST" and path == f"repos/{SELF}/issues":
			# A concurrent intake created #10 first.
			gh.lists[MARKERS].insert(0, _marker_issue(10))
		return created

	gh.send = racing_send
	result = outage.record(gh, SELF, provider="openrouter", status="402", kind="credits", key="OPENROUTER_API_KEY",
		source="x", now=NOW)
	assert result == {"action": "duplicate_closed", "issue": 10, "alert": False}
	assert ("PATCH", f"repos/{SELF}/issues/11", {"state": "closed", "state_reason": "not_planned"}) in gh.calls


def test_record_only_if_open_never_creates_and_records_release_runs_once():
	gh = FakeGitHub(objects={"user": {"login": LOGIN}})
	release = {"id": 36760421499, "workflow": "Test & Mark Stable Release", "url": "https://x/runs/36760421499"}
	assert outage.record(gh, SELF, provider="openrouter", status="", kind="", key="OPENROUTER_API_KEY", source="release",
		now=NOW, release_run=release, only_if_open=True) == {"action": "none", "alert": False}
	assert not gh.sent("POST")
	gh.lists[MARKERS] = [_marker_issue(10)]
	result = outage.record(gh, SELF, provider="openrouter", status="", kind="", key="OPENROUTER_API_KEY", source="release",
		now=NOW, release_run=release, only_if_open=True)
	assert (result["action"], result["release_run"]) == ("exists", "recorded")
	comment = gh.sent("POST", "/issues/10/comments")[0][2]["body"]
	gh.lists[f"repos/{SELF}/issues/10/comments"] = [{"id": 1, "user": {"login": LOGIN}, "body": comment}]
	again = outage.record(gh, SELF, provider="openrouter", status="", kind="", key="OPENROUTER_API_KEY", source="release",
		now=NOW, release_run=release, only_if_open=True)
	assert again["release_run"] == "already_recorded"
	assert outage.recorded_release_runs(gh.lists[f"repos/{SELF}/issues/10/comments"], LOGIN) == [36760421499]


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class _Response(io.BytesIO):
	status = 200

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


def test_probe_counts_only_http_200_as_recovered():
	sent = {}

	def ok_opener(request, timeout):
		sent["auth"] = request.get_header("Authorization")
		sent["body"] = json.loads(request.data)
		return _Response(b"{}")

	assert outage.probe_provider("m/x", "key-value", opener=ok_opener) == {"ok": True, "status": "200"}
	assert sent["body"]["max_tokens"] == 1 and sent["auth"] == "Bearer key-value"

	def payment_opener(request, timeout):
		raise urllib.error.HTTPError(outage.PROBE_URL, 402, "Payment Required", {}, io.BytesIO(b""))

	assert outage.probe_provider("m/x", "key-value", opener=payment_opener) == {"ok": False, "status": "402"}

	def network_opener(request, timeout):
		raise urllib.error.URLError("down")

	assert outage.probe_provider("m/x", "key-value", opener=network_opener)["status"] == "network_error"
	assert outage.probe_provider("m/x", "", opener=ok_opener) == {"ok": False, "status": "missing_key"}


# ---------------------------------------------------------------------------
# Tick and resume
# ---------------------------------------------------------------------------


def _pr(number, *, head=HEAD, updated="2026-09-30T19:00:00Z", labels=(), body="", draft=False, author=LOGIN):
	return {"number": number, "draft": draft, "updated_at": updated, "title": f"PR {number}", "body": body,
		"head": {"sha": head, "ref": f"claude/pr-{number}"}, "user": {"login": author},
		"labels": [{"name": name} for name in labels], "base": {"repo": {"default_branch": "main"}}}


def _outage_failure(comment_id, run, head=HEAD):
	return _marker_comment(comment_id, "provider_unavailable", run=str(run), head=head,
		title="**AI review/autofix paused: model provider unavailable**")


def _hold(comment_id, head=HEAD, created="2026-09-30T20:36:46Z", login=LOGIN):
	return {"id": comment_id, "user": {"login": login}, "author_association": "OWNER", "created_at": created,
		"body": f"**Claude fixes on hold**\n\n<!-- ai:claude-fix-claim:v1 head={head} kind=hold by=session_x -->"}


def _check(run_id, conclusion="failure"):
	return {"name": "review / codex-agent", "status": "completed", "conclusion": conclusion,
		"details_url": f"https://github.com/{SELF}/actions/runs/{run_id}/job/1"}


def _labeled(created, actor=LOGIN):
	return {"event": "labeled", "label": {"name": "ai:review-blocked"}, "actor": {"login": actor}, "created_at": created}


def _recovery_github(release_runs=()):
	marker_comments = [
		{"id": 1, "user": {"login": LOGIN}, "body": outage.render_release_run_comment(run_id, "Test & Mark Stable Release", "u")}
		for run_id in release_runs
	]
	lists = {
		MARKERS: [_marker_issue(10)],
		f"repos/{SELF}/issues/10/comments": marker_comments,
		f"repos/{SELF}/pulls?state=open": [
			_pr(1, labels=("ai:review-blocked",), body="Refs #50\nRefs #51"),
			_pr(2),
			_pr(3),
			_pr(4, draft=True),
			_pr(5, updated="2026-09-30T12:00:00Z"),
			_pr(6),
		],
		f"repos/{SELF}/issues/1/comments": [_outage_failure(11, 501), _outage_failure(12, 502), _hold(13)],
		f"repos/{SELF}/issues/2/comments": [_marker_comment(21, "workflow_failure", run="600")],
		f"repos/{SELF}/issues/3/comments": [_outage_failure(31, 701),
			{"id": 32, "user": {"login": LOGIN}, "body": "AI autofix editor summary\n\nChanges made: x"}],
		f"repos/{SELF}/issues/6/comments": [_outage_failure(61, 801), _hold(62)],
		f"repos/{SELF}/commits/{HEAD}/check-runs": [_check(501), _check(1, conclusion="skipped")],
		f"repos/{SELF}/issues/1/events": [_labeled("2026-09-30T18:00:00Z")],
		f"repos/{SELF}/issues?labels=ai:review-blocked&state=open": [
			{"number": 50}, {"number": 51}, {"number": 52}, {"number": 1, "pull_request": {}},
		],
		f"repos/{SELF}/issues/50/events": [_labeled("2026-09-30T18:05:00Z")],
		f"repos/{SELF}/issues/51/events": [_labeled("2026-09-30T10:00:00Z")],
		f"repos/{CONSUMER}/pulls?state=open": [_pr(7)],
		f"repos/{CONSUMER}/issues/7/comments": [_outage_failure(71, 901)],
	}
	objects = {"user": {"login": LOGIN}, f"repos/{SELF}/pulls/1": {"mergeable_state": "clean"},
		f"repos/{SELF}/pulls/6": {"mergeable_state": "clean"}}
	return FakeGitHub(lists=lists, objects=objects)


def _tick(gh, *, ok=True, rerun=False):
	return outage.tick(gh, SELF, [SELF, CONSUMER], NOW, probe=lambda: {"ok": ok, "status": "200" if ok else "402"},
		release_rerun_enabled=rerun, allow_workflow_edits="true", run_id="999")


def test_tick_without_a_marker_costs_one_read():
	gh = FakeGitHub()
	assert _tick(gh) == {"outage_open": False, "skip_review_dispatch": False}
	assert gh.calls == [("LIST", MARKERS)]


def test_tick_fails_open_when_the_marker_list_fails():
	gh = FakeGitHub(fail={MARKERS})
	result = _tick(gh)
	assert result["outage_open"] is False and result["skip_review_dispatch"] is False and "error" in result


def test_tick_skips_the_sweep_while_the_probe_fails():
	gh = _recovery_github()
	result = _tick(gh, ok=False)
	assert result["outage_open"] is True and result["skip_review_dispatch"] is True and "recovered" not in result
	assert not gh.sent("POST") and not gh.sent("DELETE") and not gh.sent("PATCH")


def test_recovery_resumes_only_what_the_outage_paused():
	gh = _recovery_github(release_runs=(36760421499,))
	result = _tick(gh)
	summary = result["summary"]
	assert result["recovered"] is True and result["skip_review_dispatch"] is True
	# Re-dispatch: PR 1 and 6 here (internal-review.yml), PR 7 in the consumer (ai-review.yml).
	assert summary["dispatched"] == [f"{SELF}#1", f"{SELF}#6", f"{CONSUMER}#7"]
	dispatches = gh.sent("POST", "/dispatches")
	assert dispatches[0][1] == f"repos/{SELF}/actions/workflows/internal-review.yml/dispatches"
	assert dispatches[0][2] == {"ref": "main", "inputs": {"pr_number": "1", "allow_workflow_edits": "true"}}
	assert dispatches[2] == ("POST", f"repos/{CONSUMER}/actions/workflows/ai-review.yml/dispatches",
		{"ref": "main", "inputs": {"pr_number": "7"}})
	# Labels: the PR's and issue #50's were applied in the window by the workflow account; #51's before it, #52 unreferenced.
	assert summary["labels_removed"] == [f"{SELF}#1", f"{SELF}#50"]
	assert {call[1] for call in gh.sent("DELETE")} == {f"repos/{SELF}/issues/1/labels/ai:review-blocked",
		f"repos/{SELF}/issues/50/labels/ai:review-blocked"}
	# Holds: PR 1's only failed check is its outage run 501; PR 6's failed check (run 501) is not one of its outage runs.
	assert summary["holds_released"] == [f"{SELF}#1"]
	release_claim = gh.sent("POST", f"repos/{SELF}/issues/1/comments")[0][2]["body"]
	assert f"<!-- ai:claude-fix-claim:v1 head={HEAD} kind=review by=provider-outage-probe-999 -->" in release_claim
	# Release run reported, not re-run (opt-in off), then the marker is recorded and closed.
	assert "not re-run" in summary["release"] and not gh.sent("POST", "/rerun-failed-jobs")
	resume = gh.sent("POST", f"repos/{SELF}/issues/10/comments")[0][2]["body"]
	assert f"pr:{SELF}#1@{HEAD[:12]}" in resume and "release:36760421499" in resume
	assert ("PATCH", f"repos/{SELF}/issues/10", {"state": "closed", "state_reason": "completed"}) in gh.calls
	assert result["alert_text"].startswith("Model provider recovered: openrouter")
	assert "Re-dispatched 3 review(s), removed 2 outage label(s), released 1 hold(s)" in result["alert_text"]


def test_recovery_reruns_the_newest_release_run_only_when_enabled():
	gh = _recovery_github(release_runs=(36760000001, 36760421499))
	summary = _tick(gh, rerun=True)["summary"]
	assert summary["release"] == "run 36760421499 re-run"
	assert gh.sent("POST", "/rerun-failed-jobs")[0][1] == f"repos/{SELF}/actions/runs/36760421499/rerun-failed-jobs"


def test_a_second_tick_does_not_repeat_handled_work():
	gh = _recovery_github()
	_tick(gh)
	resume = gh.sent("POST", f"repos/{SELF}/issues/10/comments")[0][2]["body"]
	# The close failed last time: the marker is still open and carries the resume record.
	gh.lists[MARKERS] = [_marker_issue(10)]
	gh.lists[f"repos/{SELF}/issues/10/comments"] = [{"id": 5, "user": {"login": LOGIN}, "body": resume}]
	gh.calls.clear()
	summary = _tick(gh)["summary"]
	assert summary["dispatched"] == [] and summary["labels_removed"] == [] and summary["holds_released"] == []
	assert not gh.sent("POST", "/dispatches") and not gh.sent("DELETE")


def test_a_failing_consumer_repo_is_reported_and_does_not_stop_the_resume():
	gh = _recovery_github()
	gh.fail.add(f"repos/{CONSUMER}/pulls?state=open")
	result = _tick(gh)
	assert result["recovered"] is True and result["marker_closed"] is False and result["skip_review_dispatch"] is True
	assert result["summary"]["errors"] and CONSUMER in result["summary"]["errors"][0]
	# The rest of the resume still ran and was recorded; the marker stays open for a retry, with no alert yet.
	assert result["summary"]["dispatched"] == [f"{SELF}#1", f"{SELF}#6"]
	resume = gh.sent("POST", f"repos/{SELF}/issues/10/comments")[0][2]["body"]
	assert f"pr:{SELF}#1@{HEAD[:12]}" in resume and "resume attempt 1 of 3" in resume
	assert not gh.sent("PATCH") and "alert_text" not in result


def _retry_marker_comments(gh, *bodies):
	gh.lists[f"repos/{SELF}/issues/10/comments"] = [
		{"id": index + 5, "user": {"login": LOGIN}, "body": body} for index, body in enumerate(bodies)
	]
	gh.calls.clear()


def test_a_retry_tick_redoes_only_the_failed_work_and_closes_when_it_succeeds():
	gh = _recovery_github()
	gh.fail.add(f"repos/{CONSUMER}/pulls?state=open")
	_tick(gh)
	_retry_marker_comments(gh, gh.sent("POST", f"repos/{SELF}/issues/10/comments")[0][2]["body"])
	gh.fail.clear()
	result = _tick(gh)
	assert result["summary"]["dispatched"] == [f"{CONSUMER}#7"] and not result["summary"]["errors"]
	assert result["marker_closed"] is True
	assert ("PATCH", f"repos/{SELF}/issues/10", {"state": "closed", "state_reason": "completed"}) in gh.calls
	assert result["alert_text"].startswith("Model provider recovered: openrouter")


def test_the_last_resume_attempt_closes_the_marker_and_names_the_errors():
	gh = _recovery_github()
	gh.fail.add(f"repos/{CONSUMER}/pulls?state=open")
	earlier = []
	for _ in range(outage.MAX_RESUME_ATTEMPTS - 1):
		result = _tick(gh)
		assert result["marker_closed"] is False
		earlier.append(gh.sent("POST", f"repos/{SELF}/issues/10/comments")[-1][2]["body"])
		_retry_marker_comments(gh, *earlier)
	result = _tick(gh)
	assert result["marker_closed"] is True
	assert ("PATCH", f"repos/{SELF}/issues/10", {"state": "closed", "state_reason": "completed"}) in gh.calls
	assert "Not resumed: 1 error(s)" in result["alert_text"]
	resume = gh.sent("POST", f"repos/{SELF}/issues/10/comments")[-1][2]["body"]
	assert f"closed after {outage.MAX_RESUME_ATTEMPTS} resume attempts with errors left" in resume


def test_resume_attempts_count_only_the_workflow_accounts_resume_comments():
	resume = outage.render_resume_comment({"recovered_at": "t", "dispatched": [], "labels_removed": [],
		"holds_released": [], "release": "none", "errors": []}, [])
	comments = [{"id": 1, "user": {"login": LOGIN}, "body": resume}, {"id": 2, "user": {"login": "someone"}, "body": resume},
		{"id": 3, "user": {"login": LOGIN}, "body": "unrelated"}]
	assert outage.resume_attempts(comments, LOGIN) == 1
	assert outage.resume_attempts(comments, "") == 0


def test_the_pr_that_first_hit_the_outage_is_resumed_although_it_predates_the_marker():
	gh = _recovery_github()
	# PR 8 failed (and was last updated) ten minutes before the intake opened the marker at 17:20.
	gh.lists[f"repos/{SELF}/pulls?state=open"].append(_pr(8, updated="2026-09-30T17:10:00Z"))
	gh.lists[f"repos/{SELF}/issues/8/comments"] = [_outage_failure(81, 1001)]
	# PR 9 also failed for the provider but was last updated beyond the lookback.
	gh.lists[f"repos/{SELF}/pulls?state=open"].append(_pr(9, updated="2026-09-30T11:00:00Z"))
	gh.lists[f"repos/{SELF}/issues/9/comments"] = [_outage_failure(91, 1002)]
	summary = _tick(gh)["summary"]
	assert f"{SELF}#8" in summary["dispatched"] and f"{SELF}#9" not in summary["dispatched"]
	assert ("LIST", f"repos/{SELF}/issues/9/comments") not in gh.calls


def test_a_hold_is_kept_while_github_has_not_computed_the_merge_state():
	gh = _recovery_github()
	gh.objects[f"repos/{SELF}/pulls/1"] = {"mergeable_state": "unknown", "mergeable": None}
	# PR 6's hold has another cause (a failed check that is not its outage run): no retry is needed for it.
	gh.objects[f"repos/{SELF}/pulls/6"] = {"mergeable_state": "unknown", "mergeable": None}
	result = _tick(gh)
	assert result["summary"]["holds_released"] == []
	assert [error for error in result["summary"]["errors"] if "not computed yet" in error] == [
		f"{SELF}#1: merge state of PR #1 not computed yet; hold kept for the next tick"]
	assert not gh.sent("POST", f"repos/{SELF}/issues/1/comments") and result["marker_closed"] is False


def test_a_hold_is_kept_when_github_reports_the_pr_unmergeable():
	gh = _recovery_github()
	gh.objects[f"repos/{SELF}/pulls/1"] = {"mergeable_state": "blocked", "mergeable": False}
	result = _tick(gh)
	assert result["summary"]["holds_released"] == [] and not result["summary"]["errors"]


def test_a_failed_release_rerun_is_an_error_and_is_retried():
	gh = _recovery_github(release_runs=(36760421499,))
	gh.fail.add(("POST", f"repos/{SELF}/actions/runs/36760421499/rerun-failed-jobs"))
	result = _tick(gh, rerun=True)
	assert result["summary"]["release"].startswith("run 36760421499 re-run failed")
	assert any(error.startswith("release run 36760421499") for error in result["summary"]["errors"])
	resume = gh.sent("POST", f"repos/{SELF}/issues/10/comments")[0][2]["body"]
	assert "release:36760421499" not in resume and result["marker_closed"] is False


def test_hold_release_rules():
	window = dt.datetime(2026, 9, 30, 17, 0, tzinfo=dt.timezone.utc)
	hold = {"kind": "hold", "at": "2026-09-30T20:00:00Z"}
	assert outage.hold_is_outage_only(hold, window_start=window, failed_check_run_ids={"1"}, outage_runs={"1", "2"}, conflicted=False)
	assert not outage.hold_is_outage_only(hold, window_start=window, failed_check_run_ids={"3"}, outage_runs={"1"}, conflicted=False)
	assert not outage.hold_is_outage_only(hold, window_start=window, failed_check_run_ids=set(), outage_runs=set(), conflicted=True)
	assert not outage.hold_is_outage_only({"kind": "hold", "at": "2026-09-30T16:00:00Z"}, window_start=window,
		failed_check_run_ids=set(), outage_runs=set(), conflicted=False)
	assert not outage.hold_is_outage_only({"kind": "ci", "at": "2026-09-30T20:00:00Z"}, window_start=window,
		failed_check_run_ids=set(), outage_runs=set(), conflicted=False)


def test_label_rule_needs_the_window_and_the_workflow_account():
	window = dt.datetime(2026, 9, 30, 17, 0, tzinfo=dt.timezone.utc)
	rule = lambda events: outage.label_applied_by_outage(events, label="ai:review-blocked", window_start=window, workflow_login=LOGIN)  # noqa: E731
	assert rule([_labeled("2026-09-30T18:00:00Z")])
	assert not rule([_labeled("2026-09-30T16:00:00Z")])
	assert not rule([_labeled("2026-09-30T18:00:00Z", actor="a-human")])
	assert rule([_labeled("2026-09-30T10:00:00Z"), _labeled("2026-09-30T18:00:00Z")])
	assert not rule([])


def test_cli_record_reads_the_reporters_evidence_line(tmp_path, monkeypatch):
	evidence = tmp_path / "evidence.txt"
	evidence.write_text("failure_reason=provider_unavailable\nprovider_outage provider=openrouter status=402 kind=credits key=OPENROUTER_API_KEY\n", encoding="utf-8")
	gh = FakeGitHub(objects={"user": {"login": LOGIN}})
	monkeypatch.setattr(outage, "GitHub", lambda: gh)
	monkeypatch.setattr(outage, "_now", lambda: NOW)
	assert outage.main(["record", "--repo", SELF, "--source", "x", "--evidence-file", str(evidence)]) == 0
	created = gh.sent("POST", f"repos/{SELF}/issues")[0][2]
	assert created["title"] == "Model provider outage: openrouter HTTP 402 (credits)"
	assert created["labels"] == [outage.OUTAGE_LABEL]


def test_cli_status_prints_the_open_marker(monkeypatch, capsys):
	gh = FakeGitHub(lists={MARKERS: [_marker_issue(10)]}, objects={"user": {"login": LOGIN}})
	monkeypatch.setattr(outage, "GitHub", lambda: gh)
	assert outage.main(["status", "--repo", SELF]) == 0
	printed = json.loads(capsys.readouterr().out)
	assert printed["outage_open"] is True and printed["markers"][0]["number"] == 10
	assert printed["markers"][0]["kind"] == "credits"
	assert not [call for call in gh.calls if call[0] not in ("GET", "LIST")]
	empty = FakeGitHub()
	monkeypatch.setattr(outage, "GitHub", lambda: empty)
	assert outage.main(["status", "--repo", SELF]) == 0
	assert json.loads(capsys.readouterr().out) == {"outage_open": False, "markers": []}


def test_cli_rejects_bad_input(capsys):
	assert outage.main(["record", "--repo", "not-a-slug", "--source", "x", "--status", "402", "--kind", "credits"]) == 2
	assert outage.main(["record", "--repo", SELF, "--source", "x", "--release-run-id", "12; rm", "--status", "402", "--kind", "credits"]) == 2
	assert outage.main(["record", "--repo", SELF, "--source", "x"]) == 2
	assert "error" in capsys.readouterr().out
