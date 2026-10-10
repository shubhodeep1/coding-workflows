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

	def __call__(self, args: list[str], *, allow_existing_label: bool = False) -> str:
		self.calls.append(args)
		if args[:2] == ["label", "create"]:
			return ""
		if args[:2] == ["api", "user"]:
			return BOT
		if args[:2] == ["api", "-X"] and args[2] == "POST" and args[3].endswith("/labels"):
			self.labels_added.append((args[3], args[-1]))
			return "{}"
		if args[:3] == ["api", "-X", "DELETE"]:
			self.labels_removed.append(args[3])
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
