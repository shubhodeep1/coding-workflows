#!/usr/bin/env python3
"""Tests for scripts/dependency_wait_resume.py (stuck-states P8b, Refs #8291)."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dependency_wait_resume as dwr  # noqa: E402
import security_dependency as sd  # noqa: E402

REPO = "owner/repo"
LOGIN = "pipeline-bot"
NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.timezone.utc)
SINCE = "2026-11-04T12:00:00Z"
OLD_SINCE = "2026-10-20T12:00:00Z"
SHA = "a" * 40


def _wait_body(kind: str = "pr", repo: str = REPO, number: int = 7, since: str = SINCE, extra: dict | None = None) -> str:
	payload = {"kind": kind, "repo": repo, "number": number, "since": since, **(extra or {})}
	return f"Waiting on dependency\n<!-- AI_DEPENDENCY_WAIT_V1 {json.dumps(payload)} -->"


def _c(body: str, author: str = LOGIN, created: str = "2026-11-04T12:00:00Z") -> dict:
	return {"author": author, "body": body, "created_at": created}


class FakeGitHub:
	def __init__(self) -> None:
		self.repository = REPO
		self.calls = 0
		self.login = LOGIN
		self.issues: dict[int, dict] = {}
		self.targets: dict[tuple[str, int], dict | None] = {}
		self.target_errors: set[tuple[str, int]] = set()
		self.compares: dict[str, str | None] = {}
		self.events: dict[int, list] = {}
		self.graphql_fail = False
		self.comment_fail = False
		self.posted: list[tuple[int, str]] = []
		self.label_ops: list[tuple[str, int, str]] = []
		self.comment_reads: list[int] = []
		self.target_queries = 0
		self.compare_calls: list[str] = []
		self.event_reads: list[int] = []

	def add_issue(self, number: int, labels: set[str], comments: list[dict] | None = None, body: str = "") -> None:
		self.issues[number] = {"labels": set(labels), "comments": list(comments or []), "body": body, "hidden": 0}

	def _node(self, number: int) -> dict:
		issue = self.issues[number]
		window = issue["comments"][-dwr.COMMENT_WINDOW:]
		return {
			"number": number,
			"body": issue["body"],
			"labels": {"nodes": [{"name": name} for name in sorted(issue["labels"])]},
			"comments": {
				"totalCount": len(issue["comments"]),
				"nodes": [{"author": {"login": c["author"]}, "body": c["body"], "createdAt": c["created_at"]} for c in window],
			},
		}

	def graphql(self, query: str, variables: dict) -> dict | None:
		self.calls += 1
		if self.graphql_fail:
			return None
		if "viewer" in query:
			waiting = [self._node(n) for n in sorted(self.issues) if dwr.WAIT_LABEL in self.issues[n]["labels"]]
			blocked = [self._node(n) for n in sorted(self.issues) if dwr.BLOCKED_LABEL in self.issues[n]["labels"]]
			return {"data": {"viewer": {"login": self.login}, "repository": {
				"waiting": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": waiting},
				"blocked": {"nodes": blocked},
			}}}
		self.target_queries += 1
		data: dict = {}
		errors = []
		current = None
		for line in query.splitlines():
			repo_match = re.search(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)', line)
			if repo_match:
				current = (repo_match.group(1), f"{repo_match.group(2)}/{repo_match.group(3)}")
				data[current[0]] = {}
				continue
			node_match = re.search(r"(n\d+): issueOrPullRequest\(number: (\d+)\)", line)
			if node_match and current:
				key = (current[1], int(node_match.group(2)))
				if key in self.target_errors:
					data[current[0]][node_match.group(1)] = None
					errors.append({"path": [current[0], node_match.group(1)], "message": "boom"})
				else:
					data[current[0]][node_match.group(1)] = self.targets.get(key)
		response: dict = {"data": data}
		if errors:
			response["errors"] = errors
		return response

	def comments(self, number: int):
		self.calls += 1
		self.comment_reads.append(number)
		return [{"user": {"login": c["author"]}, "body": c["body"], "created_at": c["created_at"]} for c in self.issues[number]["comments"]]

	def comment(self, number: int, body: str) -> bool:
		self.calls += 1
		if self.comment_fail:
			return False
		self.posted.append((number, body))
		self.issues[number]["comments"].append(_c(body))
		return True

	def add_labels(self, number: int, labels: list[str]) -> bool:
		self.calls += 1
		for label in labels:
			self.label_ops.append(("add", number, label))
			self.issues[number]["labels"].add(label)
			self.events.setdefault(number, []).append({"event": "labeled", "label": {"name": label}, "actor": {"login": self.login}})
		return True

	def remove_label(self, number: int, label: str) -> bool:
		self.calls += 1
		self.label_ops.append(("remove", number, label))
		self.issues[number]["labels"].discard(label)
		return True

	def compare(self, sha: str):
		self.calls += 1
		self.compare_calls.append(sha)
		status = self.compares.get(sha)
		return {"status": status} if status else None

	def label_events(self, number: int):
		self.calls += 1
		self.event_reads.append(number)
		return self.events.get(number, [])


def _pr(state: str = "OPEN", merged: bool = False, oid: str | None = None) -> dict:
	return {"__typename": "PullRequest", "state": state, "merged": merged, "mergeCommit": {"oid": oid} if oid else None}


def _issue(state: str = "OPEN", labels: tuple[str, ...] = ()) -> dict:
	return {"__typename": "Issue", "state": state, "labels": {"nodes": [{"name": n} for n in labels]}}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv("DEPENDENCY_WAIT_RESUME_ENABLED", raising=False)
	monkeypatch.delenv("DEPENDENCY_WAIT_ESCALATE_DAYS", raising=False)


def _scan(gh: FakeGitHub, capsys: pytest.CaptureFixture | None = None) -> str:
	dwr.scan(gh, REPO, now=NOW)
	return capsys.readouterr().out if capsys else ""


def test_no_waiters_makes_exactly_one_call(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(3, {dwr.BLOCKED_LABEL}, body="no dependency line")
	out = _scan(gh, capsys)
	assert gh.calls == 1
	assert "DEPENDENCY_WAIT_RESUME op=scan waiting=0 seeded=0 api_calls=1" in out


def test_merged_pr_gets_one_answer_and_repeat_tick_is_noop(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert len(gh.posted) == 1
	body = gh.posted[0][1]
	assert body.startswith("/answer [auto-answered-by-poller]")
	assert f"<!-- AI_DEPENDENCY_WAIT_RESUMED_V1 issue=10 target=pr:{REPO}#7 since={SINCE} outcome=answer -->" in body
	assert "outcome=resumed" in out
	# plan.yml's /answer gate needs ai:blocked: the swap happens before the comment.
	assert gh.label_ops == [("add", 10, dwr.BLOCKED_LABEL), ("remove", 10, dwr.WAIT_LABEL)]
	assert gh.issues[10]["labels"] == {dwr.BLOCKED_LABEL}
	_scan(gh, capsys)
	assert len(gh.posted) == 1


def test_pr_closed_unmerged_reclarifies() -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.targets[(REPO, 7)] = _pr("CLOSED", False)
	_scan(gh)
	assert len(gh.posted) == 1
	assert gh.posted[0][1].startswith("/reclarify")
	assert "outcome=reclarify -->" in gh.posted[0][1]
	assert dwr.WAIT_LABEL not in gh.issues[10]["labels"]
	assert ("add", 10, dwr.BLOCKED_LABEL) not in gh.label_ops


def test_half_applied_seed_is_finished_without_second_marker() -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	original_add = gh.add_labels
	gh.add_labels = lambda number, labels: False  # type: ignore[method-assign]
	_scan(gh)
	assert len(gh.posted) == 1 and gh.issues[20]["labels"] == {dwr.BLOCKED_LABEL}
	gh.add_labels = original_add  # type: ignore[method-assign]
	_scan(gh)
	assert len(gh.posted) == 1
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}


def test_resumed_issue_is_not_reseeded_when_blocked_again() -> None:
	gh = FakeGitHub()
	resumed = f"<!-- AI_DEPENDENCY_WAIT_RESUMED_V1 issue=20 target=issue:{REPO}#4 since={SINCE} outcome=answer -->"
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [_c(_wait_body(kind="issue", number=4)), _c(resumed)], body="Depends on: #4")
	_scan(gh)
	assert gh.posted == [] and gh.label_ops == []


def test_failed_label_swap_posts_no_answer() -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	gh.add_labels = lambda number, labels: False  # type: ignore[method-assign]
	_scan(gh)
	assert gh.posted == []


def test_failed_answer_post_keeps_wait_label_even_when_rollback_fails(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	gh.comment_fail = True
	gh.remove_label = lambda number, label: False  # type: ignore[method-assign]
	out = _scan(gh, capsys)
	assert gh.posted == []
	assert dwr.WAIT_LABEL in gh.issues[10]["labels"]
	assert "detail=rollback_failed" in out


def test_seed_recovery_ignores_marker_for_other_dependency_or_non_seed() -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [_c(_wait_body(kind="issue", number=4, extra={"source": "seed"}))], body="Depends on: #5")
	gh.add_issue(21, {dwr.BLOCKED_LABEL}, [_c(_wait_body(kind="issue", number=4))], body="Depends on: #4")
	_scan(gh)
	assert gh.posted == [] and gh.label_ops == []


def test_seed_recovery_skips_reblocked_issue_with_later_comment() -> None:
	gh = FakeGitHub()
	seed = _c(_wait_body(kind="issue", number=4, since=OLD_SINCE, extra={"source": "seed"}))
	latch = _c(f"<!-- ai:needs-human-latch reason=dependency_wait target=issue:{REPO}#4 since={OLD_SINCE} -->")
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [seed, latch, _c("/answer", author="human")], body="Depends on: #4")
	gh.events[20] = [{"event": "labeled", "label": {"name": dwr.WAIT_LABEL}, "actor": {"login": LOGIN}}]
	_scan(gh)
	assert gh.posted == [] and gh.label_ops == []


def test_seed_recovery_skips_silent_reblock_without_comment() -> None:
	gh = FakeGitHub()
	seed = _c(_wait_body(kind="issue", number=4, extra={"source": "seed"}))
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [seed], body="Depends on: #4")
	gh.events[20] = [
		{"event": "labeled", "label": {"name": dwr.WAIT_LABEL}, "actor": {"login": LOGIN}},
		{"event": "unlabeled", "label": {"name": dwr.WAIT_LABEL}, "actor": {"login": "human"}},
	]
	_scan(gh)
	assert gh.label_ops == []
	assert len(gh.posted) == 1 and f"<!-- {dwr.SEED_DECLINED_MARKER} issue=20 -->" in gh.posted[0][1]
	assert gh.event_reads == [20]
	# The note bounds the events read to one: later ticks skip without it.
	_scan(gh)
	assert len(gh.posted) == 1 and gh.event_reads == [20] and gh.label_ops == []


def test_seed_recovery_skips_events_read_after_later_trusted_comment() -> None:
	gh = FakeGitHub()
	seed = _c(_wait_body(kind="issue", number=4, extra={"source": "seed"}))
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [seed, _c("planner note")], body="Depends on: #4")
	_scan(gh)
	assert gh.event_reads == [] and gh.posted == [] and gh.label_ops == []


def test_seed_recovery_tolerates_malformed_event_payload() -> None:
	gh = FakeGitHub()
	seed = _c(_wait_body(kind="issue", number=4, extra={"source": "seed"}))
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [seed], body="Depends on: #4")
	gh.events[20] = [{"event": "labeled", "label": "oops", "actor": "oops"}]
	_scan(gh)
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}


def test_half_applied_seed_finished_even_after_unrelated_comment() -> None:
	gh = FakeGitHub()
	seed = _c(_wait_body(kind="issue", number=4, extra={"source": "seed"}))
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [seed, _c("ping", author="someone")], body="Depends on: #4")
	_scan(gh)
	assert gh.posted == []
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}


def test_seed_recovery_events_read_failure_writes_nothing(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	seed = _c(_wait_body(kind="issue", number=4, extra={"source": "seed"}))
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, [seed], body="Depends on: #4")
	gh.label_events = lambda number: None  # type: ignore[method-assign]
	out = _scan(gh, capsys)
	assert gh.posted == [] and gh.label_ops == []
	assert "outcome=read_failed" in out


def test_escalation_text_tells_human_to_swap_to_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="issue", number=4, since=OLD_SINCE))])
	gh.targets[(REPO, 4)] = _issue("OPEN")
	_scan(gh)
	assert len(gh.posted) == 1
	assert f"replace `{dwr.WAIT_LABEL}` with `{dwr.BLOCKED_LABEL}`" in gh.posted[0][1]


@pytest.mark.parametrize(("labels", "prefix"), (((dwr.MERGED_LABEL,), "/answer"), ((), "/reclarify")))
def test_issue_target_closed(labels: tuple, prefix: str) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="issue", number=4))])
	gh.targets[(REPO, 4)] = _issue("CLOSED", labels)
	_scan(gh)
	assert len(gh.posted) == 1 and gh.posted[0][1].startswith(prefix)


def test_open_issue_waits_then_escalates_once_and_keeps_wait_label(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="issue", number=4, since=SINCE))])
	gh.targets[(REPO, 4)] = _issue("OPEN")
	out = _scan(gh, capsys)
	assert "outcome=waiting" in out and gh.posted == []
	monkeypatch.setenv("DEPENDENCY_WAIT_ESCALATE_DAYS", "7")
	gh.issues[10]["comments"] = [_c(_wait_body(kind="issue", number=4, since=OLD_SINCE))]
	out = _scan(gh, capsys)
	assert "outcome=escalated" in out
	assert len(gh.posted) == 1
	assert f"<!-- ai:needs-human-latch reason=dependency_wait target=issue:{REPO}#4 since={OLD_SINCE} -->" in gh.posted[0][1]
	assert gh.issues[10]["labels"] == {dwr.WAIT_LABEL, dwr.NEEDS_HUMAN_LABEL}
	_scan(gh, capsys)
	assert len(gh.posted) == 1
	assert gh.label_ops.count(("add", 10, dwr.NEEDS_HUMAN_LABEL)) == 1


def test_escalate_days_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
	for raw, expected in (("3", 3), ("9999", 9999), ("0", 7), ("10000", 7), ("abc", 7), ("", 7)):
		monkeypatch.setenv("DEPENDENCY_WAIT_ESCALATE_DAYS", raw)
		assert dwr._escalate_days() == expected


@pytest.mark.parametrize(
	"case",
	("self", "other_repo", "is_pr", "null"),
)
def test_invalid_issue_targets_never_resume(case: str, capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	number, repo = 4, REPO
	if case == "self":
		number = 10
	if case == "other_repo":
		repo = "evil/repo"
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="issue", number=number, repo=repo))])
	if case == "is_pr":
		gh.targets[(REPO, 4)] = _pr("MERGED", True, SHA)
	elif case == "null":
		gh.targets[(REPO, 4)] = None
	out = _scan(gh, capsys)
	assert gh.posted == []
	expected = "read_failed" if case == "null" else "invalid_marker"
	assert f"outcome={expected}" in out


def test_target_graphql_error_is_read_failed(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(since=OLD_SINCE))])
	gh.target_errors.add((REPO, 7))
	out = _scan(gh, capsys)
	assert gh.posted == [] and "outcome=read_failed" in out


@pytest.mark.parametrize(("status", "resumed"), (("ahead", True), ("identical", True), ("behind", False), ("diverged", False), (None, False)))
def test_release_compare(status: str | None, resumed: bool) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="release", repo=dwr.UPSTREAM_REPO, number=99))])
	gh.add_issue(11, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="release", repo=dwr.UPSTREAM_REPO, number=99))])
	gh.targets[(dwr.UPSTREAM_REPO, 99)] = _pr("MERGED", True, SHA)
	gh.compares[SHA] = status
	_scan(gh)
	assert len(gh.posted) == (2 if resumed else 0)
	assert gh.compare_calls == [SHA]
	assert gh.target_queries == 1


def test_release_marker_must_name_upstream() -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(kind="release", repo=REPO, number=99))])
	_scan(gh)
	assert gh.posted == [] and gh.target_queries == 0


def test_untrusted_wait_marker_ignored_and_forged_resume_does_not_suppress(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(), author="mallory")])
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert gh.posted == [] and "outcome=invalid_marker" in out
	forged = f"<!-- AI_DEPENDENCY_WAIT_RESUMED_V1 issue=11 target=pr:{REPO}#7 since={SINCE} outcome=answer -->"
	gh.add_issue(11, {dwr.WAIT_LABEL}, [_c(_wait_body()), _c(forged, author="mallory")])
	_scan(gh, capsys)
	assert [n for n, _ in gh.posted] == [11]


def test_future_since_is_invalid(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body(since="2027-01-01T00:00:00Z"))])
	out = _scan(gh, capsys)
	assert "outcome=invalid_marker" in out and gh.target_queries == 0


def test_seeding_happens_once_and_skips_excluded_labels(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Context\n- Depends on: #4\n")
	gh.add_issue(21, {dwr.BLOCKED_LABEL, "ai:security"}, body="Depends on: #4")
	gh.add_issue(22, {dwr.BLOCKED_LABEL, dwr.NEEDS_HUMAN_LABEL}, body="Depends on: #4")
	gh.add_issue(23, {dwr.BLOCKED_LABEL, "ai:orchestrator-managed"}, body="Depends on: #4")
	gh.add_issue(24, {dwr.BLOCKED_LABEL}, body="Depends on: #24")
	gh.add_issue(25, {dwr.BLOCKED_LABEL}, body="Depends on: #4\nDepends on: #5")
	gh.add_issue(26, {dwr.BLOCKED_LABEL}, body="<!-- ai:security-finding: x -->\nDepends on: #4")
	out = _scan(gh, capsys)
	assert [n for n, _ in gh.posted] == [20]
	body = gh.posted[0][1]
	assert body.startswith(f"Waiting on dependency: issue={REPO}#4")
	payload = json.loads(re.search(r"AI_DEPENDENCY_WAIT_V1 (\{.*\}) -->", body).group(1))
	assert payload == {"kind": "issue", "repo": REPO, "number": 4, "since": "2026-11-05T12:00:00Z", "source": "seed"}
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}
	assert "seeded=1" in out
	# Seeded marker is honoured on the next tick; no second seed, and a
	# deliberate move back to ai:blocked is not undone.
	gh.issues[20]["labels"] = {dwr.BLOCKED_LABEL}
	out = _scan(gh, capsys)
	assert [n for n, _ in gh.posted] == [20, 20]
	assert dwr.SEED_DECLINED_MARKER in gh.posted[1][1]
	assert "outcome=seed_declined" in out
	assert gh.issues[20]["labels"] == {dwr.BLOCKED_LABEL}


def test_switch_off_moves_waiting_to_blocked_without_target_reads(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
	monkeypatch.setenv("DEPENDENCY_WAIT_RESUME_ENABLED", "off")
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert gh.target_queries == 0
	assert [n for n, _ in gh.posted] == [10]
	assert "<!-- AI_DEPENDENCY_WAIT_DISABLED_V1 issue=10 -->" in gh.posted[0][1]
	assert gh.issues[10]["labels"] == {dwr.BLOCKED_LABEL}
	assert gh.issues[20]["labels"] == {dwr.BLOCKED_LABEL}
	assert "outcome=disabled_moved" in out


def test_incomplete_window_reads_full_history_before_writing() -> None:
	gh = FakeGitHub()
	filler = [_c(f"chatter {i}", author="someone") for i in range(40)]
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())] + filler)
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	_scan(gh)
	assert gh.comment_reads == [10]
	assert len(gh.posted) == 1


def test_incomplete_window_sees_old_resume_marker() -> None:
	gh = FakeGitHub()
	resumed = f"<!-- AI_DEPENDENCY_WAIT_RESUMED_V1 issue=10 target=pr:{REPO}#7 since={SINCE} outcome=answer -->"
	filler = [_c(f"chatter {i}", author="someone") for i in range(40)]
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body()), _c(resumed)] + filler)
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	_scan(gh)
	assert gh.comment_reads == [10]
	assert gh.posted == []


def _latch(since: str = SINCE) -> str:
	return f"<!-- ai:needs-human-latch reason=dependency_wait target=pr:{REPO}#7 since={since} -->"


def test_pipeline_latch_cleared_on_resume(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL, dwr.NEEDS_HUMAN_LABEL}, [_c(_wait_body()), _c(_latch())])
	gh.events[10] = [{"event": "labeled", "label": {"name": dwr.NEEDS_HUMAN_LABEL}, "actor": {"login": LOGIN}}]
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert len(gh.posted) == 1
	assert ("remove", 10, dwr.NEEDS_HUMAN_LABEL) in gh.label_ops
	assert "outcome=latch_cleared" in out


def test_label_readded_by_person_is_kept(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL, dwr.NEEDS_HUMAN_LABEL}, [_c(_wait_body()), _c(_latch())])
	gh.events[10] = [
		{"event": "labeled", "label": {"name": dwr.NEEDS_HUMAN_LABEL}, "actor": {"login": LOGIN}},
		{"event": "labeled", "label": {"name": dwr.NEEDS_HUMAN_LABEL}, "actor": {"login": "human"}},
	]
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert len(gh.posted) == 1
	assert ("remove", 10, dwr.NEEDS_HUMAN_LABEL) not in gh.label_ops
	assert "outcome=latch_kept" in out


def test_other_latch_is_kept_without_events_read() -> None:
	gh = FakeGitHub()
	other = "<!-- ai:needs-human-latch reason=security_dependency_wait depends_on=3 held_since=1 -->"
	gh.add_issue(10, {dwr.WAIT_LABEL, dwr.NEEDS_HUMAN_LABEL}, [_c(_wait_body()), _c(other)])
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	_scan(gh)
	assert gh.event_reads == []
	assert dwr.NEEDS_HUMAN_LABEL in gh.issues[10]["labels"]


def test_failed_post_retries_next_tick() -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	gh.comment_fail = True
	_scan(gh)
	assert gh.posted == []
	gh.comment_fail = False
	_scan(gh)
	assert len(gh.posted) == 1


def test_call1_failure_and_missing_identity_write_nothing(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, {dwr.WAIT_LABEL}, [_c(_wait_body())])
	gh.graphql_fail = True
	out = _scan(gh, capsys)
	assert "op=scan outcome=read_failed" in out and gh.posted == []
	gh.graphql_fail = False
	gh.login = ""
	out = _scan(gh, capsys)
	assert "outcome=identity_unavailable" in out and gh.posted == []


def test_any_exception_still_exits_zero(capsys: pytest.CaptureFixture) -> None:
	def _boom(_repo: str):
		raise RuntimeError("kaboom")

	assert dwr.main(["scan", "--repo", REPO], github_factory=_boom) == 0
	assert "op=scan outcome=error error=RuntimeError" in capsys.readouterr().out
	assert dwr.main(["scan", "--repo", "bad repo; rm -rf"], github_factory=_boom) == 0
	assert dwr.main(["nonsense"], github_factory=_boom) == 0


@pytest.mark.parametrize("value", (None, "", "true", "TRUE", "1", "yes", "on", "false", "FALSE", "0", "no", "off", " Off ", "maybe"))
def test_resume_switch_parity_with_security_dependency(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
	if value is None:
		monkeypatch.delenv("DEPENDENCY_WAIT_RESUME_ENABLED", raising=False)
	else:
		monkeypatch.setenv("DEPENDENCY_WAIT_RESUME_ENABLED", value)
	assert dwr._resume_enabled() == sd._dependency_wait_resume_enabled()


def test_targets_query_inlines_only_validated_values() -> None:
	query, aliases = dwr.build_targets_query([(REPO, 7), (REPO, 4), (dwr.UPSTREAM_REPO, 9), (REPO, 7)])
	assert query.count("issueOrPullRequest") == 3
	assert 'repository(owner: "owner", name: "repo")' in query
	assert set(aliases) == {(REPO, 7), (REPO, 4), (dwr.UPSTREAM_REPO, 9)}


# ---------------------------------------------------------------------------
# Stuck-states P8a-2: parse-waiting and park (clarify / plan)
# ---------------------------------------------------------------------------

UPSTREAM = "shubhodeep1/coding-workflows"


@pytest.mark.parametrize(
	("text", "kind", "repo", "number"),
	(
		("STATUS: CLEAR\nWAITING: pr=owner/repo#7\n", "pr", REPO, 7),
		("waiting: `pr=shubhodeep1/coding-workflows#3968`", "pr", UPSTREAM, 3968),
		(f"WAITING: release={UPSTREAM}@stable contains={UPSTREAM}#4566", "release", UPSTREAM, 4566),
		("  WAITING:   issue=owner/repo#3  ", "issue", REPO, 3),
	),
)
def test_parse_waiting_accepts_the_three_kinds(text: str, kind: str, repo: str, number: int) -> None:
	result = dwr.parse_waiting(text, REPO, 5, NOW)
	assert result["status"] == "waiting"
	assert (result["kind"], result["repo"], result["number"]) == (kind, repo, number)
	assert result["target"] == dwr.target_label({"kind": kind, "repo": repo, "number": number})


@pytest.mark.parametrize(
	("text", "reason"),
	(
		("WAITING: pr=other/repo#7", "target_not_allowed"),
		("WAITING: issue=shubhodeep1/coding-workflows#7", "target_not_allowed"),
		("WAITING: release=shubhodeep1/coding-workflows@stable contains=owner/repo#7", "target_not_allowed"),
		("WAITING: release=other/repo@stable contains=shubhodeep1/coding-workflows#7", "release_not_upstream"),
		("WAITING: release=shubhodeep1/coding-workflows@main contains=shubhodeep1/coding-workflows#7", "release_not_upstream"),
		("WAITING: issue=owner/repo#5", "self_reference"),
		("WAITING: pr=owner/repo#5", "self_reference"),
		("WAITING: pr=owner/repo#0", "grammar"),
		("WAITING: pr=owner/repo#12345678901", "grammar"),
		("WAITING: pr=owner/repo#7 because reasons", "grammar"),
		("WAITING: the other PR", "grammar"),
	),
)
def test_parse_waiting_rejects_bad_targets(text: str, reason: str) -> None:
	result = dwr.parse_waiting(text, REPO, 5, NOW)
	assert result["status"] == "invalid" and result["reason"] == reason


def test_parse_waiting_none_fenced_disabled_and_line_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
	assert dwr.parse_waiting("STATUS: CLEAR\nBLOCKED: x\n", REPO, 5, NOW) == {"status": "none"}
	assert dwr.parse_waiting("```\nWAITING: pr=owner/repo#7\n```\nSTATUS: CLEAR\n", REPO, 5, NOW) == {"status": "none"}
	assert dwr.parse_waiting("```\nWAITING: pr=owner/repo#7\n", REPO, 5, NOW, ignore_fences=True)["status"] == "waiting"
	result = dwr.parse_waiting("WAITING: " + "x\x07" * 400, REPO, 5, NOW)
	assert result["status"] == "invalid" and "\x07" not in result["line"] and len(result["line"]) == 300
	monkeypatch.setenv("DEPENDENCY_WAIT_RESUME_ENABLED", "false")
	assert dwr.parse_waiting("WAITING: pr=owner/repo#7", REPO, 5, NOW)["status"] == "disabled"


def _park(gh: FakeGitHub, kind: str = "pr", target_repo: str = REPO, number: int = 7, run_url: str = "https://github.com/owner/repo/actions/runs/1") -> str:
	if 10 not in gh.issues:
		gh.add_issue(10, {"ai:planning"})
	return dwr.park(gh, REPO, 10, kind, target_repo, number, "plan", run_url, NOW)


def test_park_open_target_posts_marker_then_adds_wait_label() -> None:
	gh = FakeGitHub()
	gh.targets[(REPO, 7)] = _pr()
	assert _park(gh) == "parked"
	assert len(gh.posted) == 1 and gh.label_ops == [("add", 10, dwr.WAIT_LABEL)]
	body = gh.posted[0][1]
	assert body.startswith("Waiting on dependency: pr=owner/repo#7\n")
	assert "Run: https://github.com/owner/repo/actions/runs/1" in body
	assert "replace `ai:waiting-on-dependency` with `ai:blocked`" in body
	payload = json.loads(dwr.WAIT_MARKER_RE.search(body).group(1))
	assert payload == {"kind": "pr", "repo": REPO, "number": 7, "since": "2026-11-05T12:00:00Z"}
	assert gh.target_queries == 1 and gh.compare_calls == []


def test_park_marker_round_trips_through_the_scan(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.targets[(REPO, 7)] = _pr()
	assert _park(gh) == "parked"
	issue = dwr.IssueSnapshot(gh._node(10))
	found = dwr.newest_trusted_wait(issue.comments, LOGIN, REPO, 10, NOW)
	assert found is not None and found[1] is not None and found[1]["source"] == ""
	out = _scan(gh, capsys)
	assert "DEPENDENCY_WAIT_RESUME issue=10 target=pr:owner/repo#7 outcome=waiting" in out
	gh.targets[(REPO, 7)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert "outcome=resumed" in out and gh.posted[-1][1].startswith("/answer [auto-answered-by-poller]")


def test_park_release_line_and_bad_run_url_is_dropped() -> None:
	gh = FakeGitHub()
	gh.targets[(UPSTREAM, 4566)] = _pr("MERGED", True, SHA)
	gh.compares[SHA] = "behind"
	assert _park(gh, "release", UPSTREAM, 4566, run_url="javascript:alert(1)") == "parked"
	body = gh.posted[0][1]
	assert body.startswith(f"Waiting on dependency: release={UPSTREAM}@stable contains={UPSTREAM}#4566\n")
	assert "Run:" not in body and gh.compare_calls == [SHA]


@pytest.mark.parametrize(
	("kind", "node", "compare"),
	(
		("pr", _pr("MERGED", True, SHA), None),
		("pr", _pr("CLOSED"), None),
		("issue", _issue("CLOSED", ("ai:merged",)), None),
		("release", _pr("MERGED", True, SHA), "ahead"),
	),
)
def test_park_already_resolved_writes_nothing(kind: str, node: dict, compare: str | None) -> None:
	gh = FakeGitHub()
	target_repo = UPSTREAM if kind == "release" else REPO
	gh.targets[(target_repo, 7)] = node
	if compare:
		gh.compares[SHA] = compare
	assert _park(gh, kind, target_repo, 7) == "already_resolved"
	assert gh.posted == [] and gh.label_ops == []


def test_park_invalid_or_unreadable_target_writes_nothing() -> None:
	gh = FakeGitHub()
	gh.targets[(REPO, 7)] = _issue()
	assert _park(gh, "pr") == "invalid_target"
	gh.targets[(REPO, 7)] = None
	assert _park(gh, "pr") == "read_failed"
	gh.target_errors.add((REPO, 7))
	assert _park(gh, "pr") == "read_failed"
	gh.graphql_fail = True
	assert _park(gh, "pr") == "read_failed"
	gh.targets[(UPSTREAM, 7)] = _pr("MERGED", True, SHA)
	gh.graphql_fail = False
	assert _park(gh, "release", UPSTREAM, 7) == "read_failed"  # compare unreadable
	assert gh.posted == [] and gh.label_ops == []


def test_park_comment_first_then_label(monkeypatch: pytest.MonkeyPatch) -> None:
	gh = FakeGitHub()
	gh.targets[(REPO, 7)] = _pr()
	gh.comment_fail = True
	assert _park(gh) == "comment_failed"
	assert gh.label_ops == []
	gh.comment_fail = False
	monkeypatch.setattr(gh, "add_labels", lambda number, labels: False)
	assert _park(gh) == "label_failed"
	assert len(gh.posted) == 1


def test_park_disabled_and_invalid_args_make_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
	gh = FakeGitHub()
	gh.add_issue(10, set())
	assert dwr.park(gh, REPO, 10, "pr", "other/repo", 7, "plan", "", NOW) == "invalid_args"
	assert dwr.park(gh, REPO, 10, "issue", REPO, 10, "plan", "", NOW) == "invalid_args"
	assert dwr.park(gh, REPO, 10, "pr", REPO, 7, "implement", "", NOW) == "invalid_args"
	monkeypatch.setenv("DEPENDENCY_WAIT_RESUME_ENABLED", "off")
	assert dwr.park(gh, REPO, 10, "pr", REPO, 7, "plan", "", NOW) == "disabled"
	assert gh.calls == 0


def test_cli_prints_one_json_line_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
	output = tmp_path / "out.txt"
	output.write_text("WAITING: pr=shubhodeep1/coding-workflows#3968\n", encoding="utf-8")
	assert dwr.main(["parse-waiting", "--output-file", str(output), "--repo", REPO, "--issue-number", "5", "--phase", "plan"]) == 0
	captured = capsys.readouterr()
	assert len(captured.out.splitlines()) == 1
	assert json.loads(captured.out)["target"] == "pr:shubhodeep1/coding-workflows#3968"
	assert "DEPENDENCY_WAIT_PARK op=parse issue=5 target=pr:shubhodeep1/coding-workflows#3968 phase=plan outcome=waiting" in captured.err
	for argv in (
		["parse-waiting", "--output-file", str(tmp_path / "missing.txt"), "--repo", REPO, "--issue-number", "5"],
		["parse-waiting", "--output-file", str(output), "--repo", "bad repo", "--issue-number", "5"],
		["parse-waiting", "--repo", REPO],
		["park", "--repo", REPO],
	):
		assert dwr.main(argv) == 0
		assert json.loads(capsys.readouterr().out)["status"] == "error"

	def _boom(_repo: str):
		raise RuntimeError("kaboom")

	park_argv = ["park", "--repo", REPO, "--issue-number", "5", "--kind", "pr", "--target-repo", REPO, "--number", "7", "--phase", "clarify"]
	assert dwr.main(park_argv, github_factory=_boom) == 0
	captured = capsys.readouterr()
	assert json.loads(captured.out) == {"status": "error"} and "detail=RuntimeError" in captured.err
	gh = FakeGitHub()
	gh.add_issue(5, {"ai:clarification"})
	gh.targets[(REPO, 7)] = _pr()
	assert dwr.main(park_argv + ["--run-url", "https://example.invalid/run"], github_factory=lambda _repo: gh) == 0
	assert json.loads(capsys.readouterr().out) == {"status": "parked", "target": "pr:owner/repo#7"}
	assert dwr.main(["park", "--repo", REPO, "--issue-number", "5", "--kind", "pr", "--target-repo", REPO, "--number", "x7", "--phase", "plan"], github_factory=_boom) == 0
	assert json.loads(capsys.readouterr().out)["status"] == "invalid_args"
	gh = FakeGitHub()
	gh.add_issue(5, {"ai:clarification"})
	gh.targets[(REPO, 7)] = _pr()
	gh.add_labels = lambda number, labels: False
	assert dwr.main(park_argv, github_factory=lambda _repo: gh) == 0
	captured = capsys.readouterr()
	assert json.loads(captured.out) == {"status": "label_failed", "target": "pr:owner/repo#7"}
	assert "outcome=label_failed detail=marker_posted" in captured.err and len(gh.posted) == 1


# ---------------------------------------------------------------------------
# Stuck-states P8d: generic `Depends on: #N` seeding (Refs #8291)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("latch", sorted(dwr.SEED_HUMAN_LATCH_LABELS))
def test_seed_skips_every_human_latch_label(latch: str) -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL, latch}, body="Depends on: #4")
	_scan(gh)
	assert gh.posted == [] and gh.label_ops == []
	assert gh.calls == 1
	# Once a person removes the latch, the next tick seeds exactly once.
	gh.issues[20]["labels"].discard(latch)
	_scan(gh)
	assert [n for n, _ in gh.posted] == [20]
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}
	_scan(gh)
	assert len(gh.posted) == 1


def test_seed_human_latch_labels_cover_repo_latch_sets() -> None:
	import orchestrate_lib
	import workflow_failure_heal

	assert dwr.SEED_HUMAN_LATCH_LABELS == set(workflow_failure_heal.HUMAN_NEEDED_LABELS) | set(orchestrate_lib.STALL_RECOVERY_LATCH_LABELS)
	# The heal escalation label is a seed latch but must never start a heal report.
	assert workflow_failure_heal.ESCALATED_LABEL not in workflow_failure_heal.HUMAN_NEEDED_LABELS
	assert set(orchestrate_lib.STALL_RECOVERY_LATCH_LABELS) <= dwr.SEED_HUMAN_LATCH_LABELS
	assert dwr.NEEDS_HUMAN_LABEL in dwr.SEED_HUMAN_LATCH_LABELS


def test_seed_once_across_ticks_and_no_target_read_on_seed_tick(capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	gh.targets[(REPO, 4)] = _issue("OPEN")
	out = _scan(gh, capsys)
	assert gh.target_queries == 0
	assert "seeded=1" in out and len(gh.posted) == 1
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}
	out = _scan(gh, capsys)
	assert len(gh.posted) == 1
	assert "seeded=0" in out and "outcome=waiting" in out
	assert gh.target_queries == 1


@pytest.mark.parametrize(
	"body",
	(
		"Depends on: #0",
		"Depends on: #-4",
		"Depends on: #4abc",
		"Depends on: other/repo#4",
		"Depends on: #4, #5",
		"Depends on: #12345678901",
		"Depends on: #4\nDepends on: #4",
		"Depends on PR: #4",
		"Depends on: #20",
	),
)
def test_seed_rejects_invalid_dependency_numbers(body: str) -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body=body)
	_scan(gh)
	assert gh.posted == [] and gh.label_ops == []
	assert gh.calls == 1


@pytest.mark.parametrize("case", ("graphql_error", "null", "missing", "is_pr"))
def test_seeded_wait_with_unreadable_target_never_resumes(case: str, capsys: pytest.CaptureFixture) -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	_scan(gh)
	assert len(gh.posted) == 1
	if case == "graphql_error":
		gh.target_errors.add((REPO, 4))
	elif case == "null":
		gh.targets[(REPO, 4)] = None
	elif case == "is_pr":
		gh.targets[(REPO, 4)] = _pr("MERGED", True, SHA)
	out = _scan(gh, capsys)
	assert len(gh.posted) == 1
	assert gh.issues[20]["labels"] == {dwr.WAIT_LABEL}
	expected = "invalid_marker" if case == "is_pr" else "read_failed"
	assert f"issue=20 target=issue:{REPO}#4 outcome={expected}" in out


def test_seeded_wait_resumes_once_when_target_closes_merged() -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	_scan(gh)
	gh.targets[(REPO, 4)] = _issue("CLOSED", (dwr.MERGED_LABEL,))
	_scan(gh)
	assert len(gh.posted) == 2 and gh.posted[1][1].startswith("/answer [auto-answered-by-poller]")
	_scan(gh)
	assert len(gh.posted) == 2


def test_seeded_targets_share_one_batched_read() -> None:
	gh = FakeGitHub()
	gh.add_issue(20, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	gh.add_issue(21, {dwr.BLOCKED_LABEL}, body="Depends on: #5")
	gh.add_issue(22, {dwr.BLOCKED_LABEL}, body="Depends on: #4")
	gh.targets[(REPO, 4)] = _issue("OPEN")
	gh.targets[(REPO, 5)] = _issue("OPEN")
	_scan(gh)
	assert [n for n, _ in gh.posted] == [20, 21, 22]
	gh.calls = 0
	_scan(gh)
	assert gh.target_queries == 1
	assert gh.calls == 2
	assert gh.comment_reads == []
	assert len(gh.posted) == 3
