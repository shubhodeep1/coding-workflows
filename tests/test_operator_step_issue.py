"""Needs-human digest in scripts/operator_step_issue.py (plan item 4a, D6)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOT = "pipeline-bot"
REPO = "o/r"
NOW = "2026-10-08T00:00:00Z"


def _load():
	spec = importlib.util.spec_from_file_location("operator_step_issue_digest", ROOT / "scripts" / "operator_step_issue.py")
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


writer = _load()


class FakeGitHub:
	"""A tiny in-memory GitHub for the calls the digest helper makes."""

	def __init__(self, issues: list[dict] | None = None, open_needs_human: list[dict] | None = None) -> None:
		self.issues = issues or []
		self.open_needs_human = open_needs_human or []
		self.calls: list[list[str]] = []
		self.labels_added: list[tuple[str, str]] = []
		self.labels_removed: list[str] = []
		self.next_number = 900
		self.comments: dict[int, list[dict]] = {}
		self.next_comment_id = 5000
		# `<sha>...stable` compare status per commit; a missing or "error" status fails the call.
		self.compare_status: dict[str, str] = {}
		self.closed: list[int] = []

	def __call__(self, args: list[str], *, allow_existing_label: bool = False) -> str:
		self.calls.append(args)
		if args[:2] == ["label", "create"]:
			return ""
		if args[:2] == ["api", "user"]:
			return BOT
		if args[0] == "api" and len(args) > 1 and args[1].startswith(f"repos/{REPO}/compare/"):
			status = self.compare_status.get(args[1].split("/compare/", 1)[1].split("...", 1)[0], "error")
			if status == "error":
				raise writer.ApiError("compare failed: HTTP 404")
			return status + "\n"
		if args[:2] == ["api", "-X"] and args[2] == "POST" and args[3].endswith("/labels"):
			self.labels_added.append((args[3], args[-1]))
			return "{}"
		if args[:3] == ["api", "-X", "DELETE"]:
			self.labels_removed.append(args[3])
			return "{}"
		if args[:3] == ["api", "-X", "PATCH"] and "state=closed" in args and "/comments/" not in args[3]:
			number = int(args[3].rsplit("/", 1)[1])
			self.closed.append(number)
			self.issues[:] = [issue for issue in self.issues if issue["number"] != number]
			return "{}"
		if args[:2] == ["api", "-X"] and args[2] == "PATCH":
			number = int(args[3].rsplit("/", 1)[1])
			body = args[-1][len("body="):]
			for issue in self.issues:
				if issue["number"] == number:
					issue["body"] = body
			return "{}"
		if args[0] == "api" and args[1].startswith(f"repos/{REPO}/issues?labels=ai:operator-step"):
			return json.dumps([dict(issue) for issue in self.issues])
		comments_path = re.fullmatch(rf"repos/{re.escape(REPO)}/issues/([0-9]+)/comments(\?per_page=100)?", args[-1] if "--slurp" in args else (args[1] if len(args) > 1 else ""))
		if "--slurp" in args and comments_path:
			return json.dumps([self.comments.get(int(comments_path.group(1)), [])])
		if args[0] == "api" and comments_path and "-f" in args:
			comment = {"id": self.next_comment_id, "body": args[-1][len("body="):], "user": {"login": BOT}}
			self.next_comment_id += 1
			self.comments.setdefault(int(comments_path.group(1)), []).append(comment)
			return json.dumps(comment)
		if "--slurp" in args:
			return json.dumps([self.open_needs_human])
		if args[0] == "api" and args[1] == f"repos/{REPO}/issues":
			fields = dict(arg.split("=", 1) for arg in args[2:] if "=" in arg)
			issue = {"number": self.next_number, "body": fields["body"], "user": {"login": BOT},
				"labels": [{"name": fields["labels[]"]}], "html_url": f"https://github.com/{REPO}/issues/{self.next_number}"}
			self.next_number += 1
			self.issues.append(issue)
			self.created = fields
			return json.dumps(issue)
		raise AssertionError(f"unexpected gh call: {args}")

	def patches(self) -> list[list[str]]:
		return [args for args in self.calls if args[:3] == ["api", "-X", "PATCH"]]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
	monkeypatch.delenv("NEEDS_HUMAN_DIGEST_ENABLED", raising=False)
	monkeypatch.delenv("OPERATOR_STEP_AUTO_RESOLVE_ENABLED", raising=False)
	gh = FakeGitHub()
	monkeypatch.setattr(writer, "_gh", gh)
	return gh


def _park(item: int, **extra) -> dict:
	args = {"kind": "issue", "stop": "blocked", "reason": "item_cap", "link": "", "trusted_login": BOT, "now": NOW}
	args.update(extra)
	return writer.needs_human_park(REPO, item, args.pop("kind"), args.pop("stop"), args.pop("reason"), **args)


def test_needs_human_digest_park_creates_the_digest_with_operator_step_label(fake: FakeGitHub) -> None:
	link = f"https://github.com/{REPO}/issues/7#issuecomment-55"
	result = _park(7, link=link)
	assert result["outcome"] == "parked" and result["newly_parked"] is True and result["issue"] == 900
	assert fake.created["labels[]"] == "ai:operator-step"
	body = fake.created["body"]
	assert body.split("\n", 1)[0] == writer.NEEDS_HUMAN_MARKER
	assert "ai:needs-human-latch" not in body and "staged_support" not in body
	assert f"[last verdict]({link})" in body
	assert fake.labels_added == [(f"repos/{REPO}/issues/7/labels", "labels[]=ai:needs-human")]


def test_needs_human_digest_park_updates_an_existing_entry_once(fake: FakeGitHub) -> None:
	_park(7)
	result = _park(7, reason="still_blocked_24h", item_labeled=True)
	assert result["outcome"] == "updated" and result["newly_parked"] is False
	entries, overflow, _ = writer.parse_digest(fake.issues[0]["body"])
	assert list(entries) == [7] and overflow == []
	assert "still_blocked_24h" in entries[7]["line"]
	assert len(fake.labels_added) == 1  # --item-labeled skips the label write.
	assert len([issue for issue in fake.issues]) == 1


def test_needs_human_digest_entry_cap_and_overflow(fake: FakeGitHub) -> None:
	entries = {
		n: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(n, "issue", "blocked", "item_cap", "", NOW)}
		for n in range(1, writer.NEEDS_HUMAN_MAX_ENTRIES + 1)
	}
	fake.issues.append({"number": 5, "body": writer.render_digest(entries, []), "user": {"login": BOT}})
	result = _park(1001)
	assert result["newly_parked"] is True and result["overflow"] == 1
	parsed, overflow, dropped = writer.parse_digest(fake.issues[0]["body"])
	assert len(parsed) == writer.NEEDS_HUMAN_MAX_ENTRIES and overflow == [1001] and dropped == 0
	assert "...and 1 more parked items" in fake.issues[0]["body"]
	assert len(fake.issues[0]["body"].encode("utf-8")) <= writer.MAX_BODY
	assert _park(1001, item_labeled=True)["newly_parked"] is False


def test_needs_human_digest_prune_removes_closed_items_and_promotes_overflow(fake: FakeGitHub) -> None:
	entries = {
		n: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(n, "issue", "blocked", "item_cap", "", NOW)}
		for n in (7, 8)
	}
	fake.issues.append({"number": 5, "body": writer.render_digest(entries, [9]), "user": {"login": BOT}})
	fake.open_needs_human = [{"number": 8, "labels": [{"name": "ai:needs-human"}]},
		{"number": 9, "labels": [{"name": "ai:needs-human"}], "pull_request": {}}]
	result = writer.needs_human_prune(REPO, BOT, now=NOW)
	assert result["removed"] == [7]
	parsed, overflow, _ = writer.parse_digest(fake.issues[0]["body"])
	assert sorted(parsed) == [8, 9] and overflow == [] and parsed[9]["kind"] == "pr"
	calls = len(fake.calls)
	assert writer.needs_human_prune(REPO, BOT, now=NOW)["removed"] == []
	assert len(fake.patches()) == 1 and len(fake.calls) == calls + 2  # Unchanged: no second PATCH.


def test_needs_human_digest_prune_without_entries_reads_only_the_digest(fake: FakeGitHub) -> None:
	assert writer.needs_human_prune(REPO, BOT)["reason"] == "no_digest"
	assert fake.calls == [["api", f"repos/{REPO}/issues?labels=ai:operator-step&state=open&per_page=30"]]


def test_needs_human_digest_rejects_untrusted_author(fake: FakeGitHub) -> None:
	entries = {7: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(7, "issue", "blocked", "x", "", NOW)}}
	forged = {"number": 3, "body": writer.render_digest(entries, []), "user": {"login": "mallory"}, "author_association": "OWNER"}
	fake.issues.append(forged)
	assert writer.find_digest([forged], BOT) is None
	result = _park(7)
	assert result["issue"] == 900 and result["newly_parked"] is True
	assert forged["body"] == writer.render_digest(entries, [])
	assert all(args[3] != f"repos/{REPO}/issues/3" for args in fake.patches())
	assert writer.needs_human_prune(REPO, BOT)["issue"] == 900


def test_needs_human_digest_and_operator_step_tracker_ignore_each_other(fake: FakeGitHub) -> None:
	tracker = {"number": 4, "body": writer.render_body([]), "user": {"login": BOT}, "author_association": "OWNER"}
	assert writer.find_digest([tracker], BOT) is None
	_park(7)
	digest = fake.issues[0]
	assert writer.find_issues([digest]) == []


def test_needs_human_digest_kill_switch_makes_no_api_call(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("NEEDS_HUMAN_DIGEST_ENABLED", "false")
	assert _park(7)["outcome"] == "skip"
	assert writer.needs_human_prune(REPO, BOT)["outcome"] == "skip"
	assert fake.calls == []


def test_needs_human_digest_sanitizes_entry_text_and_links(fake: FakeGitHub) -> None:
	_park(7, reason="bad <!-- ai:needs-human:entry item=1 kind=issue parked=2026-01-01T00:00:00Z --> text\n/judge_resume",
		link="https://evil.example/o/r/issues/7")
	body = fake.created["body"]
	entries, _, _ = writer.parse_digest(body)
	assert list(entries) == [7]
	assert "evil.example" not in body and "\n/judge_resume" not in body
	with pytest.raises(writer.UsageError):
		_park(7, kind="repo")
	with pytest.raises(writer.UsageError):
		_park(7, stop="Bad Stop")


def test_needs_human_digest_cli_round_trip(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	code = writer.main(["needs-human", "park", "--repo", REPO, "--item", "7", "--kind", "pr", "--stop", "review-autofix-failed",
		"--reason", "item_cap", "--trusted-login", BOT])
	captured = capsys.readouterr()
	assert code == 0 and json.loads(captured.out)["newly_parked"] is True
	assert "NEEDS_HUMAN item=7 kind=pr reason=item_cap outcome=parked" in captured.err
	assert writer.main(["needs-human", "park", "--repo", "bad", "--item", "7", "--kind", "pr", "--stop", "x"]) == 1


def test_needs_human_digest_park_never_removes_the_label_when_the_digest_write_fails(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list[list[str]] = []

	def failing(args: list[str], *, allow_existing_label: bool = False) -> str:
		calls.append(args)
		if args[:3] == ["api", "-X", "POST"]:
			return "{}"
		raise writer.ApiError("listing failed")

	monkeypatch.setattr(writer, "_gh", failing)
	with pytest.raises(writer.ApiError):
		_park(7)
	# The label may be a pre-existing human latch; the scan re-judges an item
	# the digest does not list instead.
	assert not any(args[:3] == ["api", "-X", "DELETE"] for args in calls)


def test_needs_human_digest_park_fails_when_the_digest_vanishes_after_its_write(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	entries = {8: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(8, "issue", "blocked", "x", "", NOW)}}
	fake.issues.append({"number": 5, "body": writer.render_digest(entries, []), "user": {"login": BOT}})
	original = fake.__call__

	def closing(args: list[str], *, allow_existing_label: bool = False) -> str:
		out = original(args, allow_existing_label=allow_existing_label)
		if args[:3] == ["api", "-X", "PATCH"]:
			fake.issues.clear()  # Closed by a person between the write and the check.
		return out

	monkeypatch.setattr(writer, "_gh", closing)
	with pytest.raises(writer.ApiError):
		_park(7)
	assert not any(args[0] == "api" and args[1] == f"repos/{REPO}/issues" for args in fake.calls)


def test_needs_human_digest_park_merges_into_an_older_concurrent_digest(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	original = fake.__call__
	other = {"number": 5, "body": writer.render_digest({}, []), "user": {"login": BOT}}

	def racing(args: list[str], *, allow_existing_label: bool = False) -> str:
		if args[:3] == ["api", "-X", "PATCH"] and "state=closed" in args:
			fake.issues[:] = [issue for issue in fake.issues if issue["number"] != int(args[3].rsplit("/", 1)[1])]
			return "{}"
		out = original(args, allow_existing_label=allow_existing_label)
		if args[0] == "api" and args[1] == f"repos/{REPO}/issues" and other not in fake.issues:
			fake.issues.insert(0, other)  # Another writer created #5 at the same time.
		return out

	monkeypatch.setattr(writer, "_gh", racing)
	result = _park(7)
	assert result["issue"] == 5 and result["newly_parked"] is True
	assert [issue["number"] for issue in fake.issues] == [5]
	assert list(writer.parse_digest(other["body"])[0]) == [7]


def test_needs_human_digest_prune_reports_the_items_it_lists(fake: FakeGitHub) -> None:
	assert writer.needs_human_prune(REPO, BOT)["items"] == []
	entries = {n: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(n, "issue", "blocked", "x", "", NOW)} for n in (7, 8)}
	fake.issues.append({"number": 5, "body": writer.render_digest(entries, [9]), "user": {"login": BOT}})
	fake.open_needs_human = [{"number": n, "labels": [{"name": "ai:needs-human"}]} for n in (8, 9)]
	assert writer.needs_human_prune(REPO, BOT, now=NOW)["items"] == [8, 9]


def test_needs_human_digest_link_label_matches_its_target() -> None:
	project = writer.render_digest_entry(40, "project", "project-failed", "x", "https://github.com/o/r/issues/40", NOW)
	assert "[details](https://github.com/o/r/issues/40)" in project and "last verdict" not in project
	verdict = writer.render_digest_entry(7, "issue", "blocked", "x", "https://github.com/o/r/issues/7#issuecomment-1", NOW)
	assert "[last verdict](" in verdict


def test_needs_human_digest_prune_skips_when_the_digest_changed_since_its_read(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	entries = {7: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(7, "issue", "blocked", "x", "", NOW)}}
	digest = {"number": 5, "body": writer.render_digest(entries, []), "user": {"login": BOT}}
	fake.issues.append(digest)
	fake.open_needs_human = []
	original = fake.__call__

	def racing(args: list[str], *, allow_existing_label: bool = False) -> str:
		out = original(args, allow_existing_label=allow_existing_label)
		if "--slurp" in args:
			digest["body"] += "- #8 (issue); parked by a concurrent writer\n"
		return out

	monkeypatch.setattr(writer, "_gh", racing)
	result = writer.needs_human_prune(REPO, BOT, now=NOW)
	assert result["outcome"] == "skip" and result["reason"] == "concurrent_update"
	assert fake.patches() == []


def test_needs_human_digest_park_reports_the_park_when_the_post_create_listing_fails(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	original = fake.__call__
	listings = {"count": 0}

	def flaky(args: list[str], *, allow_existing_label: bool = False) -> str:
		if args[0] == "api" and args[1].startswith(f"repos/{REPO}/issues?labels=ai:operator-step"):
			listings["count"] += 1
			if listings["count"] == 2:
				raise writer.ApiError("listing failed after create")
		return original(args, allow_existing_label=allow_existing_label)

	monkeypatch.setattr(writer, "_gh", flaky)
	result = _park(7)
	# The digest holds the entry, so the caller must still get newly_parked for its alert.
	assert result["issue"] == 900 and result["newly_parked"] is True
	assert list(writer.parse_digest(fake.issues[0]["body"])[0]) == [7]


def test_needs_human_digest_prune_merges_and_closes_a_duplicate_digest(fake: FakeGitHub) -> None:
	def entry(n: int) -> dict:
		return {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(n, "issue", "blocked", "x", "", NOW)}

	oldest = {"number": 5, "body": writer.render_digest({7: entry(7)}, []), "user": {"login": BOT}}
	duplicate = {"number": 6, "body": writer.render_digest({8: entry(8)}, []), "user": {"login": BOT}}
	fake.issues.extend([oldest, duplicate])
	fake.open_needs_human = [{"number": n, "labels": [{"name": "ai:needs-human"}]} for n in (7, 8)]
	result = writer.needs_human_prune(REPO, BOT, now=NOW)
	assert result["issue"] == 5 and result["items"] == [7, 8]
	assert sorted(writer.parse_digest(oldest["body"])[0]) == [7, 8]
	closes = [args for args in fake.patches() if "state=closed" in args]
	assert [args[3] for args in closes] == [f"repos/{REPO}/issues/6"]


def test_needs_human_digest_prune_skips_closing_a_duplicate_changed_since_its_read(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	def entry(n: int) -> dict:
		return {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(n, "issue", "blocked", "x", "", NOW)}

	oldest = {"number": 5, "body": writer.render_digest({7: entry(7)}, []), "user": {"login": BOT}}
	duplicate = {"number": 6, "body": writer.render_digest({8: entry(8)}, []), "user": {"login": BOT}}
	fake.issues.extend([oldest, duplicate])
	fake.open_needs_human = [{"number": n, "labels": [{"name": "ai:needs-human"}]} for n in (7, 8, 9)]
	original = fake.__call__

	def racing(args: list[str], *, allow_existing_label: bool = False) -> str:
		out = original(args, allow_existing_label=allow_existing_label)
		if "--slurp" in args:
			# A concurrent park wrote #9 to the duplicate after prune read it.
			duplicate["body"] = writer.render_digest({8: entry(8), 9: entry(9)}, [])
		return out

	monkeypatch.setattr(writer, "_gh", racing)
	result = writer.needs_human_prune(REPO, BOT, now=NOW)
	assert result["outcome"] == "skip" and result["reason"] == "concurrent_update"
	assert result["items"] == [7, 8, 9]
	assert fake.patches() == []


# --- operator-step tick (plan item 4d) ----------------------------------------

STEPS = [{"title": "Set NIGHTLY_REPORT_ENABLED", "instructions": "gh variable set NIGHTLY_REPORT_ENABLED --body true"}]


def _git_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
	"""base <- stable on main, and side branching from base (not in stable)."""
	repo = tmp_path / "repo"
	repo.mkdir()

	def git(*args: str) -> str:
		return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
			"-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout.strip()

	git("init", "-q")
	git("commit", "-q", "--allow-empty", "-m", "base")
	base = git("rev-parse", "HEAD")
	git("commit", "-q", "--allow-empty", "-m", "stable")
	stable = git("rev-parse", "HEAD")
	git("checkout", "-q", "-b", "side", base)
	git("commit", "-q", "--allow-empty", "-m", "side")
	side = git("rev-parse", "HEAD")
	return repo, base, stable, side


def _tracker(fake: FakeGitHub, number: int = 4) -> int:
	fake.issues.append({"number": number, "body": writer.render_body([]), "user": {"login": BOT}, "author_association": "OWNER",
		"html_url": f"https://github.com/{REPO}/issues/{number}"})
	return number


def _entry_comment(fake: FakeGitHub, number: int, comment_id: int, body: str, login: str = BOT) -> None:
	fake.comments.setdefault(number, []).append({"id": comment_id, "body": body, "user": {"login": login}})


def _posts(fake: FakeGitHub, number: int) -> list[list[str]]:
	return [args for args in fake.calls if args[:2] == ["api", f"repos/{REPO}/issues/{number}/comments"] and "-f" in args]


def test_operator_step_tick_appends_a_superseding_comment_and_never_patches(fake: FakeGitHub, tmp_path: Path) -> None:
	repo, base, stable, _ = _git_repo(tmp_path)
	number = _tracker(fake)
	original = writer.render_entry("pr-1", "Activation of PR #1", STEPS, base)
	_entry_comment(fake, number, 10, original)
	result = writer.tick(REPO, stable, str(repo), BOT)
	assert result["issue"] == number and result["ticked"] == ["pr-1"] and result["skipped"] == {}
	assert fake.patches() == []
	assert fake.comments[number][0]["body"] == original  # Never edited.
	ticked = fake.comments[number][-1]
	assert ticked["id"] > 10
	lines = ticked["body"].split("\n")
	assert lines[0] == "<!-- ai:operator-step:entry key=pr-1 -->"
	assert lines[1] == f"<!-- ai:operator-step:source-sha={base} -->"
	assert "- [x] **Set NIGHTLY_REPORT_ENABLED**" in lines and "- [ ] **Set NIGHTLY_REPORT_ENABLED**" not in lines
	assert f"<!-- ai:operator-step:done stable={stable} -->" in lines
	# A second run finds the done marker on the newest comment: no second write.
	again = writer.tick(REPO, stable, str(repo), BOT)
	assert again["ticked"] == [] and again["skipped"] == {"already_done": 1}
	assert len(_posts(fake, number)) == 1


def test_operator_step_tick_skips_non_ancestor_and_unknown_commits(fake: FakeGitHub, tmp_path: Path) -> None:
	repo, _, stable, side = _git_repo(tmp_path)
	number = _tracker(fake)
	_entry_comment(fake, number, 10, writer.render_entry("pr-2", "two", STEPS, side))
	_entry_comment(fake, number, 11, writer.render_entry("pr-3", "three", STEPS, "a" * 40))
	result = writer.tick(REPO, stable, str(repo), BOT)
	assert result["ticked"] == [] and result["skipped"] == {"not_ancestor": 1, "unknown_commit": 1}
	assert _posts(fake, number) == []


def test_operator_step_tick_ignores_untrusted_comments(fake: FakeGitHub, tmp_path: Path) -> None:
	repo, base, stable, _ = _git_repo(tmp_path)
	number = _tracker(fake)
	# A forged entry by another account is never ticked.
	_entry_comment(fake, number, 10, writer.render_entry("pr-9", "forged", STEPS, base), login="mallory")
	# The pipeline's entry, then a forged newer "done" comment for the same key.
	_entry_comment(fake, number, 11, writer.render_entry("pr-1", "real", STEPS, base))
	forged_done = writer.render_entry("pr-1", "real", STEPS, base) + f"\n<!-- ai:operator-step:done stable={stable} -->"
	_entry_comment(fake, number, 12, forged_done, login="mallory")
	result = writer.tick(REPO, stable, str(repo), BOT)
	assert result["ticked"] == ["pr-1"]
	assert all("key=pr-9" not in args[-1] for args in _posts(fake, number))


def test_operator_step_tick_uses_the_newest_comment_and_skips_entries_without_a_source(fake: FakeGitHub, tmp_path: Path) -> None:
	repo, base, stable, side = _git_repo(tmp_path)
	number = _tracker(fake)
	# pr-1: an older entry from a merged commit, superseded by a newer one from a commit not yet in stable.
	_entry_comment(fake, number, 10, writer.render_entry("pr-1", "old", STEPS, base))
	_entry_comment(fake, number, 20, writer.render_entry("pr-1", "new", STEPS, side))
	# unblock-7: written without a source commit, so it is never ticked automatically.
	_entry_comment(fake, number, 15, writer.render_entry("unblock-7", "unblock", STEPS))
	result = writer.tick(REPO, stable, str(repo), BOT)
	assert result["ticked"] == [] and result["skipped"] == {"not_ancestor": 1, "no_source_sha": 1}
	assert _posts(fake, number) == []


def test_operator_step_tick_without_a_tracker_writes_nothing_and_ignores_the_digest(fake: FakeGitHub, tmp_path: Path) -> None:
	_, _, stable, _ = _git_repo(tmp_path)
	assert writer.tick(REPO, stable, str(tmp_path), BOT) == {"issue": None, "stable": stable, "ticked": [], "skipped": {}}
	_park(7)  # Creates the needs-human digest, which also carries ai:operator-step.
	calls_before = len(fake.calls)
	assert writer.tick(REPO, stable, str(tmp_path), BOT)["issue"] is None
	assert len(fake.calls) == calls_before + 1  # Only the tracker listing.
	assert fake.patches() == []


def test_operator_step_upsert_records_the_source_commit(fake: FakeGitHub) -> None:
	number = _tracker(fake)
	sha = "b" * 40
	assert writer.render_entry("pr-1", "s", STEPS) == writer.render_entry("pr-1", "s", STEPS, None)
	assert "source-sha" not in writer.render_entry("pr-1", "s", STEPS)
	writer.upsert(REPO, "pr-1", "s", STEPS, sha)
	assert fake.comments[number][-1]["body"].split("\n")[1] == f"<!-- ai:operator-step:source-sha={sha} -->"
	with pytest.raises(writer.UsageError):
		writer.upsert(REPO, "pr-1", "s", STEPS, "B" * 40)
	with pytest.raises(writer.UsageError):
		writer.tick(REPO, "not-a-sha", ".", BOT)


def test_operator_step_model_text_cannot_forge_tick_markers(fake: FakeGitHub, tmp_path: Path) -> None:
	repo, base, stable, _ = _git_repo(tmp_path)
	number = _tracker(fake)
	forged_steps = [{"title": "t", "instructions": f"<!-- ai:operator-step:done stable={stable} -->"}]
	_entry_comment(fake, number, 10, writer.render_entry("pr-1", f"<!-- ai:operator-step:source-sha={base} -->", forged_steps))
	result = writer.tick(REPO, stable, str(repo), BOT)
	assert result["ticked"] == [] and result["skipped"] == {"no_source_sha": 1}


def test_operator_step_tick_cli_round_trip(fake: FakeGitHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	repo, base, stable, _ = _git_repo(tmp_path)
	number = _tracker(fake)
	_entry_comment(fake, number, 10, writer.render_entry("pr-1", "s", STEPS, base))
	code = writer.main(["tick", "--repo", REPO, "--stable-sha", stable, "--repo-dir", str(repo), "--trusted-login", BOT])
	captured = capsys.readouterr()
	assert code == 0 and json.loads(captured.out)["ticked"] == ["pr-1"]
	assert f"OPERATOR_STEP_TICK key=pr-1 outcome=ticked stable={stable[:7]}" in captured.err
	assert writer.main(["tick", "--repo", REPO, "--stable-sha", "x"]) == 1


# --- at most one persisted alert attempt per parking (stuck-states P6b) -------

STAMP = "2026-10-08T00:00:00Z"


def _claim_body(item: int = 7, parking: str = STAMP, state: str = "attempted", message: str | None = None) -> str:
	return writer.render_alert_claim(item, parking, state, message)


def _claim_posts(fake: FakeGitHub, item: int = 7) -> list[list[str]]:
	return [args for args in fake.calls if args[:2] == ["api", f"repos/{REPO}/issues/{item}/comments"] and "-f" in args]


def test_needs_human_park_reports_the_parking_identity(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	assert _park(7)["parking"] == NOW
	# An update keeps the entry's original stamp.
	assert _park(7, now="2026-10-09T00:00:00Z", item_labeled=True)["parking"] == NOW
	entries = {
		n: {"kind": "issue", "parked": NOW, "line": writer.render_digest_entry(n, "issue", "blocked", "item_cap", "", NOW)}
		for n in range(1, writer.NEEDS_HUMAN_MAX_ENTRIES + 1)
	}
	fake.issues[:] = [{"number": 5, "body": writer.render_digest(entries, []), "user": {"login": BOT}}]
	assert _park(1001)["parking"] == "overflow"
	monkeypatch.setenv("NEEDS_HUMAN_DIGEST_ENABLED", "false")
	assert _park(7)["parking"] is None


def test_needs_human_alert_claim_posts_one_claim_and_wins(fake: FakeGitHub) -> None:
	result = writer.needs_human_alert_claim(REPO, 7, STAMP, BOT, now=NOW)
	assert result["claimed"] is True and result["comment_id"] == 5000 and result["alert_attempted_at"] == NOW
	posts = _claim_posts(fake)
	assert len(posts) == 1
	last = posts[0][-1].split("\n")[-1]
	assert last == f"<!-- ai:unblock-park-alert:v1 item=7 parked={STAMP} state=attempted -->"
	assert writer.NEEDS_HUMAN_ALERT_MARKER_RE.match(last)
	# Two paginated reads (before and after the claim), one POST.
	assert len([args for args in fake.calls if "--slurp" in args]) == 2


@pytest.mark.parametrize("prior", [
	_claim_body(),
	_claim_body(state="confirmed", message="4242"),
	_claim_body(parking="overflow"),
])
def test_needs_human_alert_claim_skips_an_attempted_parking(fake: FakeGitHub, prior: str) -> None:
	fake.comments[7] = [{"id": 10, "body": prior, "user": {"login": BOT}}]
	result = writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)
	assert result["claimed"] is False and result["reason"] == "already_attempted" and result["comment_id"] == 10
	assert _claim_posts(fake) == []


@pytest.mark.parametrize("prior", [
	_claim_body(parking="2026-01-01T00:00:00Z"),  # An earlier parking of the same item.
	_claim_body(item=8),  # Another item.
	"quoted\n" + _claim_body() + "\nand more",  # Not the last line.
])
def test_needs_human_alert_claim_ignores_claims_for_other_parkings(fake: FakeGitHub, prior: str) -> None:
	fake.comments[7] = [{"id": 10, "body": prior, "user": {"login": BOT}}]
	assert writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)["claimed"] is True


def test_needs_human_alert_claim_ignores_a_forged_claim(fake: FakeGitHub) -> None:
	fake.comments[7] = [{"id": 10, "body": _claim_body(state="confirmed", message="1"), "user": {"login": "mallory"}}]
	result = writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)
	assert result["claimed"] is True and len(_claim_posts(fake)) == 1


def test_needs_human_alert_claim_loses_to_a_concurrent_lower_id_claim(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	original = fake.__call__

	def racing(args: list[str], *, allow_existing_label: bool = False) -> str:
		out = original(args, allow_existing_label=allow_existing_label)
		if "-f" in args and args[1] == f"repos/{REPO}/issues/7/comments":
			# Another run's claim landed first (lower id).
			fake.comments[7].insert(0, {"id": 10, "body": _claim_body(), "user": {"login": BOT}})
		return out

	monkeypatch.setattr(writer, "_gh", racing)
	result = writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)
	assert result["claimed"] is False and result["reason"] == "lost_race"


def test_needs_human_alert_claim_unverified_when_its_claim_is_not_listed(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	original = fake.__call__

	def lagging(args: list[str], *, allow_existing_label: bool = False) -> str:
		out = original(args, allow_existing_label=allow_existing_label)
		if "-f" in args and args[1] == f"repos/{REPO}/issues/7/comments":
			fake.comments[7] = []  # The listing lags behind the POST.
		return out

	monkeypatch.setattr(writer, "_gh", lagging)
	result = writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)
	assert result["claimed"] is False and result["reason"] == "claim_unverified"


@pytest.mark.parametrize("fail", ["post", "reread", "no_id"])
def test_needs_human_alert_claim_failures_exit_2(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
		capsys: pytest.CaptureFixture[str], fail: str) -> None:
	original = fake.__call__
	reads = {"count": 0}

	def failing(args: list[str], *, allow_existing_label: bool = False) -> str:
		if "--slurp" in args:
			reads["count"] += 1
			if fail == "reread" and reads["count"] == 2:
				raise writer.ApiError("re-read failed")
		if "-f" in args and args[1] == f"repos/{REPO}/issues/7/comments":
			if fail == "post":
				raise writer.ApiError("post failed")
			if fail == "no_id":
				return "{}"
		return original(args, allow_existing_label=allow_existing_label)

	monkeypatch.setattr(writer, "_gh", failing)
	code = writer.main(["needs-human", "alert-claim", "--repo", REPO, "--item", "7", "--parking", STAMP, "--trusted-login", BOT])
	captured = capsys.readouterr()
	assert code == 2 and "error" in json.loads(captured.out)
	assert "NEEDS_HUMAN_ALERT item=7 parking=" in captured.err and "outcome=failed" in captured.err


def test_needs_human_alert_claim_kill_switch_and_bad_arguments(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	assert writer.main(["needs-human", "alert-claim", "--repo", REPO, "--item", "7", "--parking", "yesterday"]) == 1
	assert writer.main(["needs-human", "alert-confirm", "--repo", REPO, "--item", "7", "--parking", STAMP,
		"--comment-id", "5000", "--message-id", "12a"]) == 1
	assert writer.main(["needs-human", "alert-confirm", "--repo", REPO, "--item", "7", "--parking", STAMP,
		"--comment-id", "0", "--message-id", "12"]) == 1
	assert fake.calls == []
	monkeypatch.setenv("NEEDS_HUMAN_DIGEST_ENABLED", "false")
	assert writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)["reason"] == "disabled"
	assert writer.needs_human_alert_confirm(REPO, 7, STAMP, "5000", "12")["reason"] == "disabled"
	assert fake.calls == []


def test_needs_human_alert_confirm_patches_the_claim_in_place(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
		capsys: pytest.CaptureFixture[str]) -> None:
	result = writer.needs_human_alert_confirm(REPO, 7, STAMP, "5000", "4242")
	assert result["confirmed"] is True
	patch = fake.patches()
	assert len(patch) == 1 and patch[0][3] == f"repos/{REPO}/issues/comments/5000"
	assert patch[0][-1].endswith(f"<!-- ai:unblock-park-alert:v1 item=7 parked={STAMP} state=confirmed message=4242 -->")
	# A confirmed claim still counts as the parking's one attempt.
	fake.comments[7] = [{"id": 5000, "body": patch[0][-1][len("body="):], "user": {"login": BOT}}]
	assert writer.needs_human_alert_claim(REPO, 7, STAMP, BOT)["reason"] == "already_attempted"

	def failing(args: list[str], *, allow_existing_label: bool = False) -> str:
		raise writer.ApiError("patch failed")

	monkeypatch.setattr(writer, "_gh", failing)
	capsys.readouterr()
	assert writer.main(["needs-human", "alert-confirm", "--repo", REPO, "--item", "7", "--parking", STAMP,
		"--comment-id", "5000", "--message-id", "4242"]) == 2
	assert "outcome=failed reason=confirm_write_failed" in capsys.readouterr().err


def test_needs_human_cli_dispatch_keeps_prune_reachable(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	assert writer.main(["needs-human", "prune", "--repo", REPO, "--trusted-login", BOT]) == 0
	assert json.loads(capsys.readouterr().out)["reason"] == "no_digest"


# --- resolving released entries (stuck-states P10, Refs #8291) ----------------

RELEASE_SHA = "c" * 40
RELEASE_STEPS = [{"title": "Ship the change to consumers", "instructions": "The promote cycle tags stable.",
	"dormant_until": f"@stable contains {RELEASE_SHA}"}]
SECRET_STEP = {"title": "Add the DEPLOY_TOKEN secret", "instructions": "gh secret set DEPLOY_TOKEN", "dormant_until": "DEPLOY_TOKEN"}


def _release_entry(fake: FakeGitHub, number: int, comment_id: int, key: str, sha: str | None = RELEASE_SHA,
		steps: list[dict] | None = None, login: str = BOT) -> str:
	body = writer.render_entry(key, f"Activation of {key}", steps or RELEASE_STEPS, sha)
	_entry_comment(fake, number, comment_id, body, login=login)
	return body


def _compares(fake: FakeGitHub) -> list[list[str]]:
	return [args for args in fake.calls if args[0] == "api" and len(args) > 1 and "/compare/" in args[1]]


def _issue_patches(fake: FakeGitHub, number: int) -> list[list[str]]:
	return [args for args in fake.patches() if args[3] == f"repos/{REPO}/issues/{number}"]


@pytest.mark.parametrize(("status", "outcome"), [
	("ahead", "ticked"), ("identical", "ticked"), ("behind", "not_contained"), ("diverged", "not_contained"),
])
def test_operator_step_resolve_ticks_only_contained_release_entries(fake: FakeGitHub, status: str, outcome: str) -> None:
	number = _tracker(fake)
	original = _release_entry(fake, number, 10, "pr-1")
	fake.compare_status[RELEASE_SHA] = status
	result = writer.resolve(REPO, BOT)
	assert result["compares"] == 1
	assert _compares(fake)[0][1] == f"repos/{REPO}/compare/{RELEASE_SHA}...stable"
	assert fake.comments[number][0]["body"] == original  # Never edited.
	assert not any("/comments/" in args[3] for args in fake.patches())
	if outcome == "ticked":
		assert result["ticked"] == ["pr-1"] and result["skipped"] == {} and result["unticked"] == 0
		resolved = next(comment for comment in fake.comments[number] if "ai:operator-step:resolved" in comment["body"])
		lines = resolved["body"].split("\n")
		assert lines[0] == "<!-- ai:operator-step:entry key=pr-1 -->"
		assert lines[1] == f"<!-- ai:operator-step:source-sha={RELEASE_SHA} -->"
		assert "- [x] **Ship the change to consumers**" in lines and not any(line.startswith("- [ ]") for line in lines)
		assert f"<!-- ai:operator-step:resolved release=stable status={status} -->" in lines
		assert any(writer.RESOLVED_RE.match(line) for line in lines)
	else:
		assert result["ticked"] == [] and result["skipped"] == {"not_contained": 1}
		assert _posts(fake, number) == [] and fake.patches() == [] and result["closed"] is False


def test_operator_step_resolve_compare_failure_halts_later_compares(fake: FakeGitHub) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1", "d" * 40)  # No status: the compare fails.
	_release_entry(fake, number, 11, "pr-2", "e" * 40)
	fake.compare_status["e" * 40] = "ahead"
	result = writer.resolve(REPO, BOT)
	assert result["skipped"] == {"compare_failed": 1, "deferred": 1} and result["ticked"] == []
	assert len(_compares(fake)) == 1 and result["closed"] is False and fake.closed == []


def test_operator_step_resolve_caps_and_caches_compares(fake: FakeGitHub) -> None:
	number = _tracker(fake)
	for n in range(1, 13):
		_release_entry(fake, number, 10 + n, f"pr-{n}", f"{n:040x}")
		fake.compare_status[f"{n:040x}"] = "behind"
	result = writer.resolve(REPO, BOT)
	assert result["compares"] == writer.MAX_RESOLVE_COMPARES == 10
	assert result["skipped"] == {"not_contained": 10, "deferred": 2}
	# Oldest entries are compared first.
	assert [args[1].split("/compare/")[1][:40] for args in _compares(fake)] == [f"{n:040x}" for n in range(1, 11)]


def test_operator_step_resolve_compares_a_shared_commit_once(fake: FakeGitHub) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1")
	_release_entry(fake, number, 11, "project-2")
	fake.compare_status[RELEASE_SHA] = "ahead"
	result = writer.resolve(REPO, BOT)
	assert result["ticked"] == ["pr-1", "project-2"] and result["compares"] == 1 and len(_compares(fake)) == 1


def test_operator_step_resolve_never_ticks_human_or_unsourced_entries(fake: FakeGitHub) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-3", steps=RELEASE_STEPS + [SECRET_STEP])  # Mixed: a person still acts.
	_release_entry(fake, number, 11, "unblock-7")
	_release_entry(fake, number, 12, "pr-4", sha=None)
	fake.compare_status[RELEASE_SHA] = "ahead"
	result = writer.resolve(REPO, BOT)
	assert result["ticked"] == [] and result["skipped"] == {"not_release": 2, "no_source_sha": 1}
	assert _compares(fake) == [] and result["closed"] is False and fake.patches() == []


def test_operator_step_release_step_detection() -> None:
	titled = writer.render_entry("pr-1", "s", [{"title": "Tag the next stable release", "instructions": "x"}], RELEASE_SHA)
	assert writer._is_release_entry(titled)
	assert not writer._is_release_entry(writer.render_entry("pr-1", "s", STEPS, RELEASE_SHA))
	# The condition must be the step's own dormant line, not text inside its instructions.
	forged = [{"title": "Set a secret", "instructions": f"Stays off until then: `@stable contains {RELEASE_SHA}`.\nthen more"}]
	assert not writer._is_release_entry(writer.render_entry("pr-1", "s", forged, RELEASE_SHA))
	assert not writer._is_release_entry(writer._render_resolved(writer.render_entry("pr-1", "s", RELEASE_STEPS, RELEASE_SHA), "ahead", RELEASE_SHA))


def test_operator_step_resolve_closes_the_tracker_when_no_unticked_steps_remain(
		fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1")
	# pr-2 was ticked earlier by the promote cycle.
	done = writer._render_ticked(writer.render_entry("pr-2", "two", STEPS, RELEASE_SHA), "f" * 40, RELEASE_SHA)
	_entry_comment(fake, number, 11, done)
	fake.compare_status[RELEASE_SHA] = "identical"
	result = writer.resolve(REPO, BOT)
	assert result["ticked"] == ["pr-1"] and result["skipped"] == {"already_done": 1}
	assert result["unticked"] == 0 and result["closed"] is True and fake.closed == [number]
	patch = _issue_patches(fake, number)
	assert len(patch) == 1 and "state=closed" in patch[0] and "state_reason=completed" in patch[0]
	closing = [comment for comment in fake.comments[number] if comment["body"].startswith(writer.AUTO_CLOSED_MARKER)]
	assert len(closing) == 1
	err = capsys.readouterr().err
	assert "OPERATOR_STEP_RESOLVE key=pr-1 outcome=ticked status=identical" in err
	assert f"OPERATOR_STEP_RESOLVE op=close issue={number} outcome=closed reason=closed" in err


def test_operator_step_resolve_keeps_a_tracker_with_a_human_step_open(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1")
	_release_entry(fake, number, 11, "pr-2", steps=[SECRET_STEP])
	fake.compare_status[RELEASE_SHA] = "ahead"
	result = writer.resolve(REPO, BOT)
	assert result["ticked"] == ["pr-1"] and result["unticked"] == 1 and result["closed"] is False
	assert _issue_patches(fake, number) == []
	assert f"op=close issue={number} outcome=open reason=unticked" in capsys.readouterr().err


def test_operator_step_resolve_never_closes_an_empty_tracker(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	number = _tracker(fake)
	result = writer.resolve(REPO, BOT)
	assert result["closed"] is False and fake.patches() == [] and _posts(fake, number) == []
	assert "reason=no_entries" in capsys.readouterr().err


def test_operator_step_resolve_never_closes_a_reopened_tracker(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	number = _tracker(fake)
	fake.issues[0]["state_reason"] = "reopened"
	_entry_comment(fake, number, 10, writer._render_resolved(writer.render_entry("pr-1", "s", RELEASE_STEPS, RELEASE_SHA), "ahead", RELEASE_SHA))
	result = writer.resolve(REPO, BOT)
	assert result["skipped"] == {"already_done": 1} and result["closed"] is False and fake.patches() == []
	assert "reason=reopened" in capsys.readouterr().err


def test_operator_step_resolve_skips_the_close_when_a_new_entry_lands(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1")
	fake.compare_status[RELEASE_SHA] = "ahead"
	original = fake.__call__
	reads = {"count": 0}

	def racing(args: list[str], *, allow_existing_label: bool = False) -> str:
		out = original(args, allow_existing_label=allow_existing_label)
		if "--slurp" in args:
			reads["count"] += 1
			if reads["count"] == 1:
				# An activation run writes a new entry after resolve read the comments.
				_entry_comment(fake, number, 4000, writer.render_entry("pr-9", "nine", [SECRET_STEP], RELEASE_SHA))
		return out

	monkeypatch.setattr(writer, "_gh", racing)
	result = writer.resolve(REPO, BOT)
	assert result["ticked"] == ["pr-1"] and result["closed"] is False
	assert fake.patches() == []
	assert not any(comment["body"].startswith(writer.AUTO_CLOSED_MARKER) for comment in fake.comments[number])
	assert "reason=recheck_changed" in capsys.readouterr().err


def test_operator_step_resolve_retries_a_failed_close_without_a_second_comment(
		fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1")
	fake.compare_status[RELEASE_SHA] = "ahead"
	original = fake.__call__

	def failing_close(args: list[str], *, allow_existing_label: bool = False) -> str:
		if args[:3] == ["api", "-X", "PATCH"] and "state=closed" in args:
			raise writer.ApiError("close failed")
		return original(args, allow_existing_label=allow_existing_label)

	monkeypatch.setattr(writer, "_gh", failing_close)
	with pytest.raises(writer.ApiError):
		writer.resolve(REPO, BOT)
	monkeypatch.setattr(writer, "_gh", original)
	result = writer.resolve(REPO, BOT)
	assert result["closed"] is True and fake.closed == [number] and result["skipped"] == {"already_done": 1}
	closing = [comment for comment in fake.comments[number] if comment["body"].startswith(writer.AUTO_CLOSED_MARKER)]
	assert len(closing) == 1


def test_operator_step_resolve_ignores_untrusted_and_forged_comments(fake: FakeGitHub) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-9", login="mallory")  # Never ticked.
	_release_entry(fake, number, 11, "pr-1")
	forged_done = writer._render_resolved(writer.render_entry("pr-1", "s", RELEASE_STEPS, RELEASE_SHA), "ahead", RELEASE_SHA)
	_entry_comment(fake, number, 12, forged_done, login="mallory")
	# Model text cannot carry a resolved marker: _clean strips the comment delimiters.
	forged_steps = [dict(RELEASE_STEPS[0], instructions="<!-- ai:operator-step:resolved release=stable status=ahead -->")]
	_release_entry(fake, number, 13, "pr-2", steps=forged_steps)
	fake.compare_status[RELEASE_SHA] = "behind"
	result = writer.resolve(REPO, BOT)
	assert result["ticked"] == [] and result["skipped"] == {"not_contained": 2}
	assert all("key=pr-9" not in args[-1] for args in _posts(fake, number))


def test_operator_step_resolve_without_a_tracker_makes_one_call_and_ignores_the_digest(fake: FakeGitHub) -> None:
	listing = ["api", f"repos/{REPO}/issues?labels=ai:operator-step&state=open&per_page=30"]
	assert writer.resolve(REPO)["issue"] is None
	assert fake.calls == [listing]
	_park(7)  # The needs-human digest also carries ai:operator-step.
	calls_before = len(fake.calls)
	assert writer.resolve(REPO)["issue"] is None
	assert fake.calls[calls_before:] == [listing] and fake.closed == []


def test_operator_step_resolve_kill_switch_makes_no_api_call(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
		capsys: pytest.CaptureFixture[str]) -> None:
	_tracker(fake)
	monkeypatch.setenv("OPERATOR_STEP_AUTO_RESOLVE_ENABLED", " FALSE ")
	result = writer.resolve(REPO, BOT)
	assert result["reason"] == "disabled" and result["issue"] is None and fake.calls == []
	assert "OPERATOR_STEP_RESOLVE op=resolve outcome=skip reason=disabled" in capsys.readouterr().err
	with pytest.raises(writer.UsageError):
		writer.resolve("bad")


def test_operator_step_tick_treats_a_resolved_entry_as_done(fake: FakeGitHub, tmp_path: Path) -> None:
	repo, base, stable, _ = _git_repo(tmp_path)
	number = _tracker(fake)
	entry = writer.render_entry("pr-1", "s", RELEASE_STEPS, base)
	_entry_comment(fake, number, 10, entry)
	_entry_comment(fake, number, 11, writer._render_resolved(entry, "ahead", base))
	result = writer.tick(REPO, stable, str(repo), BOT)
	assert result["ticked"] == [] and result["skipped"] == {"already_done": 1}
	assert _posts(fake, number) == []


def test_operator_step_resolve_cli_round_trip(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
	number = _tracker(fake)
	_release_entry(fake, number, 10, "pr-1")
	fake.compare_status[RELEASE_SHA] = "ahead"
	assert writer.main(["resolve", "--repo", REPO, "--trusted-login", BOT]) == 0
	out = json.loads(capsys.readouterr().out)
	assert out["issue"] == number and out["ticked"] == ["pr-1"] and out["closed"] is True
	assert writer.main(["resolve", "--repo", "bad"]) == 1


def test_operator_step_release_condition_matches_activation_verify() -> None:
	script = (ROOT / "scripts" / "activation_verify.sh").read_text(encoding="utf-8")
	match = re.search(r'RELEASE_CONDITION_RE = re\.compile\(r"(.+?)", re\.I\)', script)
	assert match and match.group(1) == writer.RELEASE_CONDITION_RE.pattern
	assert writer.RELEASE_CONDITION_RE.flags & re.I


def test_poller_wires_operator_step_resolve_step() -> None:
	# Plain-text parse of the poll job's steps: PyYAML is not installed in
	# every test container, and this test must not skip without it.
	text = (ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	poll_job = re.search(r"\n  poll:\n(.*?)(?=\n  [A-Za-z0-9_-]+:\n|\Z)", text, re.S)
	assert poll_job
	blocks = poll_job.group(1).split("\n      - ")[1:]
	names = [block.split("\n", 1)[0][len("name: "):].strip() if block.startswith("name: ") else None for block in blocks]
	block = blocks[names.index("Resolve released operator steps")]
	assert names.index("Resume dependency waits") + 1 == names.index("Resolve released operator steps") < names.index("Find active tracking issues")
	keys = re.findall(r"^        ([A-Za-z-]+):", block, re.M)
	assert "if" not in keys and re.search(r"^        continue-on-error: true$", block, re.M)
	env_block = re.search(r"^        env:\n((?:          .*\n)+)", block, re.M)
	assert env_block
	env = dict(line.strip().split(": ", 1) for line in env_block.group(1).splitlines())
	assert env == {
		"GH_TOKEN": "${{ secrets.GH_PAT }}",
		"GITHUB_REPOSITORY": "${{ github.repository }}",
		"OPERATOR_STEP_AUTO_RESOLVE_ENABLED": "${{ vars.OPERATOR_STEP_AUTO_RESOLVE_ENABLED || 'true' }}",
	}
	run_block = re.search(r"^        run: \|\n((?:          .*\n|\n)+)", block + "\n", re.M)
	assert run_block
	run = "\n".join(line[10:] for line in run_block.group(1).splitlines())
	assert 'helper=".codex-workflow-src/scripts/operator_step_issue.py"' in run
	assert '[ -L "${helper}" ]' in run and "grep -q 'OPERATOR_STEP_RESOLVE' \"${helper}\"" in run
	assert "::notice::OPERATOR_STEP_RESOLVE op=resolve outcome=skip reason=helper_missing" in run
	assert run.index("exit 0") < run.index("python3 -I -B")
	assert 'PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${helper}" resolve --repo "${GITHUB_REPOSITORY}"' in run
	assert "outcome=error reason=helper_exit" in run
