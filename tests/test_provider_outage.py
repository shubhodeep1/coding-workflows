"""Model-provider outage handling (issue #6633, re-issue of #5773).

Covers the classifier and live probe, the identical-failure cap / streak
exemption for `provider_unavailable`, the ai:provider-outage tracker, and an
end-to-end replay of the 2026-09-30 OpenRouter credit outage: no cap, no
label, one alert, then re-dispatch and marker-backed clean-up on recovery.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import provider_outage as po  # noqa: E402
import workflow_failure_heal as wfh  # noqa: E402

LOGIN = "pipeline-bot"
REPO = "o/r"
HEAD = "a" * 40
FP = "b" * 64


def _marker(reason: str, run: int, head: str = HEAD, fp: str = FP) -> str:
	return wfh.render_failure_marker(head, reason, fp, False, str(run))


def _comment(body: str, *, login: str = LOGIN, created: str = "2026-09-30T18:00:00Z") -> dict:
	return {"user": {"login": login}, "author": {"login": login}, "body": body, "created_at": created, "createdAt": created}


# ---------------------------------------------------------------------------
# Classifier and probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	("line", "status"),
	[
		("LLM keyword extraction failed on attempt 1/3: HTTP Error 402: Payment Required", "402"),
		("error: Insufficient credits. Add more using https://openrouter.ai/settings/credits", "402"),
		('{"error":{"code":402,"message":"Insufficient credits"}}', "402"),
		("HTTP Error 401: Unauthorized", "401"),
		("HTTP 429 Too Many Requests", "429"),
		('{"error":{"code":503,"message":"upstream"}}', "5xx"),
		("HTTP Error 502: Bad Gateway", "5xx"),
	],
)
def test_classifier_recognises_provider_errors(line: str, status: str) -> None:
	assert po.provider_failure_status([line]) == {"provider": "openrouter", "status": status}


def test_classifier_ignores_untrusted_and_diff_lines() -> None:
	text = "UNTRUSTED_DATA: HTTP Error 402: Payment Required\n+ raise Error('402 Payment Required')\n- insufficient credits\n"
	assert po.provider_failure_status([text]) is None
	assert po.provider_failure_status(["all good"]) is None


def test_classify_requires_a_failing_probe() -> None:
	texts = ["HTTP Error 402: Payment Required"]
	assert po.classify(texts, lambda: {"outcome": "up", "status": "200"})["reason"] == "none"
	assert po.classify(texts, lambda: {"outcome": "unknown", "status": "network"})["reason"] == "none"
	result = po.classify(texts, lambda: {"outcome": "down", "status": "402"})
	assert result == {"reason": "provider_unavailable", "provider": "openrouter", "status": "402"}
	assert po.classify(["nothing"], lambda: pytest.fail("probe must not run without evidence"))["reason"] == "none"


def test_probe_openrouter_outcomes() -> None:
	def http_factory(key_code: int, key_body: dict, completion_code: int):
		def http(method, url, headers, body):
			assert headers["Authorization"] == "Bearer sk-test"
			if method == "GET":
				return key_code, json.dumps(key_body).encode()
			return completion_code, b"{}"
		return http

	assert po.probe_openrouter("sk-test", "m", http_factory(200, {"data": {"limit_remaining": None}}, 200))["outcome"] == "up"
	assert po.probe_openrouter("sk-test", "m", http_factory(200, {"data": {"limit_remaining": 0}}, 200)) == {"outcome": "down", "status": "402"}
	assert po.probe_openrouter("sk-test", "m", http_factory(401, {}, 200)) == {"outcome": "down", "status": "401"}
	assert po.probe_openrouter("sk-test", "m", http_factory(200, {}, 402)) == {"outcome": "down", "status": "402"}
	assert po.probe_openrouter("sk-test", "m", http_factory(200, {}, 503)) == {"outcome": "down", "status": "5xx"}
	assert po.probe_openrouter("", "m")["outcome"] == "unknown"

	def boom(*_args):
		raise OSError("network")

	assert po.probe_openrouter("sk-test", "m", boom)["outcome"] == "unknown"


def test_classify_cli_never_prints_key(tmp_path: Path) -> None:
	log = tmp_path / "r.log"
	log.write_text("HTTP Error 402: Payment Required\n")
	cache = tmp_path / "probe.json"
	cache.write_text(json.dumps({"outcome": "down", "status": "402"}))
	result = subprocess.run(
		[sys.executable, str(REPO_ROOT / "scripts" / "provider_outage.py"), "classify", "--log-file", str(log), "--probe-cache", str(cache)],
		capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "OPENROUTER_API_KEY": "sk-secret-value"},
	)
	assert result.stdout.splitlines()[0] == "reason=provider_unavailable provider=openrouter status=402"
	assert "sk-secret-value" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Cap and streak exemption
# ---------------------------------------------------------------------------


def test_provider_markers_do_not_raise_identical_failure_count() -> None:
	comments = [_comment(f"**AI review/autofix paused — model provider unavailable**\n\n{_marker('provider_unavailable', run)}") for run in range(1, 6)]
	result = wfh.count_identical_failures(comments, head_sha=HEAD, author_login=LOGIN)
	assert result["count"] == 0
	assert result["cap_applied"] is False


def test_provider_markers_do_not_break_real_failure_run() -> None:
	comments = [
		_comment(_marker("workflow_failure", 1)),
		_comment(_marker("provider_unavailable", 2)),
		_comment(_marker("workflow_failure", 3)),
	]
	result = wfh.count_identical_failures(comments, head_sha=HEAD, author_login=LOGIN)
	assert result["count"] == 2
	assert result["reason"] == "workflow_failure"


def test_streak_skips_provider_comments() -> None:
	comments = [
		_comment("**AI review/autofix failed — needs human intervention**"),
		_comment("**AI review/autofix paused — model provider unavailable**\n\n" + _marker("provider_unavailable", 9)),
	]
	assert wfh.count_autofix_failure_streak(comments) == 1


def test_fingerprint_cli_names_provider_unavailable(tmp_path: Path) -> None:
	result = subprocess.run(
		[sys.executable, str(REPO_ROOT / "scripts" / "workflow_failure_heal.py"), "autofix-failure-fingerprint", "--failure-reason", "provider_unavailable", "--head-sha", HEAD, "--run-id", "7"],
		capture_output=True, text=True, check=True,
	)
	assert "reason=provider_unavailable" in result.stdout
	assert "reason=provider_unavailable fp=" in result.stdout


# ---------------------------------------------------------------------------
# Fake GitHub
# ---------------------------------------------------------------------------


class FakeGitHub:
	def __init__(self) -> None:
		self.repository = REPO
		self.issues: dict[int, dict] = {}
		self.next_number = 1000
		self.prs: list[dict] = []
		self.dispatched: list[int] = []
		self.fail_dispatch: set[int] = set()
		self.reruns: list[int] = []
		self.removed: list[int] = []

	def login(self) -> str:
		return LOGIN

	def list_issues(self, label: str):
		return [
			{"number": n, "body": i["body"], "user": {"login": i["author"]}, "created_at": i["created_at"]}
			for n, i in sorted(self.issues.items()) if i["state"] == "open" and label in i["labels"]
		]

	def create_issue(self, title: str, body: str):
		self.next_number += 1
		self.issues[self.next_number] = {"title": title, "body": body, "labels": {po.TRACKER_LABEL}, "state": "open", "author": LOGIN, "comments": [], "created_at": "2026-09-30T17:20:00Z"}
		return self.next_number

	def ensure_label(self) -> None:
		pass

	def close_issue(self, number: int) -> bool:
		self.issues[number]["state"] = "closed"
		return True

	def patch_body(self, number: int, body: str) -> bool:
		self.issues[number]["body"] = body
		return True

	def comment(self, number: int, body: str) -> bool:
		self.issues.setdefault(number, {"body": "", "labels": set(), "state": "open", "author": "x", "comments": [], "created_at": ""})
		self.issues[number]["comments"].append(_comment(body, created="2026-09-30T17:30:00Z"))
		return True

	def comments(self, number: int):
		return list(self.issues.get(number, {}).get("comments", []))

	def pr_page(self, cursor: str):
		return {"defaultBranchRef": {"name": "main"}, "pullRequests": {"nodes": self.prs, "pageInfo": {"hasNextPage": False}}}

	def dispatch(self, workflow: str, ref: str, pr_number: int) -> bool:
		assert ref == "main"
		if pr_number in self.fail_dispatch:
			return False
		self.dispatched.append(pr_number)
		return True

	def remove_label(self, number: int, label: str) -> bool:
		self.issues[number]["labels"].discard(label)
		self.removed.append(number)
		return True

	def rerun_failed_jobs(self, run_id: int) -> bool:
		self.reruns.append(run_id)
		return True


def test_tracker_opens_once_and_trusts_only_pipeline_author() -> None:
	gh = FakeGitHub()
	gh.issues[5] = {"body": "<!-- ai:provider-outage:v1 kind=outage provider=openrouter status=402 opened_at=2026-09-30T00:00:00Z -->", "labels": {po.TRACKER_LABEL}, "state": "open", "author": "outsider", "comments": [], "created_at": ""}
	first = po.open_tracker(gh, kind="outage", provider="openrouter", status="402", settle_secs=0)
	second = po.open_tracker(gh, kind="outage", provider="openrouter", status="402", settle_secs=0)
	assert first["outcome"] == "opened" and first["alert"] is True
	assert second["outcome"] == "already_open" and second["alert"] is False
	assert second["number"] == first["number"] != 5


def test_capacity_tracker_never_pauses(tmp_path: Path) -> None:
	gh = FakeGitHub()
	po.open_tracker(gh, kind="capacity", provider="claude-pool", status="all_gated", settle_secs=0)
	out = tmp_path / "out"
	values = po.sweep(gh, probe=lambda: pytest.fail("no outage tracker, no probe"), output_path=str(out), alert_out=None)
	assert values["paused"] == "false" and values["capacity_open"] == "true"
	assert po.close_tracker(gh, kind="capacity", alert_out=None) == "closed"


# ---------------------------------------------------------------------------
# 2026-09-30 replay
# ---------------------------------------------------------------------------


def _replay_fixture() -> tuple[FakeGitHub, list[int]]:
	gh = FakeGitHub()
	alerts = 0
	for pr in range(4000, 4035):
		head = f"{pr:040x}"
		comments = [_comment(f"**AI review/autofix paused — model provider unavailable**\n\n{_marker('provider_unavailable', pr * 10 + attempt, head=head)}") for attempt in range(4)]
		cap = wfh.count_identical_failures(comments, head_sha=head, author_login=LOGIN)
		assert cap["count"] == 0  # no cap
		if po.open_tracker(gh, kind="outage", provider="openrouter", status="402", settle_secs=0, now=dt.datetime(2026, 9, 30, 17, 20, tzinfo=dt.timezone.utc))["alert"]:
			alerts += 1
		gh.prs.append({"number": pr, "isDraft": False, "headRefOid": head, "comments": {"nodes": comments, "pageInfo": {"hasPreviousPage": False}}})
	assert alerts == 1  # one alert for 35 failing PRs
	# A PR whose newest current-head marker is a real failure is not resumed.
	gh.prs[0]["comments"]["nodes"].append(_comment(_marker("workflow_failure", 1, head=gh.prs[0]["headRefOid"])))
	# A PR with a later editor summary is not resumed.
	gh.prs[1]["comments"]["nodes"].append(_comment("AI autofix editor summary"))
	# Labels: one applied during the outage (marker-backed), one applied for a
	# real defect without a marker, one marker superseded by a cap.
	gh.issues[700] = {"body": "", "labels": {po.REVIEW_BLOCKED_LABEL}, "state": "open", "author": "h", "comments": [], "created_at": ""}
	po.mark_label(gh, item=700, pr=4005, head=gh.prs[5]["headRefOid"], run="1")
	gh.issues[701] = {"body": "", "labels": {po.REVIEW_BLOCKED_LABEL}, "state": "open", "author": "h", "comments": [], "created_at": ""}
	gh.issues[702] = {"body": "", "labels": {po.REVIEW_BLOCKED_LABEL}, "state": "open", "author": "h", "comments": [], "created_at": ""}
	po.mark_label(gh, item=702, pr=702, head="", run="")
	gh.issues[702]["comments"].append(_comment("<!-- review-autofix-failure-cap:v1 head=" + HEAD + " -->", created="2026-09-30T19:00:00Z"))
	gh.issues[4005] = {"body": "", "labels": set(), "state": "open", "author": "h", "comments": [], "created_at": ""}
	tracker = po.find_trackers(gh, "outage")[0]["number"]
	gh.comment(tracker, "<!-- ai:provider-outage-release-run:v1 run=36776000001 workflow=Test_and_mark_stable -->")
	return gh, [pr for pr in range(4002, 4035)]


def test_replay_pauses_while_provider_down(tmp_path: Path) -> None:
	gh, _ = _replay_fixture()
	values = po.sweep(gh, probe=lambda: {"outcome": "down", "status": "402"}, output_path=str(tmp_path / "o"), alert_out=None)
	assert values["paused"] == "true"
	assert gh.dispatched == [] and gh.removed == []
	assert "paused=true" in (tmp_path / "o").read_text()


def test_replay_recovery_resumes_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv("PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED", raising=False)
	gh, expected = _replay_fixture()
	tracker = po.find_trackers(gh, "outage")[0]["number"]
	alert = tmp_path / "alert"
	values = po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=str(alert))
	assert values["recovered"] == "true"
	assert sorted(gh.dispatched) == expected
	assert gh.removed == [700]  # only the marker-backed outage label
	assert gh.issues[701]["labels"] == {po.REVIEW_BLOCKED_LABEL}
	assert gh.issues[702]["labels"] == {po.REVIEW_BLOCKED_LABEL}
	assert gh.reruns == []  # release re-run is opt-in
	assert gh.issues[tracker]["state"] == "closed"
	summary = gh.issues[tracker]["comments"][-1]["body"]
	assert "not re-run (PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED is off)" in summary
	assert alert.read_text().startswith("Model provider recovered")
	# Idempotent: a second tick finds no open tracker and does nothing.
	again = po.sweep(gh, probe=lambda: pytest.fail("no probe without a tracker"), output_path=None, alert_out=None)
	assert again["recovered"] == "false" and sorted(gh.dispatched) == expected


def test_release_rerun_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED", "true")
	gh, _ = _replay_fixture()
	gh.repository = po.SOURCE_REPO
	po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=None)
	assert gh.reruns == [36776000001]


def test_partial_resume_continues_from_marker() -> None:
	gh, expected = _replay_fixture()
	gh.fail_dispatch = {4010}
	tracker = po.find_trackers(gh, "outage")[0]["number"]
	values = po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=None)
	assert values["recovered"] == "false" and values["paused"] == "false"
	assert gh.issues[tracker]["state"] == "open"
	assert "4009" in po.parse_resume(gh.issues[tracker]["body"])["prs"]
	gh.fail_dispatch = set()
	first_round = list(gh.dispatched)
	po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=None)
	assert gh.dispatched[len(first_round):] == [4010]
	assert gh.removed == [700]
	assert gh.issues[tracker]["state"] == "closed"


def test_label_marker_from_before_outage_is_ignored() -> None:
	opened = dt.datetime(2026, 9, 30, 17, 0, tzinfo=dt.timezone.utc)
	comments = [_comment("<!-- ai:provider-outage-label:v1 item=1 pr=1 head=none run=none -->", created="2026-09-01T00:00:00Z")]
	assert po.label_marker_still_valid(comments, login=LOGIN, not_before=opened - po.LABEL_MARKER_LEAD) is None
	forged = [_comment("<!-- ai:provider-outage-label:v1 item=1 pr=1 head=none run=none -->", login="outsider")]
	assert po.label_marker_still_valid(forged, login=LOGIN, not_before=None) is None


def test_failed_tracker_close_does_not_report_recovery(tmp_path: Path) -> None:
	gh, _ = _replay_fixture()
	tracker = po.find_trackers(gh, "outage")[0]["number"]
	gh.close_issue = lambda number: False  # type: ignore[method-assign]
	alert = tmp_path / "alert"
	values = po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=str(alert))
	assert values["recovered"] == "false"
	assert not alert.exists()
	assert gh.issues[tracker]["state"] == "open"


def test_label_marker_resume_dispatches_judge_failed_pr() -> None:
	gh, _ = _replay_fixture()
	gh.issues[703] = {"body": "", "labels": {po.REVIEW_BLOCKED_LABEL}, "state": "open", "author": "h", "comments": [], "created_at": ""}
	po.mark_label(gh, item=703, pr=703, head="", run="")
	po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=None)
	assert 703 in gh.dispatched
	assert po.REVIEW_BLOCKED_LABEL not in gh.issues[703]["labels"]


def test_tracker_lookup_failure_pauses_only_on_down_probe() -> None:
	gh = FakeGitHub()
	gh.list_issues = lambda label: None  # type: ignore[method-assign]
	assert po.sweep(gh, probe=lambda: {"outcome": "down", "status": "402"}, output_path=None, alert_out=None)["paused"] == "true"
	assert po.sweep(gh, probe=lambda: {"outcome": "up", "status": "200"}, output_path=None, alert_out=None)["paused"] == "false"


def test_dispatch_honours_allow_workflow_edits(monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list[list[str]] = []
	gh = po.GitHub(REPO)
	gh._run = lambda arguments: (calls.append(arguments), subprocess.CompletedProcess(arguments, 0, "", ""))[1]  # type: ignore[method-assign]
	monkeypatch.delenv("ALLOW_WORKFLOW_EDITS", raising=False)
	gh.dispatch("internal-review.yml", "main", 1)
	monkeypatch.setenv("ALLOW_WORKFLOW_EDITS", "true")
	gh.dispatch("internal-review.yml", "main", 2)
	assert "allow_workflow_edits=false" in calls[0] and "allow_workflow_edits=true" in calls[1]


def test_stale_probe_cache_is_re_probed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	import os as _os
	cache = tmp_path / "probe.json"
	cache.write_text(json.dumps({"outcome": "down", "status": "429"}))
	assert po.cached_probe(str(cache), lambda *_a: pytest.fail("fresh cache must not probe"))["outcome"] == "down"
	old = cache.stat().st_mtime - po.PROBE_CACHE_TTL_SECS - 10
	_os.utime(cache, (old, old))
	monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
	result = po.cached_probe(str(cache), lambda method, *_a: (200, b"{}"))
	assert result["outcome"] == "up"
