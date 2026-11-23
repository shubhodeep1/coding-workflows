"""release_head_bound_needs_human_latches (scripts/orchestrate_poll_process.sh).

PR #9839: the workflow-failure-heal intake latched ai:needs-human for head
27dd193 (non-retryable review editor failure). The PR moved to head bcd1d378,
the review-blocked judge chose "merge" five times, and the PR still waited for
a person to remove the label. The review-round cap latch (PR #9675, #9283)
had the same shape. These tests run the function on its own against a fake
`gh` so every guard can be exercised without the full poller mock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REPO = "o/r"
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40

FAKE_GH = textwrap.dedent(
	r'''
	#!/usr/bin/env python3
	import json, os, re, subprocess, sys
	d = os.environ["FAKE_GH_DIR"]
	args = sys.argv[1:]
	with open(os.path.join(d, "calls.log"), "a", encoding="utf-8") as fh:
		fh.write(json.dumps(args) + "\n")
	def load(name, default):
		p = os.path.join(d, name)
		if not os.path.exists(p):
			return default
		with open(p, encoding="utf-8") as fh:
			return json.load(fh)
	def nth():
		# 1 for the first identical call, 2 for its first repeat, ...
		with open(os.path.join(d, "calls.log"), encoding="utf-8") as fh:
			return sum(1 for line in fh if json.loads(line) == args)
	def load_seq(name, default):
		# The nth identical read serves "<name>.<n>" when that file exists,
		# so a revalidation re-read can see a changed state.
		n = nth()
		stem, ext = os.path.splitext(name)
		p = os.path.join(d, "%s.%d%s" % (stem, n, ext))
		if os.path.exists(p):
			with open(p, encoding="utf-8") as fh:
				return json.load(fh)
		return load(name, default)
	fail = set(os.environ.get("FAKE_GH_FAIL", "").split(","))
	assert args[0] == "api"
	rest = [a for a in args[1:] if a != "--paginate"]
	method = "GET"
	if "-X" in rest:
		i = rest.index("-X")
		method = rest[i + 1]
		del rest[i:i + 2]
	jq = None
	if "--jq" in rest:
		i = rest.index("--jq")
		jq = rest[i + 1]
		del rest[i:i + 2]
	fields = []
	while "-f" in rest:
		i = rest.index("-f")
		fields.append(rest[i + 1])
		del rest[i:i + 2]
	path = rest[0]
	if fields and method == "GET":
		method = "POST"  # gh api sends -f fields as a POST
	def out(obj):
		if jq is not None:
			p = subprocess.run(["jq", "-c", jq], input=json.dumps(obj), capture_output=True, text=True)
			sys.stdout.write(p.stdout)
			sys.exit(p.returncode)
		print(json.dumps(obj))
		sys.exit(0)
	if method == "GET" and "/issues?" in path:
		if "list" in fail:
			sys.exit(1)
		out(load("list.json", []))
	m = re.search(r"/issues/(\d+)/comments", path)
	if m and method == "GET":
		if "comments" in fail:
			sys.exit(1)
		if "comments_refresh" in fail and nth() > 1:
			sys.exit(1)
		if "comments_post_delete" in fail and nth() > 2:
			sys.exit(1)
		out(load_seq("comments.json", []))
	if m and fields:
		if "comment_post" in fail:
			sys.exit(1)
		with open(os.path.join(d, "posted.log"), "a", encoding="utf-8") as fh:
			fh.write(json.dumps({"pr": m.group(1), "body": fields[0][len("body="):]}) + "\n")
		out({"id": 1})
	if re.search(r"/issues/(\d+)/events", path):
		if "events" in fail:
			sys.exit(1)
		out(load_seq("events.json", []))
	m = re.search(r"/issues/(\d+)/labels$", path)
	if m and method == "POST":
		if "label_add" in fail:
			sys.exit(1)
		with open(os.path.join(d, "added.log"), "a", encoding="utf-8") as fh:
			fh.write(m.group(1) + " " + ",".join(f[len("labels[]="):] for f in fields) + "\n")
		out([])
	m = re.search(r"/issues/(\d+)/labels/(.+)$", path)
	if m and method == "DELETE":
		if "label_delete" in fail:
			sys.exit(1)
		with open(os.path.join(d, "deleted.log"), "a", encoding="utf-8") as fh:
			fh.write(m.group(1) + " " + m.group(2) + "\n")
		out([])
	m = re.search(r"/actions/runs/(\d+)$", path)
	if m and method == "GET":
		if "run" in fail:
			sys.exit(1)
		runs = load("runs.json", {})
		if m.group(1) not in runs:
			sys.exit(1)
		out(runs[m.group(1)])
	if re.search(r"/pulls/(\d+)$", path):
		if "pull" in fail:
			sys.exit(1)
		if "pull_refresh" in fail and nth() > 1:
			sys.exit(1)
		out(load_seq("pull.json", {}))
	sys.stderr.write("unexpected gh call: %r\n" % (args,))
	sys.exit(2)
	'''
).lstrip()


def _function_source() -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("release_head_bound_needs_human_latches() {")
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def _non_retryable_comment(head: str = OLD_HEAD, created_at: str = "2026-11-20T11:15:10Z", login: str = "pipeline") -> dict:
	return {
		"user": {"login": login},
		"author_association": "OWNER",
		"created_at": created_at,
		"body": (
			"**Workflow failure heal: this review/autofix failure cannot be fixed by another run**\n\n"
			"Make that change by hand, push, then remove `ai:needs-human`.\n\n"
			f"<!-- ai:workflow-heal-non-retryable:v1 pr=5 head={head} reason=review_editor_sandbox_path_excluded -->"
		),
	}


def _round_cap_comment(head: str | None = OLD_HEAD, created_at: str = "2026-11-20T11:15:10Z") -> dict:
	head_marker = f"<!-- ai:review-round-cap-head:v1 head={head} -->\n" if head else ""
	return {
		"user": {"login": "pipeline"},
		"author_association": "OWNER",
		"created_at": created_at,
		"body": f"The review sweep has stopped dispatching reviews for this PR. The PR is labelled `ai:needs-human`.\n\n{head_marker}<!-- ai:review-round-cap:v1 -->",
	}


def _labeled(at: str = "2026-11-20T11:15:12Z", login: str = "pipeline") -> dict:
	return {"event": "labeled", "label": {"name": "ai:needs-human"}, "actor": {"login": login}, "created_at": at}


def _run(
	tmp_path: Path,
	*,
	comments: list[dict],
	events: list[dict] | None = None,
	labels: tuple[str, ...] = ("ai:needs-human", "ai:review-blocked"),
	head: str = NEW_HEAD,
	pull_state: str = "open",
	pull_labels: tuple[str, ...] | None = None,
	is_pr: bool = True,
	fail: str = "",
	enabled: str | None = None,
	refreshed_comments: list[dict] | None = None,
	refreshed_events: list[dict] | None = None,
	refreshed_pull: dict | None = None,
	post_delete_comments: list[dict] | None = None,
	runs: dict | None = None,
	numbers: tuple[int, ...] = (5,),
	pull_ref: str = "ai/issue-9",
	pull_head_repo: str = REPO,
) -> dict:
	fake_dir = tmp_path / "fake"
	fake_dir.mkdir(parents=True)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	items = []
	for number in numbers:
		item = {"number": number, "labels": [{"name": name} for name in labels]}
		if is_pr:
			item["pull_request"] = {"url": "x"}
		items.append(item)
	(fake_dir / "list.json").write_text(json.dumps(items), encoding="utf-8")
	if runs is not None:
		(fake_dir / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
	(fake_dir / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
	(fake_dir / "events.json").write_text(json.dumps(events if events is not None else [_labeled()]), encoding="utf-8")
	if refreshed_comments is not None:
		(fake_dir / "comments.2.json").write_text(json.dumps(refreshed_comments), encoding="utf-8")
	if post_delete_comments is not None:
		(fake_dir / "comments.3.json").write_text(json.dumps(post_delete_comments), encoding="utf-8")
	if refreshed_events is not None:
		(fake_dir / "events.2.json").write_text(json.dumps(refreshed_events), encoding="utf-8")
	(fake_dir / "pull.json").write_text(
		json.dumps({"state": pull_state, "head": {"sha": head, "ref": pull_ref, "repo": {"full_name": pull_head_repo}}, "labels": [{"name": n} for n in (pull_labels if pull_labels is not None else labels)]}),
		encoding="utf-8",
	)
	if refreshed_pull is not None:
		(fake_dir / "pull.2.json").write_text(json.dumps(refreshed_pull), encoding="utf-8")
	script = tmp_path / "run.sh"
	script.write_text(
		"set -euo pipefail\n"
		"gh_retry() { \"$@\"; }\n"
		"_is_truthy() { case \"${1,,}\" in 1|true|yes|on) return 0;; *) return 1;; esac; }\n"
		+ _function_source()
		+ "\nrelease_head_bound_needs_human_latches\n",
		encoding="utf-8",
	)
	env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(fake_dir), "FAKE_GH_FAIL": fail, "GITHUB_REPOSITORY": REPO}
	env.pop("NEEDS_HUMAN_HEAD_LATCH_AUTO_RELEASE_ENABLED", None)
	if enabled is not None:
		env["NEEDS_HUMAN_HEAD_LATCH_AUTO_RELEASE_ENABLED"] = enabled
	proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=60, check=False)

	def lines(name: str) -> list[str]:
		p = fake_dir / name
		return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

	return {
		"rc": proc.returncode,
		"stdout": proc.stdout,
		"stderr": proc.stderr,
		"deleted": lines("deleted.log"),
		"added": lines("added.log"),
		"posted": [json.loads(line) for line in lines("posted.log")],
		"calls": [json.loads(line) for line in lines("calls.log")],
	}


def test_non_retryable_latch_is_released_once_the_head_moved(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_non_retryable_comment()])
	assert result["rc"] == 0, result["stderr"]
	assert result["deleted"] == ["5 ai:needs-human"]
	assert len(result["posted"]) == 1
	body = result["posted"][0]["body"]
	assert f"<!-- ai:needs-human-auto-release reason=non_retryable_head_moved pr=5 head={NEW_HEAD} -->" in body
	assert OLD_HEAD[:12] in body and NEW_HEAD[:12] in body
	assert f"NEEDS_HUMAN_HEAD_LATCH pr=5 outcome=released latch=non_retryable_head_moved from={OLD_HEAD[:12]} head={NEW_HEAD[:12]}" in result["stdout"]
	# §15: one list, one comments read, one pull read, one events read, one
	# comments, one events and one pull re-read (revalidation), the label
	# removal, one comments read after it, and the release comment.
	assert len(result["calls"]) == 10
	assert result["added"] == []


def test_round_cap_latch_with_head_marker_is_released_once_the_head_moved(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_round_cap_comment()])
	assert result["deleted"] == ["5 ai:needs-human"]
	assert "reason=review_round_cap_head_moved pr=5" in result["posted"][0]["body"]
	assert "outcome=released latch=review_round_cap_head_moved" in result["stdout"]


def test_legacy_round_cap_comment_without_head_is_left_for_a_person(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_round_cap_comment(head=None)])
	assert result["deleted"] == [] and result["posted"] == []
	assert "outcome=skip reason=latch_head_missing latch=review_round_cap_head_moved" in result["stdout"]


def test_same_head_stays_latched(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_non_retryable_comment()], head=OLD_HEAD)
	assert result["deleted"] == [] and result["posted"] == []
	assert "outcome=skip reason=head_unchanged" in result["stdout"]


def test_already_released_for_this_head_is_not_released_again(tmp_path: Path) -> None:
	release = {
		"user": {"login": "pipeline"},
		"author_association": "OWNER",
		"created_at": "2026-11-20T12:00:00Z",
		"body": f"_Orchestrator: removed it._\n\n<!-- ai:needs-human-auto-release reason=non_retryable_head_moved pr=5 head={NEW_HEAD} -->",
	}
	# The release comment mentions no label name, so only the release-once
	# marker stops it.
	result = _run(tmp_path, comments=[_non_retryable_comment(), release])
	assert result["deleted"] == []
	assert "outcome=skip reason=already_released" in result["stdout"]


def test_untrusted_release_marker_does_not_block_the_release(tmp_path: Path) -> None:
	spoof = {
		"user": {"login": "drive-by"},
		"author_association": "NONE",
		"created_at": "2026-11-20T12:00:00Z",
		"body": f"<!-- ai:needs-human-auto-release reason=non_retryable_head_moved pr=5 head={NEW_HEAD} -->",
	}
	result = _run(tmp_path, comments=[_non_retryable_comment(), spoof])
	assert result["deleted"] == ["5 ai:needs-human"]
	assert "outcome=released" in result["stdout"]


def test_parked_pr_on_the_latched_head_costs_no_events_read(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_non_retryable_comment()], head=OLD_HEAD)
	assert "outcome=skip reason=head_unchanged" in result["stdout"]
	assert not any("/events" in arg for call in result["calls"] for arg in call)


def test_a_latch_written_during_the_release_is_kept(tmp_path: Path) -> None:
	relatch = _non_retryable_comment(head=NEW_HEAD, created_at="2026-11-20T14:00:00Z")
	comment_case = _run(tmp_path / "comment", comments=[_non_retryable_comment()], refreshed_comments=[_non_retryable_comment(), relatch])
	assert comment_case["deleted"] == [] and comment_case["posted"] == []
	assert "outcome=skip reason=latch_changed_during_release" in comment_case["stdout"]
	event_case = _run(tmp_path / "event", comments=[_non_retryable_comment()], refreshed_events=[_labeled(), _labeled(at="2026-11-20T14:00:02Z", login="someone-else")])
	assert event_case["deleted"] == [] and event_case["posted"] == []
	assert "outcome=skip reason=latch_changed_during_release" in event_case["stdout"]
	unavailable = _run(tmp_path / "unavailable", comments=[_non_retryable_comment()], fail="comments_refresh")
	assert unavailable["deleted"] == [] and "outcome=skip reason=latch_revalidation_unavailable" in unavailable["stdout"]


def test_a_latch_that_races_the_label_removal_gets_its_label_back(tmp_path: Path) -> None:
	# The writer commented and re-added the (still present, so eventless)
	# label after the revalidation reads, just before the DELETE.
	relatch = _non_retryable_comment(head=NEW_HEAD, created_at="2026-11-20T14:00:00Z")
	result = _run(tmp_path / "relatch", comments=[_non_retryable_comment()], post_delete_comments=[_non_retryable_comment(), relatch])
	assert result["deleted"] == ["5 ai:needs-human"]
	assert result["added"] == ["5 ai:needs-human"]
	assert result["posted"] == []
	assert "outcome=relatched_after_release latch=non_retryable_head_moved" in result["stdout"]
	failed = _run(tmp_path / "failed", comments=[_non_retryable_comment()], post_delete_comments=[_non_retryable_comment(), relatch], fail="label_add")
	assert failed["rc"] == 0 and failed["posted"] == []
	assert "outcome=relatch_failed" in failed["stdout"] and "::warning::" in failed["stdout"]
	# Our own release comment for this PR does not count as a re-latch.
	own = {
		"user": {"login": "pipeline"},
		"author_association": "OWNER",
		"created_at": "2026-11-20T14:00:00Z",
		"body": f"_Orchestrator: removed `ai:needs-human`._\n\n<!-- ai:needs-human-auto-release reason=non_retryable_head_moved pr=5 head={NEW_HEAD} -->",
	}
	released = _run(tmp_path / "own", comments=[_non_retryable_comment()], post_delete_comments=[_non_retryable_comment(), own])
	assert released["added"] == [] and len(released["posted"]) == 1


def test_a_failed_read_after_the_label_removal_keeps_the_release_and_logs_it(tmp_path: Path) -> None:
	# Fail open: the label is already gone and a blind re-add would never bind
	# to a latch again, so the release goes ahead, but the skipped check is logged.
	result = _run(tmp_path, comments=[_non_retryable_comment()], fail="comments_post_delete")
	assert result["rc"] == 0, result["stderr"]
	assert result["deleted"] == ["5 ai:needs-human"]
	assert result["added"] == []
	assert len(result["posted"]) == 1
	assert "::warning::" in result["stdout"]
	assert f"NEEDS_HUMAN_HEAD_LATCH pr=5 outcome=post_release_check_unavailable latch=non_retryable_head_moved head={NEW_HEAD[:12]}" in result["stdout"]
	assert "outcome=released latch=non_retryable_head_moved" in result["stdout"]


def test_a_head_or_label_change_during_the_release_is_kept(tmp_path: Path) -> None:
	labels = [{"name": "ai:needs-human"}, {"name": "ai:review-blocked"}]
	for name, pull in (
		("back_to_latched_head", {"state": "open", "head": {"sha": OLD_HEAD}, "labels": labels}),
		("other_head", {"state": "open", "head": {"sha": "c" * 40}, "labels": labels}),
		("scope_blocked", {"state": "open", "head": {"sha": NEW_HEAD}, "labels": [*labels, {"name": "ai:scope-blocked"}]}),
		("destructive_blocked", {"state": "open", "head": {"sha": NEW_HEAD}, "labels": [*labels, {"name": "ai:destructive-blocked"}]}),
		("closed", {"state": "closed", "head": {"sha": NEW_HEAD}, "labels": labels}),
	):
		result = _run(tmp_path / name, comments=[_non_retryable_comment()], refreshed_pull=pull)
		assert result["deleted"] == [] and result["posted"] == [], name
		assert "outcome=skip reason=latch_changed_during_release" in result["stdout"], (name, result["stdout"])
	unavailable = _run(tmp_path / "unavailable", comments=[_non_retryable_comment()], fail="pull_refresh")
	assert unavailable["deleted"] == [] and "outcome=skip reason=latch_revalidation_unavailable" in unavailable["stdout"]


def test_a_later_comment_reasserting_needs_human_keeps_the_label(tmp_path: Path) -> None:
	later = {
		"user": {"login": "pipeline"},
		"author_association": "OWNER",
		"created_at": "2026-11-20T13:00:00Z",
		"body": "## Persistence budget reached\n\nThe issue is labelled `ai:needs-human`.",
	}
	result = _run(tmp_path, comments=[_non_retryable_comment(), later])
	assert result["deleted"] == []
	assert "outcome=skip reason=later_needs_human_comment" in result["stdout"]


def test_a_bare_or_malformed_release_marker_does_not_exempt_a_reassertion(tmp_path: Path) -> None:
	for name, marker in (
		("bare", "<!-- ai:needs-human-auto-release invalid -->"),
		("other_pr", f"<!-- ai:needs-human-auto-release reason=non_retryable_head_moved pr=6 head={NEW_HEAD} -->"),
		("quoted", f"<!-- ai:needs-human-auto-release reason=non_retryable_head_moved pr=5 head={NEW_HEAD} --> is the release marker shape."),
	):
		later = {
			"user": {"login": "pipeline"},
			"author_association": "OWNER",
			"created_at": "2026-11-20T13:00:00Z",
			"body": f"Keeping `ai:needs-human` on this PR.\n\n{marker}",
		}
		result = _run(tmp_path / name, comments=[_non_retryable_comment(), later])
		assert result["deleted"] == [] and result["posted"] == [], name
		assert "outcome=skip reason=later_needs_human_comment" in result["stdout"], (name, result["stdout"])
		during = _run(tmp_path / f"{name}_during", comments=[_non_retryable_comment()], refreshed_comments=[_non_retryable_comment(), later])
		assert during["deleted"] == [] and during["posted"] == [], name
		assert "outcome=skip reason=latch_changed_during_release" in during["stdout"], (name, during["stdout"])


def test_label_must_be_bound_to_the_latch_comment(tmp_path: Path) -> None:
	cases = [
		[_labeled(at="2026-11-20T11:00:00Z")],  # labelled before the latch comment
		[_labeled(at="2026-11-20T12:00:00Z")],  # labelled 45 minutes later
		[_labeled(login="someone-else")],  # a different actor
		[_labeled(), {"event": "unlabeled", "label": {"name": "ai:needs-human"}, "actor": {"login": "pipeline"}, "created_at": "2026-11-20T11:20:00Z"}],
		[],
	]
	for idx, events in enumerate(cases):
		result = _run(tmp_path / str(idx), comments=[_non_retryable_comment()], events=events)
		assert result["deleted"] == [], (idx, result["stdout"])
		assert "outcome=skip reason=label_not_bound_to_latch" in result["stdout"], (idx, result["stdout"])


def test_untrusted_latch_comment_is_ignored(tmp_path: Path) -> None:
	comment = _non_retryable_comment(login="drive-by")
	comment["author_association"] = "NONE"
	result = _run(tmp_path, comments=[comment], events=[_labeled(login="drive-by")])
	assert result["deleted"] == []
	assert "outcome=skip reason=no_head_bound_latch" in result["stdout"]


def test_other_human_gated_latches_and_changed_pulls_are_left_alone(tmp_path: Path) -> None:
	scope = _run(tmp_path / "scope", comments=[_non_retryable_comment()], labels=("ai:needs-human", "ai:scope-blocked"))
	assert scope["deleted"] == [] and "reason=other_human_gated_latch_present" in scope["stdout"]
	closed = _run(tmp_path / "closed", comments=[_non_retryable_comment()], pull_state="closed")
	assert closed["deleted"] == [] and "reason=pull_changed" in closed["stdout"]
	unlabelled = _run(tmp_path / "unlabelled", comments=[_non_retryable_comment()], pull_labels=("ai:review-blocked",))
	assert unlabelled["deleted"] == [] and "reason=pull_changed" in unlabelled["stdout"]


def test_issues_are_not_touched(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_non_retryable_comment()], is_pr=False)
	assert result["deleted"] == []
	assert "NEEDS_HUMAN_HEAD_LATCH outcome=listed prs=0" in result["stdout"]


def test_read_failures_fail_open_and_switch_disables(tmp_path: Path) -> None:
	for name, expected in (
		("list", "outcome=skip reason=list_unavailable"),
		("comments", "reason=comments_unavailable"),
		("events", "reason=label_events_unavailable"),
		("pull", "reason=pull_unavailable"),
		("label_delete", "reason=label_write_failed"),
	):
		result = _run(tmp_path / name, comments=[_non_retryable_comment()], fail=name)
		assert result["rc"] == 0, (name, result["stderr"])
		assert result["deleted"] == [] and result["posted"] == [], name
		assert expected in result["stdout"], (name, result["stdout"])
	posted_failed = _run(tmp_path / "post", comments=[_non_retryable_comment()], fail="comment_post")
	assert posted_failed["deleted"] == ["5 ai:needs-human"]
	assert "outcome=released_comment_failed" in posted_failed["stdout"]
	disabled = _run(tmp_path / "off", comments=[_non_retryable_comment()], enabled="false")
	assert disabled["calls"] == []
	assert "NEEDS_HUMAN_HEAD_LATCH outcome=skip reason=disabled" in disabled["stdout"]


def test_sweep_is_wired_into_both_poller_paths_and_the_workflow() -> None:
	text = POLLER.read_text(encoding="utf-8")
	wiring = 'release_head_bound_needs_human_latches || echo "NEEDS_HUMAN_HEAD_LATCH outcome=skip reason=error rc=$?"'
	assert text.count(wiring) == 2
	sweep_only = text[text.index('if _is_truthy "${UNBLOCK_SCAN_SWEEP_ONLY:-false}"; then'):]
	assert sweep_only.index(wiring) < sweep_only.index("run_unblock_scan ||")
	wf = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert wf.count("NEEDS_HUMAN_HEAD_LATCH_AUTO_RELEASE_ENABLED: ${{ vars.NEEDS_HUMAN_HEAD_LATCH_AUTO_RELEASE_ENABLED || 'true' }}") == 3
	# The existing staged-support reason keeps its source-only gate.
	assert 'if [ "${GITHUB_REPOSITORY}" != "shubhodeep1/coding-workflows" ]; then\n    echo "STAGED_SUPPORT_LATCH_RELEASE_SKIPPED reason=repository_out_of_scope' in text
	assert not re.search(r"release_head_bound_needs_human_latches\(\) \{[^}]*shubhodeep1/coding-workflows", text)


# ---------------------------------------------------------------------------
# Autofix-cap latch (issue #9917): the default "AI review/autofix blocked —
# needs human intervention" comment from
# scripts/review_autofix_step_post_review_blocked_comment.sh.
# ---------------------------------------------------------------------------

AUTOFIX_CAP_HEADING = "**AI review/autofix blocked — needs human intervention**"
RUN_ID = "44871258266"


def _autofix_cap_comment(head: str | None = OLD_HEAD, created_at: str = "2026-11-20T11:15:10Z", run_repo: str = REPO, author_association: str = "OWNER") -> dict:
	head_marker = f"\n\n<!-- ai:autofix-cap-head:v1 head={head} -->" if head else ""
	return {
		"user": {"login": "pipeline"},
		"author_association": author_association,
		"created_at": created_at,
		"body": (
			f"{AUTOFIX_CAP_HEADING}\n\nThis PR has reached the maximum number of autofix iterations (5) without resolving all review issues.\n\n"
			"Linked issues have been labeled `ai:review-blocked`.\n\n"
			f"Run: https://github.com/{run_repo}/actions/runs/{RUN_ID}{head_marker}"
		),
	}


def _pr_run(event: str = "pull_request", head_sha: str = OLD_HEAD, head_branch: str = "ai/issue-9", head_repo: str = REPO) -> dict:
	return {RUN_ID: {"event": event, "head_sha": head_sha, "head_branch": head_branch, "head_repository": {"full_name": head_repo}}}


def _run_reads(result: dict) -> list[list[str]]:
	return [call for call in result["calls"] if any("/actions/runs/" in arg for arg in call)]


def test_autofix_cap_latch_with_head_marker_is_released_once_the_head_moved(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_autofix_cap_comment()])
	assert result["rc"] == 0, result["stderr"]
	assert result["deleted"] == ["5 ai:needs-human"]
	assert len(result["posted"]) == 1
	body = result["posted"][0]["body"]
	assert body.rstrip().endswith(f"<!-- ai:needs-human-auto-release reason=autofix_cap_head_moved pr=5 head={NEW_HEAD} -->")
	assert OLD_HEAD[:12] in body and NEW_HEAD[:12] in body
	assert f"outcome=released latch=autofix_cap_head_moved from={OLD_HEAD[:12]} head={NEW_HEAD[:12]}" in result["stdout"]
	# The head comes from the marker: no run read, the same 10 calls as the other latches.
	assert _run_reads(result) == []
	assert len(result["calls"]) == 10


def test_legacy_autofix_cap_latch_takes_its_head_from_a_pull_request_run(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_autofix_cap_comment(head=None)], runs=_pr_run())
	assert result["rc"] == 0, result["stderr"]
	assert result["deleted"] == ["5 ai:needs-human"]
	assert "reason=autofix_cap_head_moved pr=5" in result["posted"][0]["body"]
	assert f"outcome=released latch=autofix_cap_head_moved from={OLD_HEAD[:12]} head={NEW_HEAD[:12]}" in result["stdout"]
	reads = _run_reads(result)
	assert len(reads) == 1 and f"repos/{REPO}/actions/runs/{RUN_ID}" in reads[0]


def test_legacy_autofix_cap_latch_on_the_same_head_stays(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_autofix_cap_comment(head=None)], runs=_pr_run(head_sha=NEW_HEAD))
	assert result["deleted"] == [] and result["posted"] == []
	assert "outcome=skip reason=head_unchanged latch=autofix_cap_head_moved" in result["stdout"]


def test_autofix_cap_latch_on_the_same_head_stays(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_autofix_cap_comment()], head=OLD_HEAD)
	assert result["deleted"] == [] and result["posted"] == []
	assert "outcome=skip reason=head_unchanged latch=autofix_cap_head_moved" in result["stdout"]


def test_legacy_autofix_cap_latch_that_cannot_be_verified_stays(tmp_path: Path) -> None:
	cases = {
		# A dispatched run's head_sha is a default-branch commit, not the PR head.
		"dispatch_run": ({"runs": _pr_run(event="workflow_dispatch", head_branch="main")}, "reason=latch_head_unverifiable"),
		"other_branch": ({"runs": _pr_run(head_branch="feature/other")}, "reason=latch_head_unverifiable"),
		"fork_run": ({"runs": _pr_run(head_repo="fork/r")}, "reason=latch_head_unverifiable"),
		"bad_sha": ({"runs": _pr_run(head_sha="abc")}, "reason=latch_head_unverifiable"),
		"run_read_failed": ({"runs": _pr_run(), "fail": "run"}, "reason=run_unavailable"),
	}
	for name, (kwargs, expected) in cases.items():
		result = _run(tmp_path / name, comments=[_autofix_cap_comment(head=None)], **kwargs)
		assert result["rc"] == 0, (name, result["stderr"])
		assert result["deleted"] == [] and result["posted"] == [], name
		assert f"outcome=skip {expected} latch=autofix_cap_head_moved" in result["stdout"], (name, result["stdout"])
	# A run URL naming another repository is never read.
	foreign = _run(tmp_path / "foreign", comments=[_autofix_cap_comment(head=None, run_repo="evil/r")], runs=_pr_run())
	assert foreign["deleted"] == [] and _run_reads(foreign) == []
	assert "outcome=skip reason=latch_head_missing latch=autofix_cap_head_moved" in foreign["stdout"]


def test_legacy_run_read_is_cached_per_run_id_per_tick(tmp_path: Path) -> None:
	result = _run(tmp_path, comments=[_autofix_cap_comment(head=None)], runs=_pr_run(), numbers=(5, 6))
	assert result["rc"] == 0, result["stderr"]
	assert len(_run_reads(result)) == 1
	assert sorted(result["deleted"]) == ["5 ai:needs-human", "6 ai:needs-human"]


def test_a_later_comment_reasserting_the_hold_keeps_the_autofix_cap_label(tmp_path: Path) -> None:
	later_human = {
		"user": {"login": "maintainer"},
		"author_association": "OWNER",
		"created_at": "2026-11-20T13:00:00Z",
		"body": "Keep `ai:needs-human` here until I look at the migration.",
	}
	later_judge = {
		"user": {"login": "pipeline"},
		"author_association": "OWNER",
		"created_at": "2026-11-20T13:00:00Z",
		"body": "**AI review/autofix — judge approved merge but auto-merge is disabled**\n\nPlease merge this PR manually.",
	}
	for name, later in (("human", later_human), ("auto_merge_disabled", later_judge)):
		result = _run(tmp_path / name, comments=[_autofix_cap_comment(), later])
		assert result["deleted"] == [] and result["posted"] == [], name
		assert "outcome=skip reason=later_needs_human_comment latch=autofix_cap_head_moved" in result["stdout"], (name, result["stdout"])
		during = _run(tmp_path / f"{name}_during", comments=[_autofix_cap_comment()], refreshed_comments=[_autofix_cap_comment(), later])
		assert during["deleted"] == [] and during["posted"] == [], name
		assert "outcome=skip reason=latch_changed_during_release" in during["stdout"], (name, during["stdout"])


def test_untrusted_autofix_cap_heading_is_not_a_latch(tmp_path: Path) -> None:
	quoted = _autofix_cap_comment(author_association="NONE")
	quoted["user"] = {"login": "drive-by"}
	result = _run(tmp_path, comments=[quoted], events=[_labeled(login="drive-by")])
	assert result["deleted"] == []
	assert "outcome=skip reason=no_head_bound_latch" in result["stdout"]


# ---------------------------------------------------------------------------
# The writer: scripts/review_autofix_step_post_review_blocked_comment.sh
# ---------------------------------------------------------------------------

STEP_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_post_review_blocked_comment.sh"
FAKE_POST_GH = textwrap.dedent(
	r"""
	#!/usr/bin/env python3
	import json, os, sys
	with open(os.path.join(os.environ["FAKE_GH_DIR"], "gh.log"), "a", encoding="utf-8") as fh:
		fh.write(json.dumps(sys.argv[1:]) + "\n")
	print("{}")
	"""
).lstrip()


def _post_blocked_comment(tmp_path: Path, **env_extra: str) -> str:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(parents=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_POST_GH, encoding="utf-8")
	gh.chmod(0o755)
	support = tmp_path / "support"
	support.mkdir()
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_DIR": str(tmp_path),
		"SUPPORT_SCRIPTS_DIR": str(support),
		"AUTOFIX_STEP_REPOSITORY": REPO,
		"AUTOFIX_STEP_RUN_ID": RUN_ID,
		"PR_NUMBER": "5",
		"MAX_AUTOFIX_ITERATIONS": "5",
		"AUTOFIX_ITERATION": "5",
		"JUDGE_SKIP_REASON": "",
		"RB_JUDGE_STATUS": "",
		"AUTOFIX_FAILURE_HEAD_SHA": OLD_HEAD,
		"GITHUB_OUTPUT": str(tmp_path / "out.txt"),
	}
	env.update(env_extra)
	proc = subprocess.run(["bash", "-c", f'source "{STEP_SCRIPT}"'], capture_output=True, text=True, env=env, timeout=60, check=False)
	assert proc.returncode == 0, proc.stderr
	calls = [json.loads(line) for line in (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()]
	post = next(call for call in calls if any(arg.endswith("/issues/5/comments") for arg in call))
	return next(arg for arg in post if arg.startswith("body="))[len("body="):]


def test_blocked_comment_carries_the_autofix_cap_head_marker(tmp_path: Path) -> None:
	body = _post_blocked_comment(tmp_path)
	assert AUTOFIX_CAP_HEADING in body
	assert body.rstrip().endswith(f"<!-- ai:autofix-cap-head:v1 head={OLD_HEAD} -->")
	assert f"Run: https://github.com/{REPO}/actions/runs/{RUN_ID}" in body


def test_blocked_comment_head_marker_goes_before_the_crash_marker(tmp_path: Path) -> None:
	body = _post_blocked_comment(tmp_path, AUTOFIX_ITERATION="2", RB_JUDGE_STATUS="failed")
	head_marker = f"<!-- ai:autofix-cap-head:v1 head={OLD_HEAD} -->"
	crash_marker = f"<!-- rb-judge-crash:v1 head={OLD_HEAD} reason=judge_crashed -->"
	assert head_marker in body
	# The crash marker's dedupe needs it last, on its own line.
	assert body.rstrip().endswith("\n" + crash_marker)
	assert body.index(head_marker) < body.index(crash_marker)


def test_blocked_comment_writes_no_head_marker_for_other_headings_or_a_bad_head(tmp_path: Path) -> None:
	for name, env_extra in (
		("auto_merge_disabled", {"JUDGE_SKIP_REASON": "auto_merge_disabled"}),
		("stale_head", {"JUDGE_SKIP_REASON": "stale_head"}),
		("bad_head", {"AUTOFIX_FAILURE_HEAD_SHA": "not-a-sha"}),
		("no_head", {"AUTOFIX_FAILURE_HEAD_SHA": ""}),
		("env_injected_flag", {"JUDGE_SKIP_REASON": "merge_conflict", "AUTOFIX_CAP_HEAD_LATCH": "true"}),
	):
		body = _post_blocked_comment(tmp_path / name, **env_extra)
		assert "ai:autofix-cap-head:v1" not in body, name
